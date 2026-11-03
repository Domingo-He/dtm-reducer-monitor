"""交叉分析：干擾扭矩 × 振動（徑向／軸向）× 鐵粉 × 元件壽命 → 換脂、換減速機／齒輪箱、換馬達的判斷。

原則：
1. 每個訊號先各自對自己的基準做「嚴重度」0–1.5（0.5 = 注意、1.0 = 警戒）。
2. 訊號分成三個獨立家族：扭矩（摩擦）、鐵粉（磨耗量）、振動（衝擊與頻率）。
   換減速機或馬達需要「兩個家族同時異常」，單一家族最多只到「觀察／準備」，以降低誤判。
3. 用訊號的「組合」區分是哪個元件：
   - 潤滑脂：黏性摩擦 Fv 上升、油脂有效壽命用完、鐵粉偏高（油脂已被污染）
   - 減速機／齒輪箱：固定摩擦 Fc 或漣波上升＋鐵粉生成速率上升＋振動包絡、嚙合頻率或軸向比上升
   - 伺服馬達：馬達轉頻振動上升；摩擦上升但鐵粉與減速機頻率沒有跟著上升（煞車拖曳、馬達軸承）
4. 換脂會重設基準，可能把持續的磨耗「洗掉」，所以另外和新品時的基準比（累積比值）。
"""
from __future__ import annotations

import statistics
import time

from .analysis import baseline as torque_baseline
from .config import COMP_LABELS, IRON_AT_CHANGE, RESET_KINDS, VIB_METRICS, WEIGHT_KEYS, axis_cfg, drive_comp
from .i18n import TDict, _
from .life import Hours, axis_life
from .segment import ols

COMPONENT_RESETS = {"reducer_replace", "gearbox_replace", "motor_replace", "baseline_reset"}
DEFAULT_WEIGHTS = {"fc_ripple": 1.0, "f_motion": 0.8, "temp_rise": 0.6, "vib_reducer": 1.0, "vib_axial": 0.8, "iron": 1.0}


def weights(cfg: dict) -> dict:
    """[decision.weights]：各證據在家族內的比重；設定錯誤的值（check 會報錯）退回預設。"""
    w = dict(DEFAULT_WEIGHTS)
    for k, v in ((cfg.get("decision") or {}).get("weights") or {}).items():
        if k in WEIGHT_KEYS:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if 0 <= f <= 1.5:
                w[k] = f
    return w


def sev_ratio(r, c: float, a: float):
    if r is None:
        return None
    if r <= 1:
        return 0.0
    if r <= c:
        return 0.5 * (r - 1) / (c - 1)
    return min(1.5, 0.5 + 0.5 * (r - c) / (a - c))


def sev_abs(v, c: float, a: float, floor: float):
    if v is None:
        return None
    if v <= floor:
        return 0.0
    if v <= c:
        return 0.5 * (v - floor) / (c - floor)
    return min(1.5, 0.5 + 0.5 * (v - c) / (a - c))


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def _last_reset(store, ax: int, now: float, kinds=RESET_KINDS) -> float:
    ts = [e["ts"] for e in store.events(ax) if e["kind"] in kinds and e["ts"] <= now]
    return max(ts) if ts else 0.0


# ------------------------------------------------------------ 振動
def vib_analysis(store, cfg: dict, ax: int, now: float) -> dict:
    vc = cfg["vibration"]
    n = int(vc["baseline_n"])
    c, a = float(vc["caution_ratio"]), float(vc["alert_ratio"])
    reset = _last_reset(store, ax, now)
    rows = store.vib(ax, since=reset, until=now)
    out = {"reset_ts": reset, "dir": {}, "ar": None, "ready": False}
    base_rms = {}
    for d in ("radial", "axial"):
        rs = [r for r in rows if r["direction"] == d]
        info = {"n": len(rs), "ready": len(rs) >= n, "metrics": {}}
        if rs:
            info["latest_ts"] = rs[-1]["ts"]
        if len(rs) >= n:
            recent = rs[-2:]
            for m in VIB_METRICS:
                b = _mean(r[m] for r in rs[:n])
                v = _mean(r[m] for r in recent)
                if m == "kurt":
                    s = sev_abs(v, float(vc["kurt_caution"]), float(vc["kurt_alert"]), 3.0)
                    ratio = None
                else:
                    ratio = v / b if v is not None and b else None
                    s = sev_ratio(ratio, c, a)
                info["metrics"][m] = {"value": v, "base": b, "ratio": ratio, "sev": s}
            base_rms[d] = (info["metrics"]["rms"]["base"], info["metrics"]["rms"]["value"])
            win = [r for r in rs[-30:] if r["env"] is not None]
            if len(win) >= 5 and info["metrics"]["env"]["base"]:
                fit = ols([(r["ts"] - now) / 86400 for r in win], [r["env"] for r in win])
                if fit and fit[0] > 1e-12:
                    target = a * info["metrics"]["env"]["base"]
                    info["env_days_to_alert"] = max(0.0, (target - fit[1]) / fit[0])
        out["dir"][d] = info
    if len(base_rms) == 2 and all(x is not None for x in base_rms["radial"] + base_rms["axial"]) \
            and base_rms["radial"][0] and base_rms["radial"][1]:
        ar_b = base_rms["axial"][0] / base_rms["radial"][0]
        ar_n = base_rms["axial"][1] / base_rms["radial"][1]
        cr = float(vc["ar_caution_ratio"])
        out["ar"] = {"base": ar_b, "value": ar_n, "ratio": ar_n / ar_b,
                     "sev": sev_ratio(ar_n / ar_b, cr, cr + (cr - 1))}
    out["ready"] = any(v["ready"] for v in out["dir"].values())
    return out


# ------------------------------------------------------------ 鐵粉
def iron_thresholds(cfg: dict, ax: int) -> tuple[float | None, float | None]:
    a = axis_cfg(cfg, ax)
    ic = cfg["iron"]
    if float(a["iron_caution_wt"]) > 0:
        return float(a["iron_caution_wt"]), float(a["iron_alert_wt"])
    if a["reducer_type"] == "rv":
        return float(ic["rv_caution_wt"]), float(ic["rv_alert_wt"])
    return None, None


def iron_analysis(store, cfg: dict, hours: Hours, ax: int, now: float, grease_due_ts: float | None) -> dict:
    a = axis_cfg(cfg, ax)
    ic = cfg["iron"]
    M = float(a["grease_mass_g"])
    cw, aw = iron_thresholds(cfg, ax)
    samples = store.iron(ax, until=now)
    out = {"n": len(samples), "caution_wt": cw, "alert_wt": aw, "grease_mass_g": M, "points": []}
    if not samples:
        return out
    greases = sorted(c["install_ts"] for c in store.components("grease") if c["axis"] == ax and c["install_ts"])
    drive = store.current_component(ax, drive_comp(cfg, ax), now)
    drive_ts = drive["install_ts"] if drive else 0.0

    def cycle_of(s):
        lim = s["ts"] - 3600 if s["sample"] in IRON_AT_CHANGE else s["ts"]
        prev = [g for g in greases if g <= lim]
        return prev[-1] if prev else None

    c0: dict = {}
    last_in_cycle: dict = {}
    w_base = None
    for s in samples:
        cyc = cycle_of(s)
        p = {"ts": s["ts"], "wt": s["wt_pct"], "sample": s["sample"], "cycle": cyc, "W": None, "h": None}
        if cyc is not None:
            p["h"] = hours.between(cyc, s["ts"])
            if s["sample"] == "after" and cyc not in c0:
                c0[cyc] = s["wt_pct"]
            prev = last_in_cycle.get(cyc)
            if M > 0 and s["sample"] != "after":
                if prev and p["h"] - prev["h"] >= float(ic["min_hours"]):
                    p["W"] = (s["wt_pct"] - prev["wt"]) / 100 * M / (p["h"] - prev["h"]) * 1000
                    p["W_kind"] = _("區間")
                elif p["h"] >= float(ic["min_hours"]):
                    p["W"] = (s["wt_pct"] - c0.get(cyc, 0.0)) / 100 * M / p["h"] * 1000
                    p["W_kind"] = _("平均")
            last_in_cycle[cyc] = p
            if p["W"] is not None and w_base is None and s["ts"] >= drive_ts:
                w_base = p["W"]
        out["points"].append(p)
    last = out["points"][-1]
    out.update({"latest_ts": last["ts"], "latest_wt": last["wt"], "latest_sample": last["sample"],
                "W": next((p["W"] for p in reversed(out["points"]) if p["W"] is not None), None),
                "W_base": w_base, "residual_wt": c0.get(last["cycle"])})
    out["W_ratio"] = out["W"] / out["W_base"] if out["W"] is not None and out["W_base"] else None
    out["sev_conc"] = sev_abs(last["wt"], cw, aw, cw * 0.5) if cw else None
    out["sev_w"] = sev_ratio(out["W_ratio"], float(ic["w_caution_ratio"]), float(ic["w_alert_ratio"]))
    fam = [x for x in (out["sev_conc"], out["sev_w"]) if x is not None]
    out["sev"] = max(fam) if fam else None
    # 生成速率 W 連續兩次加速上升（W 三點遞增、而且後一次增量不小於前一次）→ 升一級
    ws = [p["W"] for p in out["points"] if p["W"] is not None and p["ts"] >= drive_ts]
    if len(ws) >= 3 and ws[-1] > ws[-2] > ws[-3] and ws[-1] - ws[-2] >= ws[-2] - ws[-3]:
        out["w_accel"] = True
        out["sev"] = min(1.5, (out["sev"] or 0.0) + 0.5)
    # 達注意後：磨耗已過轉折點，不再直線外推剩餘天數；取樣間隔縮短
    out["past_knee"] = (out["sev"] or 0) >= 0.5
    lv = 2 if (out["sev"] or 0) >= 1.0 else 1 if out["past_knee"] else 0
    days = float({2: ic.get("interval_alert_days", 21), 1: ic.get("interval_caution_days", 60)}.get(
        lv, ic.get("interval_days", 0)) or 0)
    out["level"] = lv
    if days > 0:
        out["sample_interval_days"] = days
        out["next_sample_ts"] = last["ts"] + days * 86400
    # 以目前生成速率外推：何時到警戒濃度、下次換脂時的預估濃度
    cur_cycle = greases[-1] if greases else None
    if out["W"] and out["W"] > 0 and M > 0 and cur_cycle is not None:
        rate = hours.rate_per_day()
        c_now_est = last["wt"] + out["W"] * hours.between(last["ts"], now) / 1000 / M * 100 \
            if last["cycle"] == cur_cycle else c0.get(cur_cycle, 0.0) + out["W"] * hours.between(cur_cycle, now) / 1000 / M * 100
        out["wt_now_est"] = c_now_est
        if aw and not out["past_knee"]:
            need_h = (aw - c_now_est) / 100 * M / out["W"] * 1000
            out["days_to_alert"] = max(0.0, need_h / rate) if rate > 0 else None
        if grease_due_ts and grease_due_ts > now:
            out["wt_at_grease_due"] = c_now_est + out["W"] * hours.between(now, grease_due_ts) / 1000 / M * 100
    return out


# ------------------------------------------------------------ 扭矩：相對新品基準
def cumulative_torque(store, cfg: dict, ax: int, now: float) -> dict | None:
    """和元件更換後第一批診斷建立的基準比，換脂不會把它重設。"""
    ref_ts = _last_reset(store, ax, now, COMPONENT_RESETS)
    rows = store.features(ax, since=ref_ts, until=now)
    b = torque_baseline(rows, cfg)
    if not b or len(rows) <= int(cfg["health"]["baseline_runs"]):
        return None
    recent = rows[-3:]
    out = {"ref_ts": ref_ts}
    for m in ("F", "Fc", "ripple"):
        base = b["stats"][m][0]
        v = _mean(r[m] for r in recent)
        out[m] = v / base if v is not None and base else None
    return out


# ------------------------------------------------------------ 決策
ACTIONS = {
    "grease": TDict({2: "立即換脂", 1: "建議換脂（排入下次停機）", 0: "正常"}),
    "drive": TDict({2: "安排更換（排程停機）", 1: "準備備品、縮短監測週期", 0: "正常"}),
    "motor": TDict({2: "安排更換或送修", 1: "檢查馬達（軸承、煞車、編碼器）並準備備品", 0: "正常"}),
}


def _fam_score(fams: list) -> float:
    vals = sorted((f for f in fams if f is not None), reverse=True)
    if not vals:
        return 0.0
    if len(vals) == 1 or vals[1] < 0.25:
        return min(vals[0], 1.5) * 0.45         # 只有一個家族異常：警戒程度也只到「觀察」，極端時到「準備」
    return (min(vals[0], 1.5) + min(vals[1], 1.5)) / 2


def decide(svc, ax: int, now: float | None = None) -> dict:
    now = now or time.time()
    cfg, store = svc.cfg, svc.store
    dc, hc = cfg["decision"], cfg["health"]
    ac = axis_cfg(cfg, ax)
    hours = Hours(store, cfg)
    life = axis_life(store, cfg, ax, now)
    health = svc.health.get(ax) or svc.refresh_health(ax, now)
    vib = vib_analysis(store, cfg, ax, now)
    iron = iron_analysis(store, cfg, hours, ax, now, (life["grease"] or {}).get("due_ts"))
    cum = cumulative_torque(store, cfg, ax, now)
    cr, ar = float(hc["caution_ratio"]), float(hc["alert_ratio"])
    # 生產中監測（動作代碼每日中位數）：屬於扭矩家族，跟診斷扭矩取大的，不算獨立的第二類證據
    mh = (getattr(svc, "motion_health", None) or {}).get(ax) or {}
    mc = cfg.get("motion") or {}
    mready = mh.get("level", -1) >= 0
    s_mot = sev_ratio(mh.get("ratio"), float(mc.get("caution_ratio", 1.15)), float(mc.get("alert_ratio", 1.3))) \
        if mready else None
    s_mot_vib = sev_ratio(mh.get("vib_ratio"), float(mc.get("vib_caution_ratio", 1.5)),
                          float(mc.get("vib_alert_ratio", 2.0))) if mready else None

    def tsev(m):
        d = (health.get("metrics") or {}).get(m) or {}
        if not health.get("baseline_ready"):
            return None
        s = sev_ratio(d.get("ratio"), cr, ar) or 0.0
        return max(s, 0.5 * d.get("level", 0))

    sF, sFc, sFv, sRip = (tsev(m) for m in ("F", "Fc", "Fv", "ripple"))
    sCumFc = sev_ratio((cum or {}).get("Fc"), cr, ar)
    sCumRip = sev_ratio((cum or {}).get("ripple"), cr, ar)
    W = weights(cfg)
    wt = lambda k, v: None if v is None else W[k] * v  # noqa: E731
    th = (getattr(svc, "thermal", None) or {}).get(ax) or {}
    s_temp = wt("temp_rise", th.get("sev"))
    torque_core = max([x for x in (wt("fc_ripple", sFc), wt("fc_ripple", sRip), wt("f_motion", sF), wt("fc_ripple", sCumFc),
                                   wt("fc_ripple", sCumRip), wt("f_motion", s_mot)) if x is not None], default=None)
    torque_wear = max([x for x in (torque_core, s_temp) if x is not None], default=None)
    torque_visc = sFv
    vm = lambda d, m: ((vib["dir"].get(d) or {}).get("metrics", {}).get(m) or {}).get("sev")  # noqa: E731
    vib_red = max([x for x in (wt("vib_reducer", vm("radial", "env")), wt("vib_reducer", vm("radial", "mesh")),
                               wt("vib_reducer", vm("radial", "kurt")), wt("vib_axial", vm("axial", "rms")),
                               wt("vib_axial", vib["ar"]["sev"] if vib["ar"] else None), wt("vib_reducer", s_mot_vib))
                   if x is not None], default=None)
    vib_mot = max([x for x in (vm("radial", "motor1x"), vm("axial", "motor1x")) if x is not None], default=None)
    iron_f = wt("iron", iron.get("sev"))
    g = life["grease"]
    start = float(dc["grease_age_start"])
    age_sev = None
    if g:
        age_sev = 0.0 if g["frac"] < start else min(1.5, 0.5 + 0.5 * (g["frac"] - start) / (1 - start))

    drive, motor = life["drive"], life["motor"]
    p_drive, p_motor = drive.get("p_fail_lead"), motor.get("p_fail_lead")
    pp = float(dc["p_fail_prepare"])
    age_drive = None if p_drive is None else min(1.0, 0.5 * p_drive / pp)
    age_motor = None if p_motor is None else min(1.0, 0.5 * p_motor / pp)

    # --- 「解釋掉」：摩擦上升但鐵粉、減速機頻率都正常，而馬達轉頻振動上升 → 摩擦歸給馬達，不算減速機磨耗
    explained = False
    if torque_wear is not None and (iron_f or 0) < 0.5 and (vib_red or 0) < 0.5 and vib_mot:
        torque_wear *= 1 - 0.7 * min(1.0, vib_mot)
        explained = vib_mot >= 0.5

    # --- 分數
    grease_score = max([x for x in (torque_visc, age_sev, 0.8 * min(iron_f, 1.0) if iron_f is not None else None)
                        if x is not None], default=0.0)
    drive_score = _fam_score([torque_wear, iron_f, vib_red])
    act, urg = float(dc["act_ratio"]), float(dc["urgent_ratio"])
    temp_only = (s_temp or 0) >= 0.25 and (torque_core or 0) < 0.25 and (iron_f or 0) < 0.25 and (vib_red or 0) < 0.25
    if temp_only:                                       # 只有溫度異常：最多「觀察」
        drive_score = min(drive_score, act - 0.01)
    if age_drive is not None:
        drive_score = max(drive_score, age_drive)
    no_wear = (iron_f or 0) < 0.5 and (vib_red or 0) < 0.5
    brake_like = max(sFc or 0, sF or 0) if no_wear else 0.0
    motor_score = _fam_score([vib_mot, brake_like if health.get("baseline_ready") else None])
    if age_motor is not None:
        motor_score = max(motor_score, age_motor)

    def level(score):
        return 2 if score >= urg else 1 if score >= act else 0

    lv_g, lv_d, lv_m = level(grease_score), level(drive_score), level(motor_score)
    if g and g["frac"] >= 1.0:
        lv_g = 2
    caw = iron.get("alert_wt")
    if caw and iron.get("latest_wt") is not None and iron["latest_wt"] >= caw:
        lv_g = 2
    knee = bool(iron.get("past_knee")) and not explained
    if knee:
        lv_d = max(lv_d, 1)                             # 鐵粉已過轉折點：至少「準備備品」

    # --- 剩餘時間（天）（鐵粉過轉折點後不外推，iron 不提供 days_to_alert）
    rul = [x for x in (None if explained else health.get("days_to_alert"), iron.get("days_to_alert"),
                       ((vib["dir"].get("radial") or {}).get("env_days_to_alert")),
                       None if explained else mh.get("days_to_alert")) if x is not None]
    drive_rul = min(rul) if rul else None
    lead = int(cfg["life"]["spare_lead_days"])

    comp_d = drive_comp(cfg, ax)
    res = {"axis": ax, "name": ac["name"], "ts": now, "drive_comp": comp_d, "drive_label": COMP_LABELS[comp_d],
           "scores": {"grease": grease_score, "drive": drive_score, "motor": motor_score},
           "families": {
               _("扭矩－磨耗型（Fc、漣波、累積）"): torque_wear, _("扭矩－黏性（Fv）"): torque_visc,
               _("扭矩－生產循環（每日中位數）"): s_mot, _("振動－生產循環"): s_mot_vib,
               _("溫度突升（ΔT）"): s_temp,
               _("鐵粉（濃度、生成速率）"): iron_f, _("振動－減速機（包絡、嚙合、軸向）"): vib_red,
               _("振動－馬達轉頻"): vib_mot, _("油脂壽命"): age_sev,
               _("年齡風險－{comp}", comp=COMP_LABELS[comp_d]): age_drive,
               _("年齡風險－{comp}", comp=COMP_LABELS["motor"]): age_motor},
           "grease": {"level": lv_g, "action": ACTIONS["grease"][lv_g], "age": g,
                      "due_ts": now if lv_g == 2 else (g or {}).get("due_ts")},
           "drive": {"level": lv_d, "action": ACTIONS["drive"][lv_d], "life": drive, "rul_days": drive_rul},
           "motor": {"level": lv_m, "action": ACTIONS["motor"][lv_m], "life": motor},
           "health_level": health.get("level"), "vib": vib, "iron": iron, "cum": cum, "notes": [], "next": [],
           "thermal": {k: th.get(k) for k in ("level", "rise", "hold", "overheat", "jump", "dT", "temp", "ref_kind",
                                              "message", "suppressed", "code")} if th else None,
           "trend_accel": bool((health.get("trend_accel") and (health.get("level") or 0) >= 1)
                               or any(c.get("trend_accel") and (c.get("level") or 0) >= 1 for c in mh.get("codes", []))),
           "motion": {"level": mh.get("level"), "ratio": mh.get("ratio"), "vib_ratio": mh.get("vib_ratio"),
                      "days_to_alert": mh.get("days_to_alert")} if mh.get("codes") else None}
    if lv_d == 0 and drive_score >= 0.25:
        res["drive"]["action"] = _("觀察")
    if knee:
        res["drive"]["action"] += _("；鐵粉已過轉折點，可能隨時惡化，備品建議直接下單")
        res["drive"]["knee"] = True
    if lv_m == 0 and motor_score >= 0.25:
        res["motor"]["action"] = _("觀察")
    if lv_d == 2:
        res["grease"]["action"] = _("隨{comp}更換一併處理", comp=COMP_LABELS[comp_d])
        res["grease"]["level"] = 0
    if drive_rul is not None and lv_d >= 1 and drive_rul <= lead and not knee:
        res["drive"]["action"] += _("；") + (_("已有指標達警戒，備品請立即訂購") if drive_rul < 1 else
                                            _("預估 {days:.0f} 天內達警戒，短於備品前置 {lead} 天，請立即訂購",
                                              days=drive_rul, lead=lead))

    # --- 主要研判與下一步
    cand = sorted([("grease", grease_score), ("drive", drive_score), ("motor", motor_score)], key=lambda x: -x[1])
    top, top_s = cand[0]
    lab = {"grease": _("潤滑脂劣化"), "drive": _("{comp}磨耗", comp=COMP_LABELS[comp_d]), "motor": _("伺服馬達異常")}
    res["primary"] = lab[top] if top_s >= 0.25 else _("無明顯異常")
    nx = res["next"]
    if lv_g >= 1 and 0.25 <= drive_score < urg:
        nx.append(_("先換脂並登錄事件，運轉 2–4 週後比對：Fc 與鐵粉生成速率回到新品基準 {ratio} 倍內 → 潤滑問題已解決；"
                    "仍偏高 → {comp}劣化，準備更換", ratio=dc["recovery_ratio"], comp=COMP_LABELS[comp_d]))
    if lv_d >= 1:
        nx.append(_("鐵粉加做鐵譜（看大顆粒比例），振動做包絡頻譜，確認損傷部位；拆下後填寫拆檢結果，用來修正門檻"))
    if lv_m >= 1:
        nx.append(_("量馬達外殼溫度、確認煞車釋放與間隙；比較馬達轉頻與減速機頻率成分，必要時對調馬達驗證"))
    hg = ((health.get("metrics") or {}).get("G") or {}).get("level", 0)
    if hg:
        nx.append(_("正反轉同向偏移：檢查平衡器與負載設定（這不是摩擦）"))
    if iron.get("next_sample_ts"):
        nx.append(_("鐵粉取樣改為每 {d:.0f} 天一次，下次 {date}（取換脂排出的中段油脂，前段先丟棄）",
                    d=iron["sample_interval_days"], date=time.strftime("%Y-%m-%d", time.localtime(iron["next_sample_ts"]))))
    if iron.get("w_accel"):
        res["notes"].append(_("鐵粉生成速率 W 連續兩次加速上升，鐵粉嚴重度升一級"))
    if th.get("hold"):
        nx.append(th["message"])
    elif th.get("rise") and th.get("alarm"):
        nx.append(_("溫度突升：確認負載與動作有沒有改、油脂量與油封狀態，並對照扭矩與鐵粉"))
    if th.get("suppressed"):
        res["notes"].append(th["suppressed"])
    if res["trend_accel"]:
        res["notes"].append(_("扭矩上升在加速，剩餘天數改用保守估計"))
    if iron["n"] == 0:
        res["notes"].append(_("尚無鐵粉資料：下次換脂或中途取樣送驗，才能確認是否為磨耗"))
    elif iron.get("W") is None and float(ac["grease_mass_g"]) <= 0:
        res["notes"].append(_("未設定 grease_mass_g，無法計算鐵粉生成速率"))
    if not vib["ready"]:
        res["notes"].append(_("振動資料不足基準筆數，暫不納入"))
    if not health.get("baseline_ready"):
        res["notes"].append(_("扭矩基準建立中") if not mready else _("診斷扭矩基準建立中，先用生產循環監測"))
    if mh.get("codes") and not mready:
        res["notes"].append(_("生產循環監測建立基準中（每個動作代碼要 {n} 個有效日）", n=mc.get("baseline_days", 10)))
    if drive.get("model") is None:
        res["notes"].append(_("{comp}沒有壽命模型（失效紀錄不足且未設定先驗），不做年齡風險", comp=COMP_LABELS[comp_d]))
    return res

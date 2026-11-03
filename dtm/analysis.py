"""健康判定：基準、比值與 z 分數、溫度補償、趨勢外推、失效模式提示。

溫度對黏性摩擦影響很大（冬天冷、冷機時 Fv 明顯變高），不處理會一直誤報「建議換脂」。兩層處理：
1. 溫度補償（有溫度訊號時）：值 × exp(k ×（溫度 − 基準期平均溫度））。
   k 用預設係數（health.temp_coef_fv／temp_coef_fc，可用 python -m dtm temp-coef 從自己的資料估計後更新）；
   只監測一個軸、而且基準期溫差夠大時，改用基準期資料自己學出來的 k。
2. 跨軸共模扣除（監測兩個軸以上時）：氣溫、冷機、生產節拍改變會讓「所有軸」的摩擦一起變，
   單一軸的油脂或減速機劣化只有那一軸變。用其他軸逐次的變化量（中位數）建一條連鎖指數，
   把這條共同變化從本軸扣掉；有溫度訊號時扣的是溫度補償後剩下的共同誤差（例如預設係數不準）。
   其他軸換脂、換零件造成的跳動不計入指數。代價：所有軸「同時」以同樣比例劣化會被扣掉，
   這種情況靠油脂壽命與鐵粉把關。
"""
from __future__ import annotations

import bisect
import math
import statistics
import time

from .config import RESET_KINDS, axis_cfg, enabled_axes
from .i18n import TDict, _, join
from .segment import ols
from .trend import extrapolate

HIGH_METRICS = ["F", "Fc", "Fv", "ripple"]
METRIC_LABELS = TDict({"F": "摩擦 F", "Fc": "固定摩擦 Fc", "Fv": "黏性摩擦 Fv", "G": "重力型偏差 G",
                       "ripple": "漣波", "peak": "峰值"})
LEVEL_NAMES = TDict({-1: "建立基準中", 0: "正常", 1: "注意", 2: "警戒"})


def _mean_std(vals: list[float]) -> tuple[float | None, float | None]:
    vals = [v for v in vals if v is not None and math.isfinite(v)]
    if not vals:
        return None, None
    return statistics.fmean(vals), (statistics.stdev(vals) if len(vals) > 1 else 0.0)


def baseline(rows: list[dict], cfg: dict) -> dict | None:
    h = cfg["health"]
    n = int(h["baseline_runs"])
    if len(rows) < n:
        return None
    b = rows[:n]
    temps = [r["temp"] for r in b if r["temp"] is not None]
    tcoef, tref, learned = {}, (statistics.fmean(temps) if temps else None), False
    multi = any(r.get("cm") for r in b)          # 有跨軸共模時一律用預設係數，和其他軸的指數一致
    if h["temp_comp"] and tref is not None:
        if not multi and len(temps) >= max(5, n // 2) and max(temps) - min(temps) >= float(h["temp_min_range"]):
            for m in HIGH_METRICS:
                pairs = [(r["temp"], math.log(r[m])) for r in b if r["temp"] is not None and r[m] and r[m] > 0]
                fit = ols([p[0] for p in pairs], [p[1] for p in pairs])
                if fit:
                    tcoef[m] = -fit[0]
            learned = bool(tcoef)
        if not tcoef:
            tcoef = prior_coef(cfg, *(_mean_std([r[m] for r in b])[0] for m in ("Fc", "Fv", "F")))
    base = {"n": n, "tcoef": tcoef, "tlearned": learned, "tref": tref, "until": b[-1]["ts"]}
    base["stats"] = {m: _mean_std([comp(r, m, base) for r in b]) for m in HIGH_METRICS}
    base["stats"]["G"] = _mean_std([r["G"] for r in b])
    return base


def prior_coef(cfg: dict, fc: float | None, fv: float | None, f: float | None) -> dict:
    """預設溫度係數；F ≈ Fc + Fv × 參考速度，係數依兩者比重加權。"""
    h = cfg["health"]
    kc, kv = float(h.get("temp_coef_fc", 0.0)), float(h.get("temp_coef_fv", 0.0))
    out = {"Fc": kc, "Fv": kv}
    if f and fc is not None and fv is not None:
        out["F"] = (kc * fc + kv * fv * float(cfg["diag"]["ref_pct"])) / f
    return out


def comp(row: dict, m: str, base: dict | None) -> float | None:
    """補償後的數值：先做溫度補償（有溫度時），再扣跨軸共同變化（有 row["cm"] 時）。"""
    v = row.get(m)
    if v is None:
        return None
    if base and row.get("temp") is not None and base.get("tref") is not None:
        k = base.get("tcoef", {}).get(m)
        if k:
            v = v * math.exp(k * (row["temp"] - base["tref"]))
    cm = row.get("cm")
    if cm and m in cm:
        v = v / cm[m]
    return v


# ------------------------------------------------------------ 跨軸共模
CM_METRICS = ("F", "Fc", "Fv")
CM_MATCH_S = 3600.0          # 同一次診斷程式裡各軸的時間差在 1 小時內
CM_MAX_DT = 30.0             # 共同變化換算成「等效溫差」最多 ±30°C；再大就不是溫度了


def _chain(rows: list[dict], kv: float, resets: list[float]) -> tuple[list[float], list[float]]:
    """一個軸黏性摩擦的連鎖對數指數（有溫度時先補償）：逐次 log 變化量累加；跨過換脂／換零件的那一步不算。"""
    tss, out, L, prev = [], [], 0.0, None
    for r in rows:
        if prev is not None and prev.get("Fv") and r.get("Fv") and prev["Fv"] > 0 and r["Fv"] > 0 \
                and not any(prev["ts"] < t <= r["ts"] for t in resets):
            dT = r["temp"] - prev["temp"] if r["temp"] is not None and prev["temp"] is not None else 0.0
            L += math.log(r["Fv"] / prev["Fv"]) + kv * dT
        tss.append(r["ts"])
        out.append(L)
        prev = r
    return tss, out


def common_mode_setting(cfg: dict) -> str:
    """health.common_mode：true（預設，監測兩軸以上就用）、"auto"（只在沒有溫度訊號時用）、false（不用）。

    預設一律用，是因為溫度係數不準時（預設 2.5%，實際可能差一倍），只靠溫度補償在冬天、換生產程式後仍會誤報；
    共模扣除對各軸相同的誤差都有效。代價是所有軸「同時」一起劣化會被扣掉。有溫度訊號、並用 temp-coef
    校正過係數之後，可以改成 "auto"，連這種情況也抓得到。"""
    v = cfg["health"].get("common_mode", True)
    if v is True or str(v).lower() in ("true", "on", "always"):
        return "on"
    if v is False or str(v).lower() in ("false", "off", "never"):
        return "off"
    return "auto"


def common_index(store, cfg: dict, ax: int, until: float | None = None) -> dict[int, dict]:
    """本軸每一次診斷（特徵 id）→ 各軸共同變化的倍數 {F, Fc, Fv}。

    共同變化只從「黏性摩擦 Fv」取：溫度、冷機、節拍改變主要影響 Fv。把共同的 Fv 變化換算成等效溫差，
    再依各指標的溫度係數換成 Fc、F 的倍數。這樣固定摩擦（磨耗）就算兩個軸同時上升，也不會被當成共同變化扣掉。
    每個時間點取各軸「累積變化」的中位數（不是逐次變化的中位數再累加，那樣雜訊會一路累積偏差）。
    """
    h = cfg["health"]
    mode = common_mode_setting(cfg)
    if mode == "off":
        return {}
    axes = enabled_axes(cfg)
    if len(axes) < 2 or ax not in axes:
        return {}
    if mode == "auto":
        # 有溫度訊號時只做溫度補償，不扣共同變化：否則所有軸「同時」劣化（例如同一天換的油脂一起老化）會被扣掉
        recent = store.feature_values(ax, until=until)[-30:]
        if recent and sum(r["temp"] is not None for r in recent) >= len(recent) / 2:
            return {}
    kc, kv = float(h.get("temp_coef_fc", 0.0)), float(h.get("temp_coef_fv", 0.0))
    if kv <= 0:
        return {}
    k_chain = kv if h["temp_comp"] else 0.0
    # 三軸以上：本軸也一起取中位數（一個軸自己劣化時，中位數會落在健康的軸上，不會被拖走）；只有兩軸：只能看另一軸
    members = axes if len(axes) >= 3 else [a for a in axes if a != ax]
    chains, own = {}, None
    for a in members:
        rows = store.feature_values(a, until=until)
        if a == ax:
            own = rows
        if len(rows) >= 2:
            resets = [e["ts"] for e in store.events(a) if e["kind"] in RESET_KINDS]
            chains[a] = _chain(rows, k_chain, resets)
    if not [a for a in chains if a != ax]:
        return {}
    own = own if own is not None else store.feature_values(ax, until=until)
    fc_m, fv_m, f_m = (_mean_std([r[m] for r in own])[0] for m in ("Fc", "Fv", "F"))
    kf = prior_coef(cfg, fc_m, fv_m, f_m).get("F", kc)

    def level(a, ts):
        tss, ls = chains[a]
        i = bisect.bisect_left(tss, ts)
        j = min((j for j in (i - 1, i) if 0 <= j < len(tss) and abs(tss[j] - ts) <= CM_MATCH_S),
                key=lambda j: abs(tss[j] - ts), default=None)
        return None if j is None else ls[j]

    C, ref, out = 0.0, {}, {}
    for r in own:
        lv = {a: level(a, r["ts"]) for a in chains}
        for a, v in lv.items():                     # 第一次出現的軸：以「目前的共同水準」為起點加入
            if v is not None and a not in ref:
                ref[a] = v - C
        vals = [v - ref[a] for a, v in lv.items() if v is not None]
        if vals:
            C = statistics.median(vals)
        dT = max(-CM_MAX_DT, min(CM_MAX_DT, -C / kv))          # 共同變化 → 等效溫差（變冷 = 負）
        out[r["id"]] = {"Fv": math.exp(-kv * dT), "Fc": math.exp(-kc * dT), "F": math.exp(-kf * dT)}
    return out


def load_rows(store, cfg: dict, ax: int, since: float = 0, until: float | None = None) -> list[dict]:
    """讀取有效診斷特徵，附上跨軸共模倍數（row["cm"]），comp() 會用到。"""
    rows = store.features(ax, since=since, until=until)
    if rows:
        idx = common_index(store, cfg, ax, until)
        for r in rows:
            if r["id"] in idx:
                r["cm"] = idx[r["id"]]
    return rows


def judge(row: dict, base: dict, cfg: dict) -> tuple[int, dict, list[str]]:
    h = cfg["health"]
    out, reasons, level = {}, [], 0
    use_z = base["n"] >= int(h["min_runs_for_z"])
    for m in HIGH_METRICS:
        v = comp(row, m, base)
        mean, std = base["stats"][m]
        ratio = v / mean if v is not None and mean and mean > 0 else None
        std_eff = max(std or 0.0, abs(mean or 0.0) * float(h["min_std_frac"]))
        z = (v - mean) / std_eff if use_z and v is not None and std_eff > 0 else None
        lv = 0
        if ratio is not None:
            # 比值是主判定；z 分數只負責「提早升到注意」與「已達注意比值時升到警戒」
            cr, ar = float(h["caution_ratio"]), float(h["alert_ratio"])
            zc = z is not None and z >= float(h["caution_z"]) and ratio >= float(h["z_min_ratio"])
            za = z is not None and z >= float(h["alert_z"]) and ratio >= cr
            if ratio >= ar or za:
                lv = 2
            elif ratio >= cr or zc:
                lv = 1
        if lv:
            reasons.append(_("{metric} 為基準 {ratio:.2f} 倍", metric=METRIC_LABELS[m], ratio=ratio)
                           + (_("（z={z:.1f}）", z=z) if z is not None else ""))
        out[m] = {"value": v, "raw": row.get(m), "base": mean, "ratio": ratio, "z": z, "level": lv}
        level = max(level, lv)
    gmean = base["stats"]["G"][0]
    fmean = base["stats"]["F"][0]
    g = row.get("G")
    frac = abs(g - gmean) / fmean if g is not None and gmean is not None and fmean else None
    if row.get("axis") and axis_cfg(cfg, int(row["axis"])).get("linear"):
        frac = None                    # 行走軸（直線、水平軌道）沒有重力矩，G 只剩雜訊，不判定
    lv = 0
    if frac is not None:
        lv = 2 if frac >= float(h["g_alert_frac"]) else 1 if frac >= float(h["g_caution_frac"]) else 0
    if lv:
        reasons.append(_("{metric} 偏移達 F 基準的 {frac:.2f} 倍", metric=METRIC_LABELS["G"], frac=frac))
    out["G"] = {"value": g, "raw": g, "base": gmean, "dev_frac": frac, "level": lv}
    return max(level, lv), out, reasons


def hint(metrics: dict) -> str:
    """依各指標變化給失效模式提示（輔助判讀，不取代拆檢）。"""
    r = {m: (metrics.get(m) or {}).get("ratio") or 1.0 for m in HIGH_METRICS}
    g = (metrics.get("G") or {}).get("level", 0)
    tips = []
    if g:
        tips.append(_("正反轉同向偏移：檢查平衡器與負載設定，這不是摩擦"))
    if r["ripple"] >= 1.4 and r["F"] >= 1.2:
        tips.append(_("摩擦與漣波同步上升：減速機磨耗或局部損傷，安排鐵粉與振動確認"))
    elif r["ripple"] >= 1.8:
        tips.append(_("漣波明顯上升：疑似局部損傷（剝離、點蝕），建議做鐵譜"))
    if r["Fv"] >= 1.15 and r["Fv"] > r["Fc"] * 1.1:
        tips.append(_("黏性項上升為主：油脂劣化、過量填脂或低溫（確認暖機）"))
    elif r["Fc"] >= 1.15 and r["Fc"] > r["Fv"] * 1.1:
        tips.append(_("固定摩擦上升為主：減速機磨耗、煞車拖曳或預壓異常"))
    return join(tips)


def evaluate_axis(rows: list[dict], cfg: dict, now: float | None = None) -> dict:
    """rows：上次重設事件之後、依時間排序的有效特徵。"""
    h = cfg["health"]
    now = now or time.time()
    res = {"level": -1, "level_name": LEVEL_NAMES[-1], "baseline_ready": False, "n_runs": len(rows),
           "need_runs": int(h["baseline_runs"]), "metrics": {}, "reasons": [], "hint": "",
           "slope_per_day": None, "slope_pct_month": None, "days_to_caution": None, "days_to_alert": None,
           "last_ts": rows[-1]["ts"] if rows else None, "temp": rows[-1]["temp"] if rows else None,
           "series": []}
    if not rows:
        return res
    base = baseline(rows, cfg)
    if base is None:
        res["metrics"] = {m: {"value": rows[-1].get(m), "raw": rows[-1].get(m)} for m in HIGH_METRICS + ["G"]}
        res["series"] = [{"ts": r["ts"], "id": r["id"], "level": -1,
                          **{m: comp(r, m, None) for m in HIGH_METRICS + ["G"]}} for r in rows]
        return res
    levels = []
    for r in rows:
        lv, met, rs = judge(r, base, cfg)
        levels.append(lv)
        res["series"].append({"ts": r["ts"], "id": r["id"], "level": lv, "temp": r.get("temp"),
                              **{m: met[m]["value"] for m in HIGH_METRICS + ["G"]}})
    lv_now, met, reasons = judge(rows[-1], base, cfg)
    # 遲滯：連續 confirm 次都達到才升級，連續 confirm 次都低於才降級，避免在門檻附近來回跳
    confirm = max(1, int(h["confirm_runs"]))
    overall = 0
    for i in range(len(levels)):
        win = levels[max(0, i - confirm + 1):i + 1]
        if len(win) < confirm:
            continue
        if min(win) > overall:
            overall = min(win)
        elif max(win) < overall:
            overall = max(win)
    res.update({"level": overall, "level_name": LEVEL_NAMES[overall], "level_latest": lv_now,
                "baseline_ready": True, "metrics": met, "reasons": reasons, "hint": hint(met),
                "baseline": {m: base["stats"][m][0] for m in base["stats"]},
                "baseline_std": {m: base["stats"][m][1] for m in base["stats"]},
                "baseline_until": base["until"], "tcoef": base["tcoef"], "tref": base["tref"],
                "temp_mode": ("learned" if base["tlearned"] else "prior")
                if base["tcoef"] and rows[-1]["temp"] is not None else None,
                "common_mode": bool(rows[-1].get("cm"))})
    res["temp_comp"] = res["temp_mode"] is not None
    # 趨勢：最近 N 次（溫度補償後）的 F 對天數回歸，外推到注意與警戒門檻
    win = rows[-int(h["trend_window_runs"]):]
    if len(win) >= 5:
        xs = [(r["ts"] - now) / 86400 for r in win]
        ys = [comp(r, "F", base) for r in win]
        fmean = base["stats"]["F"][0]
        ex = extrapolate(xs, ys, {"days_to_caution": float(h["caution_ratio"]) * fmean,
                                  "days_to_alert": float(h["alert_ratio"]) * fmean}) if fmean else None
        if ex:
            res["slope_per_day"] = ex["slope"]
            res["slope_pct_month"] = ex["slope"] * 30 / fmean * 100
            res["days_to_caution"], res["days_to_alert"] = ex["days_to_caution"], ex["days_to_alert"]
            res["trend_accel"] = ex["accel"]
            if ex["accel"] and res["level"] >= 1:
                res["reasons"].append(_("摩擦上升在加速，剩餘天數改用保守估計"))
    return res


def epochs(rows: list[dict], resets: list[float], cfg: dict) -> list[dict]:
    """依重設事件（換脂、更換零件）切成多段，每段各自建立基準與等級，供趨勢圖與報告使用。"""
    cuts = sorted(t for t in resets if rows and rows[0]["ts"] < t <= rows[-1]["ts"])
    bounds = [float("-inf"), *cuts, float("inf")]
    out = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg = [r for r in rows if a <= r["ts"] < b]
        if not seg:
            continue
        ev = evaluate_axis(seg, cfg, seg[-1]["ts"])
        base = baseline(seg, cfg)
        out.append({"start": seg[0]["ts"] if a == float("-inf") else a, "end": seg[-1]["ts"], "rows": seg,
                    "base": base, "levels": {p["id"]: p["level"] for p in ev["series"]}})
    return out


# ------------------------------------------------------------ 從自己的資料估計溫度係數
def _ols2(x1: list[float], x2: list[float], y: list[float]):
    """y = a + b1·x1 + b2·x2 的最小平方解與 b1 的標準誤；資料不夠或共線時回傳 None。"""
    n = len(y)
    if n < 6:
        return None
    m1, m2, my = statistics.fmean(x1), statistics.fmean(x2), statistics.fmean(y)
    s11 = sum((a - m1) ** 2 for a in x1)
    s22 = sum((b - m2) ** 2 for b in x2)
    s12 = sum((a - m1) * (b - m2) for a, b in zip(x1, x2))
    s1y = sum((a - m1) * (c - my) for a, c in zip(x1, y))
    s2y = sum((b - m2) * (c - my) for b, c in zip(x2, y))
    det = s11 * s22 - s12 ** 2
    if s11 <= 0 or s22 <= 0 or det <= 1e-12 * s11 * s22:
        return None
    b1 = (s22 * s1y - s12 * s2y) / det
    b2 = (s11 * s2y - s12 * s1y) / det
    a = my - b1 * m1 - b2 * m2
    rss = sum((c - a - b1 * u - b2 * v) ** 2 for u, v, c in zip(x1, x2, y))
    se = math.sqrt(rss / (n - 3) * s22 / det) if n > 3 else float("inf")
    r12 = s12 / math.sqrt(s11 * s22)
    return b1, se, r12


def estimate_temp_coef(rows: list[dict], min_runs: int = 20, min_range: float = 8.0) -> dict:
    """用有溫度的診斷資料估計溫度係數：ln(值) = a − k·溫度 + c·天數（天數項吸收慢慢的劣化趨勢）。
    回傳 {指標: {"k", "se", "n", "range", "ok", "why"}}；k 的意義同設定檔 temp_coef_fv（每 °C 的比例變化）。"""
    pts = [r for r in rows if r.get("temp") is not None]
    out = {}
    for m in ("Fv", "Fc"):
        p = [r for r in pts if r.get(m) and r[m] > 0]
        info = {"k": None, "se": None, "n": len(p), "range": 0.0, "ok": False, "why": ""}
        if p:
            temps = [r["temp"] for r in p]
            info["range"] = max(temps) - min(temps)
        if len(p) < min_runs:
            info["why"] = _("資料不足（{n} 次，至少 {need} 次）", n=len(p), need=min_runs)
        elif info["range"] < min_range:
            info["why"] = _("溫差不足（{r:.1f}°C，至少 {need:.0f}°C）", r=info["range"], need=min_range)
        else:
            t0 = p[0]["ts"]
            fit = _ols2([r["temp"] for r in p], [(r["ts"] - t0) / 86400 for r in p], [math.log(r[m]) for r in p])
            if fit is None:
                info["why"] = _("溫度和時間幾乎同步變化，分不出是溫度還是劣化")
            else:
                b, se, r12 = fit
                info.update(k=-b, se=se)
                if abs(r12) > 0.95:
                    info["why"] = _("溫度和時間幾乎同步變化，估計不可靠")
                elif se > 0.5 * abs(b) + 0.002:
                    info["why"] = _("估計誤差太大")
                else:
                    info["ok"] = True
        out[m] = info
    return out

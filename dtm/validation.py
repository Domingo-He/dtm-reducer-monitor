"""現場驗證報告（2.1）：拿實際的失效與警報紀錄，檢查 DTM 的判斷準不準。Beta 的門檻要靠這份報告修正。

這份報告回答三個問題：
1. 偵測率：確認失效（更換後拆檢確認損傷）的元件，換之前有沒有先出現注意或警戒？提早幾天？
2. 漏報：有足夠監測資料、卻一直沒有警報就壞掉的。
3. 誤報候選：出現警戒後 90 天內沒有任何保養，或只是重設基準（原因不是元件劣化）。

資料來源：
- 中央／邊緣主機：每天每軸的健康等級（axis_daily，取健康與生產監測的較高者）＋健康、生產監測警報。
- 單機：健康、生產監測警報＋有診斷資料的天數（當作有監測）。
- 失效：元件更換紀錄 failed = 1（登錄更換時勾「失效」，或拆檢表單確認損傷）。
  failed = 0 的更換是預防更換或「拆檢無異常」，不算失效；若更換前有警報，列在「警報後更換、未確認失效」。

定義寫在 DEFS，報告裡也印出來，讓看報告的人知道數字怎麼算。
"""
from __future__ import annotations

import html
import json
import math
import statistics
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from .config import COMP_LABELS, EVENT_LABELS, axis_name
from .i18n import FONT_STACK, HTML_LANG, N_, _, get_lang

LOOKBACK_D = 365        # 失效前往回找警報的最長天數（也不早於這個元件的安裝日）
COVER_WIN_D = 90        # 失效前這段期間
COVER_MIN_D = 30        # 至少要有幾天監測資料，才算「可以評估」
ACTION_D = 90           # 警戒後幾天內有保養才算「有處置」
PRE_D = 7               # 警戒前幾天內的保養也算（先排了保養、警戒才出現）
MERGE_GAP_D = 3         # 同一軸兩段警戒中間斷不到幾天，算同一次
DRIVE = ("reducer", "gearbox", "motor")
MAINT = ("grease_change", "reducer_replace", "gearbox_replace", "motor_replace", "inspection")
RESET = ("baseline_reset", "motion_reset")
COND_PREFIX = ("health:", "motion:")

RESULT = {
    "alert": N_("警戒後失效（提早偵測）"),
    "caution": N_("注意後失效（提早偵測）"),
    "missed": N_("漏報"),
    "nodata": N_("資料不足"),
    "prev_alarm": N_("警報後更換、未確認失效"),
}
OUTCOME = {
    "confirmed": N_("確認失效"),
    "maint": N_("有保養處置"),
    "reset": N_("重設基準（非劣化原因）"),
    "unresolved": N_("90 天內無處置（誤報候選）"),
    "pending": N_("觀察中（未滿 90 天）"),
}
DEFS = [
    N_("偵測率＝更換前 {lb} 天內（且在安裝之後）出現過注意或警戒的失效件數 ÷ 可以評估的失效件數。"),
    N_("可以評估＝失效前 {cw} 天內至少有 {cm} 天監測資料；不足的列為「資料不足」，不算進偵測率。"),
    N_("提早天數＝第一次出現該等級的那天到更換日。"),
    N_("誤報候選＝警戒後 {ad} 天內沒有任何保養或點檢，以及只做了重設基準的警戒；同一軸中斷不超過 {mg} 天算同一次。"),
    N_("95% 信賴區間用 Wilson 法；樣本少時區間很寬，只能當參考。"),
]


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, c - h), min(1.0, c + h)


def _mark(levels: dict, key, t0: float, t1: float, lv: int):
    d0, d1 = _d(day_of(t0)), _d(day_of(t1))
    if (d1 - d0).days > 2000:
        d0 = d1 - timedelta(days=2000)
    while d0 <= d1:
        k = d0.isoformat()
        levels[key][k] = max(levels[key].get(k, -1), lv)
        d0 += timedelta(days=1)


# ------------------------------------------------------------ 讀資料
def from_central(cs, now: float) -> dict:
    robots = {r["rkey"]: {"name": r["name"] or r["rkey"], "site": r["site"] or r["site_id"] or ""}
              for r in cs.q("SELECT r.rkey, r.name, r.site_id, s.name AS site FROM robots r "
                            "LEFT JOIN sites s ON s.site_id=r.site_id")}
    levels: dict = defaultdict(dict)
    for r in cs.q("SELECT day, rkey, axis, MAX(COALESCE(level,-1), COALESCE(motion_level,-1)) AS lv FROM axis_daily"):
        levels[(r["rkey"], int(r["axis"]))][r["day"]] = int(r["lv"])
    for a in cs.q("SELECT rkey, axis, akey, level, ts, cleared_ts FROM alarms WHERE akey LIKE 'health:%' OR akey LIKE 'motion:%'"):
        if a["axis"] and a["level"] and a["ts"]:
            _mark(levels, (a["rkey"], int(a["axis"])), float(a["ts"]), float(a["cleared_ts"] or now), int(a["level"]))
    removals = [dict(r, no_fault=False) for r in cs.q(
        "SELECT rkey, axis, comp, install_ts, removed_ts, life_h, failed, reason FROM components "
        "WHERE removed_ts IS NOT NULL AND axis IS NOT NULL")]
    events = cs.q("SELECT rkey, ts, axis, kind FROM events WHERE undone_ts IS NULL")
    return {"robots": robots, "levels": levels, "removals": removals, "events": events, "scope": "fleet"}


def from_store(cfg: dict, store, now: float) -> dict:
    from . import teardown as TD
    rk = "local"
    robots = {rk: {"name": cfg["robot"]["name"], "site": ""}}
    levels: dict = defaultdict(dict)
    with store.conn() as c:
        for r in c.execute("SELECT axis, date(ts,'unixepoch','localtime') AS d FROM features WHERE valid=1 GROUP BY axis, d"):
            levels[(rk, int(r["axis"]))].setdefault(r["d"], 0)
        for r in c.execute("SELECT axis, day FROM motion_daily GROUP BY axis, day"):
            levels[(rk, int(r["axis"]))].setdefault(r["day"], 0)
        al = [dict(r) for r in c.execute("SELECT axis, akey, level, ts, cleared_ts FROM alarms "
                                         "WHERE akey LIKE 'health:%' OR akey LIKE 'motion:%'")]
        comps = [dict(r) for r in c.execute("SELECT axis, comp, install_ts, removed_ts, life_h, failed, reason, teardown "
                                            "FROM components WHERE removed_ts IS NOT NULL AND axis IS NOT NULL")]
        events = [dict(r, rkey=rk) for r in c.execute("SELECT ts, axis, kind FROM events")]
    for a in al:
        if a["axis"] and a["level"]:
            _mark(levels, (rk, int(a["axis"])), float(a["ts"]), float(a["cleared_ts"] or now), int(a["level"]))
    removals = []
    for r in comps:
        td = TD.parse(r.pop("teardown", None)) or {}
        removals.append(dict(r, rkey=rk, no_fault=bool(td.get("no_fault"))))
    return {"robots": robots, "levels": levels, "removals": removals, "events": events, "scope": "robot"}


# ------------------------------------------------------------ 分析
def _episodes(lv: dict, lo: int) -> list[tuple[str, str, int]]:
    """等級 ≥ lo 的連續期間（中斷不超過 MERGE_GAP_D 天合併）：[(開始, 結束, 最高等級)]。"""
    days = sorted(d for d, v in lv.items() if v >= lo)
    out: list[list] = []
    for d in days:
        if out and (_d(d) - _d(out[-1][1])).days <= MERGE_GAP_D + 1:
            out[-1][1] = d
            out[-1][2] = max(out[-1][2], lv[d])
        else:
            out.append([d, d, lv[d]])
    return [tuple(x) for x in out]


def analyze(data: dict, cfg: dict | None = None, days: float = 730, now: float | None = None) -> dict:
    now = now or time.time()
    t_from = now - days * 86400
    levels, robots = data["levels"], data["robots"]
    ev_by = defaultdict(list)
    for e in data["events"]:
        ev_by[e["rkey"]].append(e)

    def rname(rk):
        r = robots.get(rk) or {}
        return r.get("name") or rk, r.get("site") or ""

    # ---- 失效與更換
    fails = []
    fail_keys = []                    # (rkey, axis, removed_ts) 給警戒期間比對
    for r in sorted(data["removals"], key=lambda x: x["removed_ts"]):
        if r["comp"] not in DRIVE or not r["removed_ts"] or r["removed_ts"] < t_from or r["removed_ts"] > now:
            continue
        rk, ax, rem = r["rkey"], int(r["axis"]), float(r["removed_ts"])
        lv = levels.get((rk, ax), {})
        start = max(float(r["install_ts"] or 0), rem - LOOKBACK_D * 86400)
        d_start, d_rem = day_of(start), day_of(rem)
        win = sorted(d for d in lv if d_start <= d < d_rem)
        cov_from = day_of(rem - COVER_WIN_D * 86400)
        cover = sum(1 for d in win if d >= cov_from and lv[d] >= 0)
        first_c = next((d for d in win if lv[d] >= 1), None)
        first_a = next((d for d in win if lv[d] >= 2), None)
        lead = lambda d: (_d(d_rem) - _d(d)).days if d else None  # noqa: E731
        failed = bool(r["failed"]) and not r.get("no_fault")
        if failed:
            res = "alert" if first_a else "caution" if first_c else ("missed" if cover >= COVER_MIN_D else "nodata")
            fail_keys.append((rk, ax, rem))
        elif first_c:
            res = "prev_alarm"
        else:
            continue                  # 計畫性預防更換，不在驗證範圍
        name, site = rname(rk)
        fails.append({"rkey": rk, "robot": name, "site": site, "axis": ax, "axis_name": axis_name(cfg, ax),
                      "comp": r["comp"], "comp_name": COMP_LABELS.get(r["comp"], r["comp"]), "removed_ts": rem,
                      "life_h": r.get("life_h"), "reason": r.get("reason") or "", "no_fault": bool(r.get("no_fault")),
                      "result": res, "result_name": _(RESULT[res]), "lead_caution": lead(first_c),
                      "lead_alert": lead(first_a), "cover_days": cover})

    # ---- 警戒期間
    eps = []
    caution = {"n": 0, "escalated": 0, "maint": 0, "cleared": 0}
    for (rk, ax), lv in levels.items():
        for s, e, top in _episodes(lv, 1):
            ts = datetime.strptime(s, "%Y-%m-%d").timestamp()
            if ts < t_from:
                continue
            evs = [x for x in ev_by.get(rk, []) if (not x["axis"] or int(x["axis"]) == ax)
                   and ts - PRE_D * 86400 <= float(x["ts"]) <= ts + ACTION_D * 86400]
            kinds = {x["kind"] for x in evs}
            conf = any(k[0] == rk and k[1] == ax and ts - PRE_D * 86400 <= k[2] <= ts + max(ACTION_D, LOOKBACK_D) * 86400
                       for k in fail_keys)
            if conf:
                out = "confirmed"
            elif kinds & set(MAINT):
                out = "maint"
            elif kinds & set(RESET):
                out = "reset"
            elif ts > now - ACTION_D * 86400:
                out = "pending"
            else:
                out = "unresolved"
            if top < 2:
                caution["n"] += 1
                if out in ("confirmed", "maint"):
                    caution["maint"] += 1
                elif out in ("unresolved", "reset"):
                    caution["cleared"] += 1
                continue
            caution["escalated"] += 1
            # 警戒那一段（等級 2 的第一天起）
            a_days = [d for d in sorted(lv) if s <= d <= e and lv[d] >= 2]
            a0 = a_days[0] if a_days else s
            name, site = rname(rk)
            eps.append({"rkey": rk, "robot": name, "site": site, "axis": ax, "axis_name": axis_name(cfg, ax),
                        "start": a0, "end": e, "days": (_d(e) - _d(a0)).days + 1, "outcome": out,
                        "outcome_name": _(OUTCOME[out]),
                        "actions": [EVENT_LABELS.get(k, k) for k in sorted(kinds)]})
    eps.sort(key=lambda x: x["start"], reverse=True)

    # ---- 監測量（機台·年）
    rdays = defaultdict(set)
    lo = day_of(t_from)
    for (rk, _ax), lv in levels.items():
        rdays[rk].update(d for d, v in lv.items() if v >= 0 and d >= lo)
    robot_years = sum(len(v) for v in rdays.values()) / 365.0

    n_eval = sum(1 for f in fails if f["result"] in ("alert", "caution", "missed"))
    n_det = sum(1 for f in fails if f["result"] in ("alert", "caution"))
    n_al = sum(1 for f in fails if f["result"] == "alert")
    leads_c = [f["lead_caution"] for f in fails if f["result"] in ("alert", "caution") and f["lead_caution"] is not None]
    leads_a = [f["lead_alert"] for f in fails if f["result"] == "alert" and f["lead_alert"] is not None]
    fa = sum(1 for x in eps if x["outcome"] in ("unresolved", "reset"))
    ci = wilson(n_det, n_eval)
    summary = {
        "failures": sum(1 for f in fails if f["result"] != "prev_alarm"), "evaluable": n_eval, "detected": n_det,
        "detected_alert": n_al, "missed": sum(1 for f in fails if f["result"] == "missed"),
        "nodata": sum(1 for f in fails if f["result"] == "nodata"),
        "prev_alarm": sum(1 for f in fails if f["result"] == "prev_alarm"),
        "rate": n_det / n_eval if n_eval else None, "rate_ci": list(ci) if ci else None,
        "rate_alert": n_al / n_eval if n_eval else None,
        "lead_caution_med": statistics.median(leads_c) if leads_c else None,
        "lead_caution_min": min(leads_c) if leads_c else None,
        "lead_alert_med": statistics.median(leads_a) if leads_a else None,
        "lead_alert_min": min(leads_a) if leads_a else None,
        "alert_episodes": len(eps), "false_alarm": fa,
        "false_alarm_per100": fa / robot_years * 100 if robot_years > 0.05 else None,
        "pending": sum(1 for x in eps if x["outcome"] == "pending"),
        "robots": len(rdays), "robot_years": round(robot_years, 1), "caution": caution,
        "small_sample": n_eval < 10,
    }
    return {"generated": now, "from": t_from, "to": now, "days": days, "scope": data.get("scope"),
            "summary": summary, "failures": fails, "episodes": eps,
            "params": {"lb": LOOKBACK_D, "cw": COVER_WIN_D, "cm": COVER_MIN_D, "ad": ACTION_D, "mg": MERGE_GAP_D},
            "defs": [_(x, lb=LOOKBACK_D, cw=COVER_WIN_D, cm=COVER_MIN_D, ad=ACTION_D, mg=MERGE_GAP_D) for x in DEFS]}


def for_central(cs, days: float = 730, now: float | None = None) -> dict:
    now = now or time.time()
    return analyze(from_central(cs, now), cs.cfg, days, now)


def for_store(cfg: dict, store, days: float = 730, now: float | None = None) -> dict:
    now = now or time.time()
    return analyze(from_store(cfg, store, now), cfg, days, now)


# ------------------------------------------------------------ 報告（A4 可列印）
def render(r: dict, title_scope: str = "", version: str = "") -> str:
    e = html.escape
    lang = get_lang()
    s = r["summary"]
    f = lambda t: time.strftime("%Y-%m-%d", time.localtime(t)) if t else "—"  # noqa: E731
    pct = lambda v: "—" if v is None else f"{v * 100:.0f}%"  # noqa: E731
    num = lambda v: "—" if v is None else f"{v:g}"  # noqa: E731
    dd = lambda v: "—" if v is None else _("{n} 天", n=f"{v:g}")  # noqa: E731
    ci = s["rate_ci"]
    ci_t = _("95% 信賴區間 {a}–{b}", a=pct(ci[0]), b=pct(ci[1])) if ci else ""
    kpis = [
        (_("偵測率（注意以上）"), pct(s["rate"]), _("{d}／{n} 件可以評估的失效", d=s["detected"], n=s["evaluable"]) + ("　" + ci_t if ci_t else "")),
        (_("警戒偵測率"), pct(s["rate_alert"]), _("失效前就達到警戒")),
        (_("提早天數（注意）"), dd(s["lead_caution_med"]), _("中位數；最短 {m}", m=dd(s["lead_caution_min"]))),
        (_("提早天數（警戒）"), dd(s["lead_alert_med"]), _("中位數；最短 {m}", m=dd(s["lead_alert_min"]))),
        (_("漏報"), str(s["missed"]), _("資料不足、無法評估 {n} 件", n=s["nodata"])),
        (_("誤報候選"), str(s["false_alarm"]), _("每百台·年 {v}", v="—" if s["false_alarm_per100"] is None else f"{s['false_alarm_per100']:.1f}")
         + "　" + _("觀察中 {n}", n=s["pending"])),
    ]
    kp = "".join(f"<div class='k'><span>{e(a)}</span><b>{e(b)}</b><small>{e(c)}</small></div>" for a, b, c in kpis)
    fleet = r.get("scope") == "fleet"
    fr = "".join(
        "<tr class='{cls}'><td>{d}</td>{site}<td>{rb}</td><td>{ax}</td><td>{comp}</td><td>{res}</td><td class='n'>{lc}</td>"
        "<td class='n'>{la}</td><td class='n'>{cov}</td><td>{why}</td></tr>".format(
            cls={"missed": "bad", "prev_alarm": "mid", "nodata": "mut"}.get(x["result"], ""), d=e(f(x["removed_ts"])),
            site=f"<td>{e(x['site'])}</td>" if fleet else "", rb=e(x["robot"]), ax=e(x["axis_name"]), comp=e(x["comp_name"]),
            res=e(x["result_name"] + (("（" + _("拆檢無異常") + "）") if x["no_fault"] else "")),
            lc=num(x["lead_caution"]), la=num(x["lead_alert"]), cov=x["cover_days"], why=e(x["reason"]))
        for x in r["failures"]) or f"<tr><td colspan='{10 if fleet else 9}' class='mut'>{e(_('期間內沒有更換紀錄'))}</td></tr>"
    er = "".join(
        "<tr class='{cls}'><td>{a}</td>{site}<td>{rb}</td><td>{ax}</td><td class='n'>{n}</td><td>{o}</td><td>{act}</td></tr>".format(
            cls={"unresolved": "bad", "reset": "mid", "pending": "mut"}.get(x["outcome"], ""), a=e(x["start"]),
            site=f"<td>{e(x['site'])}</td>" if fleet else "", rb=e(x["robot"]), ax=e(x["axis_name"]), n=x["days"],
            o=e(x["outcome_name"]), act=e(_("、").join(x["actions"]) or "—"))
        for x in r["episodes"][:300]) or f"<tr><td colspan='{7 if fleet else 6}' class='mut'>{e(_('期間內沒有警戒'))}</td></tr>"
    cs_ = s["caution"]
    title = _("現場驗證報告")
    site_h = f"<th>{e(_('廠區'))}</th>" if fleet else ""
    note = _("失效件數少於 10 件：偵測率的信賴區間很寬，請持續累積紀錄。") if s["small_sample"] else ""
    return f"""<!doctype html><html lang="{HTML_LANG.get(lang, 'zh-Hant')}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(title)}</title>
<style>@page{{size:A4;margin:14mm 12mm}}
body{{font-family:{FONT_STACK.get(lang, FONT_STACK['zh-TW'])};color:#1F2A33;max-width:1000px;margin:24px auto;padding:0 16px;font-size:13px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;border-bottom:2px solid #A35F0E;padding-bottom:4px;margin-top:24px;break-after:avoid}}
.sub{{color:#4A5866}} .kp{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:14px 0}}
.k{{border:1px solid #DDE1E4;border-radius:8px;padding:10px 12px;break-inside:avoid}} .k span{{display:block;color:#4A5866;font-size:12px}}
.k b{{display:block;font-size:22px;margin:2px 0}} .k small{{color:#4A5866}}
table{{border-collapse:collapse;width:100%;font-size:12px}} td,th{{border-bottom:1px solid #E3E6E8;padding:5px 6px;text-align:left;vertical-align:top}}
th{{background:#F2F4F5}} thead{{display:table-header-group}} tr{{break-inside:avoid}} td.n{{text-align:right;font-variant-numeric:tabular-nums}}
tr.bad td{{background:#FBEAE6}} tr.mid td{{background:#FFF6E0}} tr.mut td,.mut{{color:#6B7782}}
ol{{padding-left:20px;color:#4A5866}} .warn{{background:#FFF6E0;border-radius:8px;padding:8px 12px;margin:10px 0}}
.foot{{margin-top:24px;color:#6B7782;font-size:11px;border-top:1px solid #E3E6E8;padding-top:8px}}
@media print{{body{{margin:0;max-width:none}} .k b{{font-size:18px}}}}</style></head><body>
<h1>{e(title)}{('&ensp;' + e(title_scope)) if title_scope else ''}</h1>
<div class="sub">{e(_("期間：{a} ～ {b}（{d:g} 天）　監測：{r} 台、{y} 台·年　產生時間：{n}", a=f(r['from']), b=f(r['to']), d=r['days'], r=s['robots'], y=s['robot_years'], n=time.strftime('%Y-%m-%d %H:%M', time.localtime(r['generated']))))}</div>
{f'<div class="warn">{e(note)}</div>' if note else ''}
<div class="kp">{kp}</div>
<div class="sub">{e(_("注意期間 {n} 次：升為警戒 {a} 次、有保養處置 {m} 次、自行恢復或重設基準 {c} 次。警報後更換但未確認失效（預防更換或拆檢無異常）{p} 件。", n=cs_['n'] + cs_['escalated'], a=cs_['escalated'], m=cs_['maint'], c=cs_['cleared'], p=s['prev_alarm']))}</div>
<h2>{e(_("失效與更換"))}</h2>
<table><thead><tr><th>{e(_('更換日'))}</th>{site_h}<th>{e(_('機器人'))}</th><th>{e(_('軸'))}</th><th>{e(_('元件'))}</th><th>{e(_('結果'))}</th>
<th>{e(_('提早天數（注意）'))}</th><th>{e(_('提早天數（警戒）'))}</th><th>{e(_('監測天數'))}</th><th>{e(_('更換原因'))}</th></tr></thead><tbody>{fr}</tbody></table>
<h2>{e(_("警戒與後續處置"))}</h2>
<table><thead><tr><th>{e(_('開始'))}</th>{site_h}<th>{e(_('機器人'))}</th><th>{e(_('軸'))}</th><th>{e(_('天數'))}</th><th>{e(_('結果'))}</th><th>{e(_('處置'))}</th></tr></thead><tbody>{er}</tbody></table>
<h2>{e(_("計算方式"))}</h2><ol>{''.join(f'<li>{e(x)}</li>' for x in r['defs'])}</ol>
<div class="foot">DTM {e(version)} · {e(_("Beta：判斷門檻待現場驗證，這份報告就是驗證的依據。失效的認定以登錄的更換紀錄與拆檢結果為準。"))}</div>
</body></html>"""


def build_html(r: dict, scope: str = "") -> str:
    from . import __version__
    return render(r, scope, __version__)


def to_json(r: dict) -> str:
    return json.dumps(r, ensure_ascii=False, default=str)

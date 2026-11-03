"""導入檢查報告（2.1）：裝好跑滿 7 天自動產生，也可以用指令或按鈕隨時產生（HTML，單一檔案）。

確認的是「資料可不可信」，不是判斷結果：
1. 取樣頻率與抖動（實際頻率、取樣間隔的中位數／P95／P99）
2. 資料中斷（間隔超過 stale_s 的次數、總時間、最長）與心跳、連線警報
3. 診斷段切割成功率與被丟棄的原因
4. 各軸雜訊（診斷特徵的漣波中位數、F 的變異係數）
5. 動作代碼涵蓋率（每個代碼每天的循環數、有效比例、被排除的原因）
6. 感測器更新間隔（有接感測器時）
"""
from __future__ import annotations

import html
import json
import os
import sqlite3
import statistics
import time
from collections import Counter, defaultdict

from .config import axis_name, enabled_axes
from .i18n import FONT_STACK, HTML_LANG, _, get_lang
from . import runtime as R

OK, WARN = "ok", "warn"


def _pct(sorted_vals: list[float], p: float):
    if not sorted_vals:
        return None
    k = min(len(sorted_vals) - 1, max(0, int(round(p / 100 * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def sampling(c, t0: float, t1: float, poll_hz: float, stale_s: float) -> dict:
    """取樣間隔：用直方圖（1 ms 一格）算百分位數，不把幾百萬筆讀進記憶體。"""
    n, first, last = c.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM samples WHERE ts>=? AND ts<?", (t0, t1)).fetchone()
    out = {"n": n, "first": first, "last": last, "rate_hz": None, "p50_ms": None, "p95_ms": None, "p99_ms": None,
           "gaps": [], "gap_total_s": 0.0, "gap_max_s": 0.0, "expected_hz": poll_hz}
    if not n or n < 2:
        return out
    hist: Counter = Counter()
    prev = None
    gaps = []
    for (ts,) in c.execute("SELECT ts FROM samples WHERE ts>=? AND ts<? ORDER BY ts", (t0, t1)):
        if prev is not None:
            d = ts - prev
            if d > stale_s:
                gaps.append((prev, d))
            else:
                hist[int(d * 1000)] += 1
        prev = ts
    total = sum(hist.values())
    if total:
        acc, marks = 0, {}
        for ms in sorted(hist):
            acc += hist[ms]
            for p in (50, 95, 99):
                if p not in marks and acc >= total * p / 100:
                    marks[p] = ms
        out.update(p50_ms=marks.get(50), p95_ms=marks.get(95), p99_ms=marks.get(99))
    active = (last - first) - sum(d for _t, d in gaps)
    out["rate_hz"] = (n - 1 - len(gaps)) / active if active > 0 else None
    out["gaps"] = sorted(gaps, key=lambda g: -g[1])
    out["gap_total_s"] = sum(d for _t, d in gaps)
    out["gap_max_s"] = max((d for _t, d in gaps), default=0.0)
    return out


def diag(c, t0: float, t1: float, axes: list[int]) -> dict:
    rows = [dict(r) for r in c.execute("SELECT ts, axis, F, ripple, valid, detail FROM features WHERE ts>=? AND ts<?",
                                       (t0, t1))]
    reasons: Counter = Counter()
    per_ax = {}
    for ax in axes:
        rs = [r for r in rows if r["axis"] == ax]
        ok = [r for r in rs if r["valid"]]
        for r in rs:
            if not r["valid"]:
                try:
                    inv = (json.loads(r["detail"] or "{}") or {}).get("invalid") or []
                except ValueError:
                    inv = []
                for i in inv or [{"reason": _("缺少正反轉配對")}]:
                    reasons[i.get("reason") or _("未知")] += 1
        fs = [r["F"] for r in ok if r["F"] is not None]
        rip = [r["ripple"] for r in ok if r["ripple"] is not None]
        cv = statistics.pstdev(fs) / abs(statistics.fmean(fs)) if len(fs) >= 3 and statistics.fmean(fs) else None
        per_ax[ax] = {"runs": len(rs), "valid": len(ok), "ripple_med": statistics.median(rip) if rip else None,
                      "f_cv": cv}
    total = sum(v["runs"] for v in per_ax.values())
    valid = sum(v["valid"] for v in per_ax.values())
    return {"total": total, "valid": valid, "rate": valid / total if total else None, "reasons": reasons.most_common(8),
            "axes": per_ax}


def motion(c, t0: float, t1: float, min_day: int) -> dict:
    rows = [dict(r) for r in c.execute("SELECT ts, code, axis, valid, reason FROM cycles WHERE ts>=? AND ts<?", (t0, t1))]
    by: dict[int, dict] = defaultdict(lambda: {"n": 0, "valid": 0, "days": Counter(), "axes": set(), "reasons": Counter()})
    for r in rows:
        b = by[r["code"]]
        b["n"] += 1
        b["axes"].add(r["axis"])
        day = time.strftime("%Y-%m-%d", time.localtime(r["ts"]))
        if r["valid"]:
            b["valid"] += 1
            b["days"][day] += 1
        elif r["reason"]:
            b["reasons"][r["reason"]] += 1
    out = []
    n_days = max(1, round((t1 - t0) / 86400))
    for code, b in sorted(by.items()):
        n_ax = max(1, len(b["axes"]))
        good_days = sum(1 for v in b["days"].values() if v / n_ax >= min_day)
        out.append({"code": code, "cycles": b["n"] // n_ax, "valid_rate": b["valid"] / b["n"] if b["n"] else None,
                    "per_day": b["valid"] / n_ax / n_days, "good_days": good_days, "days": n_days,
                    "axes": sorted(b["axes"]), "reasons": b["reasons"].most_common(3)})
    return {"codes": out}


def comm_alarms(c, t0: float, t1: float) -> dict:
    rows = [dict(r) for r in c.execute("SELECT akey, COUNT(*) AS n FROM alarms WHERE ts>=? AND ts<? AND "
                                       "(akey IN ('hb','conn') OR akey LIKE 'inv:%') GROUP BY akey", (t0, t1))]
    restarts = c.execute("SELECT COUNT(*) FROM sys_events WHERE ts>=? AND ts<? AND kind IN ('start','restart')",
                         (t0, t1)).fetchone()[0]
    return {"by_key": {r["akey"]: r["n"] for r in rows}, "starts": restarts}


def collect(cfg: dict, days: float = 7, until: float | None = None, sensors_status: dict | None = None) -> dict:
    until = until or time.time()
    t0 = until - days * 86400
    c = sqlite3.connect(f"file:{os.path.abspath(cfg['storage']['db'])}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        axes = enabled_axes(cfg)
        res = {"t0": t0, "t1": until, "days": days, "robot": cfg["robot"]["name"], "axes": axes,
               "sampling": sampling(c, t0, until, float(cfg["robot"]["poll_hz"]), float(cfg["realtime"]["stale_s"])),
               "diag": diag(c, t0, until, axes), "motion": motion(c, t0, until, int(cfg["motion"]["min_cycles_day"])),
               "comm": comm_alarms(c, t0, until), "sensors": sensors_status}
    finally:
        c.close()
    res["checks"] = verdicts(cfg, res)
    return res


def verdicts(cfg: dict, r: dict) -> list[tuple[str, str, str]]:
    """（狀態, 項目, 說明）。"""
    out = []
    s = r["sampling"]
    hz = float(cfg["robot"]["poll_hz"])
    if not s["n"]:
        out.append((WARN, _("取樣"), _("這段期間沒有原始取樣（原始樣本只保留 {d} 天）", d=cfg["storage"]["raw_retention_days"])))
    else:
        ok_rate = s["rate_hz"] and abs(s["rate_hz"] - hz) / hz <= 0.1
        out.append((OK if ok_rate else WARN, _("取樣頻率"),
                    _("實際 {a:.2f} Hz（設定 {b:g} Hz）", a=s["rate_hz"] or 0, b=hz) +
                    ("" if ok_rate else _("：差超過 10%，檢查網路與控制器負載"))))
        jit_ok = s["p99_ms"] is not None and s["p99_ms"] <= 3000 / hz
        out.append((OK if jit_ok else WARN, _("取樣抖動"),
                    _("間隔中位數 {a} ms、P95 {b} ms、P99 {c} ms", a=s["p50_ms"], b=s["p95_ms"], c=s["p99_ms"]) +
                    ("" if jit_ok else _("：P99 超過 3 個取樣週期，診斷段可能切不準"))))
        hrs = s["gap_total_s"] / 3600
        gap_ok = len(s["gaps"]) <= 5 * max(1, r["days"] / 7) and hrs < 1
        out.append((OK if gap_ok else WARN, _("資料中斷"),
                    _("{n} 次，共 {h:.1f} 小時，最長 {m:.0f} 秒", n=len(s["gaps"]), h=hrs, m=s["gap_max_s"])))
    d = r["diag"]
    if not d["total"]:
        out.append((WARN, _("診斷段"), _("這段期間沒有執行診斷程式 DTM_DIAG（只用生產循環監測也可以）")))
    else:
        ok = d["rate"] >= 0.8
        out.append((OK if ok else WARN, _("診斷段切割"), _("成功 {v}／{t}（{p:.0%}）", v=d["valid"], t=d["total"], p=d["rate"]) +
                    ("" if ok else _("：低於 80%，看下面被丟棄的原因"))))
    m = r["motion"]["codes"]
    if not m:
        out.append((WARN, _("動作代碼"), _("沒有收到生產動作代碼（R[110] = 9000–9899）；只靠診斷程式也可以，但不停機監測要加代碼")))
    else:
        good = [x for x in m if x["good_days"] >= min(5, x["days"])]
        out.append((OK if good else WARN, _("動作代碼涵蓋"),
                    _("{n} 個代碼，其中 {g} 個每天循環數夠（≥ {m} 次）", n=len(m), g=len(good), m=cfg["motion"]["min_cycles_day"])))
    cm = r["comm"]["by_key"]
    n_comm = cm.get("hb", 0) + cm.get("conn", 0)
    out.append((OK if n_comm <= 2 else WARN, _("心跳與連線"),
                _("心跳停止 {a} 次、連線中斷 {b} 次、程式啟動 {s} 次", a=cm.get("hb", 0), b=cm.get("conn", 0),
                  s=r["comm"]["starts"])))
    ss = r.get("sensors") or {}
    if ss.get("enabled"):
        slow = ss.get("slow") or []
        out.append((OK if not ss.get("error") else WARN, _("感測器"),
                    _("{n} 個通道，慢速（只用每日趨勢）{s} 個", n=len(ss.get("update_s") or {}), s=len(slow)) +
                    (_("：{e}", e=ss["error"]) if ss.get("error") else "")))
    return out


def render(cfg: dict, r: dict, now: float | None = None) -> str:
    e = html.escape
    lang = get_lang()
    now = now or time.time()
    f = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "—"  # noqa: E731
    num = lambda v, d=1: "—" if v is None else f"{v:.{d}f}"  # noqa: E731
    rows = "".join(f"<tr><td class='{st}'>{'✔' if st == OK else '⚠'}</td><td>{e(k)}</td><td>{e(v)}</td></tr>"
                   for st, k, v in r["checks"])
    s = r["sampling"]
    gaps = "".join(f"<tr><td>{e(f(t))}</td><td>{d:.0f}</td></tr>" for t, d in s["gaps"][:10]) or \
        f"<tr><td colspan=2 class='muted'>{e(_('沒有'))}</td></tr>"
    d = r["diag"]
    def pct(v, d=0):
        return "—" if v is None else f"{v * 100:.{d}f}%"

    axr = "".join("<tr><td>{}</td><td>{}／{}</td><td>{}</td><td>{}</td></tr>".format(
        e(axis_name(cfg, ax)), v["valid"], v["runs"], num(v["ripple_med"], 3), pct(v["f_cv"], 1))
        for ax, v in d["axes"].items())
    rsn = "".join(f"<tr><td>{e(k)}</td><td>{n}</td></tr>" for k, n in d["reasons"]) or \
        f"<tr><td colspan=2 class='muted'>{e(_('沒有'))}</td></tr>"
    mot = "".join("<tr><td>{}</td><td>{}</td><td>{}</td><td>{}／{}</td><td>{}</td><td>{}</td></tr>".format(
        c["code"], c["cycles"], num(c["per_day"], 0), c["good_days"], c["days"], pct(c["valid_rate"]),
        e(_("、").join(f"{k}（{n}）" for k, n in c["reasons"]))) for c in r["motion"]["codes"]) or \
        f"<tr><td colspan=6 class='muted'>{e(_('沒有動作代碼'))}</td></tr>"
    ss = r.get("sensors") or {}
    slow = set(ss.get("slow") or [])
    sens = "".join("<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
        e(k), "—" if v is None else f"{v:.1f}", e(_("慢速")) if k in slow else "")
        for k, v in sorted((ss.get("update_s") or {}).items())) or \
        f"<tr><td colspan=3 class='muted'>{e(_('沒有接感測器'))}</td></tr>"
    title = _("導入檢查報告")
    return f"""<!doctype html><html lang="{HTML_LANG.get(lang, 'zh-Hant')}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(title)} {e(r['robot'])}</title>
<style>body{{font-family:{FONT_STACK.get(lang, FONT_STACK['zh-TW'])};color:#1F2A33;max-width:1000px;margin:24px auto;padding:0 16px}}
h1{{font-size:24px}} h2{{font-size:18px;border-bottom:2px solid #A35F0E;padding-bottom:4px;margin-top:28px}}
table{{border-collapse:collapse;width:100%;font-size:13px}} td,th{{border-bottom:1px solid #E3E6E8;padding:6px 8px;text-align:left}}
th{{background:#F2F4F5}} .muted{{color:#4A5866;font-size:13px}} td.ok{{color:#2F6F73;font-weight:700}} td.warn{{color:#B5462F;font-weight:700}}</style></head><body>
<h1>{e(title)}&ensp;{e(r['robot'])}</h1>
<p class="muted">{e(_("期間：{a} ～ {b}（{d:g} 天）　產生時間：{n}", a=f(r['t0']), b=f(r['t1']), d=r['days'], n=f(now)))}<br>
{e(_("這份報告確認資料可不可信（取樣、中斷、切段、雜訊、涵蓋率），不是健康判斷。⚠ 的項目請先處理，再開始依判斷安排保養。"))}</p>
<table><tr><th style="width:2em"></th><th style="width:9em">{e(_('項目'))}</th><th>{e(_('結果'))}</th></tr>{rows}</table>
<h2>{e(_('資料中斷（最長的 10 次）'))}</h2><table><tr><th>{e(_('開始'))}</th><th>{e(_('秒'))}</th></tr>{gaps}</table>
<h2>{e(_('診斷段與各軸雜訊'))}</h2>
<table><tr><th>{e(_('軸'))}</th><th>{e(_('有效／全部'))}</th><th>{e(_('漣波中位數'))}</th><th>{e(_('F 變異係數'))}</th></tr>{axr}</table>
<p class="muted">{e(_('F 變異係數＝同一軸各次診斷 F 的標準差÷平均；建議 5% 以下，太大代表姿勢、速度或負載不一致，基準會不穩。'))}</p>
<h4>{e(_('被丟棄的原因'))}</h4><table><tr><th>{e(_('原因'))}</th><th>{e(_('次數'))}</th></tr>{rsn}</table>
<h2>{e(_('動作代碼涵蓋率'))}</h2>
<table><tr><th>{e(_('代碼'))}</th><th>{e(_('循環數'))}</th><th>{e(_('每天'))}</th><th>{e(_('夠用的天數'))}</th><th>{e(_('有效比例'))}</th><th>{e(_('主要排除原因'))}</th></tr>{mot}</table>
<h2>{e(_('感測器更新間隔'))}</h2><table><tr><th>{e(_('通道'))}</th><th>{e(_('間隔（秒）'))}</th><th></th></tr>{sens}</table>
</body></html>"""


def build(cfg: dict, days: float = 7, until: float | None = None, sensors_status: dict | None = None) -> str:
    return render(cfg, collect(cfg, days, until, sensors_status))


def reports_dir(cfg: dict) -> str:
    d = R.resolve(cfg, os.path.join("data", "reports"))
    if cfg.get("_edge"):
        d = os.path.join(d, R.slug(R.robot_id(cfg)))
    os.makedirs(d, exist_ok=True)
    return d


def auto_due(cfg: dict, store, now: float | None = None, days: float = 7) -> bool:
    """第一次跑滿 7 天、而且還沒產生過自動報告。"""
    now = now or time.time()
    marker = os.path.join(reports_dir(cfg), "commissioning_auto.done")
    if os.path.exists(marker):
        return False
    with store.conn() as c:
        first = c.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
        started = c.execute("SELECT MIN(ts) FROM sys_events WHERE kind='start'").fetchone()[0]
    t = min(x for x in (first, started, now) if x)
    return now - t >= days * 86400


def auto_generate(cfg: dict, store, sensors_status: dict | None = None, now: float | None = None) -> str:
    now = now or time.time()
    path = os.path.join(reports_dir(cfg), f"commissioning_{time.strftime('%Y%m%d', time.localtime(now))}.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build(cfg, 7, now, sensors_status))
    with open(os.path.join(reports_dir(cfg), "commissioning_auto.done"), "w", encoding="utf-8") as fh:
        fh.write(path)
    store.add_sys_event("commissioning", 0, _("已產生導入檢查報告：{p}", p=os.path.basename(path)))
    return path

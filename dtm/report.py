"""離線分析報告（單一 HTML 檔，不需網路）與 CSV 匯出。"""
from __future__ import annotations

import csv
import html
import time

from .analysis import HIGH_METRICS, LEVEL_NAMES, METRIC_LABELS, comp, epochs, load_rows
from .config import EVENT_LABELS, RESET_KINDS, axis_cfg, axis_name, enabled_axes
from .convert import Converter
from .i18n import FONT_STACK, HTML_LANG, _, get_lang, join, render

LV_COLOR = {-1: "#8A9AA6", 0: "#2F6F73", 1: "#C98A12", 2: "#B5462F"}


def _fmt_t(ts: float, f: str = "%Y-%m-%d") -> str:
    return time.strftime(f, time.localtime(ts))


def svg_chart(pts: list[tuple[float, float, int]], hlines: list[tuple], marks: list[tuple[float, str]],
              unit: str, w: int = 760, h: int = 230) -> str:
    """pts: (ts, 值, 等級)；hlines: (值, 標籤, 顏色, x 起, x 終)。"""
    pts = [p for p in pts if p[1] is not None]
    if not pts:
        return "<p class='muted'>" + html.escape(_("尚無資料")) + "</p>"
    L, R, T, B = 56, 16, 24, 30
    xs = [p[0] for p in pts] + [m[0] for m in marks]
    ys = [p[1] for p in pts] + [hl[0] for hl in hlines if hl[0] is not None]
    x0, x1 = min(xs), max(xs) or 1
    y0, y1 = min(ys), max(ys)
    pad = (y1 - y0) * 0.08 or abs(y1) * 0.1 or 1
    y0, y1 = y0 - pad, y1 + pad
    if x1 == x0:
        x1 = x0 + 1

    def X(t):
        return L + (t - x0) / (x1 - x0) * (w - L - R)

    def Y(v):
        return T + (y1 - v) / (y1 - y0) * (h - T - B)

    o = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" style="max-width:{w}px">']
    for i in range(5):
        v = y0 + (y1 - y0) * i / 4
        o.append(f'<line x1="{L}" x2="{w-R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="#E3E6E8" stroke-width="1"/>')
        o.append(f'<text x="{L-6}" y="{Y(v)+4:.1f}" text-anchor="end" font-size="11" fill="#4A5866">{v:.3g}</text>')
    for i in range(5):
        t = x0 + (x1 - x0) * i / 4
        o.append(f'<text x="{X(t):.1f}" y="{h-10}" text-anchor="middle" font-size="11" fill="#4A5866">{_fmt_t(t, "%m/%d")}</text>')
    for t, lab in marks:
        xm = X(t)
        anchor, dx = ("end", -4) if xm > w - R - 80 else ("start", 4)
        o.append(f'<line x1="{xm:.1f}" x2="{xm:.1f}" y1="{T}" y2="{h-B}" stroke="#4A5866" stroke-dasharray="3 3"/>')
        o.append(f'<text x="{xm+dx:.1f}" y="{h-B-5}" text-anchor="{anchor}" font-size="11" fill="#4A5866">{html.escape(lab)}</text>')
    for v, lab, col, xa, xb in hlines:
        if v is None:
            continue
        xa, xb = X(max(xa, x0)), X(min(xb, x1))
        o.append(f'<line x1="{xa:.1f}" x2="{xb:.1f}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="{col}" stroke-dasharray="6 4"/>')
        o.append(f'<text x="{xa+4:.1f}" y="{Y(v)-4:.1f}" font-size="11" fill="{col}">{html.escape(lab)}</text>')
    path = " ".join(f"{'M' if i == 0 else 'L'}{X(t):.1f},{Y(v):.1f}" for i, (t, v, _) in enumerate(pts))
    o.append(f'<path d="{path}" fill="none" stroke="#1F2A33" stroke-width="1.2" opacity="0.55"/>')
    for t, v, lv in pts:
        o.append(f'<circle cx="{X(t):.1f}" cy="{Y(v):.1f}" r="2.6" fill="{LV_COLOR.get(lv, "#1F2A33")}"/>')
    o.append(f'<text x="8" y="12" font-size="11" fill="#4A5866">{html.escape(unit)}</text></svg>')
    return "".join(o)


def decision_html(svc) -> str:
    from .web import current_decision
    e = html.escape
    rows = []
    for ax in enabled_axes(svc.cfg):
        d = current_decision(svc, ax)
        g = d["grease"]
        due = _fmt_t(g["due_ts"]) if g.get("due_ts") else "—"
        fam = join([f"{k} {v:.2f}" for k, v in d["families"].items() if v is not None and v >= 0.5], "、") or _("無")
        cell = lambda x: f"<td style='color:{LV_COLOR[x['level']]};font-weight:700'>{e(x['action'])}</td>"  # noqa: E731
        rows.append(f"<tr><td>{e(d['name'])}</td><td>{e(d['primary'])}</td>{cell(g)}<td>{due}</td>"
                    f"{cell(d['drive'])}{cell(d['motor'])}<td>{e(fam)}</td></tr>")
        for x in d["next"]:
            rows.append(f"<tr><td></td><td colspan=6 class='muted'>→ {e(x)}</td></tr>")
    th = "".join(f"<th>{e(_(m))}</th>" for m in ("軸", "研判", "潤滑脂", "換脂到期", "減速機／齒輪箱", "伺服馬達",
                                                 "達注意以上的證據（0.5＝注意、1＝警戒）"))
    return (f"<h2>{e(_('維護決策（扭矩 × 振動 × 鐵粉 × 壽命）'))}</h2><table><tr>{th}</tr>" + "".join(rows) + "</table>"
            f"<p class='muted'>{e(_('換減速機、齒輪箱或馬達需要兩類獨立證據（扭矩、鐵粉、振動）同時異常；單一證據最多到「觀察／準備」。'))}</p>")


def build_report(svc, days: float = 180) -> str:
    from .web import current_health
    e = html.escape
    cfg = svc.cfg
    conv: Converter = svc.conv
    since = time.time() - days * 86400
    hc = cfg["health"]
    parts = []
    summary_rows = []
    for ax in enabled_axes(cfg):
        a = axis_cfg(cfg, ax)
        hres = current_health(svc, ax)
        unit = conv.unit(ax)
        rows = load_rows(svc.store, cfg, ax, since=since)
        events = [ev for ev in svc.store.events(ax) if ev["ts"] >= since]
        marks = [(ev["ts"], EVENT_LABELS.get(ev["kind"], ev["kind"])) for ev in events]
        reset = svc.store.last_reset(ax)
        resets = [ev["ts"] for ev in svc.store.events(ax) if ev["kind"] in RESET_KINDS]
        eps = epochs(rows, resets, cfg)
        charts = []
        for m in ["F", "Fc", "Fv", "ripple", "G"]:
            pts, hl = [], []
            for ep in eps:
                base = ep["base"]
                for r in ep["rows"]:
                    v = comp(r, m, base) if m in HIGH_METRICS else r[m]
                    pts.append((r["ts"], conv.feature(ax, m, v), ep["levels"].get(r["id"], -1)))
                if base and m in HIGH_METRICS:
                    bm = base["stats"][m][0]
                    for lab, k, col in ((_("基準"), 1.0, "#2F6F73"), (_("注意"), float(hc["caution_ratio"]), "#C98A12"),
                                        (_("警戒"), float(hc["alert_ratio"]), "#B5462F")):
                        hl.append((conv.feature(ax, m, bm * k), lab, col, ep["start"], ep["end"]))
            charts.append(f"<h4>{e(METRIC_LABELS[m])}</h4>"
                          + svg_chart(pts, hl, marks, unit + (_("/%速度") if m == "Fv" else "")))
        from .web import trend_vib_iron
        colmap = {"base": "#2F6F73", "caution": "#C98A12", "alert": "#B5462F"}
        for met in ("iron_wt", "iron_W", "vib_radial_env", "vib_radial_mesh", "vib_axial_rms", "vib_radial_motor1x"):
            t = trend_vib_iron(svc, ax, met, days)
            if not t["points"]:
                continue
            pts = [(p["ts"], p["v"], p["level"]) for p in t["points"]]
            hl = [(ln["v"], ln["label"], colmap[ln["kind"]], ln.get("x0", 0), ln.get("x1", 1e12)) for ln in t["lines"]]
            charts.append(f"<h4>{e(t['label'])}</h4>" + svg_chart(pts, hl, marks, t["unit"]))
        met = hres.get("metrics", {})

        def ratio(m):
            r = (met.get(m) or {}).get("ratio")
            return f"{r:.2f}" if r else "—"
        d2a = hres.get("days_to_alert")
        spm = hres.get("slope_pct_month")
        slope_txt = "" if spm is None else f"{spm:+.1f}%"
        d2a_txt = "—" if d2a is None else (_("已達") if d2a == 0 else ("> 730" if d2a > 730 else f"{d2a:.0f}"))
        lv_col = LV_COLOR[hres["level"]]
        lv_name = LEVEL_NAMES.get(hres["level"])
        summary_rows.append(
            f"<tr><td>{e(a['name'])}</td><td style='color:{lv_col};font-weight:700'>{e(lv_name)}</td>"
            f"<td>{ratio('F')}</td><td>{ratio('Fc')}</td><td>{ratio('Fv')}</td><td>{ratio('ripple')}</td>"
            f"<td>{slope_txt}</td><td>{d2a_txt}</td>"
            f"<td>{len(rows)}</td><td>{e(unit)} ({e(_(conv.ax[ax]['source']))})</td></tr>")
        reasons = join(hres.get("reasons", [])) or _("無")
        base_txt = _("基準：最近一次重設（{reset}）後的前 {n} 次診斷", reset=_fmt_t(reset) if reset else _("無重設事件"),
                     n=hc["baseline_runs"])
        tm = hres.get("temp_mode")
        temp_txt = _("溫度補償：{state}", state={
            "learned": _("啟用（係數由基準期資料學得）"),
            "prior": _("啟用（黏性摩擦每 °C {k:.1f}%）", k=float(hc["temp_coef_fv"]) * 100)}.get(tm, _("未啟用（沒有溫度訊號）")))
        if hres.get("common_mode"):
            temp_txt += _("；") + _("已扣除各軸共同變化（氣溫、冷機、節拍）")
        parts.append(
            f"<section><h2>{e(a['name'])}&ensp;<span style='color:{lv_col}'>{e(lv_name)}</span></h2>"
            f"<p>{e(_('判定依據：{reasons}', reasons=reasons))}</p>"
            f"<p>{e(_('失效模式提示：{hint}', hint=hres.get('hint') or _('無')))}</p>"
            f"<p class='muted'>{e(base_txt)}{e(_('；'))}{e(temp_txt)}</p>"
            + "".join(charts) + "</section>")
    alarms = [x for x in svc.store.alarms(500) if x["ts"] >= since]
    alarm_rows = "".join(
        f"<tr><td>{_fmt_t(x['ts'], '%Y-%m-%d %H:%M')}</td><td>{'J'+str(x['axis']) if x['axis'] else '—'}</td>"
        f"<td>{e(_(x['kind']) if x['kind'] else '')}</td><td style='color:{LV_COLOR.get(x['level'])}'>{e(LEVEL_NAMES.get(x['level']) or '')}</td>"
        f"<td>{e(render(x.get('tmpl'), x['message'] or ''))}</td>"
        f"<td>{_fmt_t(x['cleared_ts'], '%m-%d %H:%M') if x['cleared_ts'] else e(_('作用中'))}</td></tr>" for x in alarms)
    th1 = "".join(f"<th>{e(_(m))}</th>" for m in ("軸", "狀態", "F 比值", "Fc 比值", "Fv 比值", "漣波比值", "F 月變化",
                                                  "預估達警戒（天）", "診斷次數", "單位"))
    th2 = "".join(f"<th>{e(_(m))}</th>" for m in ("時間", "軸", "類別", "等級", "內容", "解除"))
    lang = get_lang()
    title = _("減速機扭力分析報告")
    return f"""<!doctype html><html lang="{HTML_LANG.get(lang, 'zh-Hant')}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>
<style>body{{font-family:{FONT_STACK.get(lang, FONT_STACK['zh-TW'])};color:#1F2A33;max-width:1000px;margin:24px auto;padding:0 16px}}
h1{{font-size:24px}} h2{{font-size:20px;border-bottom:2px solid #A35F0E;padding-bottom:4px;margin-top:32px}} h4{{margin:18px 0 4px;font-size:14px}}
table{{border-collapse:collapse;width:100%;font-size:13px}} td,th{{border-bottom:1px solid #E3E6E8;padding:6px 8px;text-align:left}}
th{{background:#F2F4F5}} .muted{{color:#4A5866;font-size:13px}}</style></head><body>
<h1>{e(title)}&ensp;{e(cfg['robot']['name'])}</h1>
<p class="muted">{e(_("期間：最近 {days:.0f} 天　產生時間：{now}", days=days, now=_fmt_t(time.time(), '%Y-%m-%d %H:%M')))}<br>
{e(_("判定：比值 ≥ {c} 注意、≥ {a} 警戒（相對基準），需連續 {n} 次才升降級", c=hc['caution_ratio'], a=hc['alert_ratio'], n=hc['confirm_runs']))}</p>
<table><tr>{th1}</tr>
{''.join(summary_rows)}</table>
{decision_html(svc)}
{''.join(parts)}
<h2>{e(_('警報紀錄'))}</h2><table><tr>{th2}</tr>{alarm_rows or '<tr><td colspan=6>' + e(_('無')) + '</td></tr>'}</table>
<p class="muted">{e(_('點的顏色：灰＝建立基準中、綠＝正常、黃＝注意、紅＝警戒；虛線直線為保養事件。趨勢外推為線性；偵測到劣化加速時改用保守估計（取較短的天數）。'))}</p>
</body></html>"""


def export_features(svc, path: str, days: float = 3650):
    since = time.time() - days * 86400
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([_("時間"), _("軸"), "F", "Fc", "Fv", "G", _("漣波"), _("峰值"), _("溫度"), _("單位")])
        for ax in enabled_axes(svc.cfg):
            for r in svc.store.features(ax, since=since):
                c = svc.conv
                w.writerow([_fmt_t(r["ts"], "%Y-%m-%d %H:%M:%S"), axis_name(svc.cfg, ax),
                            *[None if c.feature(ax, m, r[m]) is None else round(c.feature(ax, m, r[m]), 4)
                              for m in ["F", "Fc", "Fv", "G", "ripple", "peak"]],
                            r["temp"], c.unit(ax)])

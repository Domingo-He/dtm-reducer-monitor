"""Pro 報告（HTML，可直接列印）：診斷報告（P5、P6、P8、P9、P11）與改善驗證報告（P10，一頁結論＋附錄、雙方簽核）。"""
from __future__ import annotations

import html
import time

from ..i18n import FONT_STACK, HTML_LANG, _, get_lang
from . import compare as CMP
from . import impacts, suggest, views
from .store import ROLE_LABELS, Session

e = html.escape


def _t(ts, fmt="%Y-%m-%d %H:%M"):
    return time.strftime(fmt, time.localtime(ts)) if ts else "—"


def _head(title: str) -> str:
    lang = get_lang()
    return f"""<!doctype html><html lang="{HTML_LANG.get(lang, 'zh-Hant')}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>
<style>body{{font-family:{FONT_STACK.get(lang, FONT_STACK['zh-TW'])};color:#1F2A33;max-width:1000px;margin:24px auto;padding:0 16px;line-height:1.55}}
h1{{font-size:22px;margin-bottom:4px}} h2{{font-size:17px;border-bottom:2px solid #A35F0E;padding-bottom:4px;margin-top:26px}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}} td,th{{border-bottom:1px solid #E3E6E8;padding:5px 7px;text-align:left;vertical-align:top}}
th{{background:#F2F4F5}} .muted{{color:#4A5866;font-size:12.5px}} .num{{text-align:right;font-variant-numeric:tabular-nums}}
.good{{color:#1E7A46;font-weight:600}} .bad{{color:#B3261E;font-weight:600}} code{{font-family:Consolas,monospace;font-size:12.5px}}
.box{{border:1px solid #C9CED3;border-radius:6px;padding:10px 12px;margin:8px 0}} .sign td{{height:54px;border:1px solid #C9CED3}}
.bar{{display:inline-block;height:9px;background:#A35F0E;border-radius:2px;vertical-align:middle}}
@media print{{body{{margin:0;max-width:none}} h2{{break-after:avoid}} .pb{{break-before:page}}}}</style></head><body>"""


def _pct(x, d=0):
    return "—" if x is None else f"{x * 100:.{d}f}%"


def _yrs(x):
    if x is None:
        return "—"
    return _("超過 100 年") if x > 100 else _("{y:.1f} 年", y=x)


def _kv(k: str, v: str) -> str:
    """「標籤：值」（冒號依語言）；值已經過跳脫。"""
    return f"{e(k)}{e(_('：'))}{v}"


def _par(x: str) -> str:
    return e(_("（{n}）", n=x))


SP = " · "


def session_report(cfg: dict, sess: Session) -> str:
    S = views.summary(cfg, sess)
    m = S["meta"]
    out = [_head(_("程式層級診斷報告"))]
    out.append(f"<h1>{e(_('程式層級診斷報告'))} · {e(m.get('robot', ''))}</h1>")
    out.append(f"<p class='muted'>{_kv(_('程式'), '<code>' + e(m['program']['name']) + '</code>')} "
               f"{_par(_('版本') + ' ' + m['program']['version'])}{SP}"
               f"{_kv(_('期間'), _t(m.get('t0')) + ' ～ ' + _t(m.get('t1')))}{SP}"
               f"{_kv(_('類別'), e(ROLE_LABELS.get(m.get('role'), m.get('role', ''))))}<br>"
               f"{e(_('有效循環'))} {S['ncyc']}{SP}{e(_('循環時間中位數'))} {S['cycle']['med']:.2f} s{SP}"
               f"{e(_('每日循環數'))} {S['cycles_per_day']:.0f} "
               f"{_par(_('自行設定') if S['cpd_src'] == 'set' else _('依循環時間與每天運轉時數估算'))}{SP}"
               f"{e(_('產生時間'))} {_t(time.time())}</p>")
    for w in S["warnings"]:
        out.append(f"<p class='bad'>{e(w)}</p>")
    al = S["align"]
    if al.get("acc_est") is not None:
        out.append(f"<p class='muted'>{e(_('行號對位正確率（演示資料有真值）：位置比對 {a}，直接用回報行號 {b}', a=_pct(al['acc_est'], 1), b=_pct(al['acc_raw'], 1)))}</p>")
    # 各軸
    out.append(f"<h2>{e(_('各軸損耗與預估壽命'))}</h2><table><tr><th>{e(_('軸'))}</th><th class=num>{e(_('每循環損耗'))}</th>"
               f"<th class=num>{e(_('峰值'))}</th><th class=num>{e(_('預估壽命'))}</th><th class=num>{e(_('停止事件占比'))}</th></tr>")
    for a in S["axes"]:
        pk = _pct(a["peak"]) if a["absolute"] else f"{a['peak']:.3g}"
        out.append(f"<tr><td>{e(a['name'])}</td><td class=num>{a['D_cycle']:.4g}</td><td class=num>{pk}</td>"
                   f"<td class=num>{e(_yrs(a.get('life_years')))}</td><td class=num>{_pct(a.get('events_share'))}</td></tr>")
    out.append("</table>")
    out.append(f"<p class='muted'>{e(_('損耗 D = Σ|Δθ|/360·(|T|/T0)^p（額定扭矩下的等效轉數）；壽命 = 額定總轉數 60·L0·N0 ÷ 每日損耗。只用實測資料，以換件紀錄校正。'))}</p>")
    # 逐行
    rows = sorted([r for r in S["lines"] if r.get("n")], key=lambda r: -r.get("score", 0))[:12]
    names = [a["name"] for a in S["axes"]]
    out.append(f"<h2>{e(_('最傷減速機的動作行'))}</h2><table><tr><th>#</th><th>{e(_('行'))}</th><th>{e(_('指令'))}</th>"
               f"<th class=num>{e(_('占比'))}</th><th>{e(_('主要軸'))}</th><th class=num>{e(_('動作'))}</th><th class=num>{e(_('停留'))}</th></tr>")
    for r in rows:
        sh = r.get("share") or []
        top = sorted(range(len(sh)), key=lambda i: -sh[i])[:2]
        main = _("、").join(f"{names[i]} {sh[i] * 100:.0f}%" for i in top if sh[i] >= 0.05)
        out.append(f"<tr><td>{r.get('rank')}</td><td>{r['line']}</td><td><code>{e(r['text'])}</code></td>"
                   f"<td class=num><span class=bar style='width:{max(2, int(r['score'] * 120))}px'></span> {_pct(r['score'])}</td>"
                   f"<td>{e(main)}</td><td class=num>{r.get('m', 0):.2f}s</td><td class=num>{r.get('w', 0):.2f}s</td></tr>")
    out.append("</table>")
    # 建議
    sg = suggest.build(cfg, sess)
    out.append(f"<h2>{e(_('修改建議（規則版）'))}</h2><p class='muted'>{e(sg['safety'])}</p>")
    for it in sg["items"][:10]:
        prop = "".join(f"<div><code>{e(p['before'])}</code> → <code>{e(p['after'])}</code></div>" for p in it["proposal"])
        out.append(f"<div class=box><b>{it['rank']}. {e(it['title'])}</b><div>{e(it['why'])}</div>{prop}"
                   f"<div class=muted>{e(it['effect'])}{(SP + e(it['caution'])) if it['caution'] else ''}</div></div>")
    # 角度
    out.append(f"<h2>{e(_('角度集中'))}</h2><table><tr><th>{e(_('軸'))}</th><th class=num>θ80</th><th>{e(_('範圍'))}</th>"
               f"<th class=num>Gini</th><th class=num>{e(_('前 5 區間'))}</th><th class=num>{e(_('換向／天'))}</th><th class=num>{e(_('小幅擺動／天'))}</th></tr>")
    for a in S["axes"]:
        A = views.angle_map(cfg, sess, a["ax"])
        if not A or not A["bins"]:
            continue
        c = A["conc"]
        out.append(f"<tr><td>{e(a['name'])}</td><td class=num>{c['theta80']:.0f}{e(A['unit'] == 'deg' and '°' or ' mm')}</td>"
                   f"<td>{c.get('lo', 0):.0f}～{c.get('hi', 0):.0f}</td><td class=num>{c['gini']:.2f}</td><td class=num>{_pct(c['top5'])}</td>"
                   f"<td class=num>{A['rev_day']:.0f}</td><td class=num>{A['fret_day']:.0f}</td></tr>")
    out.append("</table>")
    im = impacts.summary(sess)
    if im["by_kind"]:
        kinds = {"impact": _("加速規衝擊"), "estop": _("急停"), "fence": _("安全柵開啟"), "collision": _("碰撞偵測"),
                 "controlled": _("控制停止")}
        per_day = im["per_day"]
        out.append(f"<h2>{e(_('衝擊與停止事件'))}</h2><p>" + SP.join(
            f"{e(kinds.get(k, k))} {v} {_par(f'{per_day[k]:.1f}/' + _('天'))}" for k, v in im["by_kind"].items()) + "</p>")
    out.append(f"<p class='muted'>{e(_('本報告只分析實際量到的資料；建議需經人確認、模擬與試跑後才能上線。'))}</p></body></html>")
    return "\n".join(out)


def compare_report(cfg: dict, res: dict, sessions: dict[str, list[Session]]) -> str:
    b = sessions["before"][0]
    a = sessions["after"][0]
    out = [_head(_("改善驗證報告"))]
    out.append(f"<h1>{e(_('改善驗證報告'))} · {e(b.meta.get('robot', ''))}</h1>")
    ctrl = sessions.get("ctrl_before") or []
    unit = {'day': _('每日中位數'), 'hour': _('每小時中位數'), '10min': _('每 10 分鐘中位數')}[res['unit']]
    out.append(f"<p class='muted'>{_kv(_('改前'), _t(b.meta.get('t0'), '%Y-%m-%d') + ' ～ ' + _t(b.meta.get('t1'), '%Y-%m-%d'))}{SP}"
               f"{_kv(_('改後'), _t(a.meta.get('t0'), '%Y-%m-%d') + ' ～ ' + _t(a.meta.get('t1'), '%Y-%m-%d'))}{SP}"
               f"{_kv(_('對照組'), e(ctrl[0].meta.get('robot', '') if ctrl else _('無')))}{SP}"
               f"{_kv(_('統計單位'), e(unit))}</p>")
    for n in res["notes"]:
        out.append(f"<p class='bad'>{e(n)}</p>")
    # 改了什麼
    out.append(f"<h2>{e(_('改了什麼'))}</h2>")
    if a.text("change"):
        out.append(f"<p>{e(a.text('change'))}</p>")
    d = res.get("diff") or {}
    if d.get("lines") or d.get("positions"):
        out.append("<table>")
        for x in d.get("lines", []):
            out.append(f"<tr><td><code>{'<br>'.join(e(s) for s in x['before']) or '—'}</code></td><td>→</td>"
                       f"<td><code>{'<br>'.join(e(s) for s in x['after']) or '—'}</code></td></tr>")
        for x in d.get("positions", []):
            ch = _("，").join(f"{k} {v[0]} → {v[1]}" for k, v in x["diff"].items())
            out.append(f"<tr><td><code>{e(x['point'])} {e(x['name'])}</code></td><td>→</td><td>{e(ch)}</td></tr>")
        out.append("</table>")
    # 一頁結論
    rows = {r["key"]: r for r in res["rows"]}
    out.append(f"<h2>{e(_('結論'))}</h2><table><tr><th>{e(_('指標'))}</th><th class=num>{e(_('改前'))}</th><th class=num>{e(_('改後'))}</th>"
               f"<th class=num>{e(_('效果（扣除對照組）'))}</th><th class=num>{e(_('95% 信賴區間'))}</th><th class=num>p</th></tr>")
    for r in res["rows"]:
        cls = "good" if r["significant"] and r["effect"] < 0 else "bad" if r["significant"] and r["effect"] > 0 and r["key"] != "dur" else ""
        if r["key"] in ("dur", "move"):
            cls = "bad" if r["significant"] and r["effect"] > 0.005 else ""
        fmt = (lambda v: f"{v:.2f}") if r["key"] in ("dur", "move", "impact") else (lambda v: f"{v:.4g}")
        p = "—" if r["p"] is None else f"{r['p']:.3f}"
        out.append(f"<tr><td>{e(r['label'])}</td><td class=num>{fmt(r['before'])}</td><td class=num>{fmt(r['after'])}</td>"
                   f"<td class='num {cls}'>{r['effect'] * 100:+.1f}%</td><td class=num>{r['lo'] * 100:+.1f}% ～ {r['hi'] * 100:+.1f}%</td>"
                   f"<td class=num>{p}</td></tr>")
    out.append("</table>")
    if res["life"]:
        out.append(f"<table><tr><th>{e(_('軸'))}</th><th class=num>{e(_('預估壽命（改前）'))}</th><th class=num>{e(_('預估壽命（改後）'))}</th></tr>")
        for x in res["life"]:
            if rows.get(f"dmg{x['ax']}"):
                out.append(f"<tr><td>{e(x['name'])}</td><td class=num>{e(_yrs(x['before']))}</td><td class=num>{e(_yrs(x['after']))}</td></tr>")
        out.append("</table>")
    if res.get("saving_per_year") is not None:
        out.append(f"<p><b>{e(_('年節省估計'))}</b>：{res['avoided_per_year']:.2f} {e(_('次換件／年'))} × {res['per_event']:,.0f} = "
                   f"<b>{res['saving_per_year']:,.0f}</b>（{e(_('停機時數 × 每小時損失 ＋ 備品 ＋ 人工；故障前產出下降另計'))}）</p>")
    else:
        out.append(f"<p class='muted'>{e(_('成本換算：在設定檔 [pro.cost] 填入停機時數、每小時損失、備品與人工費用後顯示。'))}</p>")
    sp = res.get("stops_per_day") or {}
    if sp.get("before") is not None:
        out.append(f"<p class='muted'>{e(_('停止事件（加權）每天：改前 {b}、改後 {a}', b=sp['before'], a=sp.get('after')))}</p>")
    out.append(f"<p class='muted'>{e(_('效果 = 處理組（改後 ÷ 改前）÷ 對照組（改後 ÷ 改前）− 1；信賴區間為拔靴法 2000 次；p 為處理組改前改後的 Mann–Whitney 檢定。'))}</p>")
    out.append(f"<table class=sign><tr><th>{e(_('保養單位'))}</th><th>{e(_('生產單位'))}</th><th>{e(_('日期'))}</th></tr><tr><td></td><td></td><td></td></tr></table>")
    # 附錄
    out.append(f"<h2 class=pb>{e(_('附錄：診斷期'))}</h2><table><tr><th>{e(_('類別'))}</th><th>{e(_('機器人'))}</th><th>{e(_('期間'))}</th>"
               f"<th class=num>{e(_('有效循環'))}</th><th>{e(_('程式版本'))}</th></tr>")
    for role, lst in sessions.items():
        for s in lst:
            n = sum(v for k, v in s.cycle_counts().items() if k in ("ok", "anomaly"))
            out.append(f"<tr><td>{e(ROLE_LABELS.get(s.meta.get('role'), ''))}</td><td>{e(s.meta.get('robot', ''))}</td>"
                       f"<td>{_t(s.meta.get('t0'))} ～ {_t(s.meta.get('t1'))}</td><td class=num>{n}</td><td><code>{e(s.meta['program']['version'])}</code></td></tr>")
    out.append("</table>")
    out.append(f"<p class='muted'>{e(_('產生時間'))} {_t(time.time())}</p></body></html>")
    return "\n".join(out)


def compare_sessions(cfg: dict, ids: dict[str, list[str]]):
    ss = {k: [Session.open(cfg, i) for i in v] for k, v in ids.items() if v}
    if not ss.get("before") or not ss.get("after"):
        raise ValueError(_("需要改前與改後的診斷期"))
    res = CMP.compare(cfg, ss["before"], ss["after"], ss.get("ctrl_before"), ss.get("ctrl_after"))
    return res, ss

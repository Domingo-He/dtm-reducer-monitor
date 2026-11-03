"""備品與作業包：維護決策發出建議時，列出要準備的備品料號與數量、潤滑脂、工具、人力、工時、安全事項與步驟。

內容來自作業包目錄（TOML，例如 workpacks.r2000ic_165f.toml），由保養單位依機型維護手冊與 Parts list 維護。
系統不自行推測料號；目錄沒有對應的作業包時，畫面與通知會提示「尚未建立作業包」。
"""
from __future__ import annotations

import html
import os
import time
import tomllib

from .analysis import LEVEL_NAMES
from .config import COMP_LABELS, axis_cfg, drive_comp
from .i18n import FONT_STACK, HTML_LANG, _, get_lang, join

KEY_COMP = {"grease": "grease", "motor": "motor"}          # 維護決策的三個建議 → 元件；drive 依軸型式為 reducer／gearbox
_cache: dict[str, tuple[float, dict]] = {}


def catalog_path(cfg: dict) -> str:
    p = (cfg.get("workpack") or {}).get("catalog", "")
    if not p or os.path.isabs(p):
        return p
    base = os.path.dirname(os.path.abspath(cfg["_path"])) if cfg.get("_path") else os.getcwd()
    cand = os.path.join(base, p)
    return cand if os.path.exists(cand) or not os.path.exists(p) else p


def localized(path: str) -> str:
    """有同名的語言版目錄（例如 workpacks.r2000ic_165f.en.toml）就優先使用。"""
    if not path:
        return path
    root, ext = os.path.splitext(path)
    lang = get_lang()
    order = [lang] + (["en"] if lang not in ("zh-TW", "zh-CN", "en") else [])   # 日韓越沒有專屬版本時先用英文版
    for lg in order:
        cand = f"{root}.{lg}{ext}"
        if lg != "zh-TW" and os.path.exists(cand):
            return cand
    return path


def load_catalog(cfg: dict) -> dict:
    """讀取作業包目錄（依目前語言挑選版本）；檔案修改後自動重新載入。"""
    path = localized(catalog_path(cfg))
    empty = {"meta": {}, "toolsets": {}, "packs": [], "path": path, "error": ""}
    if not path:
        return empty
    if not os.path.exists(path):
        return dict(empty, error=_("找不到作業包目錄：{path}", path=path))
    mt = os.path.getmtime(path)
    hit = _cache.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return dict(empty, error=_("作業包目錄格式錯誤：{err}", err=e))
    cat = {"meta": raw.get("meta", {}), "toolsets": raw.get("toolsets", {}), "packs": raw.get("pack", []),
           "path": path, "error": ""}
    _cache[path] = (mt, cat)
    return cat


def comp_for(cfg: dict, ax: int, key: str) -> str:
    return KEY_COMP.get(key) or drive_comp(cfg, ax)


def find_pack(cat: dict, ax: int, comp: str) -> dict | None:
    for p in cat["packs"]:
        if p.get("comp") == comp and (not p.get("axes") or ax in p["axes"]):
            return p
    return None


def expand(cfg: dict, cat: dict, pack: dict, ax: int) -> dict:
    """展開工具組、計算缺料與工時、補上本軸給脂量。"""
    tools = []
    for ts in pack.get("toolsets", []):
        group = cat["toolsets"].get(ts, {})
        tools += [dict(t, group=group.get("name", ts)) for t in group.get("items", [])]
    tools += [dict(t, group=_("本作業專用")) for t in pack.get("tools", [])]
    mass = float(axis_cfg(cfg, ax).get("grease_mass_g") or 0)
    parts = []
    for p in pack.get("parts", []):
        q = float(p.get("qty", 1))
        st = p.get("stock")
        item = dict(p, qty=q, short=None if st is None else max(0.0, q - float(st)))
        if p.get("grease"):
            item["grease_note"] = (_("本軸給脂量 {mass:,.0f} g（設定檔），換脂含沖洗建議準備 {need:,.0f} g 以上",
                                     mass=mass, need=mass * 1.5)
                                   if mass > 0 else _("本軸給脂量未設定：請在設定檔 [axis.N] grease_mass_g 填入手冊數值"))
        parts.append(item)
    steps = pack.get("steps", [])
    hours = sum(float(s.get("hours", 0)) for s in steps)
    man = sum(float(s.get("hours", 0)) * int(s.get("crew", 1)) for s in steps)
    crew = int(pack.get("crew") or sum(int(r.get("n", 1)) for r in pack.get("roles", [])) or 0)
    name, title = axis_cfg(cfg, ax)["name"], pack.get("title", "")
    return {
        "label": title if title.startswith(name) or title.endswith(" " + name) else f"{name} {title}",
        "id": pack.get("id", ""), "title": pack.get("title", ""), "comp": pack.get("comp", ""),
        "comp_label": COMP_LABELS.get(pack.get("comp", ""), pack.get("comp", "")), "axis": ax,
        "axis_name": name, "crew": crew, "roles": pack.get("roles", []),
        "hours_min": pack.get("hours_min"), "hours_max": pack.get("hours_max"), "hours_note": pack.get("hours_note", ""),
        "step_hours": round(hours, 2), "man_hours": round(man, 1), "steps": steps,
        "safety": pack.get("safety", []), "sop_url": pack.get("sop_url", ""), "tools": tools, "parts": parts,
        "shortages": [p for p in parts if p["short"]], "unverified": [p for p in parts if not p.get("verified", False)],
        "meta": cat["meta"], "hours_text": "",
    }


def get(cfg: dict, ax: int, comp: str) -> dict | None:
    cat = load_catalog(cfg)
    p = find_pack(cat, ax, comp)
    if not p:
        return None
    w = expand(cfg, cat, p, ax)
    w["hours_text"] = hours_text(w)
    return w


def hours_text(w: dict) -> str:
    lo, hi = w.get("hours_min"), w.get("hours_max")
    if lo and hi:
        return _("{lo:g}–{hi:g} 小時", lo=lo, hi=hi)
    if w.get("step_hours"):
        return _("約 {h:g} 小時", h=w["step_hours"])
    return _("未估")


def summary(w: dict | None) -> dict | None:
    if not w:
        return None
    return {"id": w["id"], "title": w["title"], "comp": w["comp"], "crew": w["crew"], "hours": hours_text(w),
            "n_parts": len(w["parts"]), "n_short": len(w["shortages"]), "n_unverified": len(w["unverified"])}


def print_url(cfg: dict, ax: int, comp: str) -> str:
    base = (cfg.get("notify") or {}).get("public_url", "").rstrip("/")
    return f"{base}/workorder?axis={ax}&comp={comp}" if base else f"/workorder?axis={ax}&comp={comp}"


def notify_text(cfg: dict, akey: str, level: int) -> str:
    """維護決策警報（dec:軸:grease|drive|motor）附加的備品與作業包摘要；其他警報回傳空字串。"""
    if level < int((cfg.get("workpack") or {}).get("notify_level", 1)):
        return ""
    parts = akey.split("|")[-1].split(":")             # 中央的鍵是「廠區/機器人|dec:軸:項目」
    if len(parts) != 3 or parts[0] != "dec":
        return ""
    try:
        ax = int(parts[1])
    except ValueError:
        return ""
    if not load_catalog(cfg)["path"]:
        return ""
    comp = comp_for(cfg, ax, parts[2])
    w = get(cfg, ax, comp)
    if not w:
        return "\n" + _("— 備品與作業包 —") + "\n" + _("{name} {comp}：尚未建立作業包", name=axis_cfg(cfg, ax)["name"],
                                                       comp=COMP_LABELS.get(comp, comp))
    items = []
    for p in w["parts"][:3]:
        s = f"{p['name']} {p.get('pn', '')} ×{p['qty']:g}{p.get('unit', '')}"
        if p["short"]:
            s += _("（缺 {n:g}）", n=p["short"])
        items.append(s)
    more = _(" 等 {n} 項", n=len(w["parts"])) if len(w["parts"]) > 3 else ""
    lines = ["\n" + _("— 備品與作業包：{label} —", label=w["label"]),
             _("備品：") + join(items) + more,
             _("人力 {crew} 人｜停機 {hours}（參考）", crew=w["crew"], hours=hours_text(w))]
    if w["shortages"]:
        lines.append(_("缺料 {n} 項，請立即訂購", n=len(w["shortages"])))
    if w["unverified"]:
        lines.append(_("料號待依 Parts list 確認：{n} 項", n=len(w["unverified"])))
    url = print_url(cfg, ax, comp)
    if url.startswith("http"):
        lines.append(_("工單：{url}", url=url))
    return "\n".join(lines)


# ------------------------------------------------------------ 列印工單
def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def render_workorder(cfg: dict, w: dict, decision: dict | None = None, key: str = "") -> str:
    """A4 直式、可列印的維修工單：備品勾選、工具、人力、安全指令、步驟與工時、完工驗收、簽核。"""
    robot = cfg["robot"]["name"]
    now = time.strftime("%Y-%m-%d %H:%M")
    sep = _("｜")
    trig = ""
    if decision and key in ("grease", "drive", "motor"):
        x = decision.get(key) or {}
        trig = (f"<p class='trig'><b>{_e(_('觸發：'))}</b>{_e(decision.get('name'))} "
                f"{_e(COMP_LABELS.get(w['comp'], w['comp']))} {_e(LEVEL_NAMES.get(x.get('level', 0), ''))}{sep}"
                f"{_e(x.get('action', ''))}{sep}{_e(_('研判：{primary}', primary=decision.get('primary', '')))}</p>")
        if decision.get("next"):
            trig += (f"<p class='trig'><b>{_e(_('系統建議的確認方式：'))}</b>"
                     + _e(join(decision["next"])) + "</p>")
    rows_p = "".join(
        f"<tr><td class='ck'>☐</td><td>{_e(p['name'])}</td><td class='pn'>{_e(p.get('pn', ''))}</td>"
        f"<td class='n'>{p['qty']:g} {_e(p.get('unit', ''))}</td>"
        f"<td class='n'>{'' if p.get('stock') is None else _e(p['stock'])}</td>"
        f"<td>{'<b class=bad>' + _e(_('缺 {n:g}', n=p['short'])) + '</b>' if p['short'] else ''}</td>"
        f"<td>{_e(_('已確認')) if p.get('verified') else '<span class=warn>' + _e(_('待確認')) + '</span>'}"
        f"{'<br><small>' + _e(p['source']) + '</small>' if p.get('source') else ''}"
        f"{'<br><small>' + _e(p['note']) + '</small>' if p.get('note') else ''}"
        f"{'<br><small>' + _e(p['grease_note']) + '</small>' if p.get('grease_note') else ''}</td></tr>"
        for p in w["parts"])
    groups: dict[str, list] = {}
    for t in w["tools"]:
        groups.setdefault(t["group"], []).append(t)
    rows_t = "".join(
        f"<tr><td class='ck'>☐</td><td>{_e(g)}</td><td>{_e(t['name'])}"
        f"{'<br><small>' + _e(t['spec']) + '</small>' if t.get('spec') else ''}</td>"
        f"<td class='n'>{_e(t.get('qty', ''))} {_e(t.get('unit', ''))}</td></tr>"
        for g, ts in groups.items() for t in ts)
    rows_r = "".join(f"<tr><td>{_e(r.get('role'))}</td><td class='n'>{_e(r.get('n', 1))}</td><td>{_e(r.get('duty'))}</td>"
                     f"<td class='sign'></td></tr>" for r in w["roles"])
    rows_s = "".join(f"<tr><td class='ck'>☐</td><td>{i}. {_e(s)}</td></tr>" for i, s in enumerate(w["safety"], 1))
    rows_st = "".join(f"<tr><td class='n'>{i}</td><td>{_e(s.get('name'))}</td><td class='n'>{_e(s.get('crew', ''))}</td>"
                      f"<td class='n'>{float(s.get('hours', 0)):g}</td><td></td><td class='sign'></td></tr>"
                      for i, s in enumerate(w["steps"], 1))
    if not rows_st:
        rows_st = f"<tr><td colspan=6 class='warn'>{_e(_('尚未建立步驟：請依維護手冊補上作業包目錄的 steps'))}</td></tr>"
    done = [_("給脂口／排脂口無滲漏（運轉後再檢查一次）"), _("零點校正完成，既有程式關鍵點位確認無偏移"),
            _("低速試運轉無異音、無異常發熱"), _("DTM_DIAG 已執行，診斷有效"),
            _("DTM 已登錄事件（失效／預防、拆檢結果、時數表）"), _("工具清點完成，上鎖掛牌由本人解除")]
    rows_d = "".join(f"<tr><td class='ck'>☐</td><td>{_e(s)}</td></tr>" for s in done)
    meta = w.get("meta") or {}
    sop = f"<p>SOP: <a href='{_e(w['sop_url'])}'>{_e(w['sop_url'])}</a></p>" if w.get("sop_url") else ""
    unv = (f"<p class='warn'>{_e(_('有 {n} 項料號尚未依 Parts list 確認，下單前請核對機台銘牌與維護手冊。', n=len(w['unverified'])))}</p>"
           if w["unverified"] else "")
    t = lambda m, **kw: _e(_(m, **kw))  # noqa: E731
    th = lambda *ms: "".join(f"<th>{t(m) if m else ''}</th>" for m in ms)  # noqa: E731
    return f"""<!doctype html><html lang="{HTML_LANG.get(get_lang(), 'zh-Hant')}"><head><meta charset="utf-8">
<title>{t('工單')}{sep}{_e(robot)} {_e(w['label'])}</title>
<style>
@page{{size:A4;margin:12mm}}
body{{font-family:{FONT_STACK.get(get_lang(), FONT_STACK['zh-TW'])};font-size:12px;color:#1F2A33;margin:0 auto;max-width:190mm;padding:8mm 0}}
h1{{font-size:18px;margin:0 0 4px}}h2{{font-size:14px;margin:14px 0 6px;border-left:4px solid #2F6F73;padding-left:6px}}
table{{width:100%;border-collapse:collapse;margin-bottom:4px}}th,td{{border:1px solid #B8C1C8;padding:4px 5px;vertical-align:top;text-align:left}}
th{{background:#EEF1F3;white-space:nowrap}}td.n{{text-align:right;white-space:nowrap}}td.ck{{width:16px;text-align:center}}td.pn{{font-family:Consolas,monospace;white-space:nowrap}}
td.sign{{width:70px}}small{{color:#5A6873}}.warn{{color:#9A5B00}}.bad{{color:#B5462F}}
.head{{display:grid;grid-template-columns:repeat(4,1fr);gap:0;border:1px solid #B8C1C8;margin:8px 0}}
.head div{{padding:4px 6px;border-right:1px solid #B8C1C8}}.head div:last-child{{border-right:0}}.head b{{display:block;font-size:11px;color:#5A6873}}
.sign4 div{{min-height:44px}}.trig{{background:#FBEFD9;padding:5px 8px;margin:4px 0;border-radius:4px}}
.btn{{position:fixed;top:10px;right:10px}}@media print{{.btn{{display:none}}}}
</style></head><body>
<button class="btn" onclick="window.print()">{t('列印')}</button>
<h1>{t('維修工單：{label}', label=w['label'])}</h1>
<div class="head"><div><b>{t('機台')}</b>{_e(robot)}</div><div><b>{t('機型')}</b>{_e(meta.get('robot_model', ''))}</div>
<div><b>{t('開單時間')}</b>{now}</div><div><b>{t('工單號')}</b>&nbsp;</div></div>
<div class="head"><div><b>{t('人力')}</b>{t('{n} 人', n=w['crew'])}</div><div><b>{t('預估停機')}</b>{_e(hours_text(w))}</div>
<div><b>{t('步驟工時合計')}</b>{t('{h:g} h／{mh:g} 人時', h=w['step_hours'], mh=w['man_hours'])}</div><div><b>{t('預定日期')}</b>&nbsp;</div></div>
{trig}
<p><small>{_e(w.get('hours_note', ''))}</small></p>
<h2>{t('一、備品與耗材')}</h2>
<table><tr>{th('', '品項', '料號', '數量', '庫存', '缺料', '確認狀態／來源')}</tr>{rows_p}</table>
{unv}
<h2>{t('二、工具與設備')}</h2>
<table><tr>{th('', '類別', '工具', '數量')}</tr>{rows_t}</table>
<h2>{t('三、人力與分工')}</h2>
<table><tr>{th('角色', '人數', '工作內容', '簽名')}</tr>{rows_r}</table>
<h2>{t('四、作業安全指令（開工前逐項確認）')}</h2>
<table>{rows_s}</table>
<h2>{t('五、作業步驟與工時')}</h2>
<table><tr>{th('#', '步驟', '人數', '預估 h', '實際 h', '完成簽名')}</tr>{rows_st}</table>
<h2>{t('六、完工驗收')}</h2>
<table>{rows_d}</table>
<div class="head sign4"><div><b>{t('作業負責人')}</b>&nbsp;</div><div><b>{t('保養主管')}</b>&nbsp;</div><div><b>{t('生產單位')}</b>&nbsp;</div><div><b>{t('完工時間')}</b>&nbsp;</div></div>
{sop}
<p><small>{t('作業包目錄：{manual}', manual=meta.get('manual', ''))}{sep}{t('更新 {date}', date=meta.get('updated', ''))}{sep}{_e(meta.get('note', ''))}</small></p>
</body></html>"""

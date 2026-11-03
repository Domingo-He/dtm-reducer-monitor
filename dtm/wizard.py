"""第一次設定精靈（2.1，單機）：資料來源與 IP → 連線測試 → 監測軸與額定扭矩 → 寫進設定檔 → 重新啟動。

- 只有管理員、只能在這台電腦本機操作（機器人連線不開放從其他電腦修改）。
- 寫入前先備份 config.toml；寫完跑一次設定檔檢查，有錯誤就還原。
- 只改這幾個鍵：[robot] name／line／source／poll_hz、[source.fanuc_http] host 或 [source.modbus] host／port、
  [axes] enabled、[axis.N] rated_nm。其他設定與註解都保留。
"""
from __future__ import annotations

import copy
import os
import re
import shutil

from .config import axis_cfg, enabled_axes
from .i18n import N_, _

SOURCES = {"fanuc_http": N_("Fanuc 控制器網頁伺服器（HTTP，免額外硬體）"), "modbus": N_("Modbus TCP（經由 PLC）"),
           "sim": N_("模擬器（先試用，不接機器人）")}
AXES = (1, 2, 3, 4, 5, 6, 7)
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$")
TEST_S = 5.0


def _ax_label(ax: int) -> str:
    return "E1" if ax == 7 else f"J{ax}"


def view(cfg: dict, client: str) -> dict:
    from .settings import config_editable
    ok, why = config_editable(cfg, client)
    r, src = cfg["robot"], cfg.get("source") or {}
    axes = enabled_axes(cfg)
    return {"can_edit": ok, "why": why, "path": cfg.get("_path") or "",
            "sources": [{"v": k, "label": _(v)} for k, v in SOURCES.items()],
            "values": {"name": r.get("name", ""), "line": r.get("line", ""), "model": r.get("model", ""),
                       "source": r.get("source", "sim"), "poll_hz": r.get("poll_hz", 5.0),
                       "host": (src.get(r.get("source")) or {}).get("host", "") if r.get("source") in ("fanuc_http", "modbus")
                       else (src.get("fanuc_http") or {}).get("host", ""),
                       "port": (src.get("modbus") or {}).get("port", 502),
                       "axes": axes,
                       "rated": {str(ax): float(axis_cfg(cfg, ax).get("rated_nm") or 0) for ax in AXES}},
            "axes": [{"ax": ax, "label": _ax_label(ax)} for ax in AXES],
            "pending": bool(cfg.get("_setup_pending"))}


def normalize(b: dict) -> dict:
    """檢查表單；不對就丟 ValueError（訊息直接給畫面看）。"""
    src = str(b.get("source") or "")
    if src not in SOURCES:
        raise ValueError(_("請選擇資料來源"))
    name = " ".join(str(b.get("name") or "").split())[:40]
    if not name:
        raise ValueError(_("請填機器人名稱"))
    line = " ".join(str(b.get("line") or "").split())[:40]
    host = str(b.get("host") or "").strip()
    port = 502
    if src in ("fanuc_http", "modbus"):
        if not HOST_RE.match(host):
            raise ValueError(_("請填控制器的 IP，例如 192.168.0.10"))
        if src == "modbus":
            try:
                port = int(b.get("port") or 502)
            except (TypeError, ValueError):
                port = 0
            if not 1 <= port <= 65535:
                raise ValueError(_("連接埠要在 1–65535"))
    try:
        hz = float(b.get("poll_hz") or 5)
    except (TypeError, ValueError):
        hz = 0
    if not 0.5 <= hz <= 20:
        raise ValueError(_("取樣頻率要在 0.5–20 次／秒"))
    axes = sorted({int(x) for x in (b.get("axes") or []) if str(x).isdigit() and int(x) in AXES})
    if not axes:
        raise ValueError(_("至少選一個要監測的軸"))
    rated = {}
    for k, v in (b.get("rated") or {}).items():
        if not str(k).isdigit() or int(k) not in axes or v in (None, ""):
            continue
        try:
            nm = float(v)
        except (TypeError, ValueError):
            raise ValueError(_("{ax} 的額定扭矩要填數字", ax=_ax_label(int(k)))) from None
        if not 0 <= nm <= 1e6:
            raise ValueError(_("{ax} 的額定扭矩不合理", ax=_ax_label(int(k))))
        rated[int(k)] = nm
    return {"name": name, "line": line, "source": src, "host": host, "port": port, "poll_hz": hz, "axes": axes,
            "rated": rated}


def applied(cfg: dict, v: dict) -> dict:
    """套上表單內容的設定（連線測試用；不寫檔）。"""
    c = copy.deepcopy({k: x for k, x in cfg.items() if not k.startswith("_")})
    c["_path"] = cfg.get("_path")
    c["robot"].update(name=v["name"], line=v["line"], source=v["source"], poll_hz=v["poll_hz"])
    if v["source"] == "fanuc_http":
        c["source"]["fanuc_http"]["host"] = v["host"]
    elif v["source"] == "modbus":
        c["source"]["modbus"].update(host=v["host"], port=v["port"])
    c["axes"]["enabled"] = v["axes"]
    for ax, nm in v["rated"].items():
        c.setdefault("axis", {}).setdefault(str(ax), {})["rated_nm"] = nm
    return c


def test(cfg: dict, client: str, b: dict, seconds: float = TEST_S) -> dict:
    """用表單的內容實際讀幾秒資料（不寫設定檔）。"""
    from .check import ERROR, HINT, WARN, Report, check_connection
    from .settings import config_editable
    ok, why = config_editable(cfg, client)
    if not ok:
        raise PermissionError(why)
    v = normalize(b)
    rep = Report()
    check_connection(applied(cfg, v), rep, seconds, out=lambda *a, **k: None)
    order = {ERROR: 0, WARN: 1, HINT: 2}
    items = [{"level": lv, "where": w, "msg": m} for lv, w, m in sorted(rep.items, key=lambda i: order.get(i[0], 3))]
    return {"ok": not rep.count(ERROR), "items": items}


def save(cfg: dict, client: str, b: dict) -> dict:
    """寫進設定檔；寫完檢查，有錯誤就還原。重新啟動 DTM 後生效。"""
    from .check import ERROR, run_check
    from .settings import config_editable, write_config
    ok, why = config_editable(cfg, client)
    if not ok:
        raise PermissionError(why)
    v = normalize(b)
    edits = [("robot", {"name": v["name"], "line": v["line"], "source": v["source"], "poll_hz": v["poll_hz"]})]
    if v["source"] == "fanuc_http":
        edits.append(("source.fanuc_http", {"host": v["host"]}))
    elif v["source"] == "modbus":
        edits.append(("source.modbus", {"host": v["host"], "port": v["port"]}))
    edits.append(("axes", {"enabled": v["axes"]}))
    for ax, nm in sorted(v["rated"].items()):
        edits.append((f"axis.{ax}", {"rated_nm": nm}))
    path = cfg["_path"]
    bak = write_config(cfg, edits)
    rep = run_check(path, connect=False, out=lambda *a, **k: None)
    if rep.count(ERROR):
        shutil.copy2(bak, path)
        raise ValueError(_("設定檔檢查有錯誤，已還原：{e}",
                           e=_("；").join(f"{w} {m}".strip() for lv, w, m in rep.items if lv == ERROR)[:400]))
    cfg["_setup_pending"] = True
    return {"ok": True, "backup": os.path.basename(bak), "path": path}

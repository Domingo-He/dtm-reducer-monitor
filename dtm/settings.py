"""設備設定（2.1）：每軸有哪些量測（扭矩一定有；外殼溫度、徑向振動、軸向振動可選），每台另有環境溫度。

- 只在單機模式、而且從這台電腦本機（127.0.0.1）登入管理員時可以修改；其他情況只能查看。
- 寫入 config.toml 的 [devices] 區段前，先把舊檔備份到 config_backups/，並寫一筆操作紀錄。
- 寫完會重新解析一次；解析失敗就自動還原備份，不會留下壞掉的設定檔。
- 邊緣主機的機器人設定在 [[robots]] 裡，請直接改邊緣主機的 config.toml（畫面只顯示）。
"""
from __future__ import annotations

import os
import re
import shutil
import time
import tomllib

from .auth import is_loopback
from .config import DEVICE_KINDS, axis_name, device_flags, enabled_axes
from .i18n import TDict, _

KIND_LABELS = TDict({"torque": "扭矩（干擾扭矩，必有）", "temp": "外殼溫度", "radial": "徑向振動", "axial": "軸向振動"})
BACKUP_KEEP = 20
_TABLE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def editable(cfg: dict, client: str) -> tuple[bool, str]:
    if cfg.get("_edge"):
        return False, _("邊緣主機管理的機器人：請在邊緣主機的 config.toml 該台 [[robots]] 底下加 [robots.devices]")
    if cfg.get("_demo"):
        return False, _("演示模式不能修改設定")
    if not cfg.get("_path") or not os.path.exists(cfg["_path"]):
        return False, _("沒有設定檔（用 -c 指定 config.toml 啟動後才能修改）")
    if not is_loopback(client):
        return False, _("設備設定只能在這台電腦本機修改（http://127.0.0.1）")
    return True, ""


def view(cfg: dict, sensors_status: dict | None, client: str) -> dict:
    fl = device_flags(cfg)
    ok, why = editable(cfg, client)
    st = sensors_status or {}
    upd = st.get("update_s") or {}
    slow = set(st.get("slow") or [])
    axes = []
    for ax in enabled_axes(cfg):
        f = fl["axes"].get(ax) or {}
        keys = [k for k in upd if k.split(".")[0] == str(ax)]
        axes.append({"axis": ax, "name": axis_name(cfg, ax), **{k: bool(f.get(k)) for k in ("torque", *DEVICE_KINDS)},
                     "update_s": {k: upd[k] for k in keys}, "slow": sorted(k for k in keys if k in slow)})
    return {"editable": ok, "why": why, "ambient": fl["ambient"], "axes": axes,
            "labels": dict(KIND_LABELS.items()),
            "sensors_enabled": bool((cfg.get("sensors") or {}).get("enabled")),
            "slow_s": st.get("slow_s"), "ambient_update_s": upd.get("ambient"),
            "note": _("更新間隔超過 {s:.0f} 秒的感測器（例如電池型無線）只在數值變化時記錄，主要用於每日趨勢",
                      s=float((cfg.get("sensors") or {}).get("slow_s", 20)))}


def devices_block(dev: dict) -> str:
    lines = ["[devices]              # 設備設定（儀表板「設備設定」寫入；扭矩一定監測）",
             f"ambient = {'true' if dev.get('ambient') else 'false'}"]
    for ax in sorted((dev.get("axis") or {}), key=lambda x: int(x)):
        a = dev["axis"][ax]
        lines.append(f"[devices.axis.{int(ax)}]")
        lines += [f"{k} = {'true' if a.get(k) else 'false'}" for k in DEVICE_KINDS]
    return "\n".join(lines) + "\n"


def replace_section(text: str, name: str, block: str) -> str:
    """把 [name] 與 [name.…] 的表格整段換成 block；原本沒有就加在檔尾。註解與其他區段保持不動。"""
    lines = text.splitlines(keepends=True)
    out, inside, placed = [], False, False
    for ln in lines:
        m = _TABLE.match(ln)
        if m:
            t = m.group(1)
            if t == name or t.startswith(name + "."):
                if not placed:
                    out.append(block)
                    placed = True
                inside = True
                continue
            inside = False
        if not inside:
            out.append(ln)
    s = "".join(out)
    if not placed:
        s = s.rstrip("\n") + "\n\n" + block
    return s


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


_KEY = re.compile(r"^\s*([A-Za-z0-9_\-]+)\s*=")


def set_keys(text: str, table: str, values: dict) -> str:
    """只改 [table] 底下的幾個鍵（其他鍵、註解、子表格都保留）；沒有這個表格就加在檔尾。"""
    lines = text.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        m = _TABLE.match(ln)
        if m and m.group(1) == table and not ln.lstrip().startswith("[["):
            start = i
            break
    todo = dict(values)
    if start is None:
        body = "".join(f"{k} = {_toml_value(v)}\n" for k, v in todo.items())
        return text.rstrip("\n") + f"\n\n[{table}]\n" + body
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _TABLE.match(lines[j]):
            end = j
            break
    for j in range(start + 1, end):
        m = _KEY.match(lines[j])
        if m and m.group(1) in todo:
            k = m.group(1)
            com = ""
            rest = lines[j].split("=", 1)[1]
            if "#" in rest and rest.count('"') % 2 == 0 and rest.rfind("#") > rest.rfind('"'):
                com = "   " + rest[rest.rfind("#"):].rstrip("\n")
            lines[j] = f"{k} = {_toml_value(todo.pop(k))}{com}\n"
    if todo:
        ins = "".join(f"{k} = {_toml_value(v)}\n" for k, v in todo.items())
        k = end
        while k > start + 1 and not lines[k - 1].strip():
            k -= 1
        lines[k:k] = [ins]
    return "".join(lines)


def config_editable(cfg: dict, client: str) -> tuple[bool, str]:
    """改設定檔（HTTPS、第一次設定）：要有設定檔、不是演示、從這台電腦本機操作。"""
    if cfg.get("_demo"):
        return False, _("演示模式不能修改設定")
    if not cfg.get("_path") or not os.path.exists(cfg["_path"]):
        return False, _("沒有設定檔（用 -c 指定 config.toml 啟動後才能修改）")
    if not is_loopback(client):
        return False, _("這項設定只能在這台電腦本機修改（http://127.0.0.1）")
    return True, ""


def write_config(cfg: dict, edits: list[tuple[str, dict]]) -> str:
    """依序改幾個表格的鍵，寫回 config.toml（先備份；寫完解析失敗就還原）。回傳備份檔路徑。"""
    path = cfg["_path"]
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    new = text
    for table, values in edits:
        new = set_keys(new, table, values)
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(_("產生的設定檔格式錯誤，未寫入：{e}", e=e)) from e
    bak = backup_config(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    os.replace(tmp, path)
    try:
        with open(path, "rb") as f:
            tomllib.load(f)
    except Exception:
        shutil.copy2(bak, path)
        raise
    return bak


def enable_tls(cfg: dict, client: str, extra_names: list[str] | None = None) -> dict:
    """產生自簽憑證、寫進設定檔 [web] tls_cert／tls_key。重新啟動 DTM 後生效。"""
    ok, why = config_editable(cfg, client)
    if not ok:
        raise PermissionError(why)
    from . import tlscert
    names, ips = tlscert.local_names()
    for n in [cfg["web"].get("public_host") or ""] + list(extra_names or []):
        n = str(n).strip()
        if not n:
            continue
        try:
            import ipaddress
            ipaddress.ip_address(n)
            if n not in ips:
                ips.append(n)
        except ValueError:
            if re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", n) and n not in names:
                names.append(n)
    folder = os.path.join(os.path.dirname(os.path.abspath(cfg["_path"])), "tls")
    info = tlscert.write(folder, names, ips)
    bak = write_config(cfg, [("web", {"tls_cert": "tls/dtm-cert.pem", "tls_key": "tls/dtm-key.pem"})])
    cfg["_tls_pending"] = "on"
    return dict(info, backup=bak)


def disable_tls(cfg: dict, client: str) -> str:
    ok, why = config_editable(cfg, client)
    if not ok:
        raise PermissionError(why)
    bak = write_config(cfg, [("web", {"tls_cert": "", "tls_key": ""})])
    cfg["_tls_pending"] = "off"
    return bak


def backup_config(path: str, now: float | None = None) -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(path)), "config_backups")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, os.path.basename(path) + time.strftime(".%Y%m%d-%H%M%S", time.localtime(now or time.time())))
    base, n = dst, 2
    while os.path.exists(dst):              # 同一秒內改兩次：不覆蓋前一份備份
        dst = f"{base}-{n}"
        n += 1
    shutil.copy2(path, dst)
    old = sorted(f for f in os.listdir(d) if f.startswith(os.path.basename(path) + "."))
    for f in old[:-BACKUP_KEEP]:
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    return dst


def normalize(cfg: dict, body: dict) -> dict:
    axes = set(enabled_axes(cfg))
    dev = {"ambient": bool(body.get("ambient")), "axis": {}}
    for k, v in (body.get("axes") or {}).items():
        if not str(k).isdigit() or int(k) not in axes or not isinstance(v, dict):
            raise ValueError(_("沒有監測這個軸：{ax}", ax=k))
        dev["axis"][str(int(k))] = {kind: bool(v.get(kind)) for kind in DEVICE_KINDS}
    return dev


def save(cfg: dict, body: dict, client: str) -> tuple[dict, str]:
    """寫入 config.toml；回傳 (新的 devices 設定, 備份檔路徑)。呼叫前已確認是管理員。"""
    ok, why = editable(cfg, client)
    if not ok:
        raise PermissionError(why)
    dev = normalize(cfg, body)
    path = cfg["_path"]
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    bak = backup_config(path)
    new = replace_section(text, "devices", devices_block(dev))
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(_("產生的設定檔格式錯誤，未寫入：{e}", e=e)) from e
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    os.replace(tmp, path)
    try:
        with open(path, "rb") as f:
            tomllib.load(f)
    except Exception:
        shutil.copy2(bak, path)
        raise
    cfg["devices"] = {"ambient": dev["ambient"], "axis": dict(dev["axis"])}
    return dev, bak


def summary(before: dict, after: dict) -> str:
    """操作紀錄用：哪些量測被打開或關掉。"""
    out = []
    if bool(before.get("ambient")) != bool(after.get("ambient")):
        out.append(("+" if after.get("ambient") else "-") + "ambient")
    for ax, a in (after.get("axis") or {}).items():
        b = (before.get("axes") or {}).get(int(ax)) or {}
        for k in DEVICE_KINDS:
            if bool(b.get(k)) != bool(a.get(k)):
                out.append(("+" if a.get(k) else "-") + f"{ax}.{k}")
    return " ".join(out) or _("沒有變更")

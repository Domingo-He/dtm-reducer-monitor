"""拆檢表單（2.1）：更換減速機、齒輪箱、馬達時記錄拆下零件的實際狀態。

- 損傷位置、損傷型態（點蝕／剝離／磨耗／斷齒／其他）、油脂狀態、照片（存 data/photos）、備註。
- 也可以登錄「檢查無異常」：拆下來沒有損傷，這筆更換在壽命統計裡改當「預防更換」（設限資料），
  並留下「這次判斷可能太早」的紀錄，之後用來修正門檻。
- 存在被更換元件的 teardown 欄位（JSON）與事件明細裡；撤銷事件時一併還原。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time

from .config import TEAR_GREASE, TEAR_LOCATIONS, TEAR_MODES
from .i18n import _
from . import runtime as R

REPLACE_KINDS = ("reducer_replace", "gearbox_replace", "motor_replace")
PHOTO_MAX = 8 * 1024 * 1024
SAFE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
MAGIC = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png", b"RIFF": ".webp"}


def photos_dir(cfg: dict) -> str:
    d = R.resolve(cfg, os.path.join("data", "photos"))
    if cfg.get("_edge"):
        d = os.path.join(d, R.slug(R.robot_id(cfg)))
    return d


def save_photo(cfg: dict, data: str, prefix: str = "td", now: float | None = None) -> str:
    """data：data URL（data:image/jpeg;base64,…）或純 base64。只收 JPEG、PNG、WebP，8 MB 以內。回傳檔名。"""
    raw = data.split(",", 1)[1] if data.startswith("data:") else data
    try:
        b = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as e:
        raise ValueError(_("照片格式錯誤")) from e
    if len(b) > PHOTO_MAX:
        raise ValueError(_("照片太大（上限 8 MB），請先縮小再上傳"))
    ext = next((x for m, x in MAGIC.items() if b.startswith(m) and (m != b"RIFF" or b[8:12] == b"WEBP")), None)
    if not ext:
        raise ValueError(_("只接受 JPEG、PNG、WebP 照片"))
    d = photos_dir(cfg)
    os.makedirs(d, exist_ok=True)
    stem = f"{R.slug(prefix)}_{time.strftime('%Y%m%d_%H%M%S', time.localtime(now or time.time()))}"
    name, n = stem + ext, 1
    while os.path.exists(os.path.join(d, name)):
        name, n = f"{stem}_{n}{ext}", n + 1
    with open(os.path.join(d, name), "wb") as f:
        f.write(b)
    return name


def photo_path(cfg: dict, name: str) -> str | None:
    if not SAFE.match(name or ""):
        return None
    p = os.path.join(photos_dir(cfg), name)
    return p if os.path.isfile(p) else None


def _list(v) -> list:
    if v in (None, ""):
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def normalize(cfg: dict, form: dict, who: str = "", now: float | None = None) -> dict:
    """檢查並整理表單內容；代碼不對就丟 ValueError。"""
    loc = [x for x in _list(form.get("location")) if x]
    modes = [x for x in _list(form.get("modes")) if x]
    grease = str(form.get("grease") or "")
    bad = [x for x in loc if x not in TEAR_LOCATIONS] + [x for x in modes if x not in TEAR_MODES]
    if bad or (grease and grease not in TEAR_GREASE):
        raise ValueError(_("拆檢表單有不認得的選項：{x}", x=_("、").join(bad + ([grease] if grease and grease not in TEAR_GREASE else []))))
    photos = [p for p in _list(form.get("photos")) if p]
    missing = [p for p in photos if not photo_path(cfg, p)]
    if missing:
        raise ValueError(_("找不到照片：{x}", x=_("、").join(missing)))
    no_fault = bool(form.get("no_fault"))
    if no_fault and modes:
        raise ValueError(_("勾了「檢查無異常」就不要再選損傷型態"))
    if not no_fault and not (loc or modes or grease or str(form.get("note") or "").strip()):
        raise ValueError(_("請至少填一項：損傷位置、型態、油脂狀態或備註；沒有損傷請勾「檢查無異常」"))
    return {"location": loc, "modes": modes, "grease": grease, "photos": photos, "no_fault": no_fault,
            "note": str(form.get("note") or "")[:500], "who": who, "ts": now or time.time()}


def parse(raw) -> dict | None:
    """components.teardown 欄位：JSON（2.1）或舊版純文字。"""
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {"note": str(raw)}
    except (TypeError, ValueError):
        return {"note": str(raw)}


def text(td: dict | None) -> str:
    if not td:
        return ""
    if td.get("no_fault"):
        out = [_("檢查無異常")]
    else:
        out = []
        if td.get("location"):
            out.append(_("位置：{x}", x=_("、").join(TEAR_LOCATIONS.get(x, x) for x in td["location"])))
        if td.get("modes"):
            out.append(_("型態：{x}", x=_("、").join(TEAR_MODES.get(x, x) for x in td["modes"])))
    if td.get("grease"):
        out.append(_("油脂：{x}", x=TEAR_GREASE.get(td["grease"], td["grease"])))
    if td.get("photos"):
        out.append(_("照片 {n} 張", n=len(td["photos"])))
    if td.get("note"):
        out.append(td["note"])
    return _("；").join(out)


def options() -> dict:
    return {"locations": dict(TEAR_LOCATIONS.items()), "modes": dict(TEAR_MODES.items()),
            "grease": dict(TEAR_GREASE.items())}

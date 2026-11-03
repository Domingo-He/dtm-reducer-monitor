"""單台機器人的計畫停機（2.1）：歲修、停線、換模時設定「停機中」與結束時間。

- 期間不發「資料中斷」「心跳停止」「連線中斷」「無效值」警報，看門狗也不報資料中斷；
  原本作用中的這幾種警報會在開始停機時解除。其他判斷照常。
- 單機、邊緣主機（每台各自設定）、中央（下發指令到那台）都能設定；工程師以上。
- 存在 run/downtime_<機器人代碼>.json（重新啟動也保留），到期自動失效；每次設定與取消都記在系統紀錄與操作紀錄。
"""
from __future__ import annotations

import os
import time
from datetime import datetime

from .i18n import _
from . import runtime as R

COMM_KEYS = ("hb", "conn")                 # 停機期間不發的警報 key（另外所有 inv:軸）
MAX_DAYS = 60


def path(cfg: dict) -> str:
    return os.path.join(R.run_dir(cfg), f"downtime_{R.slug(R.robot_id(cfg))}.json")


def get(cfg: dict, now: float | None = None) -> dict | None:
    """目前的計畫停機（沒有或已到期 → None）。"""
    d = R.read_json(path(cfg), None)
    now = now or time.time()
    if not d or float(d.get("until") or 0) <= now:
        return None
    return d


def parse_until(v, now: float | None = None) -> float:
    """until 可以是時間戳、"YYYY-MM-DD HH:MM"、"YYYY-MM-DDTHH:MM"，或 hours（數字＋h，例如 "8h"）。"""
    now = now or time.time()
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").strip()
    if not s:
        raise ValueError(_("請填停機結束時間"))
    if s.lower().endswith("h") and s[:-1].replace(".", "", 1).isdigit():
        return now + float(s[:-1]) * 3600
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:16], fmt).timestamp()
        except ValueError:
            continue
    raise ValueError(_("看不懂結束時間：{v}（例：2026-10-20 08:00 或 8h）", v=s))


def set_(cfg: dict, until, note: str = "", who: str = "", now: float | None = None) -> dict:
    now = now or time.time()
    t = parse_until(until, now)
    if t <= now:
        raise ValueError(_("結束時間要晚於現在"))
    if t - now > MAX_DAYS * 86400:
        raise ValueError(_("計畫停機最長 {d} 天；更久請改用維護模式或停用這台", d=MAX_DAYS))
    d = {"since": now, "until": t, "note": str(note or "")[:200], "who": who}
    R.write_json(path(cfg), d)
    return d


def clear(cfg: dict) -> bool:
    try:
        os.remove(path(cfg))
        return True
    except OSError:
        return False


def label(d: dict | None) -> str:
    if not d:
        return ""
    return _("計畫停機中，至 {t}", t=datetime.fromtimestamp(float(d["until"])).strftime("%m/%d %H:%M")) + \
        (f"（{d['note']}）" if d.get("note") else "")

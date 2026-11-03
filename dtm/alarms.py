"""警報：即時扭矩超限、資料中斷、健康劣化。通知由 notify.Notifier 處理（簡訊、Email、Webhook）。"""
from __future__ import annotations

import logging
import threading
import time

from .config import axis_cfg
from .i18n import N_, TDict, _
from .model import Sample
from .notify import Notifier  # 保留舊的匯入路徑

__all__ = ["AlarmManager", "RealtimeMonitor", "Notifier"]
log = logging.getLogger("dtm.alarm")
LEVEL_TXT = TDict({1: "注意", 2: "警戒"})


class AlarmManager:
    """同一個 key 同時只有一筆作用中警報；等級升高時開新紀錄，回到 0 時解除。"""

    def __init__(self, store, notifier=None, raw_keep_s: float = 0.0):
        self.store = store
        self.notifier = notifier
        self.raw_keep_s = raw_keep_s           # > 0：每次發出警報，保留前後各這麼多秒的原始資料
        self.lock = threading.Lock()
        self.active = {a["akey"]: {"id": a["id"], "level": a["level"], "message": a["message"], "axis": a["axis"],
                                   "kind": a["kind"]} for a in store.active_alarms()}

    def set(self, key: str, axis, kind: str, level: int, message: str, value, ts: float):
        with self.lock:
            cur = self.active.get(key)
            if level <= 0:
                if cur:
                    self.store.clear_alarm(cur["id"], ts)
                    del self.active[key]
                    if self.notifier:
                        self.notifier.send(cur["level"], cur["message"], cur["axis"], cur["kind"], ts, key, cleared=True)
                return
            if cur and cur["level"] >= level:
                return
            if cur:
                self.store.clear_alarm(cur["id"], ts)
            aid = self.store.add_alarm(ts, axis, kind, key, level, message, value)
            self.active[key] = {"id": aid, "level": level, "message": message, "axis": axis, "kind": kind}
            if self.raw_keep_s > 0 and hasattr(self.store, "add_raw_keep"):
                try:
                    self.store.add_raw_keep("alarm", ts - self.raw_keep_s, ts + self.raw_keep_s, axis, key)
                except Exception as e:                  # 保留片段失敗不能擋住警報
                    log.warning("raw_keep: %s", e)
        log.warning("[%s] %s", LEVEL_TXT.get(level), message)
        if self.notifier:
            self.notifier.send(level, message, axis, kind, ts, key)


class RealtimeMonitor:
    """即時檢查：扭矩超過額定百分比（或原始值門檻）、資料無效、心跳停止。需持續 hold_s 才觸發。"""

    def __init__(self, cfg: dict, conv, alarms: AlarmManager, axes: list[int]):
        r = cfg["realtime"]
        self.cfg = cfg
        self.conv = conv
        self.alarms = alarms
        self.axes = axes
        self.hold_s = float(r["hold_s"])
        self.stale_s = float(r["stale_s"])
        self.invalid_abs = float(r["invalid_abs"])
        self.pending: dict[str, tuple[int, float]] = {}
        self.quiet = lambda: False             # 計畫停機中：不發資料中斷、心跳、連線、無效值警報
        self.last_hb = None
        self.last_hb_change = time.time()
        self.last_sample = time.time()

    def _debounce(self, key, axis, kind, level, msg, val, now, hold):
        """msg 可以是字串或「呼叫後才產生訊息」的函式：每筆樣本都會檢查，訊息只在真的要發警報時才組。"""
        p = self.pending.get(key)
        if p is None or p[0] != level:
            self.pending[key] = p = (level, now)
        if now - p[1] < hold:
            return
        cur = self.alarms.active.get(key)
        if (level <= 0 and not cur) or (level > 0 and cur and cur["level"] >= level):
            return                                   # 狀態沒變（大部分樣本走這裡）
        self.alarms.set(key, axis, kind, level, msg() if callable(msg) else msg, val, now)

    def update(self, s: Sample):
        now = s.ts
        self.last_sample = time.time()
        quiet = self.quiet()
        if s.hb is not None:
            if s.hb != self.last_hb:
                self.last_hb, self.last_hb_change = s.hb, now
            stale = now - self.last_hb_change > self.stale_s and not quiet
            self._debounce("hb", None, N_("通訊"), 2 if stale else 0,
                           lambda: _("資料未更新：心跳停止（檢查 BG Logic 是否執行、通訊是否正常）"), None, now, 0)
        self._debounce("conn", None, N_("通訊"), 0, "", None, now, 0)
        for ax in self.axes:
            a = axis_cfg(self.cfg, ax)
            raw = s.dt.get(ax)
            bad = raw is None or abs(raw) >= self.invalid_abs
            self._debounce(f"inv:{ax}", ax, N_("資料"), 1 if bad and not quiet else 0,
                           lambda a=a, raw=raw: _("{name} 干擾扭矩無效值（{raw}）", name=a["name"], raw=raw),
                           raw, now, self.hold_s)
            if bad:
                continue
            nm = self.conv.nm(ax, raw)
            pct = self.conv.pct_rated(ax, nm)
            if pct is not None:
                v = abs(pct)
                lv = 2 if v >= float(a["rt_alert_pct"]) else 1 if v >= float(a["rt_caution_pct"]) else 0
                msg = lambda a=a, nm=nm, v=v: _("{name} 即時扭矩 {nm:.0f} Nm，達額定 {pct:.0f}%",  # noqa: E731
                                                  name=a["name"], nm=nm, pct=v)
                val = pct
            elif float(a["rt_caution_raw"]) > 0:
                v = abs(raw)
                lv = 2 if 0 < float(a["rt_alert_raw"]) <= v else 1 if v >= float(a["rt_caution_raw"]) else 0
                msg = lambda a=a, raw=raw: _("{name} 即時干擾扭矩 {raw:.0f}（原始值）超過門檻",  # noqa: E731
                                             name=a["name"], raw=raw)
                val = raw
            else:
                continue
            self._debounce(f"rt:{ax}", ax, N_("即時扭矩"), lv, msg, val, now, self.hold_s)

    def check_conn(self, now: float):
        """長時間讀不到資料時觸發（例如網路中斷）。"""
        if now - self.last_sample > self.stale_s and not self.quiet():
            self.alarms.set("conn", None, N_("通訊"), 2, _("無法讀取機器人資料（連線中斷或來源錯誤）"), None, now)

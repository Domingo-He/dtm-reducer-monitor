"""程式看門狗、硬體看門狗、維護模式。

四層防護（上一層處理不了，下一層接手；DTM 只讀資料，重啟不會影響機器人）：
1. 服務自動重啟（WinSW／systemd）：程式閃退 → 10 秒內重啟。
2. 程式看門狗（本模組，另一個獨立的服務）：狀態檔不更新或擷取迴圈不轉 = 卡住 → 重啟服務並通知。
   一小時內重啟超過 max_restarts 次就停止自動重啟、改通知人處理，避免整夜反覆重開。
3. 硬體看門狗：看門狗確認 DTM 健康才去「餵」；整台電腦當機（連看門狗自己都不動）時，硬體倒數到了自動重開機。
   一天內因停止餵狗造成的重開機超過 max_reboots_day 次，就改成一直餵、只通知人（防止無限重開）。
4. 中央遠端監看：邊緣主機太久沒回報，由中央發警報（斷電、斷網只有這一層看得到）。

維護模式（安裝 Windows 更新、手動維修前）：python -m dtm maintenance on --minutes 60
期間看門狗不重啟服務，硬體看門狗暫停（不支援暫停的就一直餵），時間到自動恢復。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

from .i18n import _
from . import runtime as R

log = logging.getLogger("dtm.watchdog")


# ------------------------------------------------------------ 維護模式
def maint_path(cfg: dict) -> str:
    return os.path.join(R.run_dir(cfg), "maintenance.json")


def maintenance(cfg: dict, now: float | None = None) -> dict | None:
    m = R.read_json(maint_path(cfg))
    now = now or time.time()
    if m and float(m.get("until", 0)) > now:
        return m
    return None


def set_maintenance(cfg: dict, minutes: float, reason: str = "", who: str = "") -> dict:
    m = {"since": time.time(), "until": time.time() + minutes * 60, "reason": reason, "who": who}
    R.write_json(maint_path(cfg), m)
    return m


def clear_maintenance(cfg: dict):
    try:
        os.remove(maint_path(cfg))
    except OSError:
        pass


# ------------------------------------------------------------ 硬體看門狗
class Feeder:
    kind = "none"
    can_disable = True

    def __init__(self, hw: dict):
        self.hw = hw
        self.error = ""

    def feed(self):
        pass

    def disable(self):
        pass

    def enable(self):
        pass


class LinuxFeeder(Feeder):
    """Linux 的 /dev/watchdog：開啟後定期寫入；寫 'V' 再關閉 = 暫停（核心沒設 nowayout 時）。"""
    kind = "linux"

    def __init__(self, hw):
        super().__init__(hw)
        self.f = None

    def enable(self):
        if self.f is None:
            self.f = open(self.hw.get("device", "/dev/watchdog"), "wb", buffering=0)

    def feed(self):
        self.enable()
        self.f.write(b"1")

    def disable(self):
        if self.f is not None:
            try:
                self.f.write(b"V")
            finally:
                self.f.close()
                self.f = None


class CommandFeeder(Feeder):
    """呼叫廠商工具（例如研華 SUSI 的命令列程式）餵狗、啟用、暫停。"""
    kind = "command"

    def _run(self, cmd):
        if cmd:
            subprocess.run(cmd, timeout=15, check=True, capture_output=True)

    def feed(self):
        self._run(self.hw.get("feed_cmd"))

    def enable(self):
        self._run(self.hw.get("enable_cmd"))

    def disable(self):
        self._run(self.hw.get("disable_cmd"))

    @property
    def can_disable(self):
        return bool(self.hw.get("disable_cmd"))


class ModbusFeeder(Feeder):
    """送心跳給 PLC：每次寫一個遞增的數字；PLC 發現數字太久沒變，就用繼電器切斷再接通電腦電源。"""
    kind = "modbus"

    def __init__(self, hw, client=None):
        super().__init__(hw)
        self.client = client
        self.n = 0

    def _cl(self):
        if self.client is None:
            from pymodbus.client import ModbusTcpClient
            self.client = ModbusTcpClient(self.hw["host"], port=int(self.hw.get("port", 502)))
        if hasattr(self.client, "connected") and not self.client.connected:
            self.client.connect()
        return self.client

    def _write(self, addr: int, value: int):
        cl = self._cl()
        for kw in ("device_id", "slave", "unit"):
            try:
                rr = cl.write_register(int(addr), int(value), **{kw: int(self.hw.get("unit", 1))})
                break
            except TypeError:
                continue
        else:  # pragma: no cover
            raise RuntimeError("pymodbus")
        if rr.isError():
            raise RuntimeError(_("PLC 心跳寫入失敗"))

    def feed(self):
        self.n = (self.n + 1) % 30000
        self._write(self.hw.get("addr", 0), self.n)

    def disable(self):
        if int(self.hw.get("maint_addr", -1)) >= 0:
            self._write(self.hw["maint_addr"], 1)

    def enable(self):
        if int(self.hw.get("maint_addr", -1)) >= 0:
            self._write(self.hw["maint_addr"], 0)

    @property
    def can_disable(self):
        return int(self.hw.get("maint_addr", -1)) >= 0


def make_feeder(hw: dict) -> Feeder:
    return {"linux": LinuxFeeder, "command": CommandFeeder, "modbus": ModbusFeeder}.get(hw.get("type", "none"),
                                                                                       Feeder)(hw)


# ------------------------------------------------------------ 程式看門狗
def default_restart_cmd(cfg: dict) -> list[str]:
    wd = cfg["watchdog"]
    if wd.get("restart_cmd"):
        return list(wd["restart_cmd"])
    name = wd.get("service_name", "DTM")
    if os.name == "nt":
        exe = os.path.join(R.base_dir(cfg), "service", f"{name}.exe")
        if os.path.exists(exe):
            return [exe, "restart"]
        return ["cmd", "/c", f"net stop {name} & net start {name}"]
    return ["systemctl", "restart", name.lower()]


class Watchdog:
    """judge() 判斷健康；tick() 做一次檢查（重啟、通知、餵狗）。時間與重啟動作都可注入，方便測試。"""

    def __init__(self, cfg: dict, notifier=None, restart=None, feeder: Feeder | None = None,
                 clock=time.time, status_files: list[str] | None = None):
        self.cfg = cfg
        self.wd = cfg["watchdog"]
        self.notifier = notifier
        self.restart_fn = restart or self._restart
        self.feeder = feeder or make_feeder(self.wd.get("hw", {}))
        self.clock = clock
        self.status_files = status_files
        self.started = clock()
        self.state_path = os.path.join(R.run_dir(cfg), "watchdog.json")
        self.log_path = os.path.join(R.run_dir(cfg), "watchdog_log.jsonl")
        st = R.read_json(self.state_path, {}) or {}
        self.restarts: list[float] = [t for t in st.get("restarts", []) if t > clock() - 30 * 86400]
        self.hw_stops: list[float] = [t for t in st.get("hw_stops", []) if t > clock() - 30 * 86400]
        self.data_notified = False
        self.gave_up = False
        self.hw_mode = "off" if self.feeder.kind == "none" else "feeding"
        self.last_feed = 0.0
        self.last_restart = st.get("last_restart", 0.0)
        self.last_reason = ""
        self.counters = st.get("counters", {"restart": 0, "data": 0, "gave_up": 0, "hw_stop": 0})

    # -------------------------------------------------------- 記錄
    def _event(self, kind: str, detail: str, level: int = 1):
        rec = {"ts": self.clock(), "kind": kind, "level": level, "detail": detail}
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
        (log.warning if level else log.info)("[watchdog] %s: %s", kind, detail)
        if self.notifier and self.wd.get("notify", True) and level:
            try:
                self.notifier.send(level, _("看門狗：{d}", d=detail), None, "系統", self.clock(), f"wd:{kind}")
            except Exception as e:
                log.warning("notify: %s", e)

    def save(self):
        R.write_json(self.state_path, {
            "ts": self.clock(), "restarts": self.restarts[-50:], "hw_stops": self.hw_stops[-50:],
            "last_restart": self.last_restart, "last_reason": self.last_reason, "hw": self.feeder.kind,
            "hw_mode": self.hw_mode, "gave_up": self.gave_up, "counters": self.counters,
            "maintenance": maintenance(self.cfg, self.clock()), "pid": os.getpid()})

    # -------------------------------------------------------- 判斷
    def files(self) -> list[str]:
        if self.status_files is not None:
            return self.status_files
        d = R.run_dir(self.cfg)
        if self.cfg["system"].get("role") in ("edge", "central"):
            return [os.path.join(d, "status__supervisor.json")]
        return [R.status_path(self.cfg)]

    def judge(self) -> tuple[str, str]:
        """回傳（狀態, 原因）：ok｜stuck（要重啟）｜data（資料中斷，通知）｜starting（寬限期）。"""
        now = self.clock()
        wd = self.wd
        in_grace = now - max(self.started, self.last_restart or 0) < float(wd["grace_s"])
        for path in self.files():
            st = R.read_json(path)
            if not st:
                return ("starting", _("還沒有狀態檔")) if in_grace else ("stuck", _("找不到狀態檔 {p}", p=os.path.basename(path)))
            age = now - float(st.get("ts", 0))
            if age > float(wd["stale_s"]):
                return ("starting", "") if in_grace else ("stuck", _("狀態檔 {s:.0f} 秒沒更新（程式卡住或已停止）", s=age))
            loop = st.get("loop_ts")
            if loop and now - float(loop) > float(wd["loop_stale_s"]):
                return ("starting", "") if in_grace else ("stuck", _("擷取迴圈 {s:.0f} 秒沒有轉", s=now - float(loop)))
            last = st.get("last_ts")
            planned = float((st.get("downtime") or {}).get("until") or 0) > now     # 計畫停機中：不報資料中斷
            if (st.get("role") != "supervisor") and float(wd["data_stale_s"]) > 0 and not in_grace and not planned and (
                    last is None or now - float(last) > float(wd["data_stale_s"])):
                return "data", _("{robot} 超過 {m:.0f} 分鐘沒有收到機器人資料", robot=st.get("robot", ""),
                                 m=(now - float(last)) / 60 if last else float(wd["data_stale_s"]) / 60)
            disk = (st.get("disk") or {}).get("free_pct")
            if disk is not None and disk < float(self.cfg["system"]["disk_critical_pct"]):
                return "data", _("磁碟只剩 {p}%", p=disk)
        return ("starting", "") if in_grace else ("ok", "")

    # -------------------------------------------------------- 動作
    def _restart(self):
        cmd = default_restart_cmd(self.cfg)
        log.warning("[watchdog] restart: %s", cmd)
        subprocess.run(cmd, timeout=120, capture_output=True)

    def tick(self) -> str:
        now = self.clock()
        wd = self.wd
        m = maintenance(self.cfg, now)
        state, why = self.judge()
        if m:
            self._hw_maintenance(True)
            self.save()
            return "maintenance"
        self._hw_maintenance(False)
        healthy = state in ("ok", "starting", "data")
        if state == "stuck" or (state == "data" and wd.get("restart_on_data_stale") and not self.data_notified):
            self.restarts = [t for t in self.restarts if t > now - float(wd["window_s"])] + []
            recent = [t for t in self.restarts if t > now - float(wd["window_s"])]
            if len(recent) < int(wd["max_restarts"]):
                self.restarts.append(now)
                self.last_restart, self.last_reason = now, why
                self.counters["restart"] = self.counters.get("restart", 0) + 1
                self._event("restart", _("DTM 沒有回應，已自動重啟服務（原因：{why}）", why=why), 1)
                try:
                    self.restart_fn()
                except Exception as e:
                    self._event("restart_fail", _("重啟指令失敗：{e}", e=e), 2)
                self.gave_up = False
                healthy = True                     # 剛重啟：寬限期內照樣餵狗
            elif not self.gave_up:
                self.gave_up = True
                self.counters["gave_up"] = self.counters.get("gave_up", 0) + 1
                self._event("gave_up", _("{n} 次自動重啟仍無法恢復（{why}），已停止自動重啟，請人工處理",
                                         n=len(recent), why=why), 2)
                healthy = False
            else:
                healthy = False
        elif state == "data":
            if not self.data_notified:
                self.data_notified = True
                self.counters["data"] = self.counters.get("data", 0) + 1
                self._event("data", why, 2)
        else:
            if self.data_notified and state == "ok":
                self._event("recovered", _("資料已恢復"), 0)
            self.data_notified = False
            if state == "ok":
                self.gave_up = False
        self._hw(healthy, now)
        self.save()
        return state

    def _hw_maintenance(self, on: bool):
        if self.feeder.kind == "none":
            return
        try:
            if on and self.hw_mode != "paused":
                if self.feeder.can_disable:
                    self.feeder.disable()
                    self.hw_mode = "paused"
                    self._event("hw_pause", _("維護模式：硬體看門狗已暫停"), 0)
                else:
                    self.feeder.feed()              # 不能暫停的就一直餵
            elif not on and self.hw_mode == "paused":
                self.feeder.enable()
                self.hw_mode = "feeding"
                self._event("hw_resume", _("維護模式結束：硬體看門狗已恢復"), 0)
        except Exception as e:
            self.feeder.error = str(e)

    def _hw(self, healthy: bool, now: float):
        if self.feeder.kind == "none" or self.hw_mode == "paused":
            return
        hw = self.wd.get("hw", {})
        self.hw_stops = [t for t in self.hw_stops if t > now - 86400 * 30]
        today = [t for t in self.hw_stops if t > now - 86400]
        if not healthy and len(today) >= int(hw.get("max_reboots_day", 3)):
            if self.hw_mode != "forced":
                self.hw_mode = "forced"
                self._event("hw_forced", _("今天已因看門狗重開機 {n} 次，改為持續餵狗、不再重開機，請人工處理",
                                           n=len(today)), 2)
            healthy = True
        if healthy:
            if now - self.last_feed >= float(hw.get("feed_s", 20)):
                try:
                    self.feeder.feed()
                    self.last_feed = now
                    self.feeder.error = ""
                    if self.hw_mode == "stopped":
                        self.hw_mode = "feeding"
                except Exception as e:
                    self.feeder.error = str(e)
        elif self.hw_mode != "stopped":
            self.hw_mode = "stopped"
            self.hw_stops.append(now)
            self.counters["hw_stop"] = self.counters.get("hw_stop", 0) + 1
            self._event("hw_stop", _("程式救不回來，停止餵硬體看門狗，電腦將由硬體重開機"), 2)

    def run(self, stop: threading.Event):
        try:
            if self.feeder.kind != "none" and not maintenance(self.cfg):
                self.feeder.enable()
        except Exception as e:
            self.feeder.error = str(e)
            log.error("hw watchdog: %s", e)
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:
                log.exception("watchdog tick: %s", e)
            stop.wait(min(float(self.wd["interval_s"]), float(self.wd.get("hw", {}).get("feed_s", 20))))


def watchdog_summary(cfg: dict) -> dict:
    """給儀表板、狀態檔、中央用的看門狗摘要（近 30 天各層觸發次數）。"""
    st = R.read_json(os.path.join(R.run_dir(cfg), "watchdog.json"), {}) or {}
    events = []
    path = os.path.join(R.run_dir(cfg), "watchdog_log.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f.readlines()[-500:]:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    since = time.time() - 30 * 86400
    ev30 = [e for e in events if e.get("ts", 0) >= since]
    count = lambda k: sum(1 for e in ev30 if e.get("kind") == k)  # noqa: E731
    return {"running": bool(st) and time.time() - float(st.get("ts", 0)) < 300, "ts": st.get("ts"),
            "hw": st.get("hw", "none"), "hw_mode": st.get("hw_mode"), "gave_up": st.get("gave_up", False),
            "maintenance": st.get("maintenance") or maintenance(cfg),
            "restarts_30d": count("restart"), "hw_stops_30d": count("hw_stop"), "data_30d": count("data"),
            "last": ev30[-1] if ev30 else None, "events": ev30[-20:]}


def run_watchdog(cfg: dict, a=None):
    from .notify import Notifier
    from .store import Store
    R.install_crash_handlers(cfg)
    notifier = None
    try:
        if cfg["watchdog"].get("notify", True):
            notifier = Notifier(cfg, Store(cfg["storage"]["db"]) if cfg["system"]["role"] != "central" else None,
                                gate=None)
    except Exception as e:
        log.warning("watchdog notifier: %s", e)
    wd = Watchdog(cfg, notifier)
    stop = threading.Event()
    log.info(_("看門狗啟動：每 %s 秒檢查，硬體看門狗：%s"), cfg["watchdog"]["interval_s"], wd.feeder.kind)
    R.on_stop_signals(stop.set)
    wd.run(stop)
    # 看門狗服務正常停止時（例如安裝更新前手動停服務），暫停硬體看門狗，避免被硬體重開機
    try:
        if wd.feeder.kind != "none" and wd.feeder.can_disable:
            wd.feeder.disable()
    except Exception:
        pass


def which_python() -> str:
    return sys.executable or shutil.which("python") or "python"

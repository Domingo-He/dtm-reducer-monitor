"""執行期的穩定性工具：路徑、狀態檔、崩潰紀錄、快速失敗、自我檢查、磁碟空間、主機資源。

狀態檔（run/status_<機器人>.json）每幾秒更新一次，內容是這台機器人的即時摘要：
- 看門狗用它判斷 DTM 是不是「真的還活著」（不是只看程式在不在）；
- 邊緣主機用它做全區總覽、同步到中央，不必再去問每個程式。
"""
from __future__ import annotations

import ctypes
import faulthandler
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime

from .i18n import _, templates

log = logging.getLogger("dtm.runtime")


# ------------------------------------------------------------ 路徑
def base_dir(cfg: dict) -> str:
    p = cfg.get("_path") or ""
    return os.path.dirname(os.path.abspath(p)) if p else os.getcwd()


def resolve(cfg: dict, rel: str) -> str:
    if not rel:
        return ""
    return rel if os.path.isabs(rel) else os.path.join(base_dir(cfg), rel)


def run_dir(cfg: dict) -> str:
    d = resolve(cfg, cfg["system"].get("run_dir", "run"))
    os.makedirs(d, exist_ok=True)
    return d


def log_dir(cfg: dict) -> str:
    d = resolve(cfg, cfg["system"].get("log_dir", "logs"))
    os.makedirs(d, exist_ok=True)
    return d


def slug(text: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", str(text), flags=re.UNICODE).strip("_")
    return s or "robot"


def robot_id(cfg: dict) -> str:
    return slug(cfg["robot"].get("id") or cfg["robot"].get("name") or "robot")


def status_path(cfg: dict, rid: str | None = None) -> str:
    return os.path.join(run_dir(cfg), f"status_{rid or robot_id(cfg)}.json")


def write_json(path: str, obj) -> None:
    """先寫暫存檔再改名，讀的人永遠不會讀到寫一半的檔案。"""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, default=str)
    for _i in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:               # Windows：別人剛好開著讀，稍等再試
            time.sleep(0.05)
    os.replace(tmp, path)


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ------------------------------------------------------------ 崩潰紀錄與快速失敗
_CFG: dict = {}


def crash_log(cfg: dict | None, title: str, text: str = "") -> str:
    cfg = cfg or _CFG
    d = log_dir(cfg) if cfg else os.getcwd()
    path = os.path.join(d, f"crash_{datetime.now():%Y%m%d_%H%M%S}.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {title}\n")
        f.write(f"python {sys.version}\npid {os.getpid()}\nargv {sys.argv}\n\n")
        f.write(text or "".join(traceback.format_stack()))
        f.write("\n")
    return path


def install_crash_handlers(cfg: dict):
    """未處理的例外寫成 logs/crash_*.log；Python 本身當掉（例如記憶體錯誤）由 faulthandler 寫 faulthandler.log。"""
    _CFG.clear()
    _CFG.update(cfg)
    try:
        fh = open(os.path.join(log_dir(cfg), "faulthandler.log"), "a", encoding="utf-8")
        faulthandler.enable(file=fh, all_threads=True)
    except OSError:
        pass

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return sys.__excepthook__(exc_type, exc, tb)
        path = crash_log(cfg, _("未處理的例外：{e}", e=exc), "".join(traceback.format_exception(exc_type, exc, tb)))
        log.critical(_("程式發生未處理的錯誤，已寫入 %s"), path)

    def thook(args):
        if args.exc_type is SystemExit:
            return
        path = crash_log(cfg, _("執行緒 {name} 發生未處理的例外：{e}", name=args.thread.name if args.thread else "?",
                                e=args.exc_value),
                         "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        log.critical(_("執行緒錯誤，已寫入 %s"), path)

    sys.excepthook = hook
    threading.excepthook = thook


def fatal(cfg: dict, msg: str, code: int = 3):
    """快速失敗：記錄原因後整個程式結束，交給 Windows 服務（WinSW）或 systemd 自動重啟。
    不讓程式停在「網頁還開著、但已經沒在收資料」的半死狀態。"""
    path = crash_log(cfg, msg)
    log.critical(_("%s；程式結束，交給服務管理員自動重啟（紀錄：%s）"), msg, path)
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass
    os._exit(code)


def watch_threads(cfg: dict, threads: dict, stopping: threading.Event, every: float = 5.0,
                  allow_exit=None) -> threading.Thread:
    """監看重要執行緒：任何一個意外停止（不是正常關閉、也不是資料來源正常結束）就快速失敗。"""
    def run():
        while not stopping.wait(every):
            for name, t in threads.items():
                if t is not None and not t.is_alive() and not stopping.is_set() and not (allow_exit and allow_exit()):
                    fatal(cfg, _("{name} 執行緒意外停止", name=name))
    th = threading.Thread(target=run, name="dtm-guard", daemon=True)
    th.start()
    return th


def on_stop_signals(fn):
    """註冊 Ctrl+C、停止服務（SIGTERM、Windows 的 Ctrl+Break）。
    處理函式只啟動一次、而且放到另一個執行緒做：重複的訊號（服務管理員或 timeout 連送兩次）直接忽略，
    避免在 Event.set() 進行到一半時又重入而互鎖（實測過會卡死，服務停不下來）。"""
    import signal
    fired: list = []

    def handler(*_a):
        if fired:
            return
        fired.append(1)
        threading.Thread(target=fn, name="dtm-stop", daemon=True).start()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handler)
    return handler


def disable_quickedit():
    """Windows 命令列視窗的「快速編輯模式」：有人在視窗裡點一下，程式就暫停到按 Enter 為止。關掉它。"""
    if os.name != "nt":
        return False
    try:
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)                 # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        new = (mode.value & ~0x0040) | 0x0080     # 去掉 ENABLE_QUICK_EDIT_MODE，加上 ENABLE_EXTENDED_FLAGS
        return bool(k32.SetConsoleMode(h, new))
    except Exception:
        return False


# ------------------------------------------------------------ 磁碟與主機資源
def disk_info(path: str) -> dict:
    d = os.path.dirname(os.path.abspath(path)) or "."
    try:
        u = shutil.disk_usage(d)
    except OSError:
        return {"free_pct": None, "free_gb": None, "total_gb": None}
    return {"free_pct": round(u.free / u.total * 100, 1) if u.total else None,
            "free_gb": round(u.free / 1e9, 1), "total_gb": round(u.total / 1e9, 1)}


_cpu_prev: list = []


def host_stats() -> dict:
    """CPU、記憶體、開機時間（Windows 用系統 API、Linux 讀 /proc；取不到就給 None）。"""
    out = {"cpu_pct": None, "mem_pct": None, "uptime_s": None, "host": os.environ.get("COMPUTERNAME")
           or (os.uname().nodename if hasattr(os, "uname") else "")}
    try:
        if os.name == "nt":
            class MEM(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MEM()
            m.dwLength = ctypes.sizeof(MEM)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            out["mem_pct"] = float(m.dwMemoryLoad)
            out["uptime_s"] = ctypes.windll.kernel32.GetTickCount64() / 1000.0
            idle, kern, user = (ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong())
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
            cur = (idle.value, kern.value + user.value)
        else:
            with open("/proc/meminfo") as f:
                mi = {ln.split(":")[0]: float(ln.split()[1]) for ln in f if ":" in ln}
            out["mem_pct"] = round(100 * (1 - mi.get("MemAvailable", 0) / mi["MemTotal"]), 1)
            with open("/proc/uptime") as f:
                out["uptime_s"] = float(f.read().split()[0])
            with open("/proc/stat") as f:
                v = [float(x) for x in f.readline().split()[1:]]
            cur = (v[3] + (v[4] if len(v) > 4 else 0), sum(v))
        if _cpu_prev:
            di, dt = cur[0] - _cpu_prev[0], cur[1] - _cpu_prev[1]
            if dt > 0:
                out["cpu_pct"] = round(max(0.0, min(100.0, 100 * (1 - di / dt))), 1)
        _cpu_prev[:] = cur
    except Exception:
        pass
    return out


# ------------------------------------------------------------ 未正常結束偵測與自我檢查
class RunMarker:
    """啟動時建立 <資料庫>.running，正常結束時刪除；啟動時發現它還在 = 上次沒有正常結束（斷電、強制結束）。"""

    def __init__(self, db: str):
        self.path = os.path.abspath(db) + ".running"

    def start(self) -> bool:
        unclean = os.path.exists(self.path)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {time.time()}\n")
        return unclean

    def stop(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


def db_quick_check(path: str, timeout: float = 60.0) -> tuple[bool, str]:
    if not os.path.exists(path):
        return True, _("資料庫尚未建立")
    try:
        c = sqlite3.connect(path, timeout=timeout)
        try:
            res = [r[0] for r in c.execute("PRAGMA quick_check").fetchall()]
        finally:
            c.close()
    except sqlite3.DatabaseError as e:
        return False, str(e)
    return (res == ["ok"]), "; ".join(res[:3])


def self_check(cfg: dict, unclean: bool = False, force_db: bool = False, db: str | None = None) -> list[dict]:
    """啟動自我檢查。level：ok／warn／error；有 error 時不應該啟動（資料庫損壞時繼續寫只會更糟）。
    db：要檢查的資料庫（邊緣主機、中央伺服器用自己的；預設是機器人的 [storage] db）。"""
    out = []
    db = os.path.abspath(db or cfg["storage"]["db"])
    stamp = db + ".checked"
    last = os.path.getmtime(stamp) if os.path.exists(stamp) else 0
    if unclean:
        out.append({"name": _("上次結束"), "level": "warn", "detail": _("上次沒有正常結束（斷電或強制結束），已檢查資料庫")})
    if unclean or force_db or time.time() - last > 7 * 86400:
        t0 = time.time()
        ok, detail = db_quick_check(db)
        out.append({"name": _("資料庫完整性"), "level": "ok" if ok else "error",
                    "detail": (_("正常（{s:.1f} 秒）", s=time.time() - t0) if ok else
                               _("資料庫損壞：{d}。請用 restore 指令從備份還原", d=detail))})
        if ok:
            try:
                with open(stamp, "w") as f:
                    f.write(str(time.time()))
            except OSError:
                pass
    di = disk_info(db)
    sc = cfg["system"]
    if di["free_pct"] is not None:
        lv = "error" if di["free_pct"] < float(sc["disk_critical_pct"]) else \
            "warn" if di["free_pct"] < float(sc["disk_alarm_pct"]) else "ok"
        out.append({"name": _("磁碟空間"), "level": lv,
                    "detail": _("剩 {pct}%（{gb} GB）", pct=di["free_pct"], gb=di["free_gb"])})
    d = os.path.dirname(db) or "."
    if not os.access(d, os.W_OK):
        out.append({"name": _("寫入權限"), "level": "error", "detail": _("資料夾沒有寫入權限：{d}", d=d)})
    if cfg.get("_path") and os.path.exists(cfg["_path"]) and not cfg.get("_demo"):
        try:
            from .check import ERROR, WARN, run_check
            rep = run_check(cfg["_path"])
            e, w = rep.count(ERROR), rep.count(WARN)
            out.append({"name": _("設定檔"), "level": "error" if e else "warn" if w else "ok",
                        "detail": _("錯誤 {e}、警告 {w}", e=e, w=w) + (
                            _("：") + _("；").join(m for lv, _w, m in rep.items if lv == ERROR)[:300] if e else "")})
        except Exception as ex:                   # 檢查本身出錯不擋啟動
            out.append({"name": _("設定檔"), "level": "warn", "detail": str(ex)})
    return out


# ------------------------------------------------------------ 狀態檔
def service_summary(svc) -> dict:
    """單台機器人的即時摘要（狀態檔內容）。"""
    from . import __version__
    from .analysis import LEVEL_NAMES
    cfg = svc.cfg
    now = time.time()
    st = dict(svc.status)
    axes = []
    for ax in svc.axes:
        h = svc.health.get(ax) or {}
        mh = svc.motion_health.get(ax) or {}
        d = svc.decisions.get(ax) or {}
        mets = {m: {"ratio": (h.get("metrics") or {}).get(m, {}).get("ratio"),
                    "z": (h.get("metrics") or {}).get(m, {}).get("z"),
                    "level": (h.get("metrics") or {}).get(m, {}).get("level", 0),
                    "value": svc.conv.feature(ax, m, (h.get("metrics") or {}).get(m, {}).get("value")),
                    "base": svc.conv.feature(ax, m, (h.get("metrics") or {}).get(m, {}).get("base"))}
                for m in ("F", "Fc", "Fv", "ripple")}
        base_f = (h.get("baseline") or {}).get("F")
        series = []
        if base_f:
            series = [[round(p["ts"], 0), round(p["F"] / base_f, 4) if p.get("F") else None, p["level"],
                       p.get("temp") if "temp" in p else None] for p in (h.get("series") or [])[-400:]]
        axes.append({
            "axis": ax, "name": h.get("name") or str(ax), "level": h.get("level", -1),
            "level_name": LEVEL_NAMES.get(h.get("level", -1)), "baseline_ready": h.get("baseline_ready", False),
            "n_runs": h.get("n_runs", 0), "need_runs": h.get("need_runs"), "ratio_F": mets["F"]["ratio"],
            "metrics": mets, "days_to_caution": h.get("days_to_caution"), "days_to_alert": h.get("days_to_alert"),
            "slope_pct_month": h.get("slope_pct_month"), "last_ts": h.get("last_ts"), "temp": h.get("temp"),
            "reasons": h.get("reasons", []), "hint": h.get("hint", ""), "unit": svc.conv.unit(ax),
            "series": series,
            "motion": {"level": mh.get("level", -1), "ratio": mh.get("ratio"), "vib_ratio": mh.get("vib_ratio"),
                       "days_to_alert": mh.get("days_to_alert"),
                       "codes": [{"code": c["code"], "name": c["name"], "level": c["level"], "ratio": c["ratio"],
                                  "vib_ratio": c["vib_ratio"], "days": c["days"], "today_n": c.get("today_n", 0),
                                  "days_to_alert": c["days_to_alert"]} for c in mh.get("codes", [])]},
            "decision": _decision_brief(d),
        })
        axes[-1]["tmpl"] = templates({k: axes[-1][k] for k in ("level_name", "reasons", "hint", "decision")})
    alarms = [{"id": a["id"], "ts": a["ts"], "axis": a["axis"], "kind": a["kind"], "akey": a["akey"],
               "level": a["level"], "message": a["message"], "acked": a["acked"], "acked_by": a.get("acked_by"),
               "tmpl": a.get("tmpl")}
              for a in svc.store.active_alarms()]
    db = cfg["storage"]["db"]
    try:
        db_mb = round(os.path.getsize(db) / 1e6, 1)
    except OSError:
        db_mb = None
    return {
        "robot": cfg["robot"]["name"], "id": robot_id(cfg), "line": cfg["robot"].get("line", ""),
        "model": cfg["robot"].get("model", ""), "impact": cfg["robot"].get("impact", "中"),
        "site_id": cfg["system"].get("site_id", ""), "site_name": cfg["system"].get("site_name", ""),
        "version": __version__, "pid": os.getpid(), "ts": now, "started": st.get("started"),
        "loop_ts": st.get("loop_ts"), "last_ts": st.get("last_ts"), "connected": st.get("connected"),
        "error": st.get("error", ""), "source": cfg["robot"]["source"], "samples": st.get("samples", 0),
        "cycles": st.get("cycles", 0), "last_cycle_ts": st.get("last_cycle_ts"), "port": cfg["web"]["port"],
        "poll_hz": cfg["robot"]["poll_hz"], "axes": axes, "alarms": alarms,
        "disk": disk_info(db), "db_mb": db_mb, "selfcheck": getattr(svc, "selfcheck", []),
        "sensors": svc.sensors.status() if getattr(svc, "sensors", None) else None,
        "backup": dict(svc.backup.info) if getattr(svc, "backup", None) else None,
        "hours": (svc.store.hours() or [{}])[-1].get("hours"), "hours_rate": None,
        "downtime": svc.downtime() if hasattr(svc, "downtime") else None,
        "clock_offset_s": (read_json(os.path.join(run_dir(cfg), "central_sync.json"), {}) or {}).get("clock_offset_s"),
    }


def _decision_brief(d: dict) -> dict:
    if not d:
        return {}
    return {"primary": d.get("primary"), "drive_label": d.get("drive_label"), "drive_comp": d.get("drive_comp"),
            "scores": d.get("scores"),
            **{k: {"level": (d.get(k) or {}).get("level", 0), "action": (d.get(k) or {}).get("action", "")}
               for k in ("grease", "drive", "motor")},
            "rul_days": (d.get("drive") or {}).get("rul_days"),
            "families": {k: v for k, v in (d.get("families") or {}).items()},
            "life": {k: {"age_h": ((d.get(k) or {}).get("life") or {}).get("age_h"),
                         "b10_h": ((d.get(k) or {}).get("life") or {}).get("b10_h"),
                         "install_ts": ((d.get(k) or {}).get("life") or {}).get("install_ts")}
                     for k in ("drive", "motor")},
            "grease_due_ts": (d.get("grease") or {}).get("due_ts"),
            "grease_frac": ((d.get("grease") or {}).get("age") or {}).get("frac"),
            "grease_temp_factor": ((d.get("grease") or {}).get("age") or {}).get("temp_factor"),
            "grease_interval_adj_h": ((d.get("grease") or {}).get("age") or {}).get("interval_adj_h"),
            "iron_wt": (d.get("iron") or {}).get("latest_wt"),
            "iron_knee": bool((d.get("iron") or {}).get("past_knee")),
            "iron_next_ts": (d.get("iron") or {}).get("next_sample_ts"),
            "iron_interval_days": (d.get("iron") or {}).get("sample_interval_days"),
            "thermal": d.get("thermal"), "trend_accel": d.get("trend_accel"),
            "next": d.get("next", []), "notes": d.get("notes", [])}


class StatusWriter:
    """每 status_every_s 秒寫一次狀態檔；寫失敗只記錄、不讓程式停。"""

    def __init__(self, svc, path: str | None = None):
        self.svc = svc
        self.path = path or status_path(svc.cfg)
        self.every = float(svc.cfg["system"].get("status_every_s", 5.0))
        self.thread: threading.Thread | None = None

    def write_once(self) -> dict:
        s = service_summary(self.svc)
        write_json(self.path, s)
        return s

    def run(self, stop: threading.Event):
        while not stop.is_set():
            try:
                self.write_once()
            except Exception as e:
                log.warning(_("狀態檔寫入失敗：%s"), e)
            stop.wait(self.every)

    def start(self, stop: threading.Event) -> threading.Thread:
        self.thread = threading.Thread(target=self.run, args=(stop,), name="dtm-status", daemon=True)
        self.thread.start()
        return self.thread

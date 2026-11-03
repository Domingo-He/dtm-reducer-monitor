"""邊緣主機：一台電腦管理一個廠區的多台機器人（[system] role = "edge"）。

設計（沿用「一台機器人一個程式、一個資料庫」）：
- 主程式（監督者）依 [[robots]] 清單，替每台機器人啟動一個獨立的 DTM 程式（python -m dtm --robot 代碼 run），
  各自讀取、判斷、存資料庫、開自己的儀表板；一台出問題不會拖累其他台。
- 監督者每 5 秒看一次：程式結束了就重啟（每台每小時最多 max_restarts 次，超過就停止並通知），
  狀態檔太久沒更新（卡住）就強制結束再重啟。
- 監督者自己開「廠區儀表板」（全區熱圖、警報、系統健康），並負責把資料同步到中央。
"""
from __future__ import annotations

import copy
import logging
import os
import secrets
import subprocess
import sys
import threading
import time

from .config import E1, _merge
from .i18n import N_, _
from . import runtime as R

log = logging.getLogger("dtm.fleet")
ROBOT_KEYS = {"id", "name", "line", "host", "axes", "e1", "port", "impact", "model", "source", "enabled", "note"}


def robots(cfg: dict) -> list[dict]:
    return [r for r in (cfg.get("robots") or []) if r.get("enabled", True)]


def apply_robot(cfg: dict, rid: str) -> dict:
    """邊緣主機設定檔 + [[robots]] 其中一台 → 那台機器人自己的完整設定。"""
    lst = robots(cfg)
    idx = next((i for i, r in enumerate(lst) if str(r.get("id")) == str(rid) or str(r.get("name")) == str(rid)), None)
    if idx is None:
        raise SystemExit(_("設定檔的 [[robots]] 裡找不到 {rid}", rid=rid))
    r = lst[idx]
    out = copy.deepcopy(cfg)
    over: dict = {"robot": {"id": str(r.get("id") or rid), "name": r.get("name") or str(r.get("id"))}}
    for k in ("line", "impact", "model", "source"):
        if r.get(k):
            over["robot"][k] = r[k]
    src = over["robot"].get("source") or cfg["robot"]["source"]
    if r.get("host"):
        over.setdefault("source", {}).setdefault(src if src in ("fanuc_http", "modbus") else "fanuc_http", {})["host"] = r["host"]
    if r.get("axes") or r.get("e1"):
        axes = list(r.get("axes") or cfg["axes"]["enabled"])
        if r.get("e1") and E1 not in axes:
            axes.append(E1)
        over["axes"] = {"enabled": axes}
    fl = cfg["fleet"]
    data = R.resolve(cfg, fl.get("data_dir", "data"))
    os.makedirs(data, exist_ok=True)
    rid_s = R.slug(over["robot"]["id"])
    over["storage"] = {"db": os.path.join(data, f"{rid_s}.sqlite3"),
                       "backup_dir": os.path.join(data, "backups", rid_s) if cfg["storage"].get("backup_dir") else ""}
    over["web"] = {"port": int(r.get("port") or int(fl.get("base_port", 8100)) + idx + 1),
                   "host": fl.get("child_host", "0.0.0.0"),
                   "tls_cert": "", "tls_key": ""}      # 每台機器人的程式由邊緣主機在內部轉送，不另外加密（HTTPS 在邊緣主機的畫面）
    for k, v in r.items():                      # 其他區段的個別設定，例如 [robots.axis.2]、[robots.sensors]
        if k not in ROBOT_KEYS and isinstance(v, dict):
            over[k] = _merge(over.get(k, {}), v)
    out = _merge(out, over)
    out["robots"] = []
    out["_robot_index"] = idx
    out["_edge"] = True                          # 由邊緣主機管理（通知平常交給中央）
    return out


class Child:
    def __init__(self, cfg: dict, robot: dict):
        self.robot = robot
        self.id = str(robot.get("id") or robot.get("name"))
        self.cfg = apply_robot(cfg, self.id)
        self.port = int(self.cfg["web"]["port"])
        self.status_path = R.status_path(self.cfg)
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.restarts: list[float] = []
        self.gave_up = False
        self.last_exit: int | None = None
        self.last_reason = ""
        self.next_try = 0.0


class Supervisor:
    def __init__(self, cfg: dict, spawn=None, clock=time.time, notifier=None, alarms=None):
        self.cfg = cfg
        self.fl = cfg["fleet"]
        self.clock = clock
        self.children = [Child(cfg, r) for r in robots(cfg)]
        self.token = os.environ.get("DTM_INTERNAL_TOKEN") or secrets.token_urlsafe(24)
        self.spawn_fn = spawn or self._spawn
        self.alarms = alarms
        self.notifier = notifier
        self.started = clock()
        self.loop_ts = clock()
        self.stop_evt = threading.Event()
        self.status_path = os.path.join(R.run_dir(cfg), "status__supervisor.json")

    # -------------------------------------------------------- 子程式
    def _spawn(self, ch: Child) -> subprocess.Popen:
        conf = os.path.abspath(self.cfg["_path"])
        env = dict(os.environ, DTM_INTERNAL_TOKEN=self.token, PYTHONUTF8="1")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen([sys.executable, "-m", "dtm", "-c", conf, "--robot", ch.id, "run", "--service"],
                                cwd=R.base_dir(self.cfg), env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=flags)

    def start_all(self):
        for ch in self.children:
            self.start(ch, _("啟動"))
            time.sleep(float(self.fl.get("start_stagger_s", 0.5)) if self.spawn_fn == self._spawn else 0)

    def start(self, ch: Child, reason: str):
        try:
            ch.proc = self.spawn_fn(ch)
            ch.started = self.clock()
            ch.last_reason = reason
            log.info(_("%s：%s（pid %s，儀表板連接埠 %s）"), ch.id, reason, getattr(ch.proc, "pid", "?"), ch.port)
        except Exception as e:
            ch.last_reason = _("啟動失敗：{e}", e=e)
            log.error("%s: %s", ch.id, e)

    def kill(self, ch: Child):
        p = ch.proc
        if p is None:
            return
        try:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
        except Exception:
            pass

    def _alarm(self, ch: Child, level: int, msg: str):
        if self.alarms:
            self.alarms.set(f"child:{ch.id}", None, N_("系統"), level, msg, None, self.clock())

    def check(self, ch: Child) -> str:
        """一台機器人程式的狀態：ok｜starting｜restarted｜gave_up｜waiting。"""
        now = self.clock()
        fl = self.fl
        alive = ch.proc is not None and ch.proc.poll() is None
        reason = ""
        if not alive:
            code = ch.proc.poll() if ch.proc is not None else None
            ch.last_exit = code
            reason = _("程式結束（代碼 {c}）", c=code) if ch.proc is not None else _("尚未啟動")
        else:
            st = R.read_json(ch.status_path) or {}
            age = now - float(st.get("ts", 0) or 0)
            if now - ch.started > float(fl["grace_s"]) and (not st or age > float(fl["stale_s"])):
                reason = _("狀態檔 {s:.0f} 秒沒更新（卡住）", s=age if st else now - ch.started)
                self.kill(ch)
        if not reason:
            if ch.gave_up:
                ch.gave_up = False
                self._alarm(ch, 0, "")
            return "ok" if now - ch.started > float(fl["grace_s"]) else "starting"
        if ch.gave_up:
            return "gave_up"
        if now < ch.next_try:
            return "waiting"
        ch.restarts = [t for t in ch.restarts if t > now - float(fl["window_s"])]
        if len(ch.restarts) >= int(fl["max_restarts"]):
            ch.gave_up = True
            msg = _("{robot} 程式 {n} 次自動重啟仍無法恢復（{why}），已停止重啟，請人工處理",
                    robot=ch.cfg["robot"]["name"], n=len(ch.restarts), why=reason)
            log.error(msg)
            self._alarm(ch, 2, msg)
            return "gave_up"
        ch.restarts.append(now)
        ch.next_try = now + min(60.0, 5.0 * len(ch.restarts))
        self.start(ch, _("重啟：{why}", why=reason))
        return "restarted"

    def tick(self) -> dict:
        self.loop_ts = self.clock()
        states = {ch.id: self.check(ch) for ch in self.children}
        self.write_status(states)
        return states

    def write_status(self, states: dict | None = None):
        from . import __version__
        now = self.clock()
        rows = []
        for ch in self.children:
            st = R.read_json(ch.status_path) or {}
            rows.append({"id": ch.id, "name": ch.cfg["robot"]["name"], "port": ch.port,
                         "pid": getattr(ch.proc, "pid", None), "alive": ch.proc is not None and ch.proc.poll() is None,
                         "state": (states or {}).get(ch.id), "restarts_1h": len(ch.restarts), "gave_up": ch.gave_up,
                         "last_exit": ch.last_exit, "last_reason": ch.last_reason, "status_age": now - float(st.get("ts", 0) or 0)
                         if st else None, "connected": st.get("connected"), "last_ts": st.get("last_ts")})
        R.write_json(self.status_path, {"role": "supervisor", "ts": now, "loop_ts": self.loop_ts, "pid": os.getpid(),
                                        "version": __version__, "started": self.started,
                                        "site_id": self.cfg["system"].get("site_id"), "children": rows,
                                        "disk": R.disk_info(R.resolve(self.cfg, self.fl.get("data_dir", "data")))})

    def run(self):
        self.start_all()
        while not self.stop_evt.wait(5.0):
            try:
                self.tick()
            except Exception as e:
                log.exception("supervisor tick: %s", e)

    def stop(self):
        self.stop_evt.set()
        for ch in self.children:
            self.kill(ch)


def run_edge(cfg: dict, a=None):
    """邊緣主機主程式：啟動每台機器人的程式、看顧它們、開廠區儀表板、同步到中央。"""
    from .__main__ import startup_checks
    from .alarms import AlarmManager
    from .notify import Notifier
    from .store import Store
    if not robots(cfg):
        raise SystemExit(_("邊緣主機的設定檔沒有 [[robots]] 清單"))
    if not cfg["system"].get("site_id"):
        raise SystemExit(_("邊緣主機要設定 [system] site_id（廠區代碼，例如 \"P3\"）"))
    data = R.resolve(cfg, cfg["fleet"].get("data_dir", "data"))
    os.makedirs(data, exist_ok=True)
    edge_db = os.path.join(data, "_edge.sqlite3")
    marker, checks, unclean = startup_checks(cfg, a, edge_db)
    store = Store(edge_db)
    sv_cfg = copy.deepcopy(cfg)
    sv_cfg["robot"]["name"] = cfg["system"].get("site_name") or cfg["system"]["site_id"]
    gate_path = os.path.join(R.run_dir(cfg), "central_sync.json")

    hub = not (cfg.get("central") or {}).get("url")

    def gate():
        if hub:                                   # 沒接中央：邊緣自己的警報也交給本機廠區資料庫統一發（避免重複）
            return "none"
        st = R.read_json(gate_path, {}) or {}
        ok = st.get("last_ok")
        return "none" if ok and time.time() - float(ok) < float(cfg["notify"].get("edge_fallback_s", 300)) else "alert_only"
    notifier = Notifier(sv_cfg, store, gate=gate)
    alarms = AlarmManager(store, notifier)
    sup = Supervisor(cfg, notifier=notifier, alarms=alarms)
    store.add_sys_event("start", 1 if unclean else 0, _("邊緣主機啟動，{n} 台機器人", n=len(sup.children)))
    from .sync import EdgeSync
    from .fleetweb import serve_fleet
    sync = EdgeSync(cfg, sup, store)
    hub_notifier = None
    if hub:
        from .central import fleet_summary
        hub_cfg = copy.deepcopy(cfg)
        hub_cfg["robot"]["name"] = cfg["system"].get("site_name") or cfg["system"]["site_id"]
        hub_cfg["system"]["site_name"] = ""
        hub_notifier = Notifier(hub_cfg, sync.local, summary_fn=lambda: fleet_summary(sync.local),
                                is_acked=sync.local.is_acked)
        sync.local.notifier = hub_notifier
        sync.local.hub = True
        log.info(_("沒有設定中央：由這台邊緣主機統一發通知、逐級通報與每日摘要，並計算本廠機群壽命規則"))
    t_sup = threading.Thread(target=sup.run, name="dtm-supervisor", daemon=True)
    t_sync = threading.Thread(target=sync.run, args=(sup.stop_evt,), name="dtm-sync", daemon=True)
    t_sup.start()
    t_sync.start()

    def tick_notify():                            # 邊緣自己的警報（例如機器人程式一直重啟失敗）的逐級通報
        last_rules = 0.0
        while not sup.stop_evt.wait(10.0):
            try:
                notifier.tick()
                if hub_notifier:                  # 小型模式：全廠的逐級通報、每日摘要、本廠壽命規則
                    now = time.time()
                    hub_notifier.tick(now)
                    if now - last_rules > float(cfg["central"].get("rules_every_s", 3600)):
                        last_rules = now
                        R.write_json(sync.rules_path, sync.local.compute_rules(now))
            except Exception as e:
                log.exception("notify tick: %s", e)
    t_nt = threading.Thread(target=tick_notify, name="dtm-edge-notify", daemon=True)
    t_nt.start()
    httpd = serve_fleet(cfg, sync.local, role="edge", notifier=hub_notifier or notifier, supervisor=sup, edge_store=store,
                        sync=sync)
    R.watch_threads(cfg, {_("監督者"): t_sup, _("同步"): t_sync, _("通知"): t_nt}, sup.stop_evt)
    log.info(_("邊緣主機 %s（%s）啟動：%s 台機器人，廠區儀表板 http://%s:%s"), cfg["system"]["site_id"],
             cfg["system"].get("site_name", ""), len(sup.children), "localhost", cfg["web"]["port"])

    def stop():
        log.info(_("停止中…"))
        sup.stop()
        httpd.shutdown()
    R.on_stop_signals(stop)
    try:
        httpd.serve_forever()
    finally:
        sup.stop()
        store.add_sys_event("stop", 0, _("正常結束"))
        marker.stop()

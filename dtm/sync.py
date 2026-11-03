"""邊緣 → 中央同步（單向、由邊緣主動送出）。

- 邊緣主機（或單機）每 interval_s 秒把「上次之後的新資料」打包送到中央：每台機器人的即時狀態（狀態檔）、
  警報（含解除、確認）、診斷特徵、生產循環每日中位數、保養事件、元件壽命、鐵粉、振動、時數，以及主機狀態。
- 每類資料各自記「游標」（送到哪裡了），中央回覆成功才往前推；斷線時資料留在邊緣，恢復後自動補送（不會掉資料）。
- 中央的回覆帶回兩樣東西：
  1. 指令：在中央登錄的保養事件、鐵粉、確認警報…，由邊緣在對應機器人上執行（一份資料只有一個主人：以中央為準）；
  2. 規則：中央用整個機群的更換紀錄算出的壽命模型，邊緣寫入 run/rules.json，各機器人程式自動套用。
- 邊緣主機同時把同一份資料寫進本機的「廠區資料庫」，廠區儀表板用它（斷網也能看）。
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request

from .i18n import N_, _
from . import runtime as R

log = logging.getLogger("dtm.sync")
TABLES = {                      # 資料表 → 游標欄位
    "alarms": "mtime", "features": "id", "events": "mtime", "iron": "id", "vib": "id", "motion_daily": "mtime",
}


def site_id(cfg: dict) -> str:
    return cfg["system"].get("site_id") or "SITE"


def robot_key(cfg: dict) -> str:
    return f"{site_id(cfg)}/{R.robot_id(cfg)}"


class Collector:
    """讀一台機器人的資料庫（唯讀）與狀態檔，依游標取出新資料。"""

    def __init__(self, key: str, db: str, status_path: str, cursor_path: str):
        self.key = key
        self.db = db
        self.status_path = status_path
        self.cursor_path = cursor_path
        self.cursors: dict = R.read_json(cursor_path, {}) or {}

    def _conn(self):
        c = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def collect(self, batch: int = 3000) -> dict:
        out = {"key": self.key, "status": R.read_json(self.status_path) or {}, "cursors": {}}
        if not os.path.exists(self.db):
            return out
        try:
            c = self._conn()
        except sqlite3.Error as e:
            out["error"] = str(e)
            return out
        try:
            have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for t, col in TABLES.items():
                if t not in have:
                    continue
                cur = self.cursors.get(t, 0)
                if t == "alarms":
                    q = "SELECT * FROM alarms WHERE COALESCE(mtime, ts)>? ORDER BY COALESCE(mtime, ts) LIMIT ?"
                elif t == "events":
                    q = "SELECT * FROM events WHERE COALESCE(mtime, ts)>? ORDER BY COALESCE(mtime, ts) LIMIT ?"
                else:
                    q = f"SELECT * FROM {t} WHERE COALESCE({col}, 0)>? ORDER BY {col} LIMIT ?"
                rows = [dict(r) for r in c.execute(q, (cur, batch))]
                if t == "features":
                    for r in rows:
                        r.pop("detail", None)
                out[t] = rows
                if rows:
                    last = rows[-1]
                    out["cursors"][t] = (last.get("mtime") or last["ts"]) if t in ("alarms", "events") else last[col]
            if "components" in have:
                out["components"] = [dict(r) for r in c.execute("SELECT id, robot, axis, comp, install_ts, removed_ts, "
                                                                "life_h, failed, reason FROM components")]
            if "hours" in have:
                h = c.execute("SELECT ts, hours FROM hours ORDER BY ts DESC LIMIT 2").fetchall()
                out["hours"] = [dict(r) for r in h]
            if "cycles" in have:                       # 今天每小時循環數（廠區儀表板的生產節拍圖）
                t0 = time.time() - 24 * 3600
                out["cycles_hourly"] = [dict(r) for r in c.execute(
                    "SELECT CAST(ts/3600 AS INTEGER) AS hour, COUNT(DISTINCT ts) AS n FROM cycles WHERE ts>=? "
                    "GROUP BY hour ORDER BY hour", (t0,))]
        finally:
            c.close()
        return out

    def commit(self, cursors: dict):
        for k, v in (cursors or {}).items():
            if v is not None and v > self.cursors.get(k, 0):
                self.cursors[k] = v
        R.write_json(self.cursor_path, self.cursors)


# ------------------------------------------------------------ 執行中央下發的指令
def execute_command(svc, cmd: dict) -> dict:
    """在一台機器人上執行指令（單機直接呼叫；邊緣主機透過機器人程式的內部 API）。"""
    kind = cmd.get("kind")
    p = cmd.get("payload") or {}
    who = cmd.get("who") or "central"
    try:
        if kind == "event":
            axis = int(p["axis"]) if p.get("axis") not in (None, "", "all") else None
            eid = svc.register_event(float(p.get("ts") or time.time()), axis, p["kind"], str(p.get("note", ""))[:500],
                                     bool(p.get("failed")), str(p.get("teardown", ""))[:1000],
                                     float(p["hours"]) if p.get("hours") not in (None, "") else None, who=who,
                                     code=p.get("code"))
            return {"ok": True, "detail": f"event #{eid}", "edge_id": eid}
        if kind == "undo":
            ev = svc.undo_event(int(p["edge_id"]), who, str(p.get("note", ""))[:300])
            return {"ok": True, "detail": f"undo #{ev['id']}"}
        if kind == "downtime":
            if p.get("cancel"):
                svc.cancel_downtime(who)
                return {"ok": True, "detail": "downtime cancel"}
            d = svc.set_downtime(p.get("until"), str(p.get("note", ""))[:200], who)
            return {"ok": True, "detail": f"downtime until {d['until']:.0f}"}
        if kind == "teardown":
            svc.set_teardown(int(p["edge_id"]), p.get("form") or {}, who)
            return {"ok": True, "detail": f"teardown #{p['edge_id']}"}
        if kind == "iron":
            svc.add_iron(float(p.get("ts") or time.time()), int(p["axis"]), float(p["wt_pct"]), p.get("sample") or svc.cfg["iron"].get("default_sample", "drain_mid"),
                         str(p.get("note", ""))[:300])
            return {"ok": True, "detail": "iron"}
        if kind == "vib":
            svc.add_vib(float(p.get("ts") or time.time()), int(p["axis"]), p.get("direction", "radial"),
                        {k: float(v) for k, v in (p.get("values") or {}).items()}, p.get("temp"), p.get("source", "central"))
            return {"ok": True, "detail": "vib"}
        if kind == "hours":
            svc.store.add_hours(float(p.get("ts") or time.time()), float(p["hours"]))
            return {"ok": True, "detail": "hours"}
        if kind == "ack":
            svc.store.ack_alarm(int(p["edge_id"]), who)
            return {"ok": True, "detail": "ack"}
        return {"ok": False, "detail": _("不支援的指令：{k}", k=kind)}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


def apply_rules(cfg: dict, rules: dict | None) -> bool:
    """中央下發的壽命模型（機群 Weibull）→ 本機設定的先驗；自己的失效紀錄足夠時仍以自己的為準。"""
    if not rules:
        return False
    pr = (rules.get("life_prior") or {})
    changed = False
    for comp, m in pr.items():
        if m and float(m.get("beta", 0)) > 0 and float(m.get("eta_h", 0)) > 0:
            cur = cfg["life"]["prior"].get(comp, {})
            if (cur.get("beta"), cur.get("eta_h")) != (m["beta"], m["eta_h"]):
                cfg["life"]["prior"][comp] = {"beta": float(m["beta"]), "eta_h": float(m["eta_h"]),
                                              "from": rules.get("scope") or "central"}
                changed = True
    return changed


class RulesWatcher:
    """機器人程式定期看 run/rules.json（中央下發、邊緣主機寫入）；有更新就套用並重算判定。"""

    def __init__(self, svc):
        self.svc = svc
        self.path = os.path.join(R.run_dir(svc.cfg), "rules.json")
        self.mtime = 0.0

    def tick(self) -> bool:
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return False
        if m == self.mtime:
            return False
        self.mtime = m
        rules = R.read_json(self.path)
        if not apply_rules(self.svc.cfg, rules):
            return False
        for ax in self.svc.axes:
            self.svc.refresh_decision(ax, raise_alarms=False)
        self.svc.store.add_sys_event("rules", 0, _("套用{src}壽命模型：{c}", src=_("本廠") if rules.get("scope") == "site" else _("中央"), c=", ".join(
            f"{k} β{v['beta']:g} η{v['eta_h']:g}h" for k, v in (rules.get("life_prior") or {}).items())))
        log.info(_("已套用中央下發的壽命模型"))
        return True


def run_robot_side(svc, stop: threading.Event):
    """機器人程式的附屬工作：套用中央規則；單機直接接中央時負責上傳。"""
    cfg = svc.cfg
    rw = RulesWatcher(svc)
    ss = StandaloneSync(svc) if cfg["central"].get("url") and cfg["system"].get("role", "standalone") == "standalone" \
        and not cfg.get("_edge") else None
    every = float(cfg["central"].get("interval_s", 60))
    last = 0.0
    stop.wait(5.0)
    while not stop.is_set():
        try:
            rw.tick()
        except Exception as e:
            log.exception("rules: %s", e)
        if ss and time.time() - last >= every:
            last = time.time()
            try:
                ss.tick()
            except Exception as e:
                log.exception("sync: %s", e)
        stop.wait(5.0)


# ------------------------------------------------------------ HTTP
class Client:
    def __init__(self, cfg: dict):
        c = cfg["central"]
        from .notify import secret
        self.url = c["url"].rstrip("/") + "/api/sync"
        self.token = secret(c.get("token", "")) or ""
        ctx = None
        if self.url.startswith("https"):
            ctx = ssl.create_default_context(cafile=R.resolve(cfg, c["ca_file"]) if c.get("ca_file") else None)
        self.ctx = ctx
        proxy = cfg["notify"].get("proxy", "")
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else [
            urllib.request.ProxyHandler({})]
        if ctx is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)

    def post(self, payload: dict, timeout: float = 60) -> dict:
        body = gzip.compress(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Content-Encoding": "gzip", "Authorization": f"Bearer {self.token}"})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} {e.read()[:200].decode('utf-8', 'replace')}") from e


def host_payload(cfg: dict, role: str, extra: dict | None = None) -> dict:
    from . import __version__
    from .watchdog import watchdog_summary
    return {"site_id": site_id(cfg), "site_name": cfg["system"].get("site_name", ""), "role": role,
            "version": __version__, "sent_ts": time.time(), "host": R.host_stats(),
            "disk": R.disk_info(cfg["storage"]["db"]), "watchdog": watchdog_summary(cfg), **(extra or {})}


class SyncState:
    """同步狀態（給儀表板與備援閘門用）：run/central_sync.json"""

    def __init__(self, cfg: dict):
        self.path = os.path.join(R.run_dir(cfg), "central_sync.json")
        self.st = R.read_json(self.path, {}) or {}

    def ok(self, n: int, ms: float, offset: float | None = None):
        self.st.update(last_ok=time.time(), last_try=time.time(), last_error="", last_rows=n, last_ms=round(ms))
        if offset is not None:
            self.st["clock_offset_s"] = round(offset, 1)
        R.write_json(self.path, self.st)


def clock_offset(t_send: float, t_recv: float, resp: dict) -> float | None:
    """本機時間 − 中央時間（秒，正值＝本機較快）；用送出與收到的中點估計，網路延遲的影響只有一半。"""
    st = resp.get("server_time")
    return None if st is None else (t_send + t_recv) / 2 - float(st)


def check_clock(cfg: dict, alarms, offset: float | None, now: float | None = None):
    """跟中央的時間差超過 [central] clock_drift_s（預設 60 秒）→ 警報；回到範圍內自動解除。"""
    if offset is None or alarms is None:
        return
    limit = float((cfg.get("central") or {}).get("clock_drift_s", 60))
    bad = limit > 0 and abs(offset) > limit
    msg = _("電腦時間跟中央差 {s:+.0f} 秒（超過 {lim:.0f} 秒）：趨勢與警報時間會錯，請校正這台電腦的時間（Windows 時間同步／NTP）",
            s=offset, lim=limit) if bad else ""
    alarms.set("clock", None, N_("系統"), 1 if bad else 0, msg, round(offset, 1), now or time.time())

    def fail(self, err: str):
        self.st.update(last_try=time.time(), last_error=err[:300])
        R.write_json(self.path, self.st)


# ------------------------------------------------------------ 邊緣主機（多台）
class EdgeSync:
    """邊緣主機的同步：讀每台機器人的資料庫與狀態檔 → 寫進本機廠區資料庫 → 送中央 → 執行回覆的指令。"""

    def __init__(self, cfg: dict, sup, edge_store, local=None):
        from .central import CentralStore
        self.cfg = cfg
        self.sup = sup
        self.edge_store = edge_store
        d = R.resolve(cfg, cfg["fleet"].get("data_dir", "data"))
        self.local = local or CentralStore(os.path.join(d, "_fleet.sqlite3"), cfg, local=True)
        self.state = SyncState(cfg)
        self.client = Client(cfg) if cfg["central"].get("url") else None
        rd = R.run_dir(cfg)
        self.col_local, self.col_central = {}, {}
        for ch in sup.children:
            key = f"{site_id(cfg)}/{R.slug(ch.id)}"
            self.col_local[ch.id] = Collector(key, ch.cfg["storage"]["db"], ch.status_path,
                                              os.path.join(rd, f"cursor_local_{R.slug(ch.id)}.json"))
            self.col_central[ch.id] = Collector(key, ch.cfg["storage"]["db"], ch.status_path,
                                                os.path.join(rd, f"cursor_central_{R.slug(ch.id)}.json"))
        self.edge_col_local = Collector(f"{site_id(cfg)}/_edge", edge_store.path, "",
                                        os.path.join(rd, "cursor_local__edge.json"))
        self.edge_col_central = Collector(f"{site_id(cfg)}/_edge", edge_store.path, "",
                                          os.path.join(rd, "cursor_central__edge.json"))
        self.results: list[dict] = []
        self.last_central = 0.0
        self.rules_path = os.path.join(rd, "rules.json")

    def _host(self) -> dict:
        sup = R.read_json(self.sup.status_path, {}) or {}
        return host_payload(self.cfg, "edge", {"supervisor": sup, "edge_host": self.cfg["web"].get("public_host", "")})

    def _robot_meta(self, ch) -> dict:
        return {"id": ch.id, "name": ch.cfg["robot"]["name"], "line": ch.cfg["robot"].get("line", ""),
                "impact": ch.cfg["robot"].get("impact", "中"), "model": ch.cfg["robot"].get("model", ""),
                "axes": ch.cfg["axes"]["enabled"], "port": ch.port}

    def local_tick(self):
        batch = int(self.cfg["central"].get("batch", 3000))
        robots = []
        for ch in self.sup.children:
            col = self.col_local[ch.id]
            d = col.collect(batch)
            d["meta"] = self._robot_meta(ch)
            robots.append(d)
        e = self.edge_col_local.collect(batch)
        e["meta"] = {"id": "_edge", "name": self.cfg["system"].get("site_name") or site_id(self.cfg)}
        robots.append(e)
        self.local.ingest({"site": self._host(), "robots": robots}, notify=self.local.hub)
        for ch in self.sup.children:
            self.col_local[ch.id].commit(next(r for r in robots if r["key"] == self.col_local[ch.id].key)["cursors"])
        self.edge_col_local.commit(e["cursors"])

    def central_tick(self) -> dict | None:
        if not self.client:
            return None
        batch = int(self.cfg["central"].get("batch", 3000))
        t0 = time.time()
        robots = []
        for ch in self.sup.children:
            d = self.col_central[ch.id].collect(batch)
            d["meta"] = self._robot_meta(ch)
            robots.append(d)
        e = self.edge_col_central.collect(batch)
        e["meta"] = {"id": "_edge", "name": self.cfg["system"].get("site_name") or site_id(self.cfg)}
        robots.append(e)
        payload = {"site": self._host(), "robots": robots, "results": self.results}
        t_send = time.time()
        try:
            resp = self.client.post(payload)
        except Exception as ex:
            self.state.fail(str(ex))
            log.warning(_("上傳中央失敗：%s（資料留在本機，下次補送）"), ex)
            return None
        if not resp.get("ok"):
            self.state.fail(str(resp.get("error")))
            return resp
        self.results = []
        for ch in self.sup.children:
            col = self.col_central[ch.id]
            col.commit(next(r for r in robots if r["key"] == col.key)["cursors"])
        self.edge_col_central.commit(e["cursors"])
        n = sum(len(r.get(t, [])) for r in robots for t in TABLES)
        off = clock_offset(t_send, time.time(), resp)
        self.state.ok(n, (time.time() - t0) * 1000, off)
        check_clock(self.cfg, getattr(self.sup, "alarms", None), off)
        for cmd in resp.get("commands", []):
            self.results.append(self.run_command(cmd))
        if resp.get("rules"):
            R.write_json(self.rules_path, resp["rules"])
        return resp

    def run_command(self, cmd: dict) -> dict:
        """透過機器人程式的內部 API 執行（只接受本機、帶內部金鑰）。"""
        rid = str(cmd.get("robot_id"))
        if rid == "_edge":                       # 邊緣主機自己的警報（例如機器人程式一直重啟失敗）
            p = cmd.get("payload") or {}
            if cmd.get("kind") == "ack":
                self.edge_store.ack_alarm(int(p["edge_id"]), cmd.get("who") or "central")
                return {"cmd_id": cmd.get("id"), "ok": True, "detail": "ack"}
            return {"cmd_id": cmd.get("id"), "ok": False, "detail": _("不支援的指令：{k}", k=cmd.get("kind"))}
        ch = next((c for c in self.sup.children if R.slug(c.id) == rid or c.id == rid), None)
        if not ch:
            return {"cmd_id": cmd.get("id"), "ok": False, "detail": _("找不到機器人 {r}", r=rid)}
        req = urllib.request.Request(f"http://127.0.0.1:{ch.port}/api/internal/cmd", method="POST",
                                     data=json.dumps(cmd, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json", "X-DTM-Internal": self.sup.token})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read() or b"{}")
        except Exception as e:
            res = {"ok": False, "detail": str(e)}
        res["cmd_id"] = cmd.get("id")
        return res

    def run(self, stop: threading.Event):
        every = float(self.cfg["central"].get("interval_s", 60))
        stop.wait(5.0)
        while not stop.is_set():
            try:
                self.local_tick()
            except Exception as e:
                log.exception("local sync: %s", e)
            if self.client and time.time() - self.last_central >= every:
                self.last_central = time.time()
                try:
                    self.central_tick()
                except Exception as e:
                    log.exception("central sync: %s", e)
            stop.wait(10.0)


# ------------------------------------------------------------ 單機（一台機器人直接上傳中央）
class StandaloneSync:
    def __init__(self, svc):
        self.svc = svc
        cfg = svc.cfg
        self.cfg = cfg
        rd = R.run_dir(cfg)
        self.col = Collector(robot_key(cfg), cfg["storage"]["db"], R.status_path(cfg),
                             os.path.join(rd, "cursor_central.json"))
        self.client = Client(cfg)
        self.state = SyncState(cfg)
        self.results: list[dict] = []
        self.rules_path = os.path.join(rd, "rules.json")

    def tick(self) -> dict | None:
        t0 = time.time()
        d = self.col.collect(int(self.cfg["central"].get("batch", 3000)))
        d["meta"] = {"id": R.robot_id(self.cfg), "name": self.cfg["robot"]["name"], "line": self.cfg["robot"].get("line", ""),
                     "impact": self.cfg["robot"].get("impact", "中"), "model": self.cfg["robot"].get("model", ""),
                     "axes": self.svc.axes, "port": self.cfg["web"]["port"]}
        payload = {"site": host_payload(self.cfg, "standalone"), "robots": [d], "results": self.results}
        t_send = time.time()
        try:
            resp = self.client.post(payload)
        except Exception as ex:
            self.state.fail(str(ex))
            log.warning(_("上傳中央失敗：%s（資料留在本機，下次補送）"), ex)
            return None
        if not resp.get("ok"):
            self.state.fail(str(resp.get("error")))
            return resp
        self.results = []
        self.col.commit(d["cursors"])
        off = clock_offset(t_send, time.time(), resp)
        self.state.ok(sum(len(d.get(t, [])) for t in TABLES), (time.time() - t0) * 1000, off)
        check_clock(self.cfg, self.svc.alarms, off)
        for cmd in resp.get("commands", []):
            r = execute_command(self.svc, cmd)
            r["cmd_id"] = cmd.get("id")
            self.results.append(r)
        if resp.get("rules"):
            R.write_json(self.rules_path, resp["rules"])
            if apply_rules(self.svc.cfg, resp["rules"]):
                for ax in self.svc.axes:
                    self.svc.refresh_decision(ax, raise_alarms=False)
        return resp

    def run(self, stop: threading.Event):
        every = float(self.cfg["central"].get("interval_s", 60))
        stop.wait(5.0)
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:
                log.exception("sync: %s", e)
            stop.wait(every)

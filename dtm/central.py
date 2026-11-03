"""中央伺服器（[system] role = "central"）：全廠機群總覽、長期資料、壽命統計、通知中心、保養排程與備品。

同一份程式的另一種角色：不讀機器人，只接收各邊緣主機（或單機）上傳的資料（/api/sync）。
- 判斷只在邊緣做一次；中央接收結果，做跨廠比較、風險排名、機群壽命統計，並把算好的壽命模型下發給邊緣。
- 通知只由中央統一發（去重、依廠區與班別派送、逐級通報）；中央連不上時邊緣才自己發警戒（備援）。
- 換脂、換件這類事件以中央為準：在中央登錄後變成「指令」，由邊緣在對應機器人上執行，再同步回來。
- 中央監看各邊緣主機：太久沒上傳就發系統警報（斷電、斷網只有這一層看得到）。
邊緣主機也用同一個 CentralStore 當「廠區資料庫」（local=True，不發通知）。
"""
from __future__ import annotations

import hmac
import json
import logging
import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from .i18n import N_, TDict, _, get_lang, in_lang, pack_json, revive
from . import runtime as R

log = logging.getLogger("dtm.central")

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sites (
  site_id TEXT PRIMARY KEY, name TEXT, country TEXT, role TEXT, version TEXT, host TEXT, last_seen REAL,
  lag_s REAL, info TEXT);
CREATE TABLE IF NOT EXISTS robots (
  rkey TEXT PRIMARY KEY, site_id TEXT, id TEXT, name TEXT, line TEXT, model TEXT, impact TEXT, axes TEXT, port INTEGER,
  version TEXT, connected INTEGER, last_ts REAL, loop_ts REAL, error TEXT, samples INTEGER, cycles INTEGER,
  hours REAL, last_seen REAL, status TEXT);
CREATE TABLE IF NOT EXISTS axis_state (
  rkey TEXT, axis INTEGER, name TEXT, level INTEGER, ratio REAL, metrics TEXT, days_to_caution REAL,
  days_to_alert REAL, slope REAL, motion TEXT, decision TEXT, series TEXT, reasons TEXT, hint TEXT, n_runs INTEGER,
  need_runs INTEGER, baseline_ready INTEGER, temp REAL, updated REAL, tmpl TEXT, PRIMARY KEY (rkey, axis));
CREATE TABLE IF NOT EXISTS axis_daily (
  day TEXT, rkey TEXT, axis INTEGER, level INTEGER, ratio REAL, motion_level INTEGER, dec_level INTEGER,
  PRIMARY KEY (day, rkey, axis));
CREATE TABLE IF NOT EXISTS alarms (
  id INTEGER PRIMARY KEY AUTOINCREMENT, rkey TEXT, site_id TEXT, edge_id INTEGER, origin TEXT, ts REAL, axis INTEGER,
  kind TEXT, akey TEXT, level INTEGER, message TEXT, value REAL, acked INTEGER DEFAULT 0, acked_by TEXT,
  acked_ts REAL, cleared_ts REAL, mtime REAL, tmpl TEXT, UNIQUE (rkey, edge_id));
CREATE INDEX IF NOT EXISTS ix_c_alarm ON alarms(cleared_ts, level);
CREATE TABLE IF NOT EXISTS features (
  rkey TEXT, edge_id INTEGER, ts REAL, axis INTEGER, F REAL, Fc REAL, Fv REAL, G REAL, ripple REAL, temp REAL,
  valid INTEGER, PRIMARY KEY (rkey, edge_id));
CREATE INDEX IF NOT EXISTS ix_c_feat ON features(rkey, axis, ts);
CREATE TABLE IF NOT EXISTS motion_daily (
  rkey TEXT, day TEXT, code INTEGER, axis INTEGER, n INTEGER, ts REAL, rms REAL, mean_abs REAL, peak REAL,
  vib_r REAL, vib_a REAL, temp REAL, dur REAL, PRIMARY KEY (rkey, day, code, axis));
CREATE TABLE IF NOT EXISTS events (
  rkey TEXT, edge_id INTEGER, ts REAL, axis INTEGER, kind TEXT, note TEXT, who TEXT, undone_ts REAL, undo_who TEXT,
  undo_note TEXT, PRIMARY KEY (rkey, edge_id));
CREATE TABLE IF NOT EXISTS components (
  rkey TEXT, edge_id INTEGER, axis INTEGER, comp TEXT, install_ts REAL, removed_ts REAL, life_h REAL, failed INTEGER,
  reason TEXT, PRIMARY KEY (rkey, edge_id));
CREATE TABLE IF NOT EXISTS iron (
  rkey TEXT, edge_id INTEGER, ts REAL, axis INTEGER, wt_pct REAL, sample TEXT, PRIMARY KEY (rkey, edge_id));
CREATE TABLE IF NOT EXISTS vib (
  rkey TEXT, edge_id INTEGER, ts REAL, axis INTEGER, direction TEXT, rms REAL, env REAL, mesh REAL, motor1x REAL,
  kurt REAL, PRIMARY KEY (rkey, edge_id));
CREATE TABLE IF NOT EXISTS commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, site_id TEXT, rkey TEXT, robot_id TEXT, kind TEXT, payload TEXT,
  who TEXT, status TEXT, result TEXT, sent_ts REAL, done_ts REAL);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT, rkey TEXT, axis INTEGER, kind TEXT, ts REAL, hours REAL,
  status TEXT, title TEXT, note TEXT, workpack TEXT, prep TEXT, created_by TEXT, created_ts REAL, done_ts REAL,
  done_by TEXT, cmd_id INTEGER);
CREATE TABLE IF NOT EXISTS spares (
  id INTEGER PRIMARY KEY AUTOINCREMENT, part TEXT UNIQUE, name TEXT, stock REAL, reserved REAL, safety REAL,
  lead_weeks REAL, unit TEXT, location TEXT, note TEXT, comp TEXT, axes TEXT, updated REAL, price REAL);
CREATE TABLE IF NOT EXISTS edge_metrics (
  ts REAL, site_id TEXT, lag_s REAL, cpu REAL, mem REAL, disk REAL, n_robots INTEGER, n_online INTEGER);
CREATE INDEX IF NOT EXISTS ix_em ON edge_metrics(site_id, ts);
CREATE TABLE IF NOT EXISTS notify_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, channel TEXT, target TEXT, level INTEGER, ok INTEGER,
  detail TEXT, message TEXT, robot TEXT, site TEXT, stage INTEGER, akey TEXT, rule TEXT);
CREATE TABLE IF NOT EXISTS sys_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, site_id TEXT, kind TEXT, level INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, who TEXT, addr TEXT, action TEXT, ok INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS rules (version REAL PRIMARY KEY, body TEXT);
"""

DRIVE_PARTS = TDict({1: "J1 減速機", 2: "J2 減速機", 3: "J3 減速機", 4: "腕部齒輪箱 J4–J6", 5: "腕部齒輪箱 J4–J6",
                     6: "腕部齒輪箱 J4–J6", 7: "E1 行走軸減速機"})


def jdump(o) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)


def jload(s, default=None):
    if isinstance(s, (list, dict)):                 # 已經解開過
        return s
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


# 舊資料庫升級：缺的欄位用 ALTER TABLE 補上
MIGRATIONS = {
    "alarms": {"tmpl": "TEXT"},             # 2.1：訊息模板（觀看者切換語言時重新產生文字，見 i18n.pack）
    "axis_state": {"tmpl": "TEXT"},         # 2.1：各軸文字（研判、建議…）的模板
    "notify_log": {"lang": "TEXT", "person": "TEXT", "ref": "TEXT", "dlr": "TEXT", "dlr_ts": "REAL",
                   "dlr_detail": "TEXT"},   # 2.1：收件人語言、姓名；簡訊送達回報
    "spares": {"price": "REAL"},            # 2.1：備品單價（保養材料費估算）
}


class CentralStore:
    def __init__(self, path: str, cfg: dict, local: bool = False):
        self.path = path
        self.cfg = cfg
        self.local = local
        self.hub = False                 # 邊緣主機沒接中央：本機廠區資料庫負責通知、逐級通報與壽命規則
        self.notifier = None
        self.lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            for table, cols in MIGRATIONS.items():
                have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                for col, typ in cols.items():
                    if col not in have:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def q(self, sql: str, args=()) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def one(self, sql: str, args=()) -> dict | None:
        r = self.q(sql, args)
        return r[0] if r else None

    def meta(self, k: str, default=None):
        r = self.one("SELECT v FROM meta WHERE k=?", (k,))
        return r["v"] if r else default

    def set_meta(self, k: str, v):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,?)", (k, str(v)))

    # -------------------------------------------------------- 接收上傳
    def ingest(self, payload: dict, notify: bool = True, now: float | None = None) -> dict:
        now = now or time.time()
        site = payload.get("site") or {}
        sid = str(site.get("site_id") or "SITE")
        sites_cfg = (self.cfg.get("central") or {}).get("sites") or {}
        name = (sites_cfg.get(sid) or {}).get("name") or site.get("site_name") or sid
        country = (sites_cfg.get(sid) or {}).get("country", "")
        events_out = []
        with self.lock, self.conn() as c:
            last_ts = [((r.get("status") or {}).get("last_ts") or 0) for r in payload.get("robots", [])
                       if not str(r.get("key", "")).endswith("/_edge")]
            lag = now - max(last_ts) if last_ts and max(last_ts) else None
            info = {k: site.get(k) for k in ("host", "disk", "watchdog", "supervisor", "role", "sent_ts", "edge_host")}
            if site.get("sent_ts"):                     # 各廠電腦時間 − 中央時間（含上傳延遲，約略值）
                info["clock_offset_s"] = round(float(site["sent_ts"]) - now, 1)
            c.execute("INSERT INTO sites (site_id,name,country,role,version,host,last_seen,lag_s,info) VALUES (?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(site_id) DO UPDATE SET name=excluded.name, country=excluded.country, role=excluded.role,"
                      " version=excluded.version, host=excluded.host, last_seen=excluded.last_seen, lag_s=excluded.lag_s,"
                      " info=excluded.info",
                      (sid, name, country, site.get("role", ""), site.get("version", ""),
                       (site.get("host") or {}).get("host", ""), now, lag, jdump(info)))
            n_rob, n_on = 0, 0
            for d in payload.get("robots", []):
                key = d["key"]
                meta = d.get("meta") or {}
                st = d.get("status") or {}
                is_edge = key.endswith("/_edge")
                if not is_edge:
                    n_rob += 1
                    fresh = st.get("last_ts") and now - float(st["last_ts"]) < 600
                    n_on += 1 if fresh else 0
                    self._robot(c, sid, key, meta, st, now, d)
                events_out += self._alarms(c, sid, key, d.get("alarms", []), now)
                self._rows(c, key, d)
            host = site.get("host") or {}
            c.execute("INSERT INTO edge_metrics (ts,site_id,lag_s,cpu,mem,disk,n_robots,n_online) VALUES (?,?,?,?,?,?,?,?)",
                      (now, sid, lag, host.get("cpu_pct"), host.get("mem_pct"), (site.get("disk") or {}).get("free_pct"),
                       n_rob, n_on))
            for r in payload.get("results", []) or []:
                c.execute("UPDATE commands SET status=?, result=?, done_ts=? WHERE id=?",
                          ("done" if r.get("ok") else "failed", jdump(r), now, r.get("cmd_id")))
            cmds = [dict(x) for x in c.execute("SELECT * FROM commands WHERE site_id=? AND status='pending' ORDER BY id",
                                               (sid,))]
            for x in cmds:
                c.execute("UPDATE commands SET status='sent', sent_ts=? WHERE id=?", (now, x["id"]))
        if notify and self.notifier and (not self.local or self.hub):
            self._notify(events_out, name, now)
        rules = self.rules()
        return {"ok": True, "server_time": now,
                "commands": [{"id": x["id"], "robot_id": x["robot_id"], "kind": x["kind"], "who": x["who"],
                              "payload": jload(x["payload"], {})} for x in cmds],
                "rules": rules}

    def _robot(self, c, sid, key, meta, st, now, d):
        status = dict(st)
        status.pop("axes", None)
        status["cycles_hourly"] = d.get("cycles_hourly", [])
        hours = None
        if d.get("hours"):
            hours = d["hours"][0]["hours"]
        c.execute("INSERT INTO robots (rkey,site_id,id,name,line,model,impact,axes,port,version,connected,last_ts,loop_ts,"
                  "error,samples,cycles,hours,last_seen,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(rkey) DO UPDATE SET name=excluded.name, line=excluded.line, model=excluded.model,"
                  " impact=excluded.impact, axes=excluded.axes, port=excluded.port, version=excluded.version,"
                  " connected=excluded.connected, last_ts=excluded.last_ts, loop_ts=excluded.loop_ts, error=excluded.error,"
                  " samples=excluded.samples, cycles=excluded.cycles, hours=COALESCE(excluded.hours, robots.hours),"
                  " last_seen=excluded.last_seen, status=excluded.status",
                  (key, sid, meta.get("id") or st.get("id"), meta.get("name") or st.get("robot"),
                   meta.get("line") or st.get("line", ""), meta.get("model") or st.get("model", ""),
                   meta.get("impact") or st.get("impact", "中"), jdump(meta.get("axes") or [a["axis"] for a in st.get("axes", [])]),
                   meta.get("port") or st.get("port"), st.get("version", ""), int(bool(st.get("connected"))),
                   st.get("last_ts"), st.get("loop_ts"), st.get("error", ""), st.get("samples"), st.get("cycles"), hours,
                   now, jdump(status)))
        today = day_of(now)
        for a in st.get("axes", []) or []:
            mo = a.get("motion") or {}
            de = a.get("decision") or {}
            lv = a.get("level", -1)
            c.execute("INSERT OR REPLACE INTO axis_state (rkey,axis,name,level,ratio,metrics,days_to_caution,days_to_alert,slope,"
                      "motion,decision,series,reasons,hint,n_runs,need_runs,baseline_ready,temp,updated,tmpl) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (key, a["axis"], a.get("name"), lv, a.get("ratio_F"), jdump(a.get("metrics")),
                       a.get("days_to_caution"), a.get("days_to_alert"), a.get("slope_pct_month"), jdump(mo), jdump(de),
                       jdump(a.get("series", [])), jdump(a.get("reasons", [])), a.get("hint", ""), a.get("n_runs"),
                       a.get("need_runs"), int(bool(a.get("baseline_ready"))), a.get("temp"), now,
                       jdump(a["tmpl"]) if a.get("tmpl") else None))
            dec_lv = max([(de.get(k) or {}).get("level", 0) for k in ("grease", "drive", "motor")] or [0])
            c.execute("INSERT OR REPLACE INTO axis_daily (day,rkey,axis,level,ratio,motion_level,dec_level) VALUES (?,?,?,?,?,?,?)",
                      (today, key, a["axis"], lv, a.get("ratio_F"), mo.get("level", -1), dec_lv))
            self._iron_job(c, sid, key, a["axis"], de)

    AUTO = "DTM"                                    # 系統自動排的保養（created_by）

    def _iron_job(self, c, sid, key, ax, de):
        """鐵粉達注意／警戒：依縮短後的取樣間隔自動排「鐵粉取樣」（人工排的不動；恢復正常就撤掉自動排的）。"""
        nxt = de.get("iron_next_ts")
        row = c.execute("SELECT id, ts, created_by FROM jobs WHERE rkey=? AND axis=? AND kind='iron' AND status!='done' "
                        "ORDER BY ts LIMIT 1", (key, ax)).fetchone()
        if not nxt:
            if row and row["created_by"] == self.AUTO:
                c.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
            return
        title = _("鐵粉取樣（每 {d} 天）", d=de.get("iron_interval_days") or "—")
        if row is None:
            c.execute("INSERT INTO jobs (site_id,rkey,axis,kind,ts,hours,status,title,note,created_by,created_ts) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (sid, key, ax, "iron", float(nxt), 0.5, "planned", title,
                       _("系統依鐵粉等級自動排程；取換脂排出的中段油脂（前段先丟棄）"), self.AUTO, time.time()))
        elif row["created_by"] == self.AUTO and abs(float(row["ts"]) - float(nxt)) > 3600:
            c.execute("UPDATE jobs SET ts=?, title=? WHERE id=?", (float(nxt), title, row["id"]))

    def _alarms(self, c, sid, key, rows, now) -> list[dict]:
        out = []
        for a in rows:
            old = c.execute("SELECT id, cleared_ts, acked, level FROM alarms WHERE rkey=? AND edge_id=?",
                            (key, a["id"])).fetchone()
            c.execute("INSERT INTO alarms (rkey,site_id,edge_id,origin,ts,axis,kind,akey,level,message,value,acked,acked_by,"
                      "acked_ts,cleared_ts,mtime,tmpl) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(rkey, edge_id) DO UPDATE SET cleared_ts=excluded.cleared_ts,"
                      " acked=MAX(alarms.acked, excluded.acked), acked_by=COALESCE(alarms.acked_by, excluded.acked_by),"
                      " acked_ts=COALESCE(alarms.acked_ts, excluded.acked_ts), mtime=excluded.mtime",
                      (key, sid, a["id"], "edge", a["ts"], a.get("axis"), a.get("kind"), a.get("akey"), a.get("level"),
                       a.get("message"), a.get("value"), int(a.get("acked") or 0), a.get("acked_by"), a.get("acked_ts"),
                       a.get("cleared_ts"), now, a.get("tmpl")))
            if old is None and a.get("cleared_ts") is None and (a.get("level") or 0) > 0:
                out.append({"type": "new", "key": key, **a})
            elif old is not None and old["cleared_ts"] is None and a.get("cleared_ts") is not None:
                out.append({"type": "clear", "key": key, **a})
        return out

    def _rows(self, c, key, d):
        for f in d.get("features", []) or []:
            c.execute("INSERT OR IGNORE INTO features (rkey,edge_id,ts,axis,F,Fc,Fv,G,ripple,temp,valid) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (key, f["id"], f["ts"], f["axis"], f.get("F"), f.get("Fc"), f.get("Fv"), f.get("G"), f.get("ripple"),
                       f.get("temp"), f.get("valid")))
        for m in d.get("motion_daily", []) or []:
            c.execute("INSERT OR REPLACE INTO motion_daily (rkey,day,code,axis,n,ts,rms,mean_abs,peak,vib_r,vib_a,temp,dur) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (key, m["day"], m["code"], m["axis"], m.get("n"), m.get("ts"), m.get("rms"), m.get("mean_abs"),
                       m.get("peak"), m.get("vib_r"), m.get("vib_a"), m.get("temp"), m.get("dur")))
        for e in d.get("events", []) or []:
            c.execute("INSERT OR REPLACE INTO events (rkey,edge_id,ts,axis,kind,note,who,undone_ts,undo_who,undo_note) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (key, e["id"], e["ts"], e.get("axis"), e.get("kind"), e.get("note"), e.get("who"),
                       e.get("undone_ts"), e.get("undo_who"), e.get("undo_note")))
        if d.get("components") is not None:
            c.execute("DELETE FROM components WHERE rkey=?", (key,))
            for x in d["components"]:
                c.execute("INSERT OR REPLACE INTO components (rkey,edge_id,axis,comp,install_ts,removed_ts,life_h,failed,reason) "
                          "VALUES (?,?,?,?,?,?,?,?,?)",
                          (key, x["id"], x.get("axis"), x.get("comp"), x.get("install_ts"), x.get("removed_ts"),
                           x.get("life_h"), x.get("failed"), x.get("reason")))
        for x in d.get("iron", []) or []:
            c.execute("INSERT OR IGNORE INTO iron (rkey,edge_id,ts,axis,wt_pct,sample) VALUES (?,?,?,?,?,?)",
                      (key, x["id"], x["ts"], x["axis"], x["wt_pct"], x.get("sample")))
        for x in d.get("vib", []) or []:
            c.execute("INSERT OR IGNORE INTO vib (rkey,edge_id,ts,axis,direction,rms,env,mesh,motor1x,kurt) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (key, x["id"], x["ts"], x["axis"], x["direction"], x.get("rms"), x.get("env"), x.get("mesh"),
                       x.get("motor1x"), x.get("kurt")))

    # -------------------------------------------------------- 通知（只有中央）
    def _notify(self, evs: list[dict], site_name: str, now: float):
        max_age = float(self.cfg["central"].get("notify_max_age_s", 3600))
        for e in evs:
            r = self.one("SELECT name FROM robots WHERE rkey=?", (e["key"],))
            robot = (r or {}).get("name") or (site_name if e["key"].endswith("/_edge") else e["key"].split("/")[-1])
            k = f"{e['key']}|{e.get('akey')}"
            msg = in_lang(revive(e.get("tmpl"), e.get("message") or ""), get_lang())   # 邊緣主機的文字換成中央的語言
            if e["type"] == "new":
                if now - float(e["ts"]) > max_age:
                    continue
                self.notifier.send(int(e["level"]), msg, e.get("axis"), e.get("kind") or "",
                                   float(e["ts"]), k, robot=robot, site=site_name)
            else:
                self.notifier.send(int(e.get("level") or 1), msg, e.get("axis"), e.get("kind") or "",
                                   float(e.get("cleared_ts") or now), k, cleared=True, robot=robot, site=site_name)

    def is_acked(self, key: str, robot: str = "", site: str = "") -> bool:
        rkey, _sep, akey = key.partition("|")
        r = self.one("SELECT acked, cleared_ts FROM alarms WHERE rkey=? AND akey=? ORDER BY ts DESC LIMIT 1", (rkey, akey))
        return not r or bool(r["acked"]) or r["cleared_ts"] is not None

    # -------------------------------------------------------- 中央自己的系統警報（邊緣失聯）
    def central_alarm(self, rkey: str, sid: str, akey: str, level: int, message: str, now: float | None = None,
                      notify: bool = True) -> int:
        now = now or time.time()
        cur = self.one("SELECT * FROM alarms WHERE rkey=? AND akey=? AND origin='central' AND cleared_ts IS NULL",
                       (rkey, akey))
        site = self.one("SELECT name FROM sites WHERE site_id=?", (sid,)) or {}
        if level <= 0:
            if cur:
                with self.conn() as c:
                    c.execute("UPDATE alarms SET cleared_ts=?, mtime=? WHERE id=?", (now, now, cur["id"]))
                if notify and self.notifier:
                    self.notifier.send(cur["level"], cur["message"], None, "系統", now, f"{rkey}|{akey}", cleared=True,
                                       robot=site.get("name", sid), site="")
            return 0
        if cur and cur["level"] >= level:
            return cur["id"]
        with self.conn() as c:
            if cur:
                c.execute("UPDATE alarms SET cleared_ts=?, mtime=? WHERE id=?", (now, now, cur["id"]))
            aid = c.execute("INSERT INTO alarms (rkey,site_id,edge_id,origin,ts,axis,kind,akey,level,message,value,mtime,"
                            "tmpl) VALUES (?,?,NULL,'central',?,NULL,?,?,?,?,NULL,?,?)",
                            (rkey, sid, now, N_("系統"), akey, level, str(message), now, pack_json(message))).lastrowid
        if notify and self.notifier:
            self.notifier.send(level, message, None, "系統", now, f"{rkey}|{akey}", robot=site.get("name", sid), site="")
        return aid

    def check_edges(self, now: float | None = None):
        now = now or time.time()
        limit = float(self.cfg["central"].get("edge_timeout_s", 300))
        for s in self.q("SELECT * FROM sites"):
            age = now - float(s["last_seen"] or 0)
            rkey = f"{s['site_id']}/_edge"
            if age > limit:
                self.central_alarm(rkey, s["site_id"], "edge:offline", 2,
                                   _("{site} 邊緣主機 {m:.0f} 分鐘沒有上傳（斷電、斷網或程式停止）", site=s["name"] or s["site_id"],
                                     m=age / 60), now)
            else:
                self.central_alarm(rkey, s["site_id"], "edge:offline", 0, "", now)

    # -------------------------------------------------------- 寫入：確認、指令、排程、備品
    def ack(self, aid: int, who: str, dispatch=None) -> dict | None:
        """確認警報：中央先記下（停止升級），再把確認送回邊緣，讓機器人自己的紀錄也一致。"""
        a = self.one("SELECT * FROM alarms WHERE id=?", (aid,))
        if not a:
            raise ValueError(_("找不到警報 #{id}", id=aid))
        now = time.time()
        with self.conn() as c:
            c.execute("UPDATE alarms SET acked=1, acked_by=?, acked_ts=?, mtime=? WHERE id=? AND acked=0", (who, now, now, aid))
        res = None
        if a["origin"] == "edge" and a["edge_id"] is not None and not a["acked"]:
            dispatch = dispatch or (None if self.local else self.command)
            if dispatch:
                res = dispatch(a["rkey"], "ack", {"edge_id": a["edge_id"]}, who)
        out = self.one("SELECT * FROM alarms WHERE id=?", (aid,))
        out["dispatch"] = res
        return out

    def command(self, rkey: str, kind: str, payload: dict, who: str) -> dict:
        """排入指令，下次該廠邊緣主機同步時帶回去執行（約 interval_s 秒內）。"""
        if rkey.endswith("/_edge"):
            sid, rid = rkey.split("/")[0], "_edge"
        else:
            r = self.one("SELECT site_id, id FROM robots WHERE rkey=?", (rkey,))
            if not r:
                raise ValueError(_("找不到機器人 {r}", r=rkey))
            sid, rid = r["site_id"], R.slug(r["id"])
        with self.conn() as c:
            cid = c.execute("INSERT INTO commands (ts,site_id,rkey,robot_id,kind,payload,who,status) VALUES (?,?,?,?,?,?,?,'pending')",
                            (time.time(), sid, rkey, rid, kind, jdump(payload), who)).lastrowid
        return {"ok": True, "queued": cid,
                "detail": _("已排入，下次邊緣主機同步時執行（約 {s:.0f} 秒內）", s=float(self.cfg["central"].get("interval_s", 60)))}

    def commands(self, limit: int = 50) -> list[dict]:
        return self.q("SELECT * FROM commands ORDER BY id DESC LIMIT ?", (limit,))

    def save_job(self, j: dict, who: str) -> int:
        cols = ("site_id", "rkey", "axis", "kind", "ts", "hours", "status", "title", "note", "workpack", "prep")
        vals = {k: j.get(k) for k in cols if k in j}
        if "prep" in vals and not isinstance(vals["prep"], str):
            vals["prep"] = jdump(vals["prep"])
        if vals.get("rkey") and not vals.get("site_id"):
            r = self.one("SELECT site_id FROM robots WHERE rkey=?", (vals["rkey"],))
            vals["site_id"] = (r or {}).get("site_id")
        with self.conn() as c:
            if j.get("id"):
                q = ", ".join(f"{k}=?" for k in vals)
                c.execute(f"UPDATE jobs SET {q} WHERE id=?", (*vals.values(), int(j["id"])))
                return int(j["id"])
            vals.setdefault("status", "planned")
            vals.update(created_by=who, created_ts=time.time())
            return c.execute(f"INSERT INTO jobs ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})",
                             tuple(vals.values())).lastrowid

    JOB_EVENT = {"rep": None, "motor": None, "grease": "grease_change", "iron": None, "check": None}
    JOB_KINDS = TDict({"rep": "換件", "motor": "換馬達", "grease": "換脂", "iron": "鐵粉取樣", "check": "檢查"})

    def job_done(self, jid: int, who: str, failed: bool = False, note: str = "", hours: float | None = None,
                 dispatch=None) -> dict:
        """完工：標記完成；換件、換脂會變成事件指令，由邊緣在機器人上登錄（元件壽命、基準一併處理）。"""
        j = self.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if not j:
            raise ValueError(_("找不到排程 #{id}", id=jid))
        kind = self.JOB_EVENT.get(j["kind"])
        if j["kind"] == "rep":
            from .config import drive_comp
            kind = "gearbox_replace" if drive_comp(self.cfg, int(j["axis"] or 1)) == "gearbox" else "reducer_replace"
        if j["kind"] == "motor":
            kind = "motor_replace"
        res = None
        dispatch = dispatch or (None if self.local else self.command)
        if kind and j["rkey"] and j["axis"] and dispatch:
            res = dispatch(j["rkey"], "event", {"axis": j["axis"], "kind": kind, "note": note or j["title"] or "",
                                                "failed": failed, "hours": hours, "ts": time.time()}, who)
        with self.conn() as c:
            c.execute("UPDATE jobs SET status='done', done_ts=?, done_by=?, cmd_id=? WHERE id=?",
                      (time.time(), who, (res or {}).get("queued"), jid))
        return {"id": jid, "dispatch": res}

    def save_spare(self, s: dict) -> int:
        cols = ("part", "name", "stock", "reserved", "safety", "lead_weeks", "unit", "location", "note", "comp", "axes",
                "price")
        vals = {k: s.get(k) for k in cols if k in s}
        if "price" in vals:                               # 單價：空白 = 未填；不能是負數
            pr = vals["price"]
            if pr in (None, ""):
                vals["price"] = None
            else:
                try:
                    vals["price"] = round(float(pr), 2)
                except (TypeError, ValueError):
                    raise ValueError(_("單價請填數字"))
                if vals["price"] < 0 or vals["price"] != vals["price"] or vals["price"] > 1e12:
                    raise ValueError(_("單價不能是負數"))
        if isinstance(vals.get("axes"), (list, tuple)):
            vals["axes"] = ",".join(str(int(x)) for x in vals["axes"])
        if not s.get("id") and not str(vals.get("part") or "").strip():
            raise ValueError(_("備品料號不能空白"))
        vals["updated"] = time.time()
        with self.conn() as c:
            if s.get("id"):
                q = ", ".join(f"{k}=?" for k in vals)
                c.execute(f"UPDATE spares SET {q} WHERE id=?", (*vals.values(), int(s["id"])))
                return int(s["id"])
            return c.execute(f"INSERT INTO spares ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})",
                             tuple(vals.values())).lastrowid

    def add_notify_log(self, ts, channel, target, level, ok, detail, message, robot="", site="", stage=1, akey="",
                       rule="", lang="", person="", ref="", dlr=""):
        with self.conn() as c:
            c.execute("INSERT INTO notify_log (ts,channel,target,level,ok,detail,message,robot,site,stage,akey,rule,"
                      "lang,person,ref,dlr) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (ts, channel, target, level, int(ok), detail, message, robot, site, stage, akey, rule,
                       lang, person, ref, dlr))

    def update_dlr(self, ref: str, status: str, detail: str = "", ts: float | None = None) -> int:
        """簡訊送達回報：找最近一筆同編號、還在等回報的發送紀錄，改成已送達／送不到（還在送只更新說明）。"""
        with self.conn() as c:
            r = c.execute("SELECT id, dlr FROM notify_log WHERE ref=? AND ts>=? ORDER BY id DESC LIMIT 1",
                          (ref, (ts or time.time()) - 7 * 86400)).fetchone()
            if not r or r["dlr"] in ("delivered", "failed"):
                return 0
            c.execute("UPDATE notify_log SET dlr=?, dlr_ts=?, dlr_detail=? WHERE id=?",
                      ("pending" if status == "pending" else status, ts or time.time(), detail, r["id"]))
            return 1

    def pending_dlr(self, since: float, prefix: str = "") -> int:
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) FROM notify_log WHERE dlr='pending' AND ts>=? AND ref LIKE ?",
                             (since, prefix + "%")).fetchone()[0]

    def notify_log(self, limit: int = 50) -> list[dict]:
        return self.q("SELECT * FROM notify_log ORDER BY id DESC LIMIT ?", (limit,))

    def add_audit(self, who: str, addr: str, action: str, ok: bool, detail: str = ""):
        with self.conn() as c:
            c.execute("INSERT INTO audit (ts,who,addr,action,ok,detail) VALUES (?,?,?,?,?,?)",
                      (time.time(), who, addr, action, int(ok), detail[:500]))

    def add_sys_event(self, kind: str, level: int, detail: str = "", site_id: str = ""):
        with self.conn() as c:
            c.execute("INSERT INTO sys_events (ts,site_id,kind,level,detail) VALUES (?,?,?,?,?)",
                      (time.time(), site_id, kind, level, detail[:500]))

    # -------------------------------------------------------- 從中央重建（2.1）
    EXPORT = {
        "features": "edge_id AS id, ts, axis, F, Fc, Fv, G, ripple, temp, valid",
        "motion_daily": "day, code, axis, n, ts, rms, mean_abs, peak, vib_r, vib_a, temp, dur",
        "events": "edge_id AS id, ts, axis, kind, note, who, undone_ts, undo_who, undo_note",
        "components": "edge_id AS id, axis, comp, install_ts, removed_ts, life_h, failed, reason",
        "iron": "edge_id AS id, ts, axis, wt_pct, sample",
        "vib": "edge_id AS id, ts, axis, direction, rms, env, mesh, motor1x, kurt",
        "alarms": "edge_id AS id, ts, axis, kind, akey, level, message, value, acked, acked_by, acked_ts, cleared_ts, mtime",
    }

    def export_robot(self, rkey: str, now: float | None = None) -> dict:
        """這台機器人上傳過的資料（編號用機器人端原本的編號），給機器人端「從中央重建」用。"""
        if not self.one("SELECT rkey FROM robots WHERE rkey=?", (rkey,)):
            raise ValueError(_("中央沒有這台機器人的資料：{key}", key=rkey))
        out = {"robot": rkey, "ts": now or time.time()}
        for t, cols in self.EXPORT.items():
            extra = " AND origin='edge' AND edge_id IS NOT NULL" if t == "alarms" else ""
            out[t] = self.q(f"SELECT {cols} FROM {t} WHERE rkey=?{extra}", (rkey,))
        return out

    # -------------------------------------------------------- 機群壽命統計（規則下發）
    def life_data(self, comp: str | None = None, axis: int | None = None, now: float | None = None) -> tuple[list, list]:
        """（壽命時數, 是否失效）：已拆下的用登錄的壽命；還在用的用安裝後的時數（設限資料）。"""
        now = now or time.time()
        hpd = float(self.cfg["life"].get("hours_per_day", 20))
        q, args = "SELECT * FROM components WHERE comp IN ('reducer','gearbox','motor')", []
        if comp:
            q += " AND comp=?"
            args.append(comp)
        if axis:
            q += " AND axis=?"
            args.append(axis)
        lives, failed = [], []
        for x in self.q(q, args):
            if x["life_h"] is not None:
                lives.append(float(x["life_h"]))
                failed.append(bool(x["failed"]))
            elif x["install_ts"] and x["removed_ts"] is None:
                lives.append(max(1.0, (now - float(x["install_ts"])) / 86400 * hpd))
                failed.append(False)
        return lives, failed

    def compute_rules(self, now: float | None = None) -> dict:
        from .life import weibull_fit
        now = now or time.time()
        minf = int(self.cfg["life"].get("weibull_min_failures", 3))
        prior = {}
        for comp in ("reducer", "gearbox", "motor"):
            lives, failed = self.life_data(comp, None, now)
            if sum(failed) >= minf:
                fit = weibull_fit(lives, failed)
                if fit:
                    prior[comp] = {"beta": round(fit["beta"], 3), "eta_h": round(fit["eta_h"], 0), "n": fit["n"],
                                   "failures": fit["failures"]}
        scope = "site" if self.hub else "central"
        rules = {"version": now, "life_prior": prior, "scope": scope,
                 "source": _("本廠機群壽命統計") if self.hub else _("中央機群壽命統計")}
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO rules (version, body) VALUES (?,?)", (now, jdump(rules)))
            c.execute("DELETE FROM rules WHERE version < ?", (now - 90 * 86400,))
        return rules

    def rules(self) -> dict | None:
        r = self.one("SELECT body FROM rules ORDER BY version DESC LIMIT 1")
        return jload(r["body"]) if r else None


# ------------------------------------------------------------ 中央伺服器主程式
def check_token(cfg: dict, site_id: str, token: str) -> bool:
    from .notify import secret
    c = cfg["central"]
    want = secret((c.get("tokens") or {}).get(site_id, "")) or secret(c.get("token", ""))
    return bool(want) and hmac.compare_digest(str(token or ""), str(want))


def fleet_summary(cs: CentralStore) -> str:
    act = cs.q("SELECT a.*, r.name AS rname FROM alarms a LEFT JOIN robots r ON r.rkey=a.rkey WHERE a.cleared_ts IS NULL")
    sites = cs.q("SELECT * FROM sites")
    off = [s for s in sites if time.time() - float(s["last_seen"] or 0) > float(cs.cfg["central"]["edge_timeout_s"])]
    lines = [_("機群：{n} 台｜作用中警報 警戒 {a}、注意 {c}｜邊緣主機 {on}/{tot} 在線",
               n=cs.one("SELECT COUNT(*) AS n FROM robots")["n"], a=sum(1 for x in act if x["level"] >= 2),
               c=sum(1 for x in act if x["level"] == 1), on=len(sites) - len(off), tot=len(sites))]
    for x in sorted(act, key=lambda r: (-r["level"], r["ts"]))[:10]:
        lines.append(f"・{x.get('rname') or x['rkey']} {x['message'][:60]}")
    return "\n".join(lines)


def run_central(cfg: dict, a=None):
    from .__main__ import startup_checks
    from .notify import Notifier
    from .fleetweb import serve_fleet
    db = R.resolve(cfg, cfg["central"].get("db", "central.sqlite3"))
    marker, checks, unclean = startup_checks(cfg, a, db)
    cs = CentralStore(db, cfg)
    demo = cs.meta("demo") == "1"
    if demo:                                          # 演示：每種語言各一份資料庫，依觀看者的語言切換
        from .demo import DemoStores
        from .i18n import visible_langs
        cs = DemoStores(cfg, cs)

        def prebuild():                               # 其他語言在背景先建好，切換語言時不用等
            for lg in visible_langs():
                try:
                    cs.store(lg)
                except Exception as e:
                    log.warning("demo %s: %s", lg, e)
        threading.Thread(target=prebuild, name="dtm-demo-langs", daemon=True).start()
    ncfg = dict(cfg)
    ncfg["robot"] = dict(cfg["robot"], name=cfg["system"].get("site_name") or _("中央"))
    ncfg["system"] = dict(cfg["system"], site_name="")
    if demo:                                          # 演示資料：不發任何通知
        ncfg["notify"] = dict(cfg["notify"], summary_time="")
    notifier = Notifier(ncfg, cs, summary_fn=lambda: fleet_summary(cs), is_acked=cs.is_acked,
                        gate=(lambda: "none") if demo else None)
    cs.notifier = notifier
    cs.add_sys_event("start", 1 if unclean else 0, _("中央伺服器啟動"))
    stop = threading.Event()
    status_path = os.path.join(R.run_dir(cfg), "status__supervisor.json")


    def loop():
        last_rules = 0.0
        while not stop.wait(10.0):
            now = time.time()
            try:
                stores = cs.all() if demo else [cs]
                if demo:                              # 演示：模擬各廠持續上傳
                    from .demo import demo_tick
                    for st in stores:
                        demo_tick(st, now)
                for st in stores:
                    st.check_edges(now)
                notifier.tick(now)
                if now - last_rules > float(cfg["central"].get("rules_every_s", 3600)):
                    last_rules = now
                    for st in stores:
                        st.compute_rules(now)
                from . import __version__
                R.write_json(status_path, {"role": "supervisor", "ts": now, "loop_ts": now, "pid": os.getpid(),
                                           "version": __version__, "disk": R.disk_info(db)})
            except Exception as e:
                log.exception("central loop: %s", e)
    t = threading.Thread(target=loop, name="dtm-central", daemon=True)
    t.start()
    httpd = serve_fleet(cfg, cs, role="central", notifier=notifier)
    R.watch_threads(cfg, {_("中央主迴圈"): t}, stop)
    log.info(_("中央伺服器啟動：http%s://%s:%s"), "s" if cfg["web"].get("tls_cert") else "", "localhost", cfg["web"]["port"])

    def _stop():
        log.info(_("停止中…"))
        stop.set()
        httpd.shutdown()
    R.on_stop_signals(_stop)
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        cs.add_sys_event("stop", 0, _("正常結束"))
        marker.stop()


def weibull_points(lives: list[float], failed: list[bool]) -> list[tuple[float, float]]:
    """含設限的中位秩（Johnson 調整秩）：給機率圖畫點。"""
    data = sorted(zip(lives, failed))
    n = len(data)
    prev = 0.0
    out = []
    for i, (t, f) in enumerate(data):
        if not f:
            continue
        rev = n - i
        inc = (n + 1 - prev) / (1 + rev)
        prev += inc
        out.append((t, (prev - 0.3) / (n + 0.4)))
    return out


def cond_fail(age: float, dt: float, beta: float, eta: float) -> float:
    a = (age / eta) ** beta
    b = ((age + dt) / eta) ** beta
    return 1 - math.exp(a - b)

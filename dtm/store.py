"""SQLite 儲存：原始樣本（保留 N 天）、每分鐘統計（永久）、診斷特徵、警報、保養事件、校正。"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

from .config import RESET_KINDS
from .model import RunFeatures, Sample
from .i18n import _, pack_json

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS samples (
  ts REAL NOT NULL, seg INTEGER, run INTEGER, ovr REAL, temp REAL,
  d1 REAL, d2 REAL, d3 REAL, d4 REAL, d5 REAL, d6 REAL);
CREATE INDEX IF NOT EXISTS ix_samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS agg_1min (
  minute INTEGER NOT NULL, axis INTEGER NOT NULL, n INTEGER, mean REAL, min REAL, max REAL, absmax REAL,
  PRIMARY KEY (minute, axis));
CREATE TABLE IF NOT EXISTS features (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, axis INTEGER NOT NULL,
  F REAL, Fc REAL, Fv REAL, G REAL, ripple REAL, peak REAL, temp REAL, valid INTEGER, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_feat ON features(axis, ts);
CREATE TABLE IF NOT EXISTS alarms (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, axis INTEGER, kind TEXT, akey TEXT,
  level INTEGER, message TEXT, value REAL, acked INTEGER DEFAULT 0, cleared_ts REAL);
CREATE INDEX IF NOT EXISTS ix_alarm_ts ON alarms(ts);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, axis INTEGER, kind TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS calib (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, axis INTEGER, k REAL, zero REAL, note TEXT);
CREATE TABLE IF NOT EXISTS vib (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, axis INTEGER NOT NULL, direction TEXT NOT NULL,
  rms REAL, env REAL, mesh REAL, motor1x REAL, kurt REAL, temp REAL, source TEXT);
CREATE INDEX IF NOT EXISTS ix_vib ON vib(axis, direction, ts);
CREATE TABLE IF NOT EXISTS iron (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, axis INTEGER NOT NULL, wt_pct REAL NOT NULL,
  sample TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS ix_iron ON iron(axis, ts);
CREATE TABLE IF NOT EXISTS components (
  id INTEGER PRIMARY KEY AUTOINCREMENT, robot TEXT, axis INTEGER, comp TEXT NOT NULL,
  install_ts REAL, removed_ts REAL, life_h REAL, failed INTEGER, reason TEXT, note TEXT, teardown TEXT);
CREATE INDEX IF NOT EXISTS ix_comp ON components(comp, axis);
CREATE TABLE IF NOT EXISTS hours (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, hours REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notify_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, channel TEXT, target TEXT, level INTEGER,
  ok INTEGER, detail TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS ix_notify_ts ON notify_log(ts);
CREATE TABLE IF NOT EXISTS cycles (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, t0 REAL, code INTEGER NOT NULL, axis INTEGER NOT NULL,
  dur REAL, ovr REAL, temp REAL, n INTEGER, mean REAL, mean_abs REAL, rms REAL, peak REAL, std REAL,
  vib_r REAL, vib_a REAL, valid INTEGER, reason TEXT);
CREATE INDEX IF NOT EXISTS ix_cyc ON cycles(axis, code, ts);
CREATE INDEX IF NOT EXISTS ix_cyc_ts ON cycles(ts);
CREATE TABLE IF NOT EXISTS motion_daily (
  day TEXT NOT NULL, code INTEGER NOT NULL, axis INTEGER NOT NULL, n INTEGER, ts REAL,
  rms REAL, mean_abs REAL, peak REAL, vib_r REAL, vib_a REAL, temp REAL, dur REAL, mtime REAL,
  PRIMARY KEY (day, code, axis));
CREATE TABLE IF NOT EXISTS sys_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT, level INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, who TEXT, addr TEXT, action TEXT, ok INTEGER, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(ts);
CREATE TABLE IF NOT EXISTS raw_keep (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, t0 REAL NOT NULL, t1 REAL NOT NULL, axis INTEGER, ref TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS ix_raw_keep ON raw_keep(t0, t1);
"""

# 舊資料庫升級：缺的欄位用 ALTER TABLE 補上（不動既有資料）
MIGRATIONS = {
    "samples": {"d7": "REAL"},                    # 2.0：第 7 軸（行走軸 E1）
    "events": {"who": "TEXT", "detail": "TEXT", "undone_ts": "REAL", "undo_who": "TEXT", "undo_note": "TEXT",
               "mtime": "REAL"},                  # 2.0：mtime = 最後異動時間，給中央同步用
    "alarms": {"acked_by": "TEXT", "acked_ts": "REAL", "mtime": "REAL",
               "tmpl": "TEXT"},                   # 2.1：訊息模板（換語言顯示用，見 i18n.pack）
    "notify_log": {"robot": "TEXT", "site": "TEXT", "stage": "INTEGER", "akey": "TEXT", "rule": "TEXT",
                   "lang": "TEXT", "person": "TEXT", "ref": "TEXT", "dlr": "TEXT", "dlr_ts": "REAL",
                   "dlr_detail": "TEXT"},             # 2.1：收件人語言、姓名；簡訊送達回報
    "cycles": {"amb": "REAL"},                    # 2.1：環境溫度（溫升 ΔT＝外殼−環境）
    "motion_daily": {"amb": "REAL"},
}
SAMPLE_COLS = ("ts", "seg", "run", "ovr", "temp", "d1", "d2", "d3", "d4", "d5", "d6", "d7")

VIB_COLS = ("rms", "env", "mesh", "motor1x", "kurt")


def outside_keep(c, table: str = "samples") -> tuple[str, tuple]:
    """SQL 條件「這筆原始樣本不在任何保留片段內」與參數。
    用片段的最大長度限定 t0 範圍，才用得到索引：不加這個，每筆樣本都要比對全部片段，
    保留 180 天、上千個片段、上千萬筆樣本時會慢到數小時（30 天模擬時就已經要 17 秒）。"""
    mx = c.execute("SELECT MAX(t1-t0) FROM raw_keep").fetchone()[0]
    if mx is None:
        return "1", ()
    return (f"NOT EXISTS (SELECT 1 FROM raw_keep k WHERE k.t0<={table}.ts AND k.t0>={table}.ts-? "
            f"AND k.t1>={table}.ts)", (float(mx) + 1e-6,))


class Store:
    def __init__(self, path: str):
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)
            for table, cols in MIGRATIONS.items():
                have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                for col, typ in cols.items():
                    if col not in have:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ------------------------------------------------------------ 原始樣本
    def add_samples(self, batch: list[Sample]):
        if not batch:
            return
        rows = [(s.ts, s.seg, s.run, s.ovr, s.temp, *[s.dt.get(a) for a in range(1, 8)]) for s in batch]
        with self.conn() as c:
            c.executemany(f"INSERT INTO samples ({','.join(SAMPLE_COLS)}) VALUES ({','.join('?' * len(SAMPLE_COLS))})",
                          rows)

    def add_agg(self, rows: list[tuple]):
        if rows:
            with self.conn() as c:
                c.executemany("INSERT OR REPLACE INTO agg_1min VALUES (?,?,?,?,?,?,?)", rows)

    def samples(self, since: float, until: float | None = None) -> list[dict]:
        with self.conn() as c:
            cur = c.execute("SELECT * FROM samples WHERE ts>=? AND ts<=? ORDER BY ts",
                            (since, until or time.time() + 1))
            return [dict(r) for r in cur]

    def agg(self, axis: int, since: float) -> list[dict]:
        with self.conn() as c:
            cur = c.execute("SELECT * FROM agg_1min WHERE axis=? AND minute>=? ORDER BY minute",
                            (axis, int(since // 60)))
            return [dict(r) for r in cur]

    def forget_caches(self):
        """還原或外部改了資料庫之後呼叫：下次清理與動作代碼清單改回完整掃描。"""
        self._purged_to = None
        self._purge_full = None
        self._codes_full = None
        self._codes_seen = set()

    def purge(self, raw_days: float, keep_days: float = 180.0, now: float | None = None) -> int:
        """原始樣本超過 raw_days 刪除；但診斷段與警報前後的片段（raw_keep）保留到 keep_days。
        每小時只清「上次清理之後新過期的時段」與「剛到期的保留片段」；完整掃描每天一次（保留中的原始資料
        可能有幾百萬筆，每小時全部重掃會拖慢寫入）。"""
        now = now or time.time()
        cut, keep_cut = now - raw_days * 86400, now - keep_days * 86400
        lo = getattr(self, "_purged_to", None)
        full = lo is None or getattr(self, "_purge_full", None) is None or abs(now - self._purge_full) > 86400
        with self.conn() as c:
            expired = [] if full else c.execute("SELECT t0, t1 FROM raw_keep WHERE t1<?", (keep_cut,)).fetchall()
            c.execute("DELETE FROM raw_keep WHERE t1<?", (keep_cut,))
            cond, p = outside_keep(c)
            if full:
                n = c.execute(f"DELETE FROM samples WHERE ts<? AND {cond}", (cut, *p)).rowcount
                self._purge_full = now
            else:
                n = c.execute(f"DELETE FROM samples WHERE ts>=? AND ts<? AND {cond}", (lo - 3600, cut, *p)).rowcount
                for t0, t1 in expired:                     # 到期的保留片段（不在其他保留片段內的才刪）
                    n += c.execute(f"DELETE FROM samples WHERE ts>=? AND ts<=? AND ts<? AND {cond}",
                                   (t0, t1, cut, *p)).rowcount
        self._purged_to = cut
        return n

    def add_raw_keep(self, kind: str, t0: float, t1: float, axis: int | None = None, ref: str = ""):
        """標記要長期保留的原始資料片段：診斷段（diag）、警報前後（alarm）。"""
        with self.conn() as c:
            c.execute("INSERT INTO raw_keep (kind,t0,t1,axis,ref,ts) VALUES (?,?,?,?,?,?)",
                      (kind, t0, t1, axis, ref, time.time()))

    def raw_keeps(self, since: float = 0, until: float | None = None) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM raw_keep WHERE t1>=? AND t0<=? ORDER BY t0",
                                               (since, until or time.time() + 1e9))]

    def set_teardown(self, cid: int, teardown: str, failed: bool | None = None):
        with self.conn() as c:
            if failed is None:
                c.execute("UPDATE components SET teardown=? WHERE id=?", (teardown, cid))
            else:
                c.execute("UPDATE components SET teardown=?, failed=? WHERE id=?", (teardown, int(failed), cid))

    # ------------------------------------------------------------ 特徵
    def add_features(self, feats: list[RunFeatures]) -> list[int]:
        ids = []
        with self.conn() as c:
            for f in feats:
                cur = c.execute(
                    "INSERT INTO features (ts,axis,F,Fc,Fv,G,ripple,peak,temp,valid,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (f.ts, f.axis, f.F, f.Fc, f.Fv, f.G, f.ripple, f.peak, f.temp, int(f.valid),
                     json.dumps(f.detail, ensure_ascii=False)))
                ids.append(cur.lastrowid)
        return ids

    def features(self, axis: int, since: float = 0, until: float | None = None, valid_only=True) -> list[dict]:
        q = "SELECT * FROM features WHERE axis=? AND ts>=? AND ts<=?"
        if valid_only:
            q += " AND valid=1"
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(q + " ORDER BY ts", (axis, since, until or time.time() + 1e9))]
        for r in rows:
            r["detail"] = json.loads(r["detail"] or "{}")
        return rows

    def feature_values(self, axis: int, until: float | None = None) -> list[dict]:
        """只取數值欄位（不解析 detail），給跨軸共模計算用，比較快。"""
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, ts, F, Fc, Fv, temp FROM features WHERE axis=? AND valid=1 AND ts<=? ORDER BY ts",
                (axis, until or time.time() + 1e9))]

    def feature(self, fid: int) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM features WHERE id=?", (fid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["detail"] = json.loads(d["detail"] or "{}")
        return d

    def set_valid(self, fid: int, valid: bool):
        with self.conn() as c:
            c.execute("UPDATE features SET valid=? WHERE id=?", (int(valid), fid))

    # ------------------------------------------------------------ 警報
    def add_alarm(self, ts, axis, kind, akey, level, message, value) -> int:
        with self.conn() as c:
            cur = c.execute("INSERT INTO alarms (ts,axis,kind,akey,level,message,value,mtime,tmpl) VALUES (?,?,?,?,?,?,?,?,?)",
                            (ts, axis, kind, akey, level, str(message), value, time.time(), pack_json(message)))
            return cur.lastrowid

    def clear_alarm(self, aid: int, ts: float):
        with self.conn() as c:
            c.execute("UPDATE alarms SET cleared_ts=?, mtime=? WHERE id=? AND cleared_ts IS NULL", (ts, time.time(), aid))

    def ack_alarm(self, aid: int, who: str = ""):
        with self.conn() as c:
            c.execute("UPDATE alarms SET acked=1, acked_by=?, acked_ts=?, mtime=? WHERE id=? AND acked=0",
                      (who, time.time(), time.time(), aid))

    def alarms(self, limit: int = 100, active_only: bool = False) -> list[dict]:
        q = "SELECT * FROM alarms"
        if active_only:
            q += " WHERE cleared_ts IS NULL"
        with self.conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY ts DESC LIMIT ?", (limit,))]

    def alarm(self, aid: int) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM alarms WHERE id=?", (aid,)).fetchone()
        return dict(r) if r else None

    def alarms_since(self, mtime: float, limit: int = 2000) -> list[dict]:
        """中央同步用：有新增或異動（解除、確認）的警報。"""
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM alarms WHERE COALESCE(mtime, ts)>? ORDER BY COALESCE(mtime, ts) "
                                               "LIMIT ?", (mtime, limit))]

    def active_alarms(self) -> list[dict]:
        return self.alarms(limit=1000, active_only=True)

    # ------------------------------------------------------------ 事件與校正
    def add_event(self, ts: float, axis: int | None, kind: str, note: str = "", who: str = "") -> int:
        with self.conn() as c:
            cur = c.execute("INSERT INTO events (ts,axis,kind,note,who,mtime) VALUES (?,?,?,?,?,?)",
                            (ts, axis, kind, note, who, time.time()))
            return cur.lastrowid

    def set_event_detail(self, eid: int, detail: dict):
        with self.conn() as c:
            c.execute("UPDATE events SET detail=?, mtime=? WHERE id=?", (json.dumps(detail, ensure_ascii=False), time.time(), eid))

    @staticmethod
    def _event(r) -> dict:
        d = dict(r)
        d["detail"] = json.loads(d.get("detail") or "{}")
        return d

    def event(self, eid: int) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        return self._event(r) if r else None

    def events(self, axis: int | None = None, include_undone: bool = False) -> list[dict]:
        """預設不含已撤銷的事件（基準、壽命、報告都不該再算它）；儀表板清單用 include_undone=True。"""
        live = "" if include_undone else " AND undone_ts IS NULL"
        with self.conn() as c:
            if axis is None:
                cur = c.execute(f"SELECT * FROM events WHERE 1=1{live} ORDER BY ts, id")
            else:
                cur = c.execute(f"SELECT * FROM events WHERE (axis=? OR axis IS NULL){live} ORDER BY ts, id", (axis,))
            return [self._event(r) for r in cur]

    def last_reset(self, axis: int, before: float | None = None) -> float:
        kinds = ",".join("?" * len(RESET_KINDS))
        with self.conn() as c:
            r = c.execute(f"SELECT MAX(ts) FROM events WHERE (axis=? OR axis IS NULL) AND kind IN ({kinds}) "
                          "AND ts<=? AND undone_ts IS NULL",
                          (axis, *RESET_KINDS, before or time.time() + 1e9)).fetchone()
        return r[0] or 0.0

    def undo_event(self, eid: int, ts: float, who: str = "", note: str = ""):
        """在同一個交易裡：刪掉此事件新裝的元件、把它結束的舊元件恢復原狀、刪掉隨事件登錄的時數，並標記已撤銷。"""
        ev = self.event(eid)
        det = ev["detail"]
        with self.conn() as c:
            for cid in det.get("installed", []):
                c.execute("DELETE FROM components WHERE id=?", (cid,))
            for old in det.get("closed", []):
                c.execute("UPDATE components SET removed_ts=?, life_h=?, failed=?, reason=?, teardown=? WHERE id=?",
                          (old.get("removed_ts"), old.get("life_h"), old.get("failed"), old.get("reason"),
                           old.get("teardown"), old["id"]))
            if det.get("hours_id"):
                c.execute("DELETE FROM hours WHERE id=?", (det["hours_id"],))
            c.execute("UPDATE events SET undone_ts=?, undo_who=?, undo_note=?, mtime=? WHERE id=?",
                      (ts, who, note, time.time(), eid))

    def set_calib(self, axis: int, k: float, zero: float, note: str = ""):
        with self.conn() as c:
            c.execute("INSERT INTO calib (ts,axis,k,zero,note) VALUES (?,?,?,?,?)", (time.time(), axis, k, zero, note))

    def calib(self, axis: int) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM calib WHERE axis=? ORDER BY ts DESC LIMIT 1", (axis,)).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------ 振動
    def add_vib(self, ts: float, axis: int, direction: str, vals: dict, temp=None, source: str = "") -> int:
        if direction not in ("radial", "axial"):
            raise ValueError(_("direction 必須是 radial 或 axial"))
        with self.conn() as c:
            cur = c.execute("INSERT INTO vib (ts,axis,direction,rms,env,mesh,motor1x,kurt,temp,source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (ts, axis, direction, *[vals.get(k) for k in VIB_COLS], temp, source))
            return cur.lastrowid

    def vib(self, axis: int, since: float = 0, until: float | None = None) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM vib WHERE axis=? AND ts>=? AND ts<=? ORDER BY ts",
                                               (axis, since, until or time.time() + 1e9))]

    # ------------------------------------------------------------ 鐵粉
    def add_iron(self, ts: float, axis: int, wt_pct: float, sample: str = "drain_mid", note: str = "") -> int:
        from .config import IRON_SAMPLES
        if sample not in IRON_SAMPLES:
            raise ValueError(_("sample 必須是 {opts}", opts=_("、").join(f"{k}（{v}）" for k, v in IRON_SAMPLES.items())))
        with self.conn() as c:
            cur = c.execute("INSERT INTO iron (ts,axis,wt_pct,sample,note) VALUES (?,?,?,?,?)", (ts, axis, wt_pct, sample, note))
            return cur.lastrowid

    def iron(self, axis: int, until: float | None = None) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM iron WHERE axis=? AND ts<=? ORDER BY ts",
                                               (axis, until or time.time() + 1e9))]

    # ------------------------------------------------------------ 元件與時數
    def install(self, axis: int | None, comp: str, ts: float, robot: str = "", note: str = "") -> int:
        with self.conn() as c:
            cur = c.execute("INSERT INTO components (robot,axis,comp,install_ts,note) VALUES (?,?,?,?,?)",
                            (robot, axis, comp, ts, note))
            return cur.lastrowid

    def close_component(self, cid: int, ts: float, life_h: float | None, failed: bool, reason: str = "",
                        teardown: str = ""):
        with self.conn() as c:
            c.execute("UPDATE components SET removed_ts=?, life_h=?, failed=?, reason=?, teardown=? WHERE id=?",
                      (ts, life_h, int(failed), reason, teardown, cid))

    def add_history(self, comp: str, life_h: float, failed: bool, robot: str = "", axis: int | None = None,
                    reason: str = "", note: str = "") -> int:
        """其他機台或過去的更換紀錄（只需壽命時數與是否失效），供 Weibull 擬合。"""
        with self.conn() as c:
            cur = c.execute("INSERT INTO components (robot,axis,comp,life_h,failed,reason,note) VALUES (?,?,?,?,?,?,?)",
                            (robot, axis, comp, life_h, int(failed), reason, note))
            return cur.lastrowid

    def component(self, cid: int) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM components WHERE id=?", (cid,)).fetchone()
        return dict(r) if r else None

    def current_component(self, axis: int, comp: str, at: float | None = None) -> dict | None:
        at = at or time.time() + 1e9
        with self.conn() as c:
            r = c.execute("SELECT * FROM components WHERE axis=? AND comp=? AND install_ts<=? "
                          "AND (removed_ts IS NULL OR removed_ts>?) ORDER BY install_ts DESC LIMIT 1",
                          (axis, comp, at, at)).fetchone()
        return dict(r) if r else None

    def components(self, comp: str | None = None) -> list[dict]:
        with self.conn() as c:
            if comp:
                cur = c.execute("SELECT * FROM components WHERE comp=? ORDER BY COALESCE(install_ts,0)", (comp,))
            else:
                cur = c.execute("SELECT * FROM components ORDER BY COALESCE(install_ts,0)")
            return [dict(r) for r in cur]

    def add_hours(self, ts: float, hours: float) -> int:
        with self.conn() as c:
            return c.execute("INSERT INTO hours (ts,hours) VALUES (?,?)", (ts, hours)).lastrowid

    def hours(self) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM hours ORDER BY ts")]

    def temps(self, axis: int, since: float, until: float) -> list[tuple[float, float]]:
        with self.conn() as c:
            return [(r[0], r[1]) for r in c.execute(
                "SELECT ts, temp FROM features WHERE axis=? AND ts>=? AND ts<=? AND temp IS NOT NULL ORDER BY ts",
                (axis, since, until))]

    # ------------------------------------------------------------ 通知紀錄與 LINE 收件人
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
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM notify_log ORDER BY id DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------ 生產循環（動作代碼）
    CYCLE_COLS = ("ts", "t0", "code", "axis", "dur", "ovr", "temp", "amb", "n", "mean", "mean_abs", "rms", "peak", "std",
                  "vib_r", "vib_a", "valid", "reason")

    def add_cycles(self, rows: list[dict]):
        if not rows:
            return
        cols = self.CYCLE_COLS
        with self.conn() as c:
            c.executemany(f"INSERT INTO cycles ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          [tuple(r.get(k) for k in cols) for r in rows])
        if getattr(self, "_codes_full", None) is not None:          # 動作代碼清單跟著更新，不用再掃表
            self._codes_seen |= {(r.get("code"), r.get("axis")) for r in rows}

    def cycles(self, axis: int | None = None, code: int | None = None, since: float = 0,
               until: float | None = None, valid_only: bool = True) -> list[dict]:
        q, args = "SELECT * FROM cycles WHERE ts>=? AND ts<=?", [since, until or time.time() + 1e9]
        if axis is not None:
            q += " AND axis=?"
            args.append(axis)
        if code is not None:
            q += " AND code=?"
            args.append(code)
        if valid_only:
            q += " AND valid=1"
        with self.conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY ts", args)]

    def cycle_codes(self) -> list[tuple[int, int]]:
        """有資料的（代碼, 軸）組合。逐筆循環表會長到幾百萬筆，DISTINCT 要掃整個索引（100 萬筆約 50 毫秒，
        每 10 分鐘每一軸都要用）：完整掃描每天一次，其他時候用記住的清單＋這個程式新寫入的循環＋每日中位數表。
        別的程式改了資料庫（還原）要呼叫 forget_caches()。"""
        now = time.time()
        full_ts = getattr(self, "_codes_full", None)
        with self.conn() as c:
            a = {(r[0], r[1]) for r in c.execute("SELECT DISTINCT code, axis FROM motion_daily")}
            if full_ts is None or abs(now - full_ts) > 86400:
                self._codes_seen = {(r[0], r[1]) for r in c.execute("SELECT DISTINCT code, axis FROM cycles")}
                self._codes_full = now
        return sorted(a | self._codes_seen)

    def cycle_count(self, since: float, until: float | None = None) -> list[dict]:
        """每小時循環次數（以 J 軸最小的那一筆計，一個循環只算一次）。"""
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT CAST(ts/3600 AS INTEGER) AS hour, code, COUNT(DISTINCT ts) AS n FROM cycles "
                "WHERE ts>=? AND ts<=? GROUP BY hour, code ORDER BY hour", (since, until or time.time() + 1))]

    def upsert_daily(self, rows: list[dict]):
        cols = ("day", "code", "axis", "n", "ts", "rms", "mean_abs", "peak", "vib_r", "vib_a", "temp", "amb", "dur", "mtime")
        with self.conn() as c:
            c.executemany(f"INSERT OR REPLACE INTO motion_daily ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          [tuple(r.get(k) if k != "mtime" else time.time() for k in cols) for r in rows])

    def daily(self, axis: int, code: int, since_day: str = "") -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM motion_daily WHERE axis=? AND code=? AND day>=? ORDER BY day",
                                               (axis, code, since_day))]

    def daily_since(self, mtime: float, limit: int = 5000) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM motion_daily WHERE mtime>? ORDER BY mtime LIMIT ?",
                                               (mtime, limit))]

    def purge_cycles(self, days: float) -> int:
        with self.conn() as c:
            return c.execute("DELETE FROM cycles WHERE ts<?", (time.time() - days * 86400,)).rowcount

    # ------------------------------------------------------------ 系統事件（重啟、看門狗、磁碟、自我檢查）
    def add_sys_event(self, kind: str, level: int, detail: str = "", ts: float | None = None):
        with self.conn() as c:
            c.execute("INSERT INTO sys_events (ts,kind,level,detail) VALUES (?,?,?,?)",
                      (ts or time.time(), kind, level, detail[:500]))

    def sys_events(self, since: float = 0, limit: int = 200) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM sys_events WHERE ts>=? ORDER BY id DESC LIMIT ?",
                                               (since, limit))]

    # ------------------------------------------------------------ 操作紀錄（誰在什麼時候寫入了什麼）
    def add_audit(self, who: str, addr: str, action: str, ok: bool, detail: str = ""):
        with self.conn() as c:
            c.execute("INSERT INTO audit (ts,who,addr,action,ok,detail) VALUES (?,?,?,?,?,?)",
                      (time.time(), who, addr, action, int(ok), detail[:500]))

    def audit(self, limit: int = 100) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]

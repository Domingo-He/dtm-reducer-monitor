"""P2 診斷期資料：每一期一個資料夾（session.json、program.ls、pro.sqlite）。

只保留「前 N 個循環＋之後每 M 個循環＋異常循環」的原始波形；其他循環只留逐行統計，7 天的資料量才不會失控。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
import zlib
from array import array

from .. import runtime as R
from ..i18n import Msg, TDict, localize, templates
from . import lsparse

SCHEMA = """
CREATE TABLE IF NOT EXISTS cyc (
  cyc INTEGER PRIMARY KEY, t0 REAL, t1 REAL, dur REAL, status TEXT, conf REAL, fit REAL,
  dmg TEXT, peak TEXT, lines BLOB);
CREATE INDEX IF NOT EXISTS cyc_t0 ON cyc(t0);
CREATE TABLE IF NOT EXISTS raw (cyc INTEGER PRIMARY KEY, why TEXT, n INTEGER, blob BLOB);
CREATE TABLE IF NOT EXISTS angle (
  ax INTEGER, bin INTEGER, dmg REAL DEFAULT 0, dwell REAL DEFAULT 0, hold REAL DEFAULT 0,
  passes INTEGER DEFAULT 0, rev INTEGER DEFAULT 0, fret INTEGER DEFAULT 0, PRIMARY KEY (ax, bin));
CREATE TABLE IF NOT EXISTS osc (ax INTEGER, amp INTEGER, n INTEGER DEFAULT 0, PRIMARY KEY (ax, amp));
CREATE TABLE IF NOT EXISTS impact (
  id INTEGER PRIMARY KEY, t REAL, kind TEXT, code TEXT, text TEXT, cyc INTEGER, line INTEGER,
  peak REAL, crest REAL, weight REAL, src TEXT);
CREATE INDEX IF NOT EXISTS impact_t ON impact(t);
CREATE TABLE IF NOT EXISTS model (k TEXT PRIMARY KEY, v TEXT);
"""

ROLES = ("diag", "before", "after", "control_before", "control_after")
ROLE_LABELS = TDict({"diag": "診斷", "before": "改前", "after": "改後",
                     "control_before": "對照組（改前期間）", "control_after": "對照組（改後期間）"})
OSC_EDGES = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 45.0, 90.0, 180.0)   # 擺動幅度分組（度；直線軸為 mm÷10）


def pro_dir(cfg: dict) -> str:
    d = R.resolve(cfg, cfg["pro"]["data_dir"])
    os.makedirs(d, exist_ok=True)
    return d


def _sid(robot: str, role: str, t: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + "-" + re.sub(r"[^A-Za-z0-9_-]", "", robot or "R")[:24] + "-" + role


def pack(cols: dict[str, list]) -> bytes:
    """原始波形壓縮：每個欄位存 float32（時間存相對循環起點的秒數）。"""
    names = list(cols)
    n = len(cols[names[0]]) if names else 0
    head = json.dumps({"n": n, "ch": names}).encode()
    body = b"".join(array("f", [float(x) for x in cols[k]]).tobytes() for k in names)
    return zlib.compress(len(head).to_bytes(4, "little") + head + body, 6)


def unpack(blob: bytes) -> dict[str, list[float]]:
    b = zlib.decompress(blob)
    hl = int.from_bytes(b[:4], "little")
    head = json.loads(b[4:4 + hl])
    n, out, off = head["n"], {}, 4 + hl
    for k in head["ch"]:
        a = array("f")
        a.frombytes(b[off:off + 4 * n])
        out[k] = a.tolist()
        off += 4 * n
    return out


TEXT_KEYS = ("label", "note", "change")


def local_meta(meta: dict) -> dict:
    """session.json 的內容，說明文字依目前語言（有模板的才換）；不含 tmpl。"""
    m = {k: v for k, v in meta.items() if k != "tmpl"}
    return localize(m, meta.get("tmpl"))


class Session:
    """一個診斷期。執行緒安全（網頁與接收串流可能同時讀寫）。"""

    def __init__(self, path: str):
        self.path = path
        self.id = os.path.basename(path)
        self._lock = threading.RLock()
        self.meta = R.read_json(os.path.join(path, "session.json"), {}) or {}
        self.db = sqlite3.connect(os.path.join(path, "pro.sqlite"), check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._prog = None
        self._raw_cache: dict[int, dict] = {}

    # ------------------------------------------------------------ 建立、列出
    @classmethod
    def create(cls, cfg: dict, robot: str, program_text: str, role: str = "diag", label: str = "",
               group: str = "", source: str = "csv", note: str = "", t: float | None = None, **extra) -> "Session":
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        prog = lsparse.parse(program_text)
        t = time.time() if t is None else t
        base = pro_dir(cfg)
        sid = _sid(robot, role, t)
        path, k = os.path.join(base, sid), 1
        while os.path.exists(path):
            k += 1
            path = os.path.join(base, f"{sid}-{k}")
        os.makedirs(path)
        with open(os.path.join(path, "program.ls"), "w", encoding="utf-8") as f:
            f.write(program_text)
        p = cfg["pro"]
        meta = {"id": os.path.basename(path), "robot": robot, "role": role, "label": label, "group": group,
                "source": source, "note": note, "created": t, "closed": None, "status": "open",
                "program": {"name": prog.name, "version": prog.version, "comment": prog.comment},
                "sample_hz": p["sample_hz"], "torque": p["torque"], "torque_unit": p["torque_unit"],
                "max_days": p["max_days"], "t0": None, "t1": None, "n_samples": 0, "n_cycles": 0,
                "cycles_per_day": 0, "hours_per_day": 20.0, "change": ""}
        meta.update(extra)
        side = templates({k: meta.get(k) for k in TEXT_KEYS})     # 程式產生的說明（例如演示）可以換語言顯示
        if side:
            meta["tmpl"] = side
        R.write_json(os.path.join(path, "session.json"), meta)
        return cls(path)

    def text(self, key: str) -> str:
        """label／note／change 依目前語言顯示。"""
        return local_meta(self.meta).get(key) or ""

    @staticmethod
    def list(cfg: dict) -> list[dict]:
        base = pro_dir(cfg)
        out = []
        for name in sorted(os.listdir(base), reverse=True):
            m = R.read_json(os.path.join(base, name, "session.json"), None)
            if m:
                out.append(m)
        return out

    @classmethod
    def open(cls, cfg: dict, sid: str) -> "Session":
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", sid or "") or sid.startswith("."):
            raise KeyError(sid)
        path = os.path.join(pro_dir(cfg), sid)
        if not os.path.exists(os.path.join(path, "session.json")):
            raise KeyError(sid)
        return cls(path)

    def delete(self):
        self.close()
        shutil.rmtree(self.path, ignore_errors=True)

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass

    # ------------------------------------------------------------ 中繼資料與程式
    def save_meta(self, **kw):
        with self._lock:
            side = dict(self.meta.get("tmpl") or {})
            for k in TEXT_KEYS:
                if k in kw:
                    t = templates(kw[k]) if isinstance(kw[k], Msg) else None
                    if t:
                        side[k] = t
                    else:
                        side.pop(k, None)               # 使用者改寫的文字照原樣顯示
            self.meta.update(kw)
            self.meta["tmpl"] = side
            R.write_json(os.path.join(self.path, "session.json"), self.meta)

    def program_text(self) -> str:
        with open(os.path.join(self.path, "program.ls"), encoding="utf-8", errors="replace") as f:
            return f.read()

    def program(self) -> lsparse.Program:
        if self._prog is None:
            self._prog = lsparse.parse(self.program_text())
        return self._prog

    # ------------------------------------------------------------ 模型（學到的路徑、統計）
    def get(self, k: str, default=None):
        with self._lock:
            r = self.db.execute("SELECT v FROM model WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, k: str, v):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO model(k, v) VALUES (?, ?)", (k, json.dumps(v)))
            self.db.commit()

    # ------------------------------------------------------------ 循環
    def add_cycle(self, rec: dict, raw: dict | None = None, why: str = ""):
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO cyc(cyc, t0, t1, dur, status, conf, fit, dmg, peak, lines) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rec["cyc"], rec["t0"], rec["t1"], rec["dur"], rec["status"], rec.get("conf"), rec.get("fit"),
                 json.dumps(rec.get("dmg", {})), json.dumps(rec.get("peak", {})),
                 zlib.compress(json.dumps(rec.get("lines", {})).encode(), 6)))
            if raw is not None:
                self.db.execute("INSERT OR REPLACE INTO raw(cyc, why, n, blob) VALUES (?,?,?,?)",
                                (rec["cyc"], why, len(raw["t"]), pack(raw)))
            self.db.commit()

    def cycles(self, status: tuple | None = ("ok", "anomaly"), with_lines: bool = True, t0: float | None = None,
               t1: float | None = None, ids: list[int] | None = None) -> list[dict]:
        q, args = "SELECT cyc, t0, t1, dur, status, conf, fit, dmg, peak" + (", lines" if with_lines else "") + " FROM cyc WHERE 1=1", []
        if ids is not None:
            if not ids:
                return []
            q += " AND cyc IN (%s)" % ",".join(str(int(i)) for i in ids)
        if status:
            q += " AND status IN (%s)" % ",".join("?" * len(status))
            args += list(status)
        if t0 is not None:
            q += " AND t0>=?"
            args.append(t0)
        if t1 is not None:
            q += " AND t0<?"
            args.append(t1)
        with self._lock:
            rows = self.db.execute(q + " ORDER BY cyc", args).fetchall()
        out = []
        for r in rows:
            d = {"cyc": r[0], "t0": r[1], "t1": r[2], "dur": r[3], "status": r[4], "conf": r[5], "fit": r[6],
                 "dmg": json.loads(r[7] or "{}"), "peak": json.loads(r[8] or "{}")}
            if with_lines:
                d["lines"] = json.loads(zlib.decompress(r[9])) if r[9] else {}
            out.append(d)
        return out

    def cycle_counts(self) -> dict:
        with self._lock:
            return dict(self.db.execute("SELECT status, COUNT(*) FROM cyc GROUP BY status").fetchall())

    def raw_list(self) -> list[tuple[int, str]]:
        with self._lock:
            return self.db.execute("SELECT cyc, why FROM raw ORDER BY cyc").fetchall()

    def raw(self, cyc: int) -> dict | None:
        """原始波形（最近用過的 48 個循環留在記憶體）。"""
        hit = self._raw_cache.get(cyc)
        if hit is not None:
            return hit
        with self._lock:
            r = self.db.execute("SELECT blob FROM raw WHERE cyc=?", (cyc,)).fetchone()
        if not r:
            return None
        d = unpack(r[0])
        self._raw_cache[cyc] = d
        if len(self._raw_cache) > 48:
            self._raw_cache.pop(next(iter(self._raw_cache)))
        return d

    def stamp(self) -> tuple:
        """資料版本（快取用）：循環數、事件數、最後更新時間。"""
        with self._lock:
            a = self.db.execute("SELECT COUNT(*), MAX(cyc) FROM cyc").fetchone()
            b = self.db.execute("SELECT COUNT(*), MAX(id) FROM impact").fetchone()
        return (a[0], a[1], b[0], b[1], self.meta.get("updated"), self.meta.get("cycles_per_day"),
                self.meta.get("hours_per_day"))

    def cycle_at(self, t: float) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT cyc, t0, t1, lines FROM cyc WHERE t0<=? AND t1>=? ORDER BY t0 DESC LIMIT 1",
                                (t, t)).fetchone()
        if not r:
            return None
        return {"cyc": r[0], "t0": r[1], "t1": r[2], "lines": json.loads(zlib.decompress(r[3])) if r[3] else {}}

    # ------------------------------------------------------------ 角度累積（P6）
    def add_angle(self, acc: dict, osc: dict):
        """acc[ax][bin] = [dmg, dwell, hold, passes, rev, fret]；osc[ax][amp_idx] = n。"""
        with self._lock:
            c = self.db.cursor()
            for ax, bins in acc.items():
                c.executemany(
                    "INSERT INTO angle(ax, bin, dmg, dwell, hold, passes, rev, fret) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(ax, bin) DO UPDATE SET dmg=dmg+excluded.dmg, dwell=dwell+excluded.dwell, "
                    "hold=hold+excluded.hold, passes=passes+excluded.passes, rev=rev+excluded.rev, fret=fret+excluded.fret",
                    [(int(ax), int(b), *v) for b, v in bins.items()])
            for ax, h in osc.items():
                c.executemany("INSERT INTO osc(ax, amp, n) VALUES (?,?,?) ON CONFLICT(ax, amp) DO UPDATE SET n=n+excluded.n",
                              [(int(ax), int(a), int(n)) for a, n in h.items()])
            self.db.commit()

    def angle(self, ax: int) -> list[tuple]:
        with self._lock:
            return self.db.execute("SELECT bin, dmg, dwell, hold, passes, rev, fret FROM angle WHERE ax=? ORDER BY bin",
                                   (ax,)).fetchall()

    def osc(self, ax: int) -> dict[int, int]:
        with self._lock:
            return dict(self.db.execute("SELECT amp, n FROM osc WHERE ax=?", (ax,)).fetchall())

    # ------------------------------------------------------------ 衝擊與停止事件（P11）
    def add_impacts(self, rows: list[dict], replace_src: str | None = None):
        with self._lock:
            if replace_src:
                self.db.execute("DELETE FROM impact WHERE src=?", (replace_src,))
            self.db.executemany(
                "INSERT INTO impact(t, kind, code, text, cyc, line, peak, crest, weight, src) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(r["t"], r["kind"], r.get("code", ""), r.get("text", ""), r.get("cyc"), r.get("line"),
                  r.get("peak"), r.get("crest"), r.get("weight", 1.0), r.get("src", "")) for r in rows])
            self.db.commit()

    def impacts(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT t, kind, code, text, cyc, line, peak, crest, weight, src FROM impact ORDER BY t").fetchall()
        keys = ("t", "kind", "code", "text", "cyc", "line", "peak", "crest", "weight", "src")
        return [dict(zip(keys, r)) for r in rows]

    def reset_data(self):
        """重新分析前清掉結果（程式與中繼資料保留）。"""
        with self._lock:
            for tb in ("cyc", "raw", "angle", "osc", "model"):
                self.db.execute(f"DELETE FROM {tb}")
            self.db.execute("DELETE FROM impact WHERE src='acc'")
            self.db.commit()

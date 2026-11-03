"""P2 資料取得：CSV 匯入、控制器串流接收（TCP）。

CSV 欄位（第一列是標題，順序不拘）：
  t            時間（秒；Unix 時間或從 0 起算都可以；毫秒的 Unix 時間會自動換算）
  q1…q6、e1    各軸角度（度；E1 為 mm）。也接受 j1…j6、J1…J6
  tq1…tq6、tqe1 各軸扭矩（單位依 [pro] torque_unit）。也接受 trq1…、torque1…
  line         加註版程式回報的行號（R[114]）；掃描程式的段落代碼 8000＋軸×10＋方向也放這裡
  true_line    （只有演示資料有）真正在執行的行，用來驗證行號對位
"""
from __future__ import annotations

import csv
import logging
import re
import socketserver
import threading
import time

from .. import runtime as R
from ..i18n import _
from . import sweep
from .analyze import Processor
from .store import Session

log = logging.getLogger("dtm.pro")


def std_name(h: str) -> str | None:
    h = h.strip().lower().replace(" ", "")
    if h in ("t", "time", "timestamp", "ts", "t_ms", "time_ms"):
        return "t_ms" if h in ("t_ms", "time_ms") else "t"
    if h in ("line", "code", "r114", "reg", "line_no", "lineno"):
        return "line"
    if h in ("true_line", "truth"):
        return "true_line"
    m = re.fullmatch(r"(?:q|j|pos)(\d)", h)
    if m and 1 <= int(m.group(1)) <= 7:
        return f"q{m.group(1)}"
    if h in ("e1", "qe1", "pose1"):
        return "q7"
    m = re.fullmatch(r"(?:tq|trq|torque|t)(\d)", h)
    if m and 1 <= int(m.group(1)) <= 7:
        return f"tq{m.group(1)}"
    if h in ("tqe1", "trqe1", "torquee1"):
        return "tq7"
    return None


def csv_blocks(path: str, block: int = 5000):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.reader(f)
        head = next(rd)
        names = [std_name(h) for h in head]
        if "t" not in names and "t_ms" not in names:
            raise ValueError(_("CSV 缺少時間欄位 t"))
        idx = [(i, n) for i, n in enumerate(names) if n]
        cols = {n if n != "t_ms" else "t": [] for _i, n in idx}
        ms = "t_ms" in names
        conv = {}
        k = 0
        for row in rd:
            if not row:
                continue
            try:
                vals = [(n, float(row[i])) for i, n in idx]
            except (ValueError, IndexError):
                continue
            for n, v in vals:
                if n == "t_ms":
                    cols["t"].append(v / 1000.0)
                elif n in ("line", "true_line"):
                    cols[n].append(int(v))
                else:
                    cols[n].append(v)
            k += 1
            if k >= block:
                yield _fix_time(cols, ms, conv)
                cols = {c: [] for c in cols}
                k = 0
        if k:
            yield _fix_time(cols, ms, conv)


def _fix_time(cols: dict, ms: bool, st: dict) -> dict:
    t = cols["t"]
    if t and not ms and t[0] > 1e11:                    # 毫秒的 Unix 時間
        cols["t"] = [x / 1000.0 for x in t]
    return cols


class SweepSink:
    """匯入時遇到掃描程式的段落，收集起來，結束時存到掃描資料庫。"""

    def __init__(self, cfg: dict, robot: str):
        self.cfg, self.robot, self.cols = cfg, robot, {}

    def __call__(self, row: dict):
        for k, v in row.items():
            self.cols.setdefault(k, []).append(v)

    def save(self) -> list:
        if not self.cols.get("t"):
            return []
        res = sweep.import_cols(self.cfg, self.robot, self.cols)
        self.cols = {}
        return res


def import_csv(cfg: dict, sess: Session, path: str, progress=None) -> dict:
    sw = SweepSink(cfg, sess.meta.get("robot", ""))
    P = Processor(cfg, sess, sweep_sink=sw)
    n = 0
    t0 = time.time()
    for blk in csv_blocks(path):
        P.feed(blk)
        n += len(blk["t"])
        if progress:
            progress(n)
    P.flush()
    sweeps = sw.save()
    return {"samples": n, "cycles": sess.cycle_counts(), "sweeps": sweeps, "seconds": round(time.time() - t0, 1)}


# ------------------------------------------------------------ 控制器串流（KAREL 範本 robot/DTM_STREAM.KL）
class Listener:
    """接收控制器串流：每行 "ms,line,q1..q6,tq1..tq6"（有 E1 時再加 e1、tqe1）。

    只接受允許清單的 IP（[pro] listen_allow；空白 = [source.fanuc_http] host 與 [[robots]] host）。
    每台機器人的資料進它目前「進行中」的診斷期；超過 max_days 自動結束。
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        p = cfg["pro"]
        allow = list(p.get("listen_allow") or [])
        self.robot_of: dict[str, str] = {}
        host = cfg.get("source", {}).get("fanuc_http", {}).get("host")
        if host:
            self.robot_of[host] = R.robot_id(cfg)
        for r in cfg.get("robots") or []:
            if r.get("host"):
                self.robot_of[str(r["host"])] = str(r.get("id"))
        self.allow = set(allow) | set(self.robot_of)
        self.proc: dict[str, Processor] = {}
        self.lock = threading.Lock()
        self.last_raw: dict[str, float] = {}

    def processor(self, robot: str) -> Processor | None:
        with self.lock:
            P = self.proc.get(robot)
            if P and not P.closed:
                return P
            for m in Session.list(self.cfg):
                if m.get("robot") == robot and m.get("status") == "open" and m.get("source") == "listen":
                    P = Processor(self.cfg, Session.open(self.cfg, m["id"]),
                                  sweep_sink=SweepSink(self.cfg, robot))
                    self.proc[robot] = P
                    return P
        return None

    def handle(self, addr: str, lines):
        robot = self.robot_of.get(addr) or addr
        P = self.processor(robot)
        if not P:
            log.warning(_("%s 沒有進行中的診斷期（來源 listen），資料略過"), robot)
            return
        names: list[str] = []
        cols: dict[str, list] = {}
        maxd = float(P.s.meta.get("max_days") or self.cfg["pro"]["max_days"])
        for raw in lines:
            parts = raw.strip().split(",")
            if not names:                                   # 欄位數決定軸數：2 + 2×軸數（7 軸 = J1–J6＋E1）
                n_ax = (len(parts) - 2) // 2
                if n_ax < 1 or len(parts) != 2 + 2 * n_ax:
                    continue
                axes = P.axes or list(range(1, n_ax + 1))
                names = ["t", "line"] + [f"q{a}" for a in axes] + [f"tq{a}" for a in axes]
                cols = {k: [] for k in names}
            if len(parts) != len(names):
                continue
            try:
                v = [float(x) for x in parts]
            except ValueError:
                continue
            cols["t"].append(v[0] / 1000.0)
            cols["line"].append(int(v[1]))
            for k, x in zip(names[2:], v[2:]):
                cols[k].append(x)
            if len(cols["t"]) >= 500:
                self._feed(P, cols, maxd)
                cols = {k: [] for k in names}
        if cols.get("t"):
            self._feed(P, cols, maxd)

    def _feed(self, P: Processor, cols: dict, maxd: float):
        # KAREL 的毫秒計數不是 Unix 時間：第一筆對齊電腦時間；控制器重開機（計數歸零）時重新對齊
        off = P.s.meta.get("clock_offset")
        last = self.last_raw.get(P.s.id)
        if off is None or (last is not None and cols["t"][0] < last - 1.0):
            off = time.time() - cols["t"][0]
            P.s.save_meta(clock_offset=off)
        self.last_raw[P.s.id] = cols["t"][-1]
        cols["t"] = [x + off for x in cols["t"]]
        P.feed(cols)
        t0 = P.s.meta.get("t0") or cols["t"][0]
        if cols["t"][-1] - t0 > maxd * 86400:
            P.close_session()
            if P.sweep_sink:
                P.sweep_sink.save()
            log.info(_("%s 診斷期滿 %s 天，自動結束"), P.s.id, maxd)

    def serve(self, host: str | None = None, port: int | None = None):
        p = self.cfg["pro"]
        me = self

        class H(socketserver.BaseRequestHandler):
            def handle(self):
                ip = self.client_address[0]
                loop = ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
                if not (ip in me.allow or (not me.allow and loop)):           # 沒設定允許清單時只接受本機
                    log.warning(_("拒絕串流連線：%s 不在允許清單"), ip)
                    return
                buf, rest = [], b""
                while True:
                    chunk = self.request.recv(65536)
                    if not chunk:
                        break
                    parts = re.split(rb"[\r\n]+", rest + chunk)       # KAREL 的 CR 可能只送 \r
                    rest = parts.pop()
                    buf.extend(x.decode("ascii", "replace") for x in parts if x)
                    if len(buf) >= 200:
                        me.handle(ip, buf)
                        buf = []
                if rest:
                    buf.append(rest.decode("ascii", "replace"))
                if buf:
                    me.handle(ip, buf)
                P = me.proc.get(me.robot_of.get(ip) or ip)
                if P:
                    P._save(force=True)

        class S(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        srv = S((host or p["listen_host"], int(port or p["listen_port"])), H)
        log.info(_("Pro 串流接收：%s:%s（允許：%s）"), host or p["listen_host"], port or p["listen_port"],
                 ", ".join(sorted(self.allow)) or "-")
        return srv

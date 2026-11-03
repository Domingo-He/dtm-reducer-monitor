"""P7 全範圍掃描與局部摩擦比較（局部磨耗指數 LWI）。

定期（範例每週）執行 robot/DTM_SWEEP.LS：每軸在無干涉的最大範圍內慢速正轉、反轉各一次，
行號暫存器寫 8000 + 軸×10 + 方向（1 正轉、2 反轉）。
依角度區間：F(θ) = (τ₊ − τ₋)/2（摩擦）、G(θ) = (τ₊ + τ₋)/2（重力與平衡器）、漣波 = 等速段扭矩標準差。
LWI = 高使用區（P6 損耗密度最高、合計 50%）的 F 中位數 ÷ 低使用區的 F 中位數；相對自己第一次掃描的倍數判定。
同一次掃描內的比值，環境溫度的共同影響大致抵銷。
"""
from __future__ import annotations

import math
import os
import sqlite3
import threading
import time

from ..i18n import _
from . import views
from .store import Session, pro_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS run (id INTEGER PRIMARY KEY, robot TEXT, t REAL, ax INTEGER, speed REAL, n INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS run_robot ON run(robot, ax, t);
CREATE TABLE IF NOT EXISTS bin (run INTEGER, b INTEGER, tp REAL, tn REAL, np INTEGER, nn INTEGER, rp REAL, rn REAL,
  PRIMARY KEY (run, b));
"""
_lock = threading.Lock()


def db(cfg: dict) -> sqlite3.Connection:
    c = sqlite3.connect(os.path.join(pro_dir(cfg), "sweeps.sqlite"), check_same_thread=False, timeout=30)
    c.executescript(SCHEMA)
    return c


def split(cols: dict) -> dict[int, dict[int, list[int]]]:
    """依掃描代碼分組：{軸: {方向: [樣本索引]}}。"""
    out: dict[int, dict[int, list[int]]] = {}
    for i, c in enumerate(cols.get("line") or []):
        c = int(c)
        if 8000 <= c < 9000:
            ax, d = (c - 8000) // 10, c % 10
            if d in (1, 2) and 1 <= ax <= 7:
                out.setdefault(ax, {}).setdefault(d, []).append(i)
    return out


def import_cols(cfg: dict, robot: str, cols: dict, note: str = "") -> list[dict]:
    """把一次（或數次）掃描的取樣存起來；回傳每軸的結果摘要。"""
    groups = split(cols)
    ts = cols["t"]
    res = []
    with _lock:
        c = db(cfg)
        for ax, dirs in sorted(groups.items()):
            if 1 not in dirs or 2 not in dirs:
                continue
            bw = 10.0 if ax == 7 else 1.0
            th, tq = cols[f"q{ax}"], cols[f"tq{ax}"]
            per = {}
            speed = []
            for d, idx in dirs.items():
                if len(idx) < 20:
                    continue
                hz = (len(idx) - 1) / max(1e-9, ts[idx[-1]] - ts[idx[0]])
                w = [abs(th[idx[min(len(idx) - 1, k + 1)]] - th[idx[max(0, k - 1)]]) * hz / 2 for k in range(len(idx))]
                wm = views._med(w)
                speed.append(wm)
                for k, i in enumerate(idx):
                    if w[k] < 0.8 * wm:                     # 只用等速段
                        continue
                    b = int(th[i] // bw)
                    s = per.setdefault(b, {1: [], 2: []})
                    s[d].append(tq[i])
            rows = []
            for b, s in per.items():
                if len(s[1]) < 2 or len(s[2]) < 2:
                    continue
                mp, mn = sum(s[1]) / len(s[1]), sum(s[2]) / len(s[2])
                sp = math.sqrt(sum((x - mp) ** 2 for x in s[1]) / (len(s[1]) - 1))
                sn = math.sqrt(sum((x - mn) ** 2 for x in s[2]) / (len(s[2]) - 1))
                rows.append((b, mp, mn, len(s[1]), len(s[2]), sp, sn))
            if not rows:
                continue
            t = ts[dirs[1][0]]
            cur = c.execute("INSERT INTO run(robot, t, ax, speed, n, note) VALUES (?,?,?,?,?,?)",
                            (robot, t, ax, round(views._med(speed), 3), len(rows), note))
            rid = cur.lastrowid
            c.executemany("INSERT INTO bin(run, b, tp, tn, np, nn, rp, rn) VALUES (?,?,?,?,?,?,?,?)",
                          [(rid, *r) for r in rows])
            res.append({"run": rid, "ax": ax, "t": t, "bins": len(rows)})
        c.commit()
        c.close()
    return res


def _usage(cfg: dict, robot: str, ax: int, sid: str | None = None):
    """取該機器人最近一期（或指定期）的角度使用分布，決定高使用區 U 與低使用區 R。"""
    cands = [m for m in Session.list(cfg) if m.get("robot") == robot and ax in (m.get("axes") or [])]
    if sid:
        cands = [m for m in cands if m["id"] == sid]
    for m in cands:
        s = Session.open(cfg, m["id"])
        try:
            A = views.angle_map(cfg, s, ax)
        finally:
            s.close()
        if A and A.get("U"):
            return m["id"], A
    return None, None


def analyze(cfg: dict, robot: str, ax: int, sid: str | None = None) -> dict:
    p = cfg["pro"]
    with _lock:
        c = db(cfg)
        runs = c.execute("SELECT id, t, speed, n, note FROM run WHERE robot=? AND ax=? ORDER BY t", (robot, ax)).fetchall()
        bins = {r[0]: c.execute("SELECT b, tp, tn, np, nn, rp, rn FROM bin WHERE run=? ORDER BY b", (r[0],)).fetchall()
                for r in runs}
        c.close()
    use_sid, A = _usage(cfg, robot, ax, sid)
    U = set(A["U"]) if A else set()
    R = set(A["R"]) if A else set()
    out = []
    base = None
    for rid, t, speed, n, note in runs:
        F = {b: (tp - tn) / 2 for b, tp, tn, *_ in bins[rid]}
        fu = [F[b] for b in U if b in F]
        fr = [F[b] for b in R if b in F]
        lwi = (views._med(fu) / views._med(fr)) if fu and fr and views._med(fr) > 0 else None
        rec = {"run": rid, "t": t, "ts": time.strftime("%Y-%m-%d %H:%M", time.localtime(t)), "speed": speed, "note": note,
               "F_med": round(views._med(list(F.values())), 3), "lwi": round(lwi, 3) if lwi else None,
               "nU": len(fu), "nR": len(fr)}
        if lwi and base is None:
            base = lwi
        if lwi and base:
            rec["ratio"] = round(lwi / base, 3)
            rec["level"] = 2 if lwi / base >= p["lwi_alert"] else 1 if lwi / base >= p["lwi_caution"] else 0
        out.append(rec)
    curve = {}
    if runs:
        first, last = runs[0][0], runs[-1][0]
        f0 = {b: (tp - tn) / 2 for b, tp, tn, *_ in bins[first]}
        g0 = {b: (tp + tn) / 2 for b, tp, tn, *_ in bins[first]}
        bw = 10.0 if ax == 7 else 1.0
        curve = {"deg": [], "F0": [], "F": [], "G0": [], "G": [], "rip": [], "use": []}
        dens = {r["b"]: r["d"] for r in (A["bins"] if A else [])}
        for b, tp, tn, np_, nn, rp, rn in bins[last]:
            curve["deg"].append(round(b * bw, 2))
            curve["F"].append(round((tp - tn) / 2, 3))
            curve["G"].append(round((tp + tn) / 2, 3))
            curve["F0"].append(round(f0.get(b, float("nan")), 3) if b in f0 else None)
            curve["G0"].append(round(g0.get(b), 3) if b in g0 else None)
            curve["rip"].append(round((rp + rn) / 2, 3))
            curve["use"].append(round(dens.get(b, 0.0), 5))
        curve["U"] = sorted(U)
        curve["R"] = sorted(R)
        dg = [abs(g - g0[b]) for b, g in zip([int(x / bw) for x in curve["deg"]], curve["G"]) if b in g0]
        curve["dG_med"] = round(views._med(dg), 3) if dg else None
    msg = []
    if not A:
        msg.append(_("找不到這台機器人的診斷期角度資料，無法區分高使用區與低使用區；請先完成一期診斷。"))
    return {"robot": robot, "ax": ax, "runs": out, "curve": curve, "usage_session": use_sid,
            "caution": p["lwi_caution"], "alert": p["lwi_alert"], "messages": msg}


def robots_axes(cfg: dict) -> list[tuple[str, int, int]]:
    with _lock:
        c = db(cfg)
        r = c.execute("SELECT robot, ax, COUNT(*) FROM run GROUP BY robot, ax ORDER BY robot, ax").fetchall()
        c.close()
    return r


def delete_run(cfg: dict, rid: int):
    with _lock:
        c = db(cfg)
        c.execute("DELETE FROM bin WHERE run=?", (rid,))
        c.execute("DELETE FROM run WHERE id=?", (rid,))
        c.commit()
        c.close()

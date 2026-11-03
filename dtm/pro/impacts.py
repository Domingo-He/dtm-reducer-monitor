"""P11 衝擊事件計數。

1. 控制器警報履歷（FANUC ERRALL.LS／ERRHIST.LS 文字檔，或 CSV：t,code,text）：
   急停、安全柵開啟、碰撞偵測、控制停止，依停止方式給權重計入損耗（每次消耗 權重 ÷ 瞬間最大扭矩容許次數 C）。
2. 加速規（CSV：t,peak,rms 視窗資料，或 t,acc 原始資料）：峰值 > k × 基準 RMS 且峰值因子（峰值 ÷ 視窗 RMS）超過門檻
   → 衝擊，對到行號。只看時間域的峰值；不做馬達電流的頻帶分析。
"""
from __future__ import annotations

import bisect
import csv
import io
import re
import time
from datetime import datetime

from .store import Session

ALARM_KIND = {
    "SRVO-001": "estop", "SRVO-002": "estop", "SRVO-007": "estop", "SRVO-204": "estop", "SRVO-230": "estop",
    "SRVO-231": "estop", "SRVO-004": "fence",
    "SRVO-050": "collision", "SRVO-053": "collision",
    "SRVO-003": "controlled", "SRVO-037": "controlled", "SRVO-199": "controlled",
}
_MON = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_DT = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{2,4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?")
_CODE = re.compile(r"\b([A-Z]{3,4}-\d{3})\b\s*(.*)")


def weight(cfg: dict, kind: str) -> float:
    w = cfg["pro"].get("stop_weight", {})
    return float(w.get("estop" if kind == "fence" else kind, 1.0))


def parse_alarms(text: str) -> list[dict]:
    out = []
    if text.lstrip().lower().startswith("t,"):
        for r in csv.DictReader(io.StringIO(text)):
            try:
                t = float(r["t"])
            except (KeyError, ValueError):
                continue
            code = (r.get("code") or "").strip().upper()
            out.append({"t": t, "code": code, "text": (r.get("text") or "").strip()})
    else:
        for line in text.splitlines():
            m = _DT.search(line)
            c = _CODE.search(line)
            if not m or not c:
                continue
            d, mon, y, hh, mm, ss = m.groups()
            if mon.upper() not in _MON:
                continue
            y = int(y)
            y = y + 2000 if y < 100 else y
            try:
                t = datetime(y, _MON[mon.upper()], int(d), int(hh), int(mm), int(ss or 0)).timestamp()
            except ValueError:
                continue
            msg = c.group(2)
            msg = _DT.sub("", msg).strip().strip('"').strip()
            out.append({"t": t, "code": c.group(1), "text": msg})
    for e in out:
        e["kind"] = ALARM_KIND.get(e["code"], "")
    return [e for e in out if e["kind"]]


def locate(sess: Session, t: float) -> tuple[int | None, int | None]:
    c = sess.cycle_at(t)
    if not c:
        return None, None
    off = t - c["t0"]
    for no, L in c["lines"].items():
        if L["s"] <= off < L["s"] + L["m"] + L["w"]:
            return c["cyc"], int(no)
    return c["cyc"], None


def import_alarms(cfg: dict, sess: Session, text: str) -> dict:
    ev = parse_alarms(text)
    t0, t1 = sess.meta.get("t0"), sess.meta.get("t1")
    rows = []
    for e in ev:
        if t0 is not None and t1 is not None and not (t0 - 60 <= e["t"] <= t1 + 60):
            continue
        cyc, line = locate(sess, e["t"])
        rows.append({"t": e["t"], "kind": e["kind"], "code": e["code"], "text": e["text"], "cyc": cyc, "line": line,
                     "weight": weight(cfg, e["kind"]), "src": "alarm"})
    sess.add_impacts(rows, replace_src="alarm")
    return {"parsed": len(ev), "in_period": len(rows)}


def detect(rows, k: float = 4.0, crest: float = 5.0, base_n: int = 200, merge_s: float = 0.2) -> list[dict]:
    """rows：(t, peak, rms) 視窗。基準 RMS = 前 base_n 個視窗的中位數。"""
    hist: list[float] = []
    order: list[float] = []
    out = []
    for t, pk, rms in rows:
        base = order[len(order) // 2] if order else rms
        if len(order) >= 20 and pk > k * base and rms > 0 and pk / rms > crest:
            if out and t - out[-1]["t"] <= merge_s:
                if pk > out[-1]["peak"]:
                    out[-1].update(peak=pk, crest=pk / rms)
            else:
                out.append({"t": t, "peak": pk, "crest": pk / rms, "base": base})
            continue
        hist.append(rms)
        bisect.insort(order, rms)
        if len(hist) > base_n:
            old = hist.pop(0)
            order.pop(bisect.bisect_left(order, old))
    return out


def windows(text_or_rows, win: float = 0.05):
    """CSV（t,peak,rms 或 t,acc）→ (t, peak, rms) 視窗。"""
    if isinstance(text_or_rows, str):
        rd = csv.reader(io.StringIO(text_or_rows))
        head = [h.strip().lower() for h in next(rd)]
        rows = rd
    else:
        head, rows = ["t", "peak", "rms"], text_or_rows
    if "peak" in head and "rms" in head:
        it, ip, ir = head.index("t"), head.index("peak"), head.index("rms")
        for r in rows:
            try:
                yield float(r[it]), float(r[ip]), float(r[ir])
            except (ValueError, IndexError):
                continue
        return
    it, ia = head.index("t"), head.index("acc")
    cur, pk, ss, n = None, 0.0, 0.0, 0
    for r in rows:
        try:
            t, a = float(r[it]), float(r[ia])
        except (ValueError, IndexError):
            continue
        if cur is None:
            cur = t
        if t - cur >= win:
            if n:
                yield cur, pk, (ss / n) ** 0.5
            cur, pk, ss, n = t, 0.0, 0.0, 0
        pk = max(pk, abs(a))
        ss += a * a
        n += 1
    if n:
        yield cur, pk, (ss / n) ** 0.5


def import_accel(cfg: dict, sess: Session, text_or_rows) -> dict:
    p = cfg["pro"]
    ev = detect(windows(text_or_rows), p["impact_k"], p["impact_crest"])
    rows = []
    for e in ev:
        cyc, line = locate(sess, e["t"])
        rows.append({"t": e["t"], "kind": "impact", "code": "", "text": "", "cyc": cyc, "line": line,
                     "peak": round(e["peak"], 4), "crest": round(e["crest"], 2), "weight": 0.0, "src": "acc"})
    sess.add_impacts(rows, replace_src="acc")
    return {"impacts": len(rows)}


def summary(sess: Session) -> dict:
    ev = sess.impacts()
    span = max(1e-6, ((sess.meta.get("t1") or 0) - (sess.meta.get("t0") or 0)) / 86400.0)
    by_kind, by_line = {}, {}
    for e in ev:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        if e.get("line"):
            k = (e["line"], e["kind"])
            by_line[k] = by_line.get(k, 0) + 1
    return {"events": [dict(e, ts=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["t"]))) for e in ev[-500:]],
            "by_kind": by_kind, "per_day": {k: round(v / span, 2) for k, v in by_kind.items()},
            "by_line": [{"line": k[0], "kind": k[1], "n": v} for k, v in sorted(by_line.items(), key=lambda x: -x[1])],
            "weighted": round(sum(float(e["weight"] or 0) for e in ev if e["kind"] != "impact"), 2), "days": round(span, 3)}

"""Pro 演示資料產生器（只用在演示與測試）。

只模擬內建的兩支演示程式（改前 DTM_PRO_DEMO、改後 DTM_PRO_DEMO 修改版），不接受使用者的程式：
DTM 的分析只用實際量到的資料，不從程式模擬機器人動作來推估負荷或壽命（見 docs/DESIGN_CONSTRAINTS.md）。

產生的資料跟控制器串流的格式一樣：時間、各軸角度、各軸扭矩（額定扭矩百分比）、加註版程式回報的行號；
另外附「真正在執行的行」（驗證行號對位正確率用，實機資料沒有這一欄）、加速規視窗資料、警報履歷、全範圍掃描資料。
"""
from __future__ import annotations

import csv
import math
import random
import time

from . import lsparse

_HEAD = """/PROG  DTM_PRO_DEMO
/ATTR
OWNER		= MNEDITOR;
COMMENT		= "%s";
PROG_SIZE	= 0;
CREATE		= DATE 26-10-17  TIME 00:00:00;
MODIFIED	= DATE 26-10-17  TIME 00:00:00;
FILE_NAME	= ;
VERSION		= 0;
LINE_COUNT	= 27;
MEMORY_SIZE	= 0;
PROTECT		= READ_WRITE;
TCD:  STACK_SIZE	= 0,
      TASK_PRIORITY	= 50,
      TIME_SLICE	= 0,
      BUSY_LAMP_OFF	= 0,
      ABORT_REQUEST	= 0,
      PAUSE_REQUEST	= 0;
DEFAULT_GROUP	= 1,*,*,*,*;
CONTROL_CODE	= 00000000 00000000;
/MN
"""

_MN_BEFORE = """   1:  !DTM Pro demo: pick and place ;
   2:  UFRAME_NUM=0 ;
   3:  UTOOL_NUM=1 ;
   4:  PAYLOAD[2] ;
   5:J P[1:HOME] 100% FINE    ;
   6:J P[2:PICK_APP] 100% CNT50    ;
   7:L P[3:PICK] 1500mm/sec FINE    ;
   8:  WAIT DI[101:PART_READY]=ON    ;
   9:  DO[101:GRIP]=ON ;
  10:  WAIT   .30(sec) ;
  11:  PAYLOAD[1] ;
  12:L P[2:PICK_APP] 2000mm/sec CNT30    ;
  13:J P[4:MID] 100% FINE    ;
  14:J P[5:PLACE_APP] 100% CNT50    ;
  15:L P[6:PLACE] 1000mm/sec FINE    ;
  16:L P[8:SEAT] 50mm/sec FINE    ;
  17:L P[6:PLACE] 50mm/sec FINE    ;
  18:L P[8:SEAT] 50mm/sec FINE    ;
  19:L P[6:PLACE] 50mm/sec FINE    ;
  20:  DO[101:GRIP]=OFF ;
  21:  WAIT   .30(sec) ;
  22:  PAYLOAD[2] ;
  23:L P[5:PLACE_APP] 2000mm/sec CNT30    ;
  24:J P[7:TURN] 100% CNT50    ;
  25:J P[4:MID] 100% CNT80    ;
  26:J P[1:HOME] 100% FINE    ;
  27:  WAIT DI[102:NEXT_CYCLE]=ON    ;
"""


def _after_mn() -> str:
    """改後：13 行 FINE 改 CNT80（中間點不需停）、24–25 行用回程的等待餘裕降速、6 行降速降加速度、P[7] 不再翻轉手腕。"""
    rows = _MN_BEFORE.split("\n")
    rep = {6: "   6:J P[2:PICK_APP] 80% CNT50 ACC70    ;", 13: "  13:J P[4:MID] 100% CNT80    ;",
           24: "  24:J P[7:TURN] 70% CNT50    ;", 25: "  25:J P[4:MID] 70% CNT80    ;"}
    return "\n".join(rep.get(i + 1, r) for i, r in enumerate(rows))


_POINTS = {1: ("HOME", [0, 0, 0, 0, -90, 0]), 2: ("PICK_APP", [-35, 25, -10, 0, -80, -35]),
           3: ("PICK", [-35, 38, -22, 0, -70, -35]), 4: ("MID", [10, 8, 2, 0, -92, 10]),
           5: ("PLACE_APP", [62, 30, -6, 0, -84, 62]), 6: ("PLACE", [62, 42, -19, 0, -73, 62]),
           7: ("TURN", [40, 12, 0, 0, -90, 220]), 8: ("SEAT", [62, 42.2, -19.15, 0, -73.05, 62])}


def _pos(points: dict) -> str:
    out = ["/POS"]
    for no, (name, j) in points.items():
        out.append(f'P[{no}:"{name}"]{{\n   GP1:\n\tUF : 0, UT : 1,\n'
                   f"\tJ1=  {j[0]:8.3f} deg,\tJ2=  {j[1]:8.3f} deg,\tJ3=  {j[2]:8.3f} deg,\n"
                   f"\tJ4=  {j[3]:8.3f} deg,\tJ5=  {j[4]:8.3f} deg,\tJ6=  {j[5]:8.3f} deg\n}};")
    return "\n".join(out) + "\n/END\n"


def demo_program(variant: str = "before") -> str:
    if variant not in ("before", "after"):
        raise ValueError("simulator only runs the built-in demo programs (before / after)")
    pts = dict(_POINTS)
    if variant == "after":
        pts[7] = ("TURN", [40, 12, 0, 0, -90, 40])
        return _HEAD % "PICK PLACE DEMO B" + _after_mn() + _pos(pts)
    return _HEAD % "PICK PLACE DEMO" + _MN_BEFORE + _pos(pts)


def builtin_versions() -> set[str]:
    return {lsparse.parse(demo_program(v)).version for v in ("before", "after")}


# ------------------------------------------------------------ 機器人（R-2000iC/165F 量級的近似值，只為產生演示資料）
VMAX = [105.0, 105.0, 105.0, 130.0, 130.0, 210.0]     # 度／秒
TA0 = 0.32                                            # ACC100 的加速時間（秒）
TJ = 0.096                                            # 平滑濾波（秒）
I0 = [0.18, 0.20, 0.16, 0.08, 0.08, 0.045]            # 慣性項：% 額定扭矩／(度/秒²)
I1 = [0.13, 0.13, 0.11, 0.07, 0.07, 0.05]             # 夾著工件時增加
FC = [5.0, 5.0, 5.0, 4.0, 4.0, 4.0]                   # 庫倫摩擦 %
FV = [0.03, 0.03, 0.03, 0.02, 0.02, 0.015]            # 黏滯摩擦 %／(度/秒)


def _gravity(q, pl):
    r = math.radians
    return [0.0,
            (40 + 29 * pl) * math.sin(r(q[1])) + (11 + 18 * pl) * 0.5 * math.cos(r(q[2])),
            (32 + 47 * pl) * math.cos(r(q[2])),
            (2 + 5 * pl) * math.sin(r(q[3])),
            (4.5 + 16 * pl) * math.cos(r(q[4] + 90 - q[2])),
            1.0 * pl * math.sin(r(q[5]))]


def _tcp(q):
    r = math.radians
    re = 312 + 1075 * math.sin(r(q[1]))
    ze = 670 + 1075 * math.cos(r(q[1]))
    rw = re + 1280 * math.cos(r(q[2])) - 225 * math.sin(r(q[2]))
    zw = ze + 1280 * math.sin(r(q[2])) + 225 * math.cos(r(q[2]))
    return (rw * math.cos(r(q[0])), rw * math.sin(r(q[0])), zw)


def _profile(T: float, ta: float, hz: float) -> list[float]:
    """梯形速度＋平滑濾波 → 0→1 的位置曲線（S 曲線）。"""
    n = max(2, int(round(T * hz)))
    ta = min(ta, T / 2)
    na = max(1, int(round(ta * hz)))
    v = [min(1.0, (i + 0.5) / na, (n - i - 0.5) / na) for i in range(n)]
    m = max(1, int(round(TJ * hz)))
    sm, acc = [], 0.0
    pad = v + [0.0] * m
    for i in range(len(pad)):
        acc += pad[i] - (pad[i - m] if i >= m else 0.0)
        sm.append(acc / m)
    tot = sum(sm) or 1.0
    out, c = [], 0.0
    for x in sm:
        c += x
        out.append(c / tot)
    return out


class Plan:
    """一個循環的時間表：每個動作的起點、時長、位移，行號回報時間，真正在執行的行。"""

    def __init__(self):
        self.moves = []        # dict(line, t0, prof, dq, bow, term)
        self.reports = []      # (t, line)
        self.truth = []        # (t, line) 從這個時間起真正在執行的行
        self.events = []       # (t, kind, data)
        self.payload = []      # (t, pl)
        self.t_end = 0.0
        self.q_end = None


def plan_cycle(prog: lsparse.Program, q0: list[float], t0: float, hz: float, signals: dict) -> Plan:
    """依內建演示程式排出一個循環。signals = {"DI[101]": 時間, "DI[102]": 時間}。"""
    eff = lsparse.effective_terms(prog)
    by = {ln.no: ln for ln in prog.lines}
    P = Plan()
    t, q = t0, list(q0)
    prev = None                 # (move dict, effective term)
    interp = t0                 # 直譯器時間（提前執行）
    pl = 0.0
    P.payload.append((t0, pl))
    for no in prog.main_path:
        ln = by[no]
        if ln.is_motion:
            tgt = prog.positions[ln.point_no].joint_vec()
            dq = [b - a for a, b in zip(q, tgt)]
            acc = (ln.acc or 100) / 100.0
            ta = TA0 / acc
            if ln.kind == "J":
                pct = (ln.speed or 100) / 100.0
                T = max(abs(d) / (VMAX[j] * pct) for j, d in enumerate(dq)) + ta
            else:
                a, b = _tcp(q), _tcp(tgt)
                dist = math.dist(a, b)
                T = max(dist / (ln.speed or 1000), max(abs(d) / VMAX[j] for j, d in enumerate(dq))) + ta
            T = max(T, 0.06)
            prof = _profile(T, ta, hz)
            dur = len(prof) / hz
            if prev and prev[1] == "CNT":
                pm = prev[0]
                pdur = len(pm["prof"]) / hz
                ov = (prev[2] / 100.0) * (min(pm["ta"], pdur / 2) + TJ) * 0.95
                start = max(pm["t0"] + 0.1, pm["t0"] + pdur - ov)
                P.reports.append((pm["t0"] + 0.01, no))                 # 直譯器在上一個動作開始時就寫了這一行
                mid = start + (pm["t0"] + pdur - start) / 2
                P.truth.append((mid, no))
            else:
                start = max(t, interp)
                P.reports.append((start - 0.005, no))
                P.truth.append((start if prev else t0, no))
            bow = [0.0] * 6
            if ln.kind == "L":
                bow[1], bow[2] = 0.15 * dq[2], -0.15 * dq[1]
            mv = {"line": no, "t0": start, "prof": prof, "dq": dq, "bow": bow, "ta": ta}
            P.moves.append(mv)
            q = list(tgt)
            t = start + dur
            interp = start + 0.01
            prev = (mv, eff[no], ln.cnt or 0)
            if eff[no] == "FINE":
                t += 0.05                                                # 定位確認
                interp = t
            continue
        if prev is None or prev[1] == "FINE":
            cur = max(t, interp)
        else:
            cur = interp
        if ln.kind == "wait":
            if ln.wait_s is not None:
                cur += ln.wait_s
            else:
                key = ln.wait_cond.split(":")[0].split("]")[0] + "]"
                cur = max(cur, signals.get(key, cur)) + 0.02
        elif ln.kind == "io":
            cur += 0.008
            if "GRIP" in ln.text or "DO[101" in ln.text:
                P.events.append((cur, "grip", "ON" in ln.text.split("=")[-1]))
        elif ln.kind == "payload":
            pl = 1.0 if "PAYLOAD[1]" in ln.text else 0.0
            P.payload.append((cur, pl))
        else:
            cur += 0.002
        if prev is None or prev[1] == "FINE":
            t = cur
        interp = cur
    P.t_end = max(t, interp) + 0.03
    P.q_end = q
    return P


def render(P: Plan, q0: list[float], hz: float, rng: random.Random, wear=None, drift: float = 1.0,
           noise: float = 0.6, t_from: float | None = None, t_to: float | None = None) -> dict:
    """把時間表變成取樣資料（各欄位為 list）。wear(ax, θ) → 額外摩擦 %。"""
    t0 = P.moves[0]["t0"] if t_from is None else t_from
    t0 = min(t0, P.truth[0][0]) if t_from is None else t0
    t1 = P.t_end if t_to is None else t_to
    n = int((t1 - t0) * hz)
    ts = [t0 + i / hz for i in range(n)]
    q = [[q0[j]] * n for j in range(6)]
    for mv in P.moves:
        i0 = int(round((mv["t0"] - t0) * hz))
        prof = mv["prof"]
        dq, bow = mv["dq"], mv["bow"]
        for j in range(6):
            col, d, b = q[j], dq[j], bow[j]
            if d == 0 and b == 0:
                continue
            for k, s in enumerate(prof):
                i = i0 + k
                if 0 <= i < n:
                    col[i] += d * s + (b * math.sin(math.pi * s) if b else 0.0)
            start = max(0, i0 + len(prof))
            for i in range(start, n):
                col[i] += d
    # 行號回報、真正的行、負載
    def step(series, default):
        out, k, cur = [], 0, default
        for tt in ts:
            while k < len(series) and series[k][0] <= tt:
                cur = series[k][1]
                k += 1
            out.append(cur)
        return out
    rep = step(P.reports, P.reports[0][1])
    tru = step(P.truth, P.truth[0][1])
    pls = step(P.payload, 0.0)
    tq = [[0.0] * n for _ in range(6)]
    dt = 1.0 / hz
    for j in range(6):
        col = q[j]
        for i in range(n):
            a = col[i - 1] if i > 0 else col[i]
            b = col[i + 1] if i + 1 < n else col[i]
            w = (b - a) / (2 * dt)
            al = (b - 2 * col[i] + a) / (dt * dt)
            pl = pls[i]
            fr = (FC[j] * drift + (wear(j + 1, col[i]) if wear else 0.0)) * math.tanh(w / 0.3) + FV[j] * drift * w
            tq[j][i] = (I0[j] + I1[j] * pl) * al + fr + rng.gauss(0, noise)
    for i in range(n):
        g = _gravity([q[j][i] for j in range(6)], pls[i])
        for j in range(6):
            tq[j][i] += g[j]
    out = {"t": ts}
    for j in range(6):
        out[f"q{j + 1}"] = [round(x + rng.gauss(0, 0.002), 4) for x in q[j]]
    for j in range(6):
        out[f"tq{j + 1}"] = [round(x, 3) for x in tq[j]]
    out["line"] = rep
    out["true_line"] = tru
    return out


# ------------------------------------------------------------ 一個診斷期
COLS = ["t"] + [f"q{j}" for j in range(1, 7)] + [f"tq{j}" for j in range(1, 7)] + ["line", "true_line"]


def run(variant: str, days: float, cycles_per_day: int, hz: float, seed: int = 1, t_start: float | None = None,
        drift: float = 1.0, wear=None, estop_p: float = 0.004, sink=None, acc_sink=None, alarm_sink=None,
        progress=None):
    """產生一個診斷期的資料。sink(cols) 收到每個循環的取樣；acc_sink(rows) 收到加速規視窗；alarm_sink(rows) 收到停止事件。"""
    prog = lsparse.parse(demo_program(variant))
    if prog.version not in builtin_versions():
        raise ValueError("simulator only runs the built-in demo programs")
    rng = random.Random(seed)
    t_start = t_start or time.time() - days * 86400
    home = prog.positions[1].joint_vec()
    # 節拍：以改前程式最慢的情況＋1.2 秒（改後在同一個節拍下運轉）
    ref = plan_cycle(lsparse.parse(demo_program("before")), home, 0.0, hz, {"DI[101]": 6.5, "DI[102]": 0.0})
    takt = round(ref.t_end + 1.2, 1)
    total = max(1, int(round(days * cycles_per_day)))
    span = days * 86400
    stats = {"cycles": 0, "estops": 0, "takt": takt}
    free = t_start
    for c in range(total):
        tc = max(t_start + span * c / total + rng.uniform(0, 30), free)
        sig = {"DI[101]": tc + rng.uniform(4.0, 6.5), "DI[102]": tc + takt + rng.uniform(-0.05, 0.05)}
        P = plan_cycle(prog, home, tc, hz, sig)
        d = render(P, home, hz, rng, wear=wear, drift=drift * (1 + 0.02 * math.sin(2 * math.pi * (tc % 86400) / 86400)))
        stop = None
        if rng.random() < estop_p:
            k = rng.randrange(len(d["t"]) // 5, len(d["t"]))
            stop = (d["t"][k], rng.choice(["estop", "estop", "fence", "collision"]))
            for key in d:
                d[key] = d[key][:k]
            stats["estops"] += 1
        free = (stop[0] + rng.uniform(60, 180)) if stop else P.t_end + 0.05
        if sink:
            sink(d)
        if acc_sink:
            acc_sink(_accel(P, d, hz, rng, stop))
        if alarm_sink and stop:
            alarm_sink([stop])
        stats["cycles"] += 1
        if progress and c % 20 == 0:
            progress(c, total)
    return stats


def _accel(P: Plan, d: dict, hz: float, rng: random.Random, stop) -> list[tuple]:
    """加速規 50 ms 視窗（峰值、RMS，g）。到達 PLACE（15 行 FINE）時偶爾工件撞到治具；停止事件有大衝擊。"""
    if not d["t"]:
        return []
    t0, t1 = d["t"][0], d["t"][-1]
    hits = []
    for mv in P.moves:
        if mv["line"] == 15 and rng.random() < 0.2:
            hits.append((mv["t0"] + len(mv["prof"]) / hz, rng.uniform(1.2, 2.5)))
    if stop:
        hits.append((stop[0], rng.uniform(3.0, 5.0)))
    rows, t = [], t0
    while t < t1:
        rms = 0.04 + rng.uniform(0, 0.01)
        peak = rms * rng.uniform(2.4, 3.4)
        for th, g in hits:
            if t <= th < t + 0.05:
                peak, rms = g, max(rms, g / 7)
        rows.append((round(t, 3), round(peak, 4), round(rms, 4)))
        t += 0.05
    return rows


def alarm_lines(events: list[tuple]) -> list[str]:
    """停止事件 → FANUC 警報履歷格式（ERRALL.LS 風格）。"""
    codes = {"estop": ("SRVO-001", "Operator panel E-stop"), "fence": ("SRVO-004", "Fence open"),
             "collision": ("SRVO-050", "Collision Detection Alarm (G:1 A:3)")}
    out = []
    for i, (t, kind) in enumerate(sorted(events, reverse=True)):
        c, txt = codes[kind]
        ts = time.strftime("%d-%b-%y %H:%M:%S", time.localtime(t)).upper()
        out.append(f'{i + 1:5d} {ts}  {c} {txt}')
    return out


def write_csv(path: str, cols: list[str], chunks):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for d in chunks:
            n = len(d[cols[0]])
            for i in range(n):
                w.writerow([round(d[k][i], 3) if k == "t" else d[k][i] for k in cols])


# ------------------------------------------------------------ 全範圍掃描（P7）
SWEEP_RANGE = {2: (-40.0, 60.0), 3: (-60.0, 40.0)}


def sweep(ax: int, t0: float, hz: float, rng: random.Random, wear=None, pct: float = 5.0, drift: float = 1.0) -> dict:
    """單軸慢速正轉一次、反轉一次。行號暫存器寫 8000 + 軸×10 + 方向（1 正轉、2 反轉）。"""
    a, b = SWEEP_RANGE.get(ax, (-90.0, 90.0))
    home = [0.0, 0.0, 0.0, 0.0, -90.0, 0.0]
    v = VMAX[ax - 1] * pct / 100
    out = {k: [] for k in COLS}
    t = t0
    for direction, (s, e) in ((1, (a, b)), (2, (b, a))):
        T = abs(e - s) / v + TA0
        prof = _profile(T, TA0, hz)
        for k, x in enumerate(prof + [1.0] * int(0.5 * hz)):
            th = s + (e - s) * x
            q = list(home)
            q[ax - 1] = th
            nxt = s + (e - s) * (prof[k + 1] if k + 1 < len(prof) else 1.0)
            w = (nxt - th) * hz
            g = _gravity(q, 0.0)
            for j in range(6):
                out[f"q{j + 1}"].append(round(q[j], 4))
                if j == ax - 1:
                    fr = (FC[j] * drift + (wear(ax, th) if wear else 0.0)) * math.tanh(w / 0.3) + FV[j] * drift * w
                    ripple = 0.15 * math.sin(math.radians(th) * 40)
                    out[f"tq{j + 1}"].append(round(g[j] + fr + ripple + rng.gauss(0, 0.4), 3))
                else:
                    out[f"tq{j + 1}"].append(round(g[j] + rng.gauss(0, 0.4), 3))
            out["t"].append(t)
            code = 8000 + ax * 10 + direction if k < len(prof) else 0
            out["line"].append(code)
            out["true_line"].append(code)
            t += 1 / hz
        t += 1.0
    return out


def gaussian_wear(center: float = -15.0, width: float = 6.0, amp: float = 1.0, axis: int = 3):
    def f(ax, th):
        return amp * math.exp(-((th - center) / width) ** 2) if ax == axis else 0.0
    return f

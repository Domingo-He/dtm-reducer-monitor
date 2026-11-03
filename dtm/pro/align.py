"""P3 行號對位（位置比對）＋ P4 循環切割。

問題：程式在每個動作行前把行號寫進暫存器，CNT 平滑過點時控制器會提前執行後面的指令，
回報的行號比機器人實際位置早（最多 lookahead 行）。

做法：
1. 每個動作行是一個狀態，依主流程順序排列；「路徑樣板」= 該行從起點到終點的關節空間路徑（12 點）。
   有行號回報時，從前幾個循環學出樣板（回報行號＋速度最低點做初始切分，再用位置對位反覆修正）；
   沒有行號回報、但教導點是關節值時，以前後教導點連線當初始樣板。
2. 每個循環降到 20 Hz，用 Viterbi 找「只能停留或往後走」的最佳狀態序列：
   成本 = 到該行路徑樣板的距離² + 換行成本；有行號回報時只允許「回報行 − lookahead」到「回報行」之間的狀態。
3. 換行點若落在靜止時段，移到機器人真正開始動的那一刻（靜止時段算前一行的停留：WAIT、定位、夾取）。
"""
from __future__ import annotations

import math

from . import AXIS_LINEAR_SCALE, AXIS_ROTARY_SCALE, lsparse

K = 12                 # 每行路徑樣板點數
ALIGN_HZ = 20.0
SIGMA = 1.0            # 位置雜訊尺度（度）
T1, T2, T3 = 3.0, 12.0, 30.0
V_STILL = 1.0          # 靜止判定：關節速度範數 < 1 度／秒


def scales(axes: list[int]) -> list[float]:
    return [AXIS_LINEAR_SCALE if a == 7 else AXIS_ROTARY_SCALE for a in axes]


def speeds(cols: dict, axes: list[int], hz: float) -> list[float]:
    """關節速度範數（5 點平滑差分），單位：度／秒（直線軸 mm÷10）。"""
    sc = scales(axes)
    n = len(cols["t"])
    out = [0.0] * n
    if n < 5:
        return out
    k = 2
    span = 2 * k / hz
    qs = [cols[f"q{a}"] for a in axes]
    for i in range(n):
        a, b = max(0, i - k), min(n - 1, i + k)
        dt = (b - a) / hz or span
        s = 0.0
        for q, c in zip(qs, sc):
            d = (q[b] - q[a]) * c / dt
            s += d * d
        out[i] = math.sqrt(s)
    return out


def _resample(pts: list[list[float]], k: int = K) -> list[list[float]]:
    """依弧長重新取樣成 k 點；路徑長度為 0 時回傳同一點。"""
    if not pts:
        return []
    if len(pts) == 1:
        return [list(pts[0]) for _ in range(k)]
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + math.dist(pts[i - 1], pts[i]))
    L = cum[-1]
    if L < 1e-9:
        return [list(pts[0]) for _ in range(k)]
    out, j = [], 0
    for m in range(k):
        s = L * m / (k - 1)
        while j < len(cum) - 2 and cum[j + 1] < s:
            j += 1
        seg = cum[j + 1] - cum[j]
        f = 0.0 if seg <= 0 else (s - cum[j]) / seg
        out.append([a + (b - a) * f for a, b in zip(pts[j], pts[j + 1])])
    return out


def _median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


class Template:
    __slots__ = ("pts", "segs", "length")

    def __init__(self, pts: list[list[float]]):
        self.pts = pts
        self.segs = []
        for a, b in zip(pts, pts[1:]):
            d = [y - x for x, y in zip(a, b)]
            dd = sum(x * x for x in d)
            self.segs.append((a, d, dd))
        self.length = sum(math.sqrt(s[2]) for s in self.segs)

    def dist2(self, x) -> float:
        best = 1e18
        for a, d, dd in self.segs:
            if dd <= 1e-12:
                s = 0.0
                for xi, ai in zip(x, a):
                    s += (xi - ai) ** 2
            else:
                t = 0.0
                for xi, ai, di in zip(x, a, d):
                    t += (xi - ai) * di
                t /= dd
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
                s = 0.0
                for xi, ai, di in zip(x, a, d):
                    e = xi - ai - t * di
                    s += e * e
            if s < best:
                best = s
        return best


class Aligner:
    def __init__(self, prog: lsparse.Program, axes: list[int], lookahead: int = 2):
        self.prog = prog
        self.axes = list(axes)
        self.sc = scales(self.axes)
        self.ml = prog.motion_lines()
        self.states = [ln.no for ln in self.ml]
        self.idx = {no: k for k, no in enumerate(self.states)}
        self.eff = lsparse.effective_terms(prog)
        self.L = max(0, int(lookahead))
        # 回報第 r 行時，機器人可能還在前面幾行：只有前一行是 CNT（沒有 WAIT 擋住）才可能，最多 lookahead 行
        self.lo_of = []
        for r in range(len(self.states)):
            lo = r
            while lo > 0 and r - lo < self.L and self.eff[self.states[lo - 1]] == "CNT":
                lo -= 1
            self.lo_of.append(lo)
        self.tpl: list[Template] | None = None
        self.mode = ""

    # ------------------------------------------------------------ 樣板
    def chord_templates(self) -> bool:
        """教導點是關節值時：以前後教導點連線當初始樣板。"""
        pts = []
        for ln in self.ml:
            if ln.point_reg or ln.offset or ln.point_no not in self.prog.positions:
                return False
            v = self.prog.positions[ln.point_no].joint_vec(6)
            if v is None:
                return False
            if 7 in self.axes:
                v = v + [self.prog.positions[ln.point_no].joint.get("E1", 0.0)]
            pts.append([v[a - 1] * c for a, c in zip(self.axes, self.sc)])
        tpl = []
        for k, p in enumerate(pts):
            a = pts[k - 1] if k > 0 else pts[-1]          # 第一行從上一循環的終點出發
            tpl.append(Template([[x + (y - x) * m / (K - 1) for x, y in zip(a, p)] for m in range(K)]))
        self.tpl = tpl
        return True

    def to_dict(self) -> dict:
        return {"mode": self.mode, "states": self.states, "tpl": [t.pts for t in self.tpl] if self.tpl else None}

    def load(self, d: dict) -> bool:
        if not d or d.get("states") != self.states or not d.get("tpl"):
            return False
        self.tpl = [Template(p) for p in d["tpl"]]
        self.mode = d.get("mode", "")
        return True

    def state_of(self, code) -> int | None:
        if code is None:
            return None
        return self.idx.get(int(code))

    def has_codes(self, cols: dict) -> bool:
        c = cols.get("line")
        if not c:
            return False
        hit = sum(1 for x in c[:: max(1, len(c) // 200)] if int(x) in self.idx)
        return hit >= 0.5 * len(c[:: max(1, len(c) // 200)])

    def _points(self, cols: dict, idx: list[int]) -> list[list[float]]:
        qs = [cols[f"q{a}"] for a in self.axes]
        return [[q[i] * c for q, c in zip(qs, self.sc)] for i in idx]

    # ------------------------------------------------------------ 初始切分（學習用）
    def initial_states(self, cols: dict, spd: list[float]) -> list[int]:
        """由回報行號推出每筆樣本的大約狀態（見模組說明）。"""
        code = cols["line"]
        n = len(code)
        st = [0] * n
        runs, i = [], 0
        while i < n:
            r = self.state_of(code[i])
            j = i
            while j < n and self.state_of(code[j]) == r:
                j += 1
            runs.append((i, j, r))
            i = j
        last = 0
        for a, b, r in runs:
            if r is None:
                for i in range(a, b):
                    st[i] = last
                continue
            prev_cnt = r > 0 and self.eff[self.states[r - 1]] == "CNT"
            if not prev_cnt:
                fill = [(a, b, r)]
            elif self.eff[self.states[r]] == "CNT":
                fill = [(a, b, r - 1)]
            else:
                e = b
                while e > a + 1 and spd[e - 1] < V_STILL:
                    e -= 1
                lo, hi = a + (e - a) // 6, e - (e - a) // 6
                cut = min(range(lo, hi), key=lambda i: spd[i]) if hi > lo else a
                fill = [(a, cut, r - 1), (cut, b, r)]
            for x, y, s in fill:
                for i in range(x, y):
                    st[i] = s
            last = r
        return st

    def learn(self, cycles: list[dict], hz: float, iters: int = 2) -> dict:
        """從學習循環學出路徑樣板。cycles 為各循環的欄位資料。"""
        use_codes = all(self.has_codes(c) for c in cycles) if cycles else False
        if use_codes:
            self.mode = "code"
            labels = []
            for c in cycles:
                labels.append(self.initial_states(c, speeds(c, self.axes, hz)))
            self._fit(cycles, labels)
        else:
            if not self.chord_templates():
                raise ValueError("no line reports and taught points are not joint values")
            self.mode = "pos"
        for _ in range(iters):
            labels = [self.align(c, hz)["state"] for c in cycles]
            self._fit(cycles, labels)
        return self.to_dict()

    def _fit(self, cycles: list[dict], labels: list[list[int]]):
        m = len(self.states)
        per = [[] for _ in range(m)]
        for c, lab in zip(cycles, labels):
            n = len(lab)
            i = 0
            while i < n:
                j = i
                while j < n and lab[j] == lab[i]:
                    j += 1
                k = lab[i]
                a = max(0, i - 1)
                per[k].append(_resample(self._points(c, list(range(a, j)))))
                i = j
        tpl = []
        for k in range(m):
            if per[k]:
                pts = [[_median([p[i][d] for p in per[k]]) for d in range(len(self.axes))] for i in range(K)]
            elif self.tpl:
                pts = self.tpl[k].pts
            else:
                nb = next((per[j] for j in list(range(k - 1, -1, -1)) + list(range(k + 1, m)) if per[j]), None)
                pts = [list(nb[0][-1])] * K if nb else [[0.0] * len(self.axes)] * K
            tpl.append(Template(pts))
        self.tpl = tpl

    # ------------------------------------------------------------ 對位
    def _segs(self):
        """所有行的路徑線段，依程式順序串成一條：狀態 g = (行 k, 線段 j)。只能停留或往前走。"""
        if getattr(self, "_seg_cache", None) is not None and self._seg_cache[0] is self.tpl:
            return self._seg_cache[1]
        segs, line_of, first = [], [], []
        for k, t in enumerate(self.tpl):
            first.append(len(segs))
            for a, d, dd in t.segs:
                segs.append((a, d, dd))
                line_of.append(k)
        first.append(len(segs))
        res = (segs, line_of, first)
        self._seg_cache = (self.tpl, res)
        return res

    def align(self, cols: dict, hz: float) -> dict:
        """回傳 state（每筆樣本的狀態索引）、seg（每段：狀態、行號、起點、到達、終點）、conf、fit。"""
        n = len(cols["t"])
        if n == 0 or not self.tpl:
            return {"state": [], "seg": [], "conf": 0.0, "fit": 0.0}
        m = len(self.states)
        segs, line_of, first = self._segs()
        G = len(segs)
        ds = max(1, int(round(hz / ALIGN_HZ)))
        idx = list(range(0, n, ds))
        X = self._points(cols, idx)
        codes = cols.get("line") if self.mode == "code" else None
        s2 = 2 * SIGMA * SIGMA
        STEP, LINE, J = 0.3, T1, K - 1                 # 每前進一段的成本、換行成本、每筆最多前進幾段
        inf = 1e18
        prev: dict[int, float] = {}
        back: list[dict[int, int]] = []
        dists = []
        lo_l, hi_l = 0, m - 1

        def d2(g, x):
            a, d, dd = segs[g]
            if dd <= 1e-12:
                return sum((xi - ai) ** 2 for xi, ai in zip(x, a))
            u = sum((xi - ai) * di for xi, ai, di in zip(x, a, d)) / dd
            u = 0.0 if u < 0 else 1.0 if u > 1 else u
            return sum((xi - ai - u * di) ** 2 for xi, ai, di in zip(x, a, d))
        for t, i in enumerate(idx):
            if codes is not None:
                r = self.state_of(codes[i])
                if r is not None:
                    lo_l, hi_l = self.lo_of[r], r
                else:
                    hi_l = min(m - 1, hi_l + 1)
            x = X[t]
            if t == 0:
                start = first[lo_l] if codes is not None else 0
                cand = range(start, min(G, start + 2 * J))
                cur = {g: d2(g, x) / s2 + STEP * (g - start) + LINE * (line_of[g] - line_of[start]) for g in cand}
                back.append({g: g for g in cur})
            else:
                if codes is not None:
                    allowed_lo, allowed_hi = first[lo_l], first[hi_l + 1] - 1
                else:
                    best = min(prev.values())
                    act = [g for g, c in prev.items() if c < best + 40]
                    allowed_lo, allowed_hi = min(act), min(G - 1, max(act) + J)
                cur, bp = {}, {}
                for g in range(allowed_lo, allowed_hi + 1):
                    bc, bg = inf, -1
                    for h in range(max(0, g - J), g + 1):
                        c = prev.get(h)
                        if c is None:
                            continue
                        c += STEP * (g - h) + LINE * (line_of[g] - line_of[h])
                        if c < bc:
                            bc, bg = c, h
                    if bg < 0:
                        continue
                    cur[g] = bc + d2(g, x) / s2
                    bp[g] = bg
                if not cur:                                  # 回報行號跳太多（例如重新起動）：直接進入
                    best = min(prev.values())
                    hb = min(prev, key=prev.get)
                    for g in range(allowed_lo, allowed_hi + 1):
                        cur[g] = best + T3 + d2(g, x) / s2
                        bp[g] = hb
                back.append(bp)
            prev = cur
            gbest = min(cur, key=cur.get)
            dists.append(math.sqrt(d2(gbest, x)))
        g = min(prev, key=prev.get)
        path = [0] * len(idx)
        for t in range(len(idx) - 1, -1, -1):
            path[t] = line_of[g]
            g = back[t].get(g, g)
        state = [0] * n
        for t, i in enumerate(idx):
            for j in range(i, min(n, i + ds)):
                state[j] = path[t]
        spd = speeds(cols, self.axes, hz)
        # 只用位置時：換行點落在靜止時段 → 移到開始動的那一刻（有行號回報時，FINE 之後的換行點由回報決定）
        i = 1 if codes is None else n
        while i < n:
            if state[i] != state[i - 1] and spd[i] < V_STILL:
                j = i
                while j < n and spd[j] < V_STILL and (j == i or state[j] == state[i]):
                    j += 1
                for x in range(i, j):
                    state[x] = state[i - 1]
                i = j + 1
                continue
            i += 1
        seg = []
        i = 0
        while i < n:
            j = i
            while j < n and state[j] == state[i]:
                j += 1
            arr = j
            e, lim = j, max(i + 1, j - int(0.12 * hz) - 1)       # 下一行起步時的平滑差分會滲進最後幾筆
            while e > lim and spd[e - 1] >= V_STILL:
                e -= 1
            if e > i + 1 and spd[e - 1] < V_STILL:
                arr = e
                while arr > i + 1 and spd[arr - 1] < V_STILL:
                    arr -= 1
                if (e - arr) < 0.05 * hz:
                    arr = j
            k = state[i]
            seg.append((k, self.states[k], i, arr, j))
            i = j
        good = sum(1 for d in dists if d < 2.0 * SIGMA)
        return {"state": state, "seg": seg, "conf": good / len(dists), "fit": _median(dists)}

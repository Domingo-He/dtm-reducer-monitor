"""趨勢外推（2.1）：直線外推剩餘天數；偵測到加速時改用保守估計。

磨耗後期常常是越走越快，直線外推會高估剩餘時間。判斷加速的方法：
- 把視窗分成前後兩半各自回歸，後半斜率 ≥ 前半的 accel_ratio 倍（而且後半斜率為正）；或
- 二次回歸的曲率為正，而且曲線在最新一點的斜率明顯大於整段直線的斜率。
加速時取「直線」「後半斜率直線」「二次曲線」三種估計中最短的那個。
"""
from __future__ import annotations

from .segment import ols

ACCEL_RATIO = 1.5


def _quad(xs: list[float], ys: list[float]):
    """y = a x² + b x + c 最小平方；回傳 (a, b, c) 或 None。"""
    n = len(xs)
    if n < 5:
        return None
    s = [sum(x ** k for x in xs) for k in range(5)]
    t = [sum((x ** k) * y for x, y in zip(xs, ys)) for k in range(3)]
    m = [[s[4], s[3], s[2], t[2]], [s[3], s[2], s[1], t[1]], [s[2], s[1], s[0], t[0]]]
    for i in range(3):                                   # 高斯消去
        p = max(range(i, 3), key=lambda r: abs(m[r][i]))
        if abs(m[p][i]) < 1e-15:
            return None
        m[i], m[p] = m[p], m[i]
        for r in range(3):
            if r != i:
                f = m[r][i] / m[i][i]
                m[r] = [a - f * b for a, b in zip(m[r], m[i])]
    return tuple(m[i][3] / m[i][i] for i in range(3))


def _days_linear(v_now: float, slope: float, target: float) -> float | None:
    if v_now >= target:
        return 0.0
    if slope > 1e-12:
        return (target - v_now) / slope
    return None


def extrapolate(xs: list[float], ys: list[float], targets: dict, accel_ratio: float = ACCEL_RATIO) -> dict | None:
    """xs = 距今天數（≤ 0，最新一點接近 0），ys = 數值；targets = {"days_to_caution": 值, ...}。
    回傳 {"slope", "now", "accel", "method", 各目標的天數}；資料不足回 None。"""
    pts = sorted((x, y) for x, y in zip(xs, ys) if y is not None)
    if len(pts) < 5:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    fit = ols(xs, ys)
    if not fit:
        return None
    slope, v_now = fit
    out = {"slope": slope, "now": v_now, "accel": False, "method": "linear"}
    late_slope = None
    if len(pts) >= 8:
        h = len(pts) // 2
        f1, f2 = ols(xs[:h], ys[:h]), ols(xs[h:], ys[h:])
        if f1 and f2 and f2[0] > 1e-12 and f2[0] >= accel_ratio * max(f1[0], 1e-12):
            late_slope = f2
    q = _quad(xs, ys)
    q_ok = False
    if q and q[0] > 0 and q[1] > 1e-12 and q[1] >= accel_ratio * max(slope, 1e-12):
        sse_l = sum((y - (slope * x + v_now)) ** 2 for x, y in pts)
        sse_q = sum((y - (q[0] * x * x + q[1] * x + q[2])) ** 2 for x, y in pts)
        q_ok = sse_q <= 0.8 * sse_l                      # 曲線要明顯比直線貼合，避免雜訊誤判成加速
    out["accel"] = bool(late_slope) or q_ok
    for key, target in targets.items():
        cands = [_days_linear(v_now, slope, target)]
        if out["accel"]:
            if late_slope:
                cands.append(_days_linear(late_slope[1], late_slope[0], target))
            if q_ok:
                a, b, c = q
                if c >= target:
                    cands.append(0.0)
                else:
                    disc = b * b - 4 * a * (c - target)
                    if disc >= 0:
                        cands.append((-b + disc ** 0.5) / (2 * a))
        cands = [d for d in cands if d is not None and d >= 0]
        out[key] = min(cands) if cands else None
    if out["accel"]:
        out["method"] = "conservative"
    return out

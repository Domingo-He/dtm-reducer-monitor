"""從保留的原始波形取出每一行的運動量（角度、角速度、角加速度、急動度、扭矩）。只用實際量到的資料。"""
from __future__ import annotations

from .store import Session


def smooth(x: list[float], k: int = 2) -> list[float]:
    n = len(x)
    if n == 0 or k <= 0:
        return list(x)
    out = [0.0] * n
    c = [0.0]
    for v in x:
        c.append(c[-1] + v)
    for i in range(n):
        a, b = max(0, i - k), min(n, i + k + 1)
        out[i] = (c[b] - c[a]) / (b - a)
    return out


def deriv(x: list[float], hz: float, k: int = 2) -> list[float]:
    s = smooth(x, k)
    n = len(s)
    out = [0.0] * n
    for i in range(n):
        a, b = max(0, i - 1), min(n - 1, i + 1)
        out[i] = (s[b] - s[a]) * hz / (b - a) if b > a else 0.0
    return out


def line_cycles(sess: Session, line: int, axes: list[int], limit: int = 30, kinds=("first", "sample")) -> list[dict]:
    """每個保留循環中，該行「動作段」的樣本（不含停留）。"""
    keep = [cid for cid, why in sess.raw_list() if why in kinds]
    cyc = {c["cyc"]: c for c in sess.cycles(("ok",), with_lines=True, ids=keep)}
    out = []
    for cid in keep:
        if cid not in cyc:
            continue
        L = cyc[cid]["lines"].get(str(line))
        if not L or L["m"] <= 0.05:
            continue
        r = sess.raw(cid)
        if not r:
            continue
        t = r["t"]
        n = len(t)
        hz = (n - 1) / (t[-1] - t[0]) if n > 1 and t[-1] > t[0] else 100.0
        i0 = max(0, int(round((L["s"] - t[0]) * hz)))
        i1 = min(n, int(round((L["s"] + L["m"] - t[0]) * hz)))
        if i1 - i0 < 5:
            continue
        pad = 6                                            # 前後多取幾筆，微分才不會在邊界失真
        a, b = max(0, i0 - pad), min(n, i1 + pad)
        rec = {"cyc": cid, "hz": hz, "m": L["m"], "off": i0 - a, "n": i1 - i0, "ax": {}}
        for ax in axes:
            th = r[f"q{ax}"][a:b]
            w = deriv(th, hz)
            al = deriv(w, hz)
            jk = deriv(al, hz)
            sl = slice(i0 - a, i1 - a)
            rec["ax"][ax] = {"th": th[sl], "w": w[sl], "al": al[sl], "jk": jk[sl], "tq": r[f"tq{ax}"][i0:i1]}
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def lstsq(rows: list[list[float]], y: list[float], ridge: float = 1e-9) -> list[float] | None:
    """最小平方（正規方程式＋高斯消去）。"""
    k = len(rows[0]) if rows else 0
    if not k or len(rows) < k + 2:
        return None
    A = [[0.0] * k for _ in range(k)]
    b = [0.0] * k
    for r, yy in zip(rows, y):
        for i in range(k):
            ri = r[i]
            b[i] += ri * yy
            Ai = A[i]
            for j in range(i, k):
                Ai[j] += ri * r[j]
    for i in range(k):
        for j in range(i):
            A[i][j] = A[j][i]
        A[i][i] += ridge * (A[i][i] + 1.0)
    for c in range(k):
        p = max(range(c, k), key=lambda i: abs(A[i][c]))
        if abs(A[p][c]) < 1e-15:
            return None
        A[c], A[p] = A[p], A[c]
        b[c], b[p] = b[p], b[c]
        for i in range(c + 1, k):
            f = A[i][c] / A[c][c]
            if f:
                for j in range(c, k):
                    A[i][j] -= f * A[c][j]
                b[i] -= f * b[c]
    x = [0.0] * k
    for i in range(k - 1, -1, -1):
        x[i] = (b[i] - sum(A[i][j] * x[j] for j in range(i + 1, k))) / A[i][i]
    return x

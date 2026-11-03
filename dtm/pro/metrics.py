"""P5 逐行損耗帳本＋ P6 角度集中圖（逐循環累積）。

損耗（以額定扭矩下的等效轉數計）：D = Σ |Δθ|/360 · (|T|/T0)^p
等效扭矩：Tm = (Σ|Δθ|·|T|^p / Σ|Δθ|)^(1/p)
傳遞扭矩用來算損耗；只有干擾扭矩時只能當相對參考（干擾扭矩不含重力與慣性）。
"""
from __future__ import annotations

import bisect

from ..config import axis_cfg
from .store import OSC_EDGES

DEFAULT_LIMITS = {"rv": (2.5, 5.0), "rack": (2.5, 5.0)}   # 起動停止容許、瞬間最大容許（額定扭矩倍數，RV 型錄常見比例）


def axis_params(cfg: dict, ax: int, unit: str) -> dict:
    a = axis_cfg(cfg, ax)
    typ = a.get("reducer_type", "rv")
    p = float(a.get("life_exp") or 0) or (3.0 if typ == "gearbox" else 10.0 / 3.0)
    rated = float(a.get("rated_nm") or 0)
    if unit == "pct":
        scale, absolute = 0.01, True                       # 額定扭矩 T0 的百分比
    elif unit == "nm" and rated > 0:
        scale, absolute = 1.0 / rated, True
    else:
        scale, absolute = 1.0, False                       # 原始值：只做相對排名
    ss = pk = None
    if rated > 0 and float(a.get("startstop_nm") or 0) > 0:
        ss = float(a["startstop_nm"]) / rated
    if rated > 0 and float(a.get("peak_nm") or 0) > 0:
        pk = float(a["peak_nm"]) / rated
    if absolute and ss is None and typ in DEFAULT_LIMITS:
        ss = DEFAULT_LIMITS[typ][0]
    if absolute and pk is None and typ in DEFAULT_LIMITS:
        pk = DEFAULT_LIMITS[typ][1]
    linear = ax == 7 or bool(a.get("linear"))
    return {"ax": ax, "name": a["name"], "p": p, "scale": scale, "absolute": absolute, "ss": ss, "pk": pk,
            "C": max(1, int(a.get("peak_count") or 1000)), "rated_rpm": float(a.get("rated_rpm") or 15.0),
            "rated_life_h": float(a.get("rated_life_h") or 6000.0), "type": typ, "linear": linear,
            "bin": 10.0 if linear else 1.0, "hyst": 0.5 if linear else 0.05,
            "contact": float(a.get("contact_deg") or 0.5) * (10.0 if linear else 1.0),
            "ratio": float(a.get("ratio") or 0), "pins": int(a.get("pins") or 0)}


class Reversal:
    """換向偵測（含遲滯）；記錄每次擺動幅度。跨循環保留狀態。"""
    __slots__ = ("h", "dir", "ext", "last")

    def __init__(self, h: float):
        self.h, self.dir, self.ext, self.last = h, 0, None, None

    def feed(self, th: float):
        """回傳 (換向點角度, 幅度) 或 None。"""
        if self.ext is None:
            self.ext = self.last = th
            return None
        if self.dir >= 0:
            if th > self.ext:
                self.ext = th
                if self.dir == 0 and th - self.last > self.h:
                    self.dir = 1
            elif th < self.ext - self.h:
                ev = (self.ext, abs(self.ext - self.last)) if self.dir == 1 else None
                self.last, self.ext, self.dir = self.ext, th, -1
                return ev
        if self.dir <= 0:
            if th < self.ext:
                self.ext = th
                if self.dir == 0 and self.last - th > self.h:
                    self.dir = -1
            elif th > self.ext + self.h:
                ev = (self.ext, abs(self.ext - self.last)) if self.dir == -1 else None
                self.last, self.ext, self.dir = self.ext, th, 1
                return ev
        return None


def osc_bin(amp: float) -> int:
    return bisect.bisect_left(OSC_EDGES, amp)


def cycle_metrics(cols: dict, seg: list[tuple], axp: list[dict], hz: float, rev: dict, still: list[bool],
                  t0: float) -> tuple[dict, dict, dict, dict, dict]:
    """一個循環的逐行統計、各軸總損耗與峰值、角度累積。

    seg：(狀態, 行號, 起點, 到達, 終點)；still：每筆樣本是否靜止；rev：各軸 Reversal（跨循環）。
    回傳 lines、dmg、peak、angle、osc。
    """
    dt = 1.0 / hz
    ts = cols["t"]
    lines: dict[str, dict] = {}
    for k, no, i0, ia, i1 in seg:
        rec = lines.setdefault(str(no), {"s": round(ts[i0] - t0, 3), "m": 0.0, "w": 0.0, "n": 0,
                                         "D": [0.0] * len(axp), "pk": [0.0] * len(axp), "tm": [0.0] * len(axp),
                                         "ang": [0.0] * len(axp), "h": [0.0] * len(axp), "ss": [0] * len(axp),
                                         "px": [0] * len(axp)})
        rec["m"] += round((ia - i0) * dt, 3)
        rec["w"] += round((i1 - ia) * dt, 3)
        rec["n"] += 1
    dmg, peak, angle, osc = {}, {}, {}, {}
    for ai, P in enumerate(axp):
        ax = P["ax"]
        th = cols[f"q{ax}"]
        tq = cols[f"tq{ax}"]
        p, sc, bw = P["p"], P["scale"], P["bin"]
        ss, pk, contact = P["ss"], P["pk"], P["contact"]
        acc: dict[int, list] = {}
        oh: dict[int, int] = {}
        rv = rev.setdefault(ax, Reversal(P["hyst"]))
        tot, pmax = 0.0, 0.0
        prev_bin = None
        for k, no, i0, ia, i1 in seg:
            rec = lines[str(no)]
            D = num = ang = hold = 0.0
            pkx = 0.0
            nss = npx = 0
            over_ss = over_pk = False
            for i in range(i0, i1):
                x = abs(tq[i]) * sc
                d = abs(th[i] - th[i - 1]) if i > 0 else 0.0
                w = x ** p
                dd = d * w
                num += dd
                ang += d
                b = int(th[i] // bw)
                a = acc.get(b)
                if a is None:
                    a = acc[b] = [0.0, 0.0, 0.0, 0, 0, 0]
                a[0] += dd / 360.0
                a[1] += dt
                if still[i]:
                    hold += x * dt
                    a[2] += x * dt
                if b != prev_bin:
                    a[3] += 1
                    prev_bin = b
                ev = rv.feed(th[i])
                if ev:
                    eb = int(ev[0] // bw)
                    ea = acc.get(eb)
                    if ea is None:
                        ea = acc[eb] = [0.0, 0.0, 0.0, 0, 0, 0]
                    ea[4] += 1
                    if ev[1] < contact:
                        ea[5] += 1
                    ob = osc_bin(ev[1] / (10.0 if P["linear"] else 1.0))
                    oh[ob] = oh.get(ob, 0) + 1
                if x > pkx:
                    pkx = x
                if ss is not None:
                    if x > ss and not over_ss:
                        nss += 1
                    over_ss = x > ss
                if pk is not None:
                    if x > pk and not over_pk:
                        npx += 1
                    over_pk = x > pk
            D = num / 360.0
            rec["D"][ai] += D
            rec["ang"][ai] += ang
            rec["tm"][ai] = (num / ang) ** (1.0 / p) if ang > 0 else 0.0
            rec["pk"][ai] = max(rec["pk"][ai], pkx)
            rec["h"][ai] += hold
            rec["ss"][ai] += nss
            rec["px"][ai] += npx
            tot += D
            pmax = max(pmax, pkx)
        dmg[str(ax)] = tot
        peak[str(ax)] = pmax
        angle[ax] = acc
        osc[ax] = oh
    for rec in lines.values():                         # 縮小儲存量
        rec["D"] = [float(f"{x:.4g}") for x in rec["D"]]
        rec["tm"] = [round(x, 4) for x in rec["tm"]]
        rec["pk"] = [round(x, 4) for x in rec["pk"]]
        rec["ang"] = [round(x, 2) for x in rec["ang"]]
        rec["h"] = [round(x, 4) for x in rec["h"]]
    return lines, dmg, peak, angle, osc

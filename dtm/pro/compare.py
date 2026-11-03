"""P10 改前改後與對照試驗。

流程：改前診斷 → 修改 → 改後診斷 → 追蹤；對照組 = 同型號、相近負載、不修改的另一台，兩期同時量。
統計單位：每日中位數（診斷期不滿 3 天時改用每小時中位數，並在報告註明）。
效果 = 處理組（改後 ÷ 改前）÷ 對照組（改後 ÷ 改前）− 1（對數尺度的差異中的差異）；
95% 信賴區間以拔靴法（各組內重抽時段，2000 次）；處理組改前改後另做 Mann–Whitney 檢定。
"""
from __future__ import annotations

import difflib
import math
import random
import time

from ..i18n import _
from . import impacts, views
from .store import Session

METRICS = ("dmg", "peak", "dur", "move", "impact")


def _blocks(sess: Session, unit: float) -> dict[str, dict]:
    """依時段彙整：每個時段每項指標的中位數。"""
    cyc = sess.cycles(("ok", "anomaly"))
    imp = [e for e in sess.impacts() if e["kind"] == "impact"]
    axes = [str(a) for a in sess.meta.get("axes") or []]
    per: dict[int, dict] = {}
    for c in cyc:
        b = int(c["t0"] // unit)
        d = per.setdefault(b, {"n": 0, "dur": [], "move": [], **{f"dmg{a}": [] for a in axes}, **{f"peak{a}": [] for a in axes}})
        d["n"] += 1
        d["dur"].append(c["dur"])
        d["move"].append(sum(L["m"] for L in c["lines"].values()))
        for a in axes:
            d[f"dmg{a}"].append(c["dmg"].get(a, 0.0))
            d[f"peak{a}"].append(c["peak"].get(a, 0.0))
    for e in imp:
        b = int(e["t"] // unit)
        if b in per:
            per[b]["imp"] = per[b].get("imp", 0) + 1
    out = {}
    for b, d in per.items():
        if d["n"] < 3:
            continue
        rec = {k: views._med(v) for k, v in d.items() if isinstance(v, list)}
        rec["impact"] = 100.0 * d.get("imp", 0) / d["n"]
        rec["n"] = d["n"]
        out[b] = rec
    return out


def mann_whitney(a: list[float], b: list[float]) -> float | None:
    """雙尾 p 值（常態近似、含同值修正；樣本少時用精確分布）。"""
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return None
    allv = sorted([(x, 0) for x in a] + [(x, 1) for x in b])
    ranks = [0.0] * len(allv)
    i = 0
    ties = 0.0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = r
        t = j - i + 1
        ties += t ** 3 - t
        i = j + 1
    R1 = sum(r for r, (x, g) in zip(ranks, allv) if g == 0)
    U = R1 - n1 * (n1 + 1) / 2
    if n1 * n2 <= 400 and ties == 0:
        # 精確分布：U 的計數（動態規劃）
        from functools import lru_cache

        @lru_cache(maxsize=None)
        def cnt(u, m, n):
            if u < 0:
                return 0
            if m == 0 or n == 0:
                return 1 if u == 0 else 0
            return cnt(u - n, m - 1, n) + cnt(u, m, n - 1)
        total = math.comb(n1 + n2, n1)
        u = min(U, n1 * n2 - U)
        p = sum(cnt(k, n1, n2) for k in range(int(math.floor(u)) + 1)) / total
        return min(1.0, 2 * p)
    mu = n1 * n2 / 2
    N = n1 + n2
    sd = math.sqrt(n1 * n2 / 12 * ((N + 1) - ties / (N * (N - 1))))
    if sd == 0:
        return 1.0
    z = (abs(U - mu) - 0.5) / sd
    return min(1.0, math.erfc(z / math.sqrt(2)))


def _effect(tb, ta, cb, ca, log: bool, rng: random.Random, n_boot: int = 2000):
    def stat(tb, ta, cb, ca):
        mt_b, mt_a = sum(tb) / len(tb), sum(ta) / len(ta)
        if log:
            d = mt_a - mt_b
            if cb and ca:
                d -= (sum(ca) / len(ca) - sum(cb) / len(cb))
            return math.exp(d) - 1
        base = mt_b if mt_b else 1.0
        d = mt_a - mt_b
        if cb and ca:
            d -= (sum(ca) / len(ca) - sum(cb) / len(cb))
        return d / base if base else d
    tf = (lambda xs: [math.log(max(x, 1e-12)) for x in xs]) if log else (lambda xs: list(xs))
    TB, TA, CB, CA = tf(tb), tf(ta), tf(cb), tf(ca)
    est = stat(TB, TA, CB, CA)
    def rs(xs):
        return [xs[rng.randrange(len(xs))] for k in range(len(xs))] if xs else []
    boots = [stat(rs(TB), rs(TA), rs(CB), rs(CA)) for k in range(n_boot)]
    boots.sort()
    lo, hi = boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot) - 1]
    return est, lo, hi


def program_diff(a: Session, b: Session) -> dict:
    pa, pb = a.program(), b.program()
    la = {ln.no: ln.text for ln in pa.lines}
    lb = {ln.no: ln.text for ln in pb.lines}
    lines = []
    if [x for x in la.values()] != [x for x in lb.values()]:
        sm = difflib.SequenceMatcher(a=[ln.text for ln in pa.lines], b=[ln.text for ln in pb.lines])
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                continue
            lines.append({"op": op, "before": [f"{pa.lines[i].no}: {pa.lines[i].text}" for i in range(i1, i2)],
                          "after": [f"{pb.lines[j].no}: {pb.lines[j].text}" for j in range(j1, j2)]})
    pos = []
    for no in sorted(set(pa.positions) | set(pb.positions)):
        x, y = pa.positions.get(no), pb.positions.get(no)
        vx = (x.joint or x.cart) if x else {}
        vy = (y.joint or y.cart) if y else {}
        diff = {k: (vx.get(k), vy.get(k)) for k in sorted(set(vx) | set(vy))
                if vx.get(k) is None or vy.get(k) is None or abs(vx[k] - vy[k]) > 0.01}
        if diff:
            pos.append({"point": f"P[{no}]", "name": (y or x).name, "diff": diff})
    return {"lines": lines, "positions": pos, "same_version": pa.version == pb.version,
            "before": pa.version, "after": pb.version}


def compare(cfg: dict, before: list[Session], after: list[Session], cb: list[Session] | None = None,
            ca: list[Session] | None = None, seed: int = 1) -> dict:
    cb, ca = cb or [], ca or []
    # 統計單位：改前、改後都有 3 天以上（每天至少 3 個循環）就用每日中位數，否則每小時，再不夠用每 10 分鐘
    unit, unit_name = 600.0, "10min"
    for u, name in ((86400.0, "day"), (3600.0, "hour")):
        if all(sum(len(_blocks(s, u)) for s in grp) >= 3 for grp in (before, after)):
            unit, unit_name = u, name
            break

    def collect(group):
        out: dict[str, list[float]] = {}
        n = 0
        for s in group:
            for rec in _blocks(s, unit).values():
                n += rec["n"]
                for k, v in rec.items():
                    out.setdefault(k, []).append(v)
        return out, n
    TB, nTB = collect(before)
    TA, nTA = collect(after)
    CB, nCB = collect(cb)
    CA, nCA = collect(ca)
    axes = [str(a) for a in (before[0].meta.get("axes") if before else [])]
    axp = {str(P["ax"]): P for P in views.axis_params(cfg, before[0])} if before else {}
    rng = random.Random(seed)
    rows = []

    def add(key, label, log=True, ax=None):
        tb, ta = TB.get(key, []), TA.get(key, [])
        if len(tb) < 2 or len(ta) < 2:
            return
        cb_, ca_ = CB.get(key, []), CA.get(key, [])
        use_ctrl = len(cb_) >= 2 and len(ca_) >= 2
        if log and (min(tb + ta) <= 0 or (use_ctrl and min(cb_ + ca_) <= 0)):
            log = False
        est, lo, hi = _effect(tb, ta, cb_ if use_ctrl else [], ca_ if use_ctrl else [], log, rng)
        rows.append({"key": key, "label": label, "ax": ax, "before": views._med(tb), "after": views._med(ta),
                     "ctrl_before": views._med(cb_) if use_ctrl else None, "ctrl_after": views._med(ca_) if use_ctrl else None,
                     "effect": est, "lo": lo, "hi": hi, "p": mann_whitney(tb, ta), "nb": len(tb), "na": len(ta),
                     "significant": (lo > 0 or hi < 0), "control": use_ctrl})
    add("dur", _("循環時間"))
    add("move", _("動作時間"))
    mx = max([views._med(TB.get(f"dmg{a}", [0])) for a in axes] or [0])
    moving = [a for a in axes if views._med(TB.get(f"dmg{a}", [0])) > 1e-4 * mx]      # 幾乎沒動的軸不比
    for a in moving:
        nm = axp.get(a, {}).get("name", f"J{a}")
        add(f"dmg{a}", _("{ax} 每循環損耗", ax=nm), ax=int(a))
    for a in moving:
        nm = axp.get(a, {}).get("name", f"J{a}")
        add(f"peak{a}", _("{ax} 峰值扭矩", ax=nm), ax=int(a))
    add("impact", _("衝擊（每 100 循環）"), log=False)
    # 壽命與成本
    life = []
    Sb = views.summary(cfg, before[0]) if before else None
    if Sb:
        for a in Sb["axes"]:
            r = next((x for x in rows if x["key"] == f"dmg{a['ax']}"), None)
            if r and a.get("life_years"):
                from .slack import after_life
                L0 = a["life_years"]
                L1 = after_life(L0, a.get("events_share") or 0.0, r["effect"])
                life.append({"ax": a["ax"], "name": a["name"], "before": L0, "after": L1})
    cost = cfg["pro"].get("cost", {})
    per_event = float(cost.get("downtime_h", 0)) * float(cost.get("per_hour", 0)) + float(cost.get("spare", 0)) + float(cost.get("labor", 0))
    avoided = sum(max(0.0, 1 / x["before"] - 1 / x["after"]) for x in life if x["before"] > 0 and x["after"] > 0)
    stops = {}
    for grp, name in ((before, "before"), (after, "after")):
        tot, days = 0.0, 0.0
        for s in grp:
            sm = impacts.summary(s)
            tot += sm["weighted"]
            days += sm["days"]
        stops[name] = round(tot / days, 3) if days > 0 else None
    res = {"unit": unit_name, "rows": rows, "life": life, "avoided_per_year": avoided,
           "saving_per_year": avoided * per_event if per_event > 0 else None, "per_event": per_event,
           "n": {"before": nTB, "after": nTA, "ctrl_before": nCB, "ctrl_after": nCA}, "stops_per_day": stops,
           "sessions": {"before": [s.id for s in before], "after": [s.id for s in after],
                        "ctrl_before": [s.id for s in cb], "ctrl_after": [s.id for s in ca]},
           "diff": program_diff(before[0], after[0]) if before and after else None,
           "generated": time.time()}
    notes = []
    if unit_name != "day":
        notes.append(_("診斷期不滿 3 天，改用每{u}中位數做統計；正式驗證建議各 3–7 天。", u=_("小時") if unit_name == "hour" else _("10 分鐘")))
    if not (cb and ca):
        notes.append(_("沒有對照組：環境與產品變化無法扣除，效果可能高估或低估。"))
    res["notes"] = notes
    return res

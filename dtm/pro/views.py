"""P5、P6、P13 的彙整：總覽、逐行損耗排行、典型波形時間軸、角度集中圖。"""
from __future__ import annotations

import math

from ..i18n import _
from . import lsparse, metrics
from .store import OSC_EDGES, Session, local_meta


def _med(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _q(xs, q):
    s = sorted(xs)
    if not s:
        return 0.0
    k = (len(s) - 1) * q / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


_CACHE: dict[tuple, tuple] = {}


def cached(sess: Session, key: str, fn):
    """依資料版本快取計算結果（網頁重複開同一頁時不必重算）。"""
    from ..i18n import get_lang
    k = (sess.path, key, get_lang())
    st = sess.stamp()
    hit = _CACHE.get(k)
    if hit and hit[0] == st:
        return hit[1]
    v = fn()
    _CACHE[k] = (st, v)
    if len(_CACHE) > 200:
        _CACHE.pop(next(iter(_CACHE)))
    return v


def axis_params(cfg: dict, sess: Session) -> list[dict]:
    unit = sess.meta.get("torque_unit", cfg["pro"]["torque_unit"])
    return [metrics.axis_params(cfg, a, unit) for a in sess.meta.get("axes") or []]


def cycles_per_day(sess: Session, med_dur: float) -> tuple[float, str]:
    v = float(sess.meta.get("cycles_per_day") or 0)
    if v > 0:
        return v, "set"
    h = float(sess.meta.get("hours_per_day") or 20.0)
    return (3600.0 / med_dur * h if med_dur > 0 else 0.0), "estimated"


def line_table(cfg: dict, sess: Session, cycles: list[dict] | None = None) -> dict:
    """逐行統計（中位數）與損耗占比。"""
    prog = sess.program()
    axp = axis_params(cfg, sess)
    cyc = cycles if cycles is not None else sess.cycles(("ok", "anomaly"))
    ml = prog.motion_lines()
    eff = lsparse.effective_terms(prog)
    nax = len(axp)
    tot_ax = [0.0] * nax
    per: dict[str, dict] = {}
    for c in cyc:
        for no, r in c["lines"].items():
            d = per.setdefault(no, {"m": [], "w": [], "D": [[] for _ in range(nax)], "pk": [[] for _ in range(nax)],
                                    "tm": [[] for _ in range(nax)], "h": [[] for _ in range(nax)],
                                    "ss": [0] * nax, "px": [0] * nax, "sumD": [0.0] * nax})
            d["m"].append(r["m"])
            d["w"].append(r["w"])
            for i in range(nax):
                d["D"][i].append(r["D"][i])
                d["pk"][i].append(r["pk"][i])
                d["tm"][i].append(r["tm"][i])
                d["h"][i].append(r["h"][i])
                d["ss"][i] += r["ss"][i]
                d["px"][i] += r["px"][i]
                d["sumD"][i] += r["D"][i]
                tot_ax[i] += r["D"][i]
    rows = []
    ncyc = max(1, len(cyc))
    R0 = [60.0 * P["rated_life_h"] * P["rated_rpm"] for P in axp]
    for ln in ml:
        d = per.get(str(ln.no))
        row = {"line": ln.no, "text": ln.text, "kind": ln.kind, "term": ln.term, "cnt": ln.cnt, "acc": ln.acc,
               "speed": ln.speed, "unit": ln.speed_unit, "eff": eff.get(ln.no), "point": ln.point,
               "point_name": ln.point_name, "n": len(d["m"]) if d else 0}
        waits = [x for x in prog.between(ln.no, next((m.no for m in ml[ml.index(ln) + 1:]), None))]
        row["after"] = [{"no": x.no, "kind": x.kind, "text": x.text} for x in waits if x.kind != "comment"]
        if d:
            row["m"] = round(_med(d["m"]), 3)
            row["w"] = round(_med(d["w"]), 3)
            row["w10"] = round(_q(d["w"], 10), 3)
            row["w90"] = round(_q(d["w"], 90), 3)
            row["D"] = [_med(x) for x in d["D"]]
            row["share"] = [(d["sumD"][i] / tot_ax[i]) if tot_ax[i] > 0 else 0.0 for i in range(nax)]
            row["tm"] = [round(_med(x), 4) for x in d["tm"]]
            row["pk"] = [round(max(x), 4) if x else 0.0 for x in d["pk"]]
            row["pk95"] = [round(_q(x, 95), 4) for x in d["pk"]]
            row["ss_pct"] = [(row["pk"][i] / axp[i]["ss"]) if axp[i]["ss"] else None for i in range(nax)]
            row["ss"] = [round(x / ncyc, 3) for x in d["ss"]]
            row["px"] = d["px"]
            row["hold"] = [round(_med(x), 4) for x in d["h"]]
            row["cv"] = []
            for x in d["D"]:
                m = sum(x) / len(x) if x else 0.0
                sd = math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1)) if len(x) > 1 else 0.0
                row["cv"].append(round(sd / m, 3) if m > 0 else 0.0)
            row["life"] = sum((d["sumD"][i] / ncyc) / R0[i] for i in range(nax) if axp[i]["absolute"])
        rows.append(row)
    if any(r.get("life") for r in rows):
        tot = sum(r.get("life", 0.0) for r in rows) or 1.0
        for r in rows:
            r["score"] = r.get("life", 0.0) / tot
    else:
        for r in rows:
            r["score"] = max(r.get("share") or [0.0])
    rank = sorted([r for r in rows if r.get("n")], key=lambda r: -r["score"])
    for i, r in enumerate(rank):
        r["rank"] = i + 1
    return {"rows": rows, "tot_ax": tot_ax, "ncyc": len(cyc)}


def summary(cfg: dict, sess: Session) -> dict:
    meta = local_meta(sess.meta)
    axp = axis_params(cfg, sess)
    cyc = sess.cycles(("ok", "anomaly"))
    counts = sess.cycle_counts()
    lt = line_table(cfg, sess, cyc)
    durs = [c["dur"] for c in cyc]
    med_dur = _med(durs)
    cpd, cpd_src = cycles_per_day(sess, med_dur)
    axes = []
    events = {}
    for imp in sess.impacts():
        if imp["kind"] in ("estop", "collision", "controlled", "fence"):
            events[imp["kind"]] = events.get(imp["kind"], 0) + float(imp["weight"] or 1.0)
    span_days = max(1e-6, ((meta.get("t1") or 0) - (meta.get("t0") or 0)) / 86400.0)
    dmax = max([_med([c["dmg"].get(str(P["ax"]), 0.0) for c in cyc]) for P in axp] or [0.0])
    for i, P in enumerate(axp):
        k = str(P["ax"])
        per_cyc = [c["dmg"].get(k, 0.0) for c in cyc]
        D = _med(per_cyc)
        moving = D > 1e-4 * dmax                      # 幾乎沒動的軸不估壽命（只剩停止事件的部分，會誤導）
        R0 = 60.0 * P["rated_life_h"] * P["rated_rpm"]
        px = sum(r["px"][i] for r in lt["rows"] if r.get("px"))
        a = {"ax": P["ax"], "name": P["name"], "p": round(P["p"], 3), "absolute": P["absolute"], "D_cycle": D,
             "peak": max((c["peak"].get(k, 0.0) for c in cyc), default=0.0), "type": P["type"],
             "ss": P["ss"], "pk": P["pk"], "px_events": px, "moving": moving}
        if P["absolute"] and moving and cpd > 0:
            per_day = D * cpd / R0 + (px / max(1, len(cyc))) * cpd / P["C"]
            ev = sum(events.values()) / span_days / P["C"] if events else 0.0
            a["consume_day"] = per_day + ev
            a["life_years"] = 1.0 / (per_day + ev) / 365.0
            a["events_share"] = ev / (per_day + ev) if per_day + ev > 0 else 0.0
        axes.append(a)
    ev = sess.get("eval") or {}
    warn = []
    if meta.get("torque") == "disturbance":
        warn.append(_("損耗用干擾扭矩計算：干擾扭矩不含重力與慣性，損耗排名只能當相對參考。"))
    if not all(P["absolute"] for P in axp):
        warn.append(_("沒有額定扭矩，只能做相對排名，不估算壽命。"))
    if len(cyc) < 30:
        warn.append(_("有效循環不到 30 個，統計結果僅供參考。"))
    confs = [c["conf"] for c in cyc if c.get("conf") is not None]
    if confs and _med(confs) < 0.85:
        warn.append(_("行號對位信心偏低：請確認程式版本與資料是否一致。"))
    bad = counts.get("bad", 0)
    if bad:
        warn.append(_("{n} 個循環對位失敗（位置與程式路徑不符），已排除。", n=bad))
    return {"meta": meta, "counts": counts, "axes": axes, "lines": lt["rows"], "ncyc": len(cyc),
            "cycle": {"med": round(med_dur, 3), "p10": round(_q(durs, 10), 3), "p90": round(_q(durs, 90), 3),
                      "move": round(sum(r.get("m", 0) for r in lt["rows"]), 3)},
            "cycles_per_day": round(cpd, 1), "cpd_src": cpd_src, "events": events,
            "align": {"mode": (sess.get("align") or {}).get("mode", ""), "conf": round(_med(confs), 3) if confs else None,
                      "acc_est": (ev["est"] / ev["n"]) if ev.get("n") else None,
                      "acc_raw": (ev["raw"] / ev["n"]) if ev.get("n") and ev.get("raw") else None},
            "warnings": warn}


# ------------------------------------------------------------ P13 典型波形時間軸
def timeline(cfg: dict, sess: Session, ax: int, grid: int | None = None, limit: int = 300) -> dict:
    grid = int(grid or cfg["pro"]["grid"])
    P = next((p for p in axis_params(cfg, sess) if p["ax"] == ax), None)
    if not P:
        return {}
    mul = 100.0 * P["scale"] if P["absolute"] else 1.0
    prog = sess.program()
    order = [ln.no for ln in prog.motion_lines()]
    raws = [c for c, why in sess.raw_list() if why in ("first", "sample")][-limit:]
    cyc = {c["cyc"]: c for c in sess.cycles(("ok",), with_lines=True, ids=raws)}
    acc = {no: {"m": [], "w": [], "mv": [[] for _ in range(grid)], "dw": [[] for _ in range(10)],
                "pos": [[] for _ in range(grid)]} for no in order}
    for cid in raws:
        c = cyc.get(cid)
        if not c:
            continue
        r = sess.raw(cid)
        if not r or f"tq{ax}" not in r:
            continue
        t, tq, q = r["t"], r[f"tq{ax}"], r[f"q{ax}"]
        n = len(t)
        hz = (n - 1) / (t[-1] - t[0]) if n > 1 and t[-1] > t[0] else 100.0
        for no in order:
            L = c["lines"].get(str(no))
            if not L:
                continue
            a = acc[no]
            a["m"].append(L["m"])
            a["w"].append(L["w"])
            i0 = int(round((L["s"] - t[0]) * hz))
            im = int(round((L["s"] + L["m"] - t[0]) * hz))
            i1 = int(round((L["s"] + L["m"] + L["w"] - t[0]) * hz))
            i0, im, i1 = max(0, min(n - 1, i0)), max(0, min(n, im)), max(0, min(n, i1))
            if im - i0 >= 2:
                for g in range(grid):
                    j = i0 + int(g * (im - i0 - 1) / max(1, grid - 1))
                    a["mv"][g].append(tq[j] * mul)
                    a["pos"][g].append(q[j])
            if i1 - im >= 2:
                for g in range(10):
                    j = im + int(g * (i1 - im - 1) / 9)
                    a["dw"][g].append(tq[j] * mul)
    lt = line_table(cfg, sess)
    ai = (sess.meta.get("axes") or []).index(ax)
    share = {r["line"]: (r.get("share") or [0.0] * (ai + 1))[ai] for r in lt["rows"]}
    xs, med, lo, hi, pos, blocks = [], [], [], [], [], []
    x = 0.0
    for no in order:
        a = acc[no]
        m, w = _med(a["m"]), _med(a["w"])
        b = {"line": no, "x0": round(x, 3), "xm": round(x + m, 3), "x1": round(x + m + w, 3), "share": share.get(no, 0.0)}
        if a["mv"][0]:
            for g in range(grid):
                xs.append(round(x + m * g / max(1, grid - 1), 3))
                v = a["mv"][g]
                med.append(round(_med(v), 2))
                lo.append(round(_q(v, 10), 2))
                hi.append(round(_q(v, 90), 2))
                pos.append(round(_med(a["pos"][g]), 2))
        if w > 0.02 and a["dw"][0]:
            for g in range(10):
                xs.append(round(x + m + w * g / 9, 3))
                v = a["dw"][g]
                med.append(round(_med(v), 2))
                lo.append(round(_q(v, 10), 2))
                hi.append(round(_q(v, 90), 2))
                pos.append(pos[-1] if pos else 0.0)
        blocks.append(b)
        x += m + w
    # 中層：衝擊事件（依行數）
    imp = {}
    for e in sess.impacts():
        if e.get("line"):
            imp[e["line"]] = imp.get(e["line"], 0) + 1
    return {"ax": ax, "name": P["name"], "unit": "%" if P["absolute"] else "raw", "x": xs, "med": med, "lo": lo,
            "hi": hi, "pos": pos, "blocks": blocks, "impacts": imp, "ncyc": len(raws),
            "ss": P["ss"] * 100 if P["ss"] and P["absolute"] else None,
            "pk": P["pk"] * 100 if P["pk"] and P["absolute"] else None}


# ------------------------------------------------------------ P6 角度集中圖
def concentration(dens: list[tuple[int, float]], bw: float, frac: float = 0.8) -> dict:
    """θ80（包含 80% 損耗的最窄連續角度範圍）、Gini、前 5 區間占比。dens = [(bin, 值)]，依 bin 排序。"""
    if not dens:
        return {"theta80": 0.0, "gini": 0.0, "top5": 0.0, "range": [0, 0]}
    b0, b1 = dens[0][0], dens[-1][0]
    arr = [0.0] * (b1 - b0 + 1)
    for b, v in dens:
        arr[b - b0] = v
    tot = sum(arr)
    if tot <= 0:
        return {"theta80": 0.0, "gini": 0.0, "top5": 0.0, "range": [b0 * bw, (b1 + 1) * bw]}
    need = frac * tot
    best, bi, bj = len(arr) + 1, 0, len(arr) - 1
    s, i = 0.0, 0
    for j in range(len(arr)):
        s += arr[j]
        while s - arr[i] >= need and i < j:
            s -= arr[i]
            i += 1
        if s >= need and j - i + 1 < best:
            best, bi, bj = j - i + 1, i, j
    xs = sorted(arr)
    n = len(xs)
    cum = sum((i + 1) * x for i, x in enumerate(xs))
    gini = (2 * cum) / (n * tot) - (n + 1) / n if n > 1 else 0.0
    top5 = sum(sorted(arr, reverse=True)[:5]) / tot
    return {"theta80": round(best * bw, 2), "lo": round((b0 + bi) * bw, 2), "hi": round((b0 + bj + 1) * bw, 2),
            "gini": round(gini, 3), "top5": round(top5, 3), "range": [b0 * bw, (b1 + 1) * bw]}


def angle_map(cfg: dict, sess: Session, ax: int) -> dict:
    P = next((p for p in axis_params(cfg, sess) if p["ax"] == ax), None)
    if not P:
        return {}
    rows = sess.angle(ax)
    bw = P["bin"]
    used = [(b, d) for b, d, *_ in rows]
    conc = concentration(used, bw)
    tot = sum(d for _, d in used) or 1.0
    bins = [{"b": b, "deg": round(b * bw, 2), "d": d / tot, "dwell": round(dw, 2), "hold": round(h, 4), "pass": ps,
             "rev": rv, "fret": fr} for b, d, dw, h, ps, rv, fr in rows]
    osc = sess.osc(ax)
    edges = list(OSC_EDGES) + [None]
    hist = [{"lo": (OSC_EDGES[i - 1] if i > 0 else 0.0), "hi": edges[i], "n": osc.get(i, 0)} for i in range(len(edges))]
    fret = sum(r["fret"] for r in bins)
    rev = sum(r["rev"] for r in bins)
    # 每天次數：依每日循環數換算（資料可能只取了部分循環）；沒有循環時依實際時間
    cnt = sess.cycle_counts()
    ncyc = sum(cnt.values())
    durs = [c["dur"] for c in sess.cycles(("ok",), with_lines=False)]
    cpd = cycles_per_day(sess, _med(durs))[0] if durs else 0.0
    span = (ncyc / cpd) if ncyc and cpd else max(1e-6, ((sess.meta.get("t1") or 0) - (sess.meta.get("t0") or 0)) / 86400.0)
    # 高使用區（U）：損耗密度由高到低累積到 50% 的區間；低使用區（R）：有經過、但密度最低的 30%
    srt = sorted(bins, key=lambda r: -r["d"])
    U, acc = [], 0.0
    for r in srt:
        if acc >= 0.5:
            break
        U.append(r["b"])
        acc += r["d"]
    visited = [r for r in bins if r["pass"] > 0]
    low = sorted(visited, key=lambda r: r["d"])[: max(1, int(0.3 * len(visited)))]
    out = {"ax": ax, "name": P["name"], "bin": bw, "unit": "mm" if P["linear"] else "deg", "bins": bins, "conc": conc,
           "osc": hist, "fret": fret, "rev": rev, "fret_day": round(fret / span, 1), "rev_day": round(rev / span, 1),
           "contact": P["contact"], "U": sorted(U), "R": sorted(r["b"] for r in low), "pins": P["pins"], "ratio": P["ratio"]}
    if P["pins"] > 0 and not P["linear"]:
        pitch = 360.0 / P["pins"]
        top = sorted(bins, key=lambda r: -r["d"])[:10]
        out["pin_map"] = [{"deg": r["deg"], "pin": int((r["deg"] % 360) // pitch) + 1, "d": r["d"]} for r in top]
    return out

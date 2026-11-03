"""P8 等待餘裕分析與時間縮放（不影響節拍的降載）＋ 節拍與壽命取捨表。

1. 動作行後面有 WAIT 訊號（等外部條件）時，量每個循環實際等了多久 w。
2. 可用餘裕 S = w 的第 q 百分位 − 固定等待（計時 WAIT、定位確認）− 安全裕度。
3. 等待前的動作區塊（從上一個等待到這一行）時間由 T 延長為 T + S：σ = (T + S)/T，速度 ÷σ、加速度 ÷σ²。
4. 新扭矩估計 T' = T − c·α·(1 − 1/σ²) − v·ω·(1 − 1/σ)：c、v 由實測資料對每一行回歸
   （τ = 三次多項式(u) + c·α + v·ω，u 為行內進度；位置只跟 u 有關，所以重力項不受縮放影響）。
5. 以 P5 的公式重算損耗。節拍不變的條件：機器人仍在訊號到來前到達。

只用實際量到的資料估計；不從程式模擬機器人動作，也不自動修改程式（只產生建議文字）。
"""
from __future__ import annotations

from ..i18n import _
from . import kin, lsparse, views
from .store import Session


def _q(xs, q):
    return views._q(xs, q)


def program_blocks(prog: lsparse.Program) -> list[dict]:
    """每個動作行後面的等待；等待前的動作區塊。"""
    ml = prog.motion_lines()
    info = []
    for i, ln in enumerate(ml):
        nxt = ml[i + 1].no if i + 1 < len(ml) else None
        after = [x for x in prog.between(ln.no, nxt) if x.kind != "comment"]
        sig = [x for x in after if x.kind == "wait" and x.wait_s is None]
        timer = sum(x.wait_s for x in after if x.kind == "wait" and x.wait_s is not None)
        stop = any(x.kind in ("wait", "call") for x in after)
        info.append({"line": ln.no, "sig": [x.text for x in sig], "timer": timer, "stop": stop})
    blocks = []
    for i, d in enumerate(info):
        if not d["sig"]:
            continue
        j = i
        while j - 1 >= 0 and not info[j - 1]["stop"]:
            j -= 1
        blocks.append({"wait_line": d["line"], "signal": d["sig"], "timer": d["timer"],
                       "lines": [info[k]["line"] for k in range(j, i + 1)]})
    return blocks


def fit_line(recs: list[dict], ax: int) -> dict | None:
    rows, y = [], []
    for rec in recs:
        a = rec["ax"][ax]
        n = rec["n"]
        step = 2 if n > 60 else 1
        for i in range(0, n, step):
            u = i / max(1, n - 1)
            rows.append([1.0, u, u * u, u * u * u, a["al"][i], a["w"][i]])
            y.append(a["tq"][i])
    x = kin.lstsq(rows, y)
    if not x:
        return None
    my = sum(y) / len(y)
    sst = sum((v - my) ** 2 for v in y) or 1e-12
    sse = 0.0
    for r, v in zip(rows, y):
        e = v - sum(a * b for a, b in zip(r, x))
        sse += e * e
    return {"g": x[:4], "c": x[4], "v": x[5], "r2": 1 - sse / sst, "n": len(y)}


def ratio(recs: list[dict], ax: int, m: dict, sigma: float, scale: float, p: float) -> tuple[float, float]:
    """時間縮放後的損耗比 D'/D 與每循環動作段損耗 D。"""
    if sigma <= 1.0:
        return 1.0, 0.0
    k1, k2 = 1 - 1 / sigma ** 2, 1 - 1 / sigma
    c, v = m["c"], m["v"]
    D = D2 = 0.0
    for rec in recs:
        a = rec["ax"][ax]
        th, tq, al, w = a["th"], a["tq"], a["al"], a["w"]
        for i in range(1, len(th)):
            d = abs(th[i] - th[i - 1])
            if d == 0:
                continue
            t = tq[i]
            D += d * abs(t * scale) ** p
            D2 += d * abs((t - c * al[i] * k1 - v * w[i] * k2) * scale) ** p
    if D <= 0:
        return 1.0, 0.0
    return D2 / D, D / 360.0 / max(1, len(recs))


def after_life(L0: float, events_share: float, change: float) -> float:
    """壽命換算：循環損耗變化 change（−0.3 = 減少 30%），停止事件部分不變。"""
    k = (1 - events_share) * max(0.0, 1 + change) + events_share
    return L0 / max(1e-9, k)


def _suggest_text(ln: lsparse.Line, sigma: float) -> str:
    txt = ln.text
    if ln.speed is not None:
        txt = lsparse.with_speed(txt, ln.speed / sigma)
    acc = ln.acc or 100
    return lsparse.with_acc(txt, max(1, int(round(acc / sigma))))


def analyze(cfg: dict, sess: Session) -> dict:
    p = cfg["pro"]
    prog = sess.program()
    S = views.summary(cfg, sess)
    axp = views.axis_params(cfg, sess)
    axes = [P["ax"] for P in axp]
    rows = {r["line"]: r for r in S["lines"]}
    cyc = sess.cycles(("ok",))
    dcyc = {P["ax"]: views._med([c["dmg"].get(str(P["ax"]), 0.0) for c in cyc]) for P in axp}
    cache: dict[int, list] = {}
    fits: dict[tuple, dict | None] = {}

    def recs(no):
        if no not in cache:
            cache[no] = kin.line_cycles(sess, no, axes, limit=30)
        return cache[no]

    def model(no, ax):
        if (no, ax) not in fits:
            r = recs(no)
            fits[(no, ax)] = fit_line(r, ax) if r else None
        return fits[(no, ax)]

    def effect(lines: list[int], sig_of) -> dict:
        """各軸每循環損耗下降比例（相對整個循環）。"""
        out = {}
        for P in axp:
            ax = P["ax"]
            save = 0.0
            for no in lines:
                sigma = sig_of(no)
                mdl = model(no, ax)
                if not mdl or sigma <= 1.0:
                    continue
                rt, dmove = ratio(recs(no), ax, mdl, sigma, P["scale"], P["p"])
                save += dmove * (1 - rt)
            out[str(ax)] = save / dcyc[ax] if dcyc[ax] > 0 else 0.0
        return out

    blocks = []
    in_block = set()
    for b in program_blocks(prog):
        w = [c["lines"].get(str(b["wait_line"]), {}).get("w") for c in cyc]
        w = [x for x in w if x is not None]
        if not w:
            continue
        fixed = b["timer"] + 0.05
        slack = _q(w, p["slack_q"]) - fixed - p["slack_margin_s"]
        T = sum(rows[no].get("m", 0.0) for no in b["lines"] if no in rows)
        d = dict(b, w_p10=round(_q(w, 10), 3), w_med=round(views._med(w), 3), w_min=round(min(w), 3),
                 slack=round(max(0.0, slack), 3), T=round(T, 3))
        if slack > 0.05 and T > 0.05:
            sigma = min(2.0, (T + slack) / T)
            d["sigma"] = round(sigma, 3)
            d["effect"] = effect(b["lines"], lambda no: sigma)
            d["fit"] = {str(no): {str(ax): (round(model(no, ax)["r2"], 3) if model(no, ax) else None) for ax in axes}
                        for no in b["lines"]}
            d["proposal"] = [{"line": no, "before": prog.line(no).text, "after": _suggest_text(prog.line(no), sigma)}
                             for no in b["lines"] if prog.line(no) and rows.get(no, {}).get("m", 0) > 0.05]
            in_block.update(b["lines"])
        blocks.append(d)
    # 節拍與壽命取捨表：沒有餘裕的高損耗行，每多 0.1 秒換多少損耗
    trade = []
    med = S["cycle"]["med"] or 1.0
    life = {str(a["ax"]): (a.get("life_years"), a.get("events_share") or 0.0) for a in S["axes"]}
    cands = sorted([r for r in S["lines"] if r.get("n") and r["line"] not in in_block and r.get("m", 0) > 0.15],
                   key=lambda r: -r.get("score", 0))[:3]
    for r in cands:
        steps = []
        for dt in (0.1, 0.2, 0.3, 0.5):
            sigma = (r["m"] + dt) / r["m"]
            eff = effect([r["line"]], lambda no: sigma)
            ax_best = max(eff, key=lambda k: eff[k]) if eff else None
            st = {"dt": dt, "sigma": round(sigma, 3), "effect": eff, "takt_pct": round(dt / med * 100, 2)}
            if ax_best and life.get(ax_best, (None,))[0]:
                L0, ev = life[ax_best]
                st["axis"] = int(ax_best)
                st["life_before"] = L0
                st["life_after"] = after_life(L0, ev, -eff[ax_best])
            steps.append(st)
        trade.append({"line": r["line"], "text": r["text"], "m": r["m"], "steps": steps})
    notes = [_("估計只用實測資料：每一行回歸「重力（隨行內進度）＋慣性（角加速度）＋黏滯（角速度）」，慣性項 ÷σ²、黏滯項 ÷σ。"),
             _("庫倫摩擦與重力不隨速度變，維持原值（保守估計）。"),
             _("節拍不變的條件：改後仍在訊號到來前到達；請以改後診斷確認等待時間仍大於 0。")]
    return {"blocks": blocks, "tradeoff": trade, "notes": notes, "cycle": S["cycle"]}

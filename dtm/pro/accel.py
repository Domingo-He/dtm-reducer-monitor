"""P12 加速曲線：每個「靜止到靜止」的動作行，量實際的峰值加速度、急動度與曲線形狀。

同樣距離 D、時間 T：梯形速度（加速、等速、減速各 1/3）峰值加速度 4.5·D/T²；最小急動度（五次多項式）5.77·D/T²。
形狀係數 κ = 實測峰值加速度 × T² ÷ D。
判斷：衝擊或共振（加速規看得到）→ 降低急動度；累積損耗高 → 降低峰值（有餘裕就延長時間）。
只用實際量到的位置；不做馬達電流頻帶分析。
"""
from __future__ import annotations

from ..i18n import _
from . import kin, lsparse, views
from .store import Session


def shape(kappa: float) -> str:
    if kappa <= 0:
        return ""
    if kappa < 4.2:
        return _("接近等加速")
    if kappa < 5.2:
        return _("梯形")
    if kappa < 6.5:
        return _("平滑（S 曲線）")
    return _("加速集中")


def analyze(cfg: dict, sess: Session, limit: int = 10) -> dict:
    prog = sess.program()
    eff = lsparse.effective_terms(prog)
    ml = prog.motion_lines()
    axes = sess.meta.get("axes") or []
    rot = [a for a in axes if a != 7]
    S = views.summary(cfg, sess)
    rows = {r["line"]: r for r in S["lines"]}
    imp = {}
    for e in sess.impacts():
        if e["kind"] == "impact" and e.get("line"):
            imp[e["line"]] = imp.get(e["line"], 0) + 1
    ncyc = max(1, S["ncyc"])
    out = []
    for i, ln in enumerate(ml):
        prev_fine = i == 0 or eff[ml[i - 1].no] == "FINE"
        rest = prev_fine and eff[ln.no] == "FINE"
        r = rows.get(ln.no, {})
        rec = {"line": ln.no, "text": ln.text, "rest": rest, "impacts": imp.get(ln.no, 0),
               "impacts_100": round(imp.get(ln.no, 0) / ncyc * 100, 2), "score": r.get("score", 0.0)}
        if r.get("m", 0) > 0.1:
            recs = kin.line_cycles(sess, ln.no, rot, limit=limit)
            vals = []
            for c in recs:
                best = None
                for ax in rot:
                    a = c["ax"][ax]
                    D = abs(a["th"][-1] - a["th"][0])
                    if best is None or D > best[1]:
                        best = (ax, D, a)
                if not best or best[1] < 0.5:
                    continue
                ax, D, a = best
                vals.append((ax, D, c["m"], max(abs(x) for x in a["al"]), max(abs(x) for x in a["jk"])))
            if vals:
                ax = max(set(v[0] for v in vals), key=[v[0] for v in vals].count)
                D = views._med([v[1] for v in vals])
                T = views._med([v[2] for v in vals])
                ap = views._med([v[3] for v in vals])
                jp = views._med([v[4] for v in vals])
                rec.update(ax=ax, D=round(D, 2), T=round(T, 3), a_peak=round(ap, 1), j_peak=round(jp, 0))
                if rest and D > 0:
                    k = ap * T * T / D
                    rec.update(kappa=round(k, 2), shape=shape(k), a_trap=round(4.5 * D / T / T, 1),
                               a_mj=round(5.77 * D / T / T, 1))
        adv = []
        if rec["impacts_100"] >= 1:
            adv.append(_("有衝擊：降低急動度（加長加減速、終點前減速），或檢查干涉"))
        if rec["score"] >= 0.1:
            adv.append(_("累積損耗高：降低峰值（有等待餘裕就延長時間）"))
        rec["advice"] = adv
        out.append(rec)
    return {"lines": out,
            "notes": [_("同距離同時間：梯形速度峰值加速度 4.5·D/T²，最小急動度 5.77·D/T²；曲線越平滑衝擊越小，但峰值越高。"),
                      _("只計算「靜止到靜止」的動作（前後都是 FINE）；CNT 過點的動作形狀受前後動作影響。")]}

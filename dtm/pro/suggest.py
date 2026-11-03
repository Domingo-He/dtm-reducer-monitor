"""P9 修改建議（規則版）。

所有數字由系統從實測資料計算；只輸出建議文字，不寫回控制器、不自動修改程式、不自動降速。
修改由人執行：先用模擬軟體確認干涉與節拍 → T1 低速試跑 → 上線 → 改後診斷驗證（P10）。
"""
from __future__ import annotations

from ..i18n import N_, _, join
from . import accel, impacts, lsparse, slack, views
from .store import Session

SAFETY = N_("只提供建議，不會寫回控制器，也不會自動修改程式或降速。"
            "修改由人執行：先用模擬軟體確認干涉與節拍 → T1 低速試跑 → 上線 → 開一期改後診斷驗證效果。")


def _fmt_axes(eff: dict, axp: list[dict]) -> str:
    names = {str(P["ax"]): P["name"] for P in axp}
    parts = [f"{names.get(k, k)} −{v * 100:.0f}%" for k, v in sorted(eff.items(), key=lambda x: -x[1]) if v >= 0.01]
    return join(parts[:4], "、")


def build(cfg: dict, sess: Session) -> dict:
    prog = sess.program()
    S = views.summary(cfg, sess)
    axp = views.axis_params(cfg, sess)
    names = [P["name"] for P in axp]
    rows = {r["line"]: r for r in S["lines"]}
    ml = prog.motion_lines()
    out = []

    def add(rule, line, title, why, proposal=None, effect=None, score=0.0, caution=""):
        out.append({"rule": rule, "line": line, "title": title, "why": why, "proposal": proposal or [],
                    "effect": effect or "", "score": round(score, 4), "caution": caution})

    # R1 不需停的 FINE 點改 CNT（本行與下一行都是有份量的動作；微小動作與第一個點不算）
    cyc50 = sess.cycles(("ok",))[:50]

    def ang(no):
        v = [max(c["lines"][str(no)].get("ang") or [0]) for c in cyc50 if str(no) in c["lines"]]
        return views._med(v) if v else 0.0
    for i, ln in enumerate(ml[:-1]):
        if ln.term != "FINE" or i == 0:
            continue
        between = [x for x in prog.between(ln.no, ml[i + 1].no) if x.kind not in ("comment",)]
        if between:
            continue
        r = rows.get(ln.no, {})
        if not r.get("n") or ang(ln.no) < 2.0 or ang(ml[i + 1].no) < 2.0:
            continue
        nxt = rows.get(ml[i + 1].no, {})
        add("fine_cnt", ln.no, _("第 {n} 行：中間點不需停，FINE 改 CNT", n=ln.no),
            _("這一點後面直接接下一個動作，沒有夾取、I/O 或等待，卻完全停下再起動：每循環多一次減速到 0、再從 0 加速"
              "（本行與下一行合計損耗占比 {s:.0%}，停留 {w:.2f} 秒）。", s=r.get("score", 0) + nxt.get("score", 0), w=r.get("w", 0)),
            [{"line": ln.no, "before": ln.text, "after": lsparse.with_term(ln.text, "CNT50")}],
            _("每循環約省 {t:.2f} 秒；損耗下降幅度以改後診斷量測。", t=r.get("w", 0) + 0.1),
            r.get("score", 0) + nxt.get("score", 0),
            _("確認這一點不需要精準定位，且 CNT 轉角不會碰到周邊設備。"))
    # R2 用等待餘裕降速
    sl = slack.analyze(cfg, sess)
    for b in sl["blocks"]:
        if not b.get("sigma"):
            continue
        tot = max(b["effect"].values()) if b["effect"] else 0.0
        if tot < 0.01:
            continue
        add("slack", b["wait_line"], _("第 {a}–{b} 行：用等待餘裕降速，不影響節拍", a=b["lines"][0], b=b["lines"][-1]),
            _("機器人到了以後還要等 {sig}：最少等 {mn:.2f} 秒、第 10 百分位 {p10:.2f} 秒。扣掉安全裕度後可用 {s:.2f} 秒，"
              "動作時間 {T:.2f} 秒可以放慢為 {k:.2f} 倍。", sig=join(b["signal"], "、"), mn=b["w_min"], p10=b["w_p10"],
              s=b["slack"], T=b["T"], k=b["sigma"]),
            b.get("proposal"), _("估計每循環損耗：{e}", e=_fmt_axes(b["effect"], axp)), tot,
            _("速度與加速度一起調低，等待時間會變短但仍大於 0；請以改後診斷確認。"))
    # R3 關鍵路徑：節拍與壽命取捨
    for tr in sl["tradeoff"][:2]:
        st = next((s for s in tr["steps"] if s["dt"] == 0.1), None)
        if not st:
            continue
        add("tradeoff", tr["line"], _("第 {n} 行：在關鍵路徑上，每多 0.1 秒換多少壽命", n=tr["line"]),
            _("這一行後面沒有等待餘裕，放慢會增加循環時間。多 0.1 秒（循環 +{p:.1f}%）估計每循環損耗：{e}。",
              p=st["takt_pct"], e=_fmt_axes(st["effect"], axp) or "—"),
            None, _("完整取捨表見「等待餘裕」頁。"), max(st["effect"].values() or [0]) * 0.5)
    # R4 起步峰值高 → 軟啟動
    for r in S["lines"]:
        if not r.get("n"):
            continue
        for i, P in enumerate(axp):
            ratio = (r.get("ss_pct") or [None] * len(axp))[i]
            if ratio and ratio >= 0.8:
                add("soft_start", r["line"], _("第 {n} 行：{ax} 峰值接近起動停止容許扭矩", n=r["line"], ax=names[i]),
                    _("峰值為容許值的 {p:.0%}（超過 {c} 次）。", p=ratio, c=sum(r.get("px") or [0])),
                    None, _("在起點後加一個過渡點，前段用較低速度（軟啟動），或降低本行 ACC。"), ratio * 0.2,
                    _("過渡點由人教導並確認干涉。"))
                break
    # R5 手腕大幅翻轉（同一軸相鄰的行合併成一則）
    for i, P in enumerate(axp):
        if P["ax"] not in (4, 6):
            continue
        hit = []
        for no in [ln.no for ln in ml]:
            a = [c["lines"].get(str(no), {}).get("ang", [0] * len(axp))[i] for c in cyc50 if str(no) in c["lines"]]
            if a and views._med(a) >= 150:
                hit.append((no, views._med(a)))
        if hit:
            nos = join([str(n) for n, _a in hit], "、")
            degs = join([f"{a:.0f}°" for _n, a in hit], "、")
            sc = sum((rows.get(n, {}).get("share") or [0] * len(axp))[i] for n, _a in hit)
            add("wrist", hit[0][0], _("第 {n} 行：{ax} 大幅翻轉（{d}）", n=nos, ax=names[i], d=degs),
                _("手腕每循環轉超過 150° 再轉回來，占 {ax} 損耗 {s:.0%}，也增加轉數與換向。", ax=names[i], s=sc),
                None, _("確認是否需要翻轉：可改用不同轉數姿態達成相同姿勢，或調整工具座標方向。"), sc * 0.5,
                _("改點位要確認線材與管線的扭轉範圍。"))
    # R6 負載設定
    pay = {x.text for x in prog.lines if x.kind == "payload"}
    grip = [x for x in prog.lines if x.kind == "io"]
    if grip and len(pay) < 2:
        add("payload", None, _("確認負載設定：有工件與沒工件分開設定"),
            _("程式有夾爪 I/O，但只有 {n} 種 PAYLOAD 設定。負載設定不對，控制器的加減速與碰撞偵測都會偏離。", n=len(pay)),
            None, _("在夾取後、放下後分別切換 PAYLOAD（依實際工件重量與重心）。"), 0.05)
    # R7 L 動作改 J
    rank = sorted([r for r in S["lines"] if r.get("n")], key=lambda r: -r.get("score", 0))[:3]
    for r in rank:
        ln = prog.line(r["line"])
        if ln and ln.kind == "L" and r.get("score", 0) >= 0.08:
            add("l_to_j", ln.no, _("第 {n} 行：直線動作經過高扭矩姿態", n=ln.no),
                _("損耗排名第 {k}（占 {s:.0%}）。直線動作在關節空間不是最短路徑，可能經過較高扭矩的姿態。", k=r.get("rank"), s=r["score"]),
                [{"line": ln.no, "before": ln.text, "after": _("（若不需要直線路徑）改為 J 動作，速度另訂")}],
                _("改後量測"), r["score"] * 0.3, _("只有空中移動、不需要直線軌跡時才適用。"))
    # R8 角度集中、R9 微動
    for P in axp:
        A = views.angle_map(cfg, sess, P["ax"])
        if not A or not A["bins"]:
            continue
        c = A["conc"]
        rng = c["range"][1] - c["range"][0]
        if P["type"] in ("rv", "rack") and c["theta80"] > 0 and rng > 0 and c["theta80"] <= max(20.0, 0.3 * rng) and c["gini"] >= 0.4:
            add("concentration", None, _("{ax}：80% 損耗集中在 {lo:.0f}～{hi:.0f}°（寬 {w:.0f}°）", ax=P["name"], lo=c["lo"], hi=c["hi"], w=c["theta80"]),
                _("磨耗集中在同一段齒面與滾道。Gini {g:.2f}、前 5 個角度區間占 {t:.0%}。", g=c["gini"], t=c["top5"]),
                None, _("可交替使用不同角度區段；定期執行全範圍慢速運轉（DTM_SWEEP，同時讓潤滑脂重新分布，效果待驗證），並用局部磨耗指數追蹤。"),
                0.05)
        if A["fret_day"] >= 50:
            top = sorted(A["bins"], key=lambda r: -r["fret"])[:1]
            where = f"{top[0]['deg']:.0f}°" if top else ""
            add("fretting", None, _("{ax}：小幅擺動 {n:.0f} 次／天（{where} 附近）", ax=P["name"], n=A["fret_day"], where=where),
                _("擺動幅度小於接觸寬度（{c}°）時，滾動體與齒面在同一位置來回，潤滑脂不易補回：微動磨損風險。", c=P["contact"]),
                None, _("檢查是否能合併成一次動作、改變停留角度，或避免在停留時反覆微調。"), 0.04)
    # R10 衝擊與停止事件
    sm = impacts.summary(sess)
    ncyc = max(1, S["ncyc"])
    for b in sm["by_line"]:
        if b["kind"] == "impact" and b["n"] / ncyc * 100 >= 1:
            ln = prog.line(b["line"])
            add("impact", b["line"], _("第 {n} 行：常有衝擊（每 100 循環 {k:.1f} 次）", n=b["line"], k=b["n"] / ncyc * 100),
                _("加速規在這一行偵測到衝擊（峰值超過基準 RMS 的 {k} 倍且峰值因子高）。", k=cfg["pro"]["impact_k"]),
                [{"line": b["line"], "before": ln.text if ln else "", "after": _("降低接近速度，或在終點前加減速過渡點")}] if ln else None,
                _("檢查治具與工件干涉、夾爪對位。"), 0.06)
    stops = {k: v for k, v in sm["per_day"].items() if k in ("estop", "fence", "collision")}
    if sum(stops.values()) >= 1:
        add("stops", None, _("停止事件 {n:.1f} 次／天", n=sum(stops.values())),
            _("急停、安全柵開啟、碰撞偵測都會讓減速機承受突然的煞車扭矩；每次依權重計入損耗。"), None,
            _("檢查安全柵門的作業流程（先暫停再開門）、碰撞原因；可改用控制停止。"), 0.05)
    # R12 加速曲線
    ac = accel.analyze(cfg, sess)
    for r in ac["lines"]:
        if r.get("kappa") and r["impacts_100"] >= 1 and r["kappa"] < 5.0:
            add("profile", r["line"], _("第 {n} 行：加減速接近梯形且有衝擊", n=r["line"]),
                _("形狀係數 κ = {k:.2f}（梯形約 4.5、最小急動度約 5.77）。", k=r["kappa"]), None,
                _("降低急動度：降低 ACC 或在終點前加減速過渡點。"), 0.03)
    out.sort(key=lambda x: -x["score"])
    for i, s in enumerate(out):
        s["rank"] = i + 1
    return {"items": out, "safety": _(SAFETY), "lines": S["lines"]}

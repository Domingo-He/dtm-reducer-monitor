"""命令列：python -m dtm pro <指令>"""
from __future__ import annotations

import os
import sys
import time

from ..i18n import _
from .store import ROLES, Session


def add(sub):
    s = sub.add_parser("pro", help=_("程式層級診斷（Pro）：逐行損耗、角度集中、等待餘裕、修改建議、改善驗證"))
    ps = s.add_subparsers(dest="pro_cmd", required=True)
    x = ps.add_parser("parse", help=_("解析程式（.LS），列出每一行與主流程"))
    x.add_argument("file")
    x = ps.add_parser("instrument", help=_("產生加註版程式：每個動作行前把行號寫進暫存器（只加行號回報，不改動作）"))
    x.add_argument("file")
    x.add_argument("--out", default="")
    x = ps.add_parser("new", help=_("開一期診斷：指定機器人、程式與類別（改前、改後、對照組）"))
    x.add_argument("--robot", dest="pro_robot", default="")
    x.add_argument("--program", required=True, help=_("程式文字版 .LS"))
    x.add_argument("--role", choices=list(ROLES), default="diag")
    x.add_argument("--label", default="")
    x.add_argument("--group", default="")
    x.add_argument("--source", choices=["csv", "listen"], default="csv")
    x.add_argument("--change", default="", help=_("改後：改了什麼（寫進改善驗證報告）"))
    ps.add_parser("list", help=_("列出診斷期"))
    x = ps.add_parser("import", help=_("匯入診斷期取樣資料 CSV（掃描段落另存到掃描資料庫）"))
    x.add_argument("id")
    x.add_argument("file")
    x = ps.add_parser("alarms", help=_("匯入控制器警報履歷（ERRALL.LS 等文字檔或 CSV），計數急停與碰撞"))
    x.add_argument("id")
    x.add_argument("file")
    x = ps.add_parser("accel", help=_("匯入加速規資料 CSV（t,peak,rms 或 t,acc），偵測衝擊並對到行號"))
    x.add_argument("id")
    x.add_argument("file")
    x = ps.add_parser("close", help=_("結束診斷期"))
    x.add_argument("id")
    x = ps.add_parser("summary", help=_("列印診斷結果：各軸損耗、最傷的動作行、修改建議"))
    x.add_argument("id")
    x = ps.add_parser("report", help=_("產生診斷報告（HTML）"))
    x.add_argument("id")
    x.add_argument("--out", default="pro_report.html")
    x = ps.add_parser("compare", help=_("改善驗證：改前、改後（可加對照組），產生一頁報告"))
    x.add_argument("--before", required=True, help=_("改前診斷期（多期用逗號分隔）"))
    x.add_argument("--after", required=True)
    x.add_argument("--control-before", default="")
    x.add_argument("--control-after", default="")
    x.add_argument("--out", default="pro_compare.html")
    x = ps.add_parser("sweep", help=_("全範圍掃描：import 匯入、show 局部磨耗指數"))
    x.add_argument("action", choices=["import", "show"])
    x.add_argument("file", nargs="?")
    x.add_argument("--robot", dest="pro_robot", default="")
    x.add_argument("--axis", type=int, default=3)
    ps.add_parser("listen", help=_("接收控制器串流（KAREL 範本 robot/DTM_STREAM.KL），寫進進行中的診斷期"))
    x = ps.add_parser("sim", help=_("產生演示資料 CSV（只模擬內建演示程式）"))
    x.add_argument("--variant", choices=["before", "after"], default="before")
    x.add_argument("--days", type=float, default=1.0)
    x.add_argument("--cycles-per-day", type=int, default=40)
    x.add_argument("--hz", type=float, default=100.0)
    x.add_argument("--out", default="pro_sim.csv")
    x.add_argument("--program-out", default="")
    x = ps.add_parser("demo", help=_("Pro 演示：產生兩台機器人改前改後的演示資料並啟動畫面"))
    x.add_argument("--port", type=int, default=8091)
    x.add_argument("--rebuild", action="store_true")
    x.add_argument("--build-only", action="store_true")


def _p(msg=""):
    print(msg, flush=True)


def run(cfg: dict, a):
    from .. import runtime as R
    c = a.pro_cmd
    if c == "parse":
        from . import lsparse
        prog = lsparse.parse_file(a.file)
        eff = lsparse.effective_terms(prog)
        _p(_("程式 {n}（版本 {v}）：{k} 行，主流程上 {m} 個動作行", n=prog.name, v=prog.version, k=len(prog.lines),
             m=len(prog.motion_lines())))
        main = set(prog.main_path)
        for ln in prog.lines:
            mark = "*" if ln.no in main else " "
            extra = ""
            if ln.is_motion:
                extra = f"  [{ln.kind} {ln.point} {ln.speed or ln.speed_reg}{ln.speed_unit} {ln.term}{ln.cnt or ''}" \
                        f"{' ACC' + str(ln.acc) if ln.acc else ''} → {eff.get(ln.no, '')}]"
            _p(f"{mark}{ln.no:4d}: {ln.text}{extra}")
        for w in prog.warnings:
            _p("! " + w)
        return
    if c == "instrument":
        from . import lsparse
        with open(a.file, encoding="utf-8", errors="replace") as f:
            txt = lsparse.instrument(f.read(), int(cfg["pro"]["line_reg"]))
        out = a.out or os.path.splitext(a.file)[0] + "_DTM.LS"
        with open(out, "w", encoding="utf-8") as f:
            f.write(txt)
        _p(_("加註版程式：{p}（診斷結束後換回原程式）", p=out))
        return
    if c == "new":
        with open(a.program, encoding="utf-8", errors="replace") as f:
            txt = f.read()
        s = Session.create(cfg, a.pro_robot or R.robot_id(cfg), txt, role=a.role, label=a.label, group=a.group,
                           source=a.source, change=a.change)
        _p(s.id)
        return
    if c == "list":
        for m in Session.list(cfg):
            cnt = m.get("counts") or {}
            _p(f"{m['id']:45s} {m.get('robot', ''):8s} {m.get('role', ''):15s} {m['program']['name']:12s} "
               f"{m.get('status', ''):7s} ok={cnt.get('ok', 0)} {time.strftime('%Y-%m-%d', time.localtime(m.get('t0') or m['created']))}")
        return
    if c == "import":
        from .io import import_csv
        s = Session.open(cfg, a.id)
        res = import_csv(cfg, s, a.file, progress=lambda n: n % 500000 < 5000 and _p(_("已讀取 {n:,} 筆", n=n)))
        _p(_("完成：{n:,} 筆、循環 {c}、{t} 秒", n=res["samples"], c=res["cycles"], t=res["seconds"]))
        if res["sweeps"]:
            _p(_("全範圍掃描：{k} 軸", k=len(res["sweeps"])))
        return
    if c in ("alarms", "accel"):
        from . import impacts
        s = Session.open(cfg, a.id)
        with open(a.file, encoding="utf-8", errors="replace") as f:
            txt = f.read()
        res = impacts.import_alarms(cfg, s, txt) if c == "alarms" else impacts.import_accel(cfg, s, txt)
        _p(str(res))
        return
    if c == "close":
        s = Session.open(cfg, a.id)
        s.save_meta(status="closed", closed=time.time())
        return
    if c == "summary":
        from . import suggest, views
        s = Session.open(cfg, a.id)
        S = views.summary(cfg, s)
        _p(_("有效循環 {n}，循環時間中位數 {t:.2f} 秒", n=S["ncyc"], t=S["cycle"]["med"]))
        for ax in S["axes"]:
            ly = ax.get("life_years")
            _p(f"  {ax['name']}: D/cycle={ax['D_cycle']:.4g}  peak={ax['peak']:.3g}  " +
               (_("預估壽命 {y:.1f} 年", y=ly) if ly and ly < 100 else ""))
        for r in sorted([r for r in S["lines"] if r.get("n")], key=lambda r: -r.get("score", 0))[:8]:
            _p(f"  #{r.get('rank')} {r['line']:4d} {r['score'] * 100:5.1f}%  {r['text']}")
        for it in suggest.build(cfg, s)["items"][:8]:
            _p(f"- {it['title']}")
        for w in S["warnings"]:
            _p("! " + w)
        return
    if c == "report":
        from . import report
        s = Session.open(cfg, a.id)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(report.session_report(cfg, s))
        _p(a.out)
        return
    if c == "compare":
        from . import report
        ids = {"before": a.before.split(","), "after": a.after.split(","),
               "ctrl_before": [x for x in a.control_before.split(",") if x],
               "ctrl_after": [x for x in a.control_after.split(",") if x]}
        res, ss = report.compare_sessions(cfg, ids)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(report.compare_report(cfg, res, ss))
        for r in res["rows"]:
            _p(f"  {r['label']}: {r['effect'] * 100:+.1f}% [{r['lo'] * 100:+.1f}, {r['hi'] * 100:+.1f}]")
        _p(a.out)
        return
    if c == "sweep":
        from . import sweep
        robot = a.pro_robot or R.robot_id(cfg)
        if a.action == "import":
            from .io import csv_blocks
            cols: dict = {}
            for blk in csv_blocks(a.file):
                for k, v in blk.items():
                    cols.setdefault(k, []).extend(v)
            _p(str(sweep.import_cols(cfg, robot, cols)))
            return
        res = sweep.analyze(cfg, robot, a.axis)
        for r in res["runs"]:
            _p(f"  {r['ts']}  LWI={r['lwi']}  ×{r.get('ratio')}  level={r.get('level')}")
        for m in res["messages"]:
            _p("! " + m)
        return
    if c == "listen":
        from .io import Listener
        srv = Listener(cfg).serve()
        _p(_("Pro 串流接收中（Ctrl+C 結束）"))
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return
    if c == "sim":
        from . import sim
        chunks = []
        st = sim.run(a.variant, days=a.days, cycles_per_day=a.cycles_per_day, hz=a.hz, sink=chunks.append)
        sim.write_csv(a.out, sim.COLS, chunks)
        if a.program_out:
            with open(a.program_out, "w", encoding="utf-8") as f:
                f.write(sim.demo_program(a.variant))
        _p(_("演示資料：{p}（{n} 個循環）", p=a.out, n=st["cycles"]))
        return
    if c == "demo":
        from . import demo, web
        if not cfg["pro"].get("_demo_dir_set"):
            cfg["pro"]["data_dir"] = os.path.join(os.path.dirname(cfg["pro"]["data_dir"]) or "data", "pro_demo")
        cfg["_pro_demo"] = True
        _p(_("產生 Pro 演示資料（約 1 分鐘）…"))
        demo.build(cfg, rebuild=a.rebuild, progress=_p)
        if a.build_only:
            return
        srv = web.serve_demo(cfg, "127.0.0.1", a.port)
        _p(_("Pro 演示：http://localhost:{port}（按 Ctrl+C 結束）", port=a.port))
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return
    sys.exit(2)

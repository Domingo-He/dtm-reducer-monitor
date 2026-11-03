"""Pro 程式層級診斷測試：程式解析、行號對位、逐行損耗、角度集中、等待餘裕、建議、改善驗證、掃描、衝擊、網頁 API。"""
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
__import__("_cleanup")                  # 測試結束時刪掉暫存資料夾

from dtm import config as C  # noqa: E402
from dtm.pro import (analyze, compare, impacts, io as pio, lsparse, metrics, sim, slack, store, suggest,  # noqa: E402
                     sweep, views)

DAY = 86400.0

LS = """/PROG  TEST1
/ATTR
COMMENT = "x";
LINE_COUNT = 17;
/MN
   1:  UFRAME_NUM=1 ;
   2:  LBL[1:TOP] ;
   3:J P[1:HOME] 100% FINE ACC80   ;
   4:L P[2] 2000mm/sec CNT50 Tool_Offset,PR[3] ;
   5:  WAIT DI[101:PART]=ON TIMEOUT,LBL[9] ;
   6:C P[3] ;
    :  P[4] 500mm/sec CNT100 ;
   7:  WAIT  .30(sec) ;
   8:  DO[5]=ON ;
   9:  IF R[1]=1,JMP LBL[2] ;
  10:L PR[10:APPROACH] R[5]mm/sec FINE ;
  11:  //L P[5] 100mm/sec FINE ;
  12:  JMP LBL[1] ;
  13:  LBL[2] ;
  14:A P[6] 300mm/sec CNT R[7] ;
  15:  CALL SUB1 ;
  16:  END ;
  17:  LBL[9] ;
/POS
P[1:"HOME"]{
   GP1:
	UF : 1, UT : 2,		CONFIG : 'N U T, 0, 0, 0',
	X =   100.000  mm,	Y =  -200.000  mm,	Z =   300.000  mm,
	W =  -180.000 deg,	P =     0.000 deg,	R =    90.000 deg
};
P[2]{
   GP1:
	UF : 1, UT : 2,
	J1=    10.000 deg,	J2=   -20.000 deg,	J3=    30.000 deg,
	J4=     0.000 deg,	J5=   -90.000 deg,	J6=     0.000 deg,	E1=  1500.000  mm
   GP2:
	UF : 0, UT : 1,
	J1=    99.000 deg
};
/END
"""


def tmp_cfg():
    d = tempfile.mkdtemp()
    cfg = C.load(None)
    cfg["_path"] = os.path.join(d, "config.toml")
    cfg["pro"]["data_dir"] = os.path.join(d, "pro")
    cfg["robot"]["name"] = "R01"
    return cfg, d


def run_session(cfg, variant="before", role="before", cycles=40, days=1.0, seed=3, robot="R01", t_start=None,
                drift=1.0, estop_p=0.0):
    s = store.Session.create(cfg, robot, sim.demo_program(variant), role=role, source="sim",
                             t=t_start or 1.79e9)
    P = analyze.Processor(cfg, s)
    acc, stops = [], []
    sim.run(variant, days=days, cycles_per_day=int(cycles / days), hz=100, seed=seed, t_start=t_start or 1.79e9,
            drift=drift, estop_p=estop_p, sink=P.feed, acc_sink=acc.extend, alarm_sink=stops.extend)
    P.flush()
    return s, acc, stops


class TestParse(unittest.TestCase):
    def test_lines(self):
        p = lsparse.parse(LS)
        by = {ln.no: ln for ln in p.lines}
        self.assertEqual((by[3].kind, by[3].point, by[3].term, by[3].acc, by[3].speed, by[3].speed_unit),
                         ("J", "P[1]", "FINE", 80, 100.0, "%"))
        self.assertEqual((by[4].kind, by[4].cnt, by[4].offset), ("L", 50, "Tool_Offset,PR[3]"))
        self.assertEqual((by[5].kind, by[5].wait_cond, by[5].jmp), ("wait", "DI[101:PART]=ON", 9))
        self.assertEqual((by[6].kind, by[6].via, by[6].point, by[6].cnt), ("C", "P[3]", "P[4]", 100))
        self.assertEqual(by[7].wait_s, 0.3)
        self.assertEqual((by[10].point_reg, by[10].speed_reg), (True, "R[5]"))
        self.assertEqual(by[11].kind, "comment")
        self.assertEqual((by[14].term, by[14].cnt), ("CNT", None))
        self.assertEqual(by[15].call, "SUB1")
        self.assertEqual(p.main_path, list(range(1, 13)))   # 跟到 JMP LBL[1] 回到已走過的行為止
        self.assertEqual(p.positions[1].cart["Z"], 300.0)
        self.assertEqual(p.positions[2].joint["E1"], 1500.0)
        self.assertNotIn("J1", p.positions[2].cart)
        self.assertEqual(p.positions[2].joint["J1"], 10.0)  # 第二群組不覆蓋
        self.assertEqual(len(p.version), 12)

    def test_effective_terms_and_rewrite(self):
        p = lsparse.parse(sim.demo_program())
        eff = lsparse.effective_terms(p)
        self.assertEqual(eff[6], "CNT")
        self.assertEqual(eff[7], "FINE")
        self.assertEqual(eff[26], "FINE")
        self.assertEqual(lsparse.with_term("J P[1] 100% FINE", "CNT50"), "J P[1] 100% CNT50")
        self.assertEqual(lsparse.with_acc("L P[2] 2000mm/sec CNT50", 60), "L P[2] 2000mm/sec CNT50 ACC60")
        self.assertEqual(lsparse.with_acc("L P[2] 100mm/sec FINE ACC80", 40), "L P[2] 100mm/sec FINE ACC40")
        self.assertEqual(lsparse.with_speed("L P[2] 2000mm/sec CNT50", 1407), "L P[2] 1410mm/sec CNT50")
        self.assertEqual(lsparse.with_speed("J P[7] 100% CNT50", 70.4), "J P[7] 70% CNT50")

    def test_instrument_keeps_motion(self):
        src = sim.demo_program()
        out = lsparse.instrument(src, 114)
        a, b = lsparse.parse(src), lsparse.parse(out)
        self.assertEqual([ln.text for ln in a.motion_lines()], [ln.text for ln in b.motion_lines()])
        regs = [ln for ln in b.lines if ln.kind == "reg" and ln.reg_no == 114]
        self.assertEqual([int(r.reg_value) for r in regs], [ln.no for ln in a.motion_lines()])
        self.assertIn("LINE_COUNT\t= %d;" % len(b.lines), out)
        # 加註版的每個 R[114] 都緊接在對應的動作行之前
        for r in regs:
            nxt = b.lines[b.lines.index(r) + 1]
            self.assertTrue(nxt.is_motion)


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg, cls.dir = tmp_cfg()
        cls.s, cls.acc, _ = run_session(cls.cfg, cycles=40)

    @classmethod
    def tearDownClass(cls):
        cls.s.close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_alignment_beats_reported_line(self):
        ev = self.s.get("eval")
        est, raw = ev["est"] / ev["n"], ev["raw"] / ev["n"]
        self.assertGreater(est, 0.95)
        self.assertLess(raw, 0.8)
        cnt = self.s.cycle_counts()
        self.assertEqual(cnt.get("ok", 0) + cnt.get("anomaly", 0), 40)
        self.assertEqual(self.s.meta["axes"], [1, 2, 3, 4, 5, 6])

    def test_damage_ranking(self):
        S = views.summary(self.cfg, self.s)
        top = sorted([r for r in S["lines"] if r.get("n")], key=lambda r: -r["score"])
        self.assertIn(top[0]["line"], (13, 14))          # 夾著工件的 J1 大擺動最傷
        self.assertAlmostEqual(sum(r.get("score", 0) for r in S["lines"]), 1.0, places=6)
        j1 = next(a for a in S["axes"] if a["ax"] == 1)
        self.assertTrue(5 < j1["life_years"] < 60)
        j4 = next(a for a in S["axes"] if a["ax"] == 4)
        self.assertFalse(j4["moving"])
        self.assertIsNone(j4.get("life_years"))
        r7 = next(r for r in S["lines"] if r["line"] == 7)
        self.assertGreater(r7["w"], 2.0)                 # 等工件的時間算在第 7 行的停留

    def test_damage_formula(self):
        P = metrics.axis_params(self.cfg, 2, "pct")
        cols = {"t": [i / 100 for i in range(101)], "q2": [i * 0.36 for i in range(101)], "tq2": [100.0] * 101}
        lines, dmg, peak, ang, osc = metrics.cycle_metrics(cols, [(0, 5, 0, 101, 101)], [P], 100, {}, [False] * 101, 0.0)
        self.assertAlmostEqual(dmg["2"], 0.1, places=6)        # 36° 在額定扭矩 = 0.1 轉
        self.assertAlmostEqual(lines["5"]["tm"][0], 1.0, places=6)
        self.assertAlmostEqual(sum(v[0] for v in ang[2].values()), 0.1, places=6)

    def test_angle_and_fretting(self):
        A = views.angle_map(self.cfg, self.s, 3)
        self.assertTrue(10 <= A["conc"]["theta80"] <= 30)
        self.assertGreater(A["fret"], 0)                       # 壓入兩次的小幅擺動
        small = sum(o["n"] for o in A["osc"] if o["hi"] is not None and o["hi"] <= 0.5)
        self.assertEqual(small, A["fret"])

    def test_timeline(self):
        T = views.timeline(self.cfg, self.s, 1)
        self.assertEqual(len(T["x"]), len(T["med"]))
        self.assertEqual([b["line"] for b in T["blocks"]], [ln.no for ln in self.s.program().motion_lines()])
        self.assertGreater(T["ncyc"], 20)

    def test_slack_and_suggest(self):
        R = slack.analyze(self.cfg, self.s)
        blocks = {b["wait_line"]: b for b in R["blocks"]}
        self.assertEqual(blocks[7]["lines"], [5, 6, 7])
        self.assertEqual(blocks[26]["lines"], [23, 24, 25, 26])
        self.assertGreater(blocks[26]["slack"], 0.5)
        self.assertGreater(blocks[26]["effect"]["6"], 0.3)     # 回程降速：J6 損耗明顯下降
        prop = {p["line"]: p["after"] for p in blocks[26]["proposal"]}
        self.assertIn("ACC", prop[24])
        G = suggest.build(self.cfg, self.s)
        rules = [it["rule"] for it in G["items"]]
        self.assertEqual(G["items"][0]["rule"], "fine_cnt")
        self.assertEqual(G["items"][0]["line"], 13)
        for r in ("slack", "wrist", "tradeoff", "fretting"):
            self.assertIn(r, rules)
        self.assertNotIn(16, [it["line"] for it in G["items"] if it["rule"] == "fine_cnt"])   # 微小動作不建議

    def test_impacts(self):
        res = impacts.import_accel(self.cfg, self.s, self.acc)
        self.assertGreater(res["impacts"], 0)
        lines = {e["line"] for e in self.s.impacts() if e["kind"] == "impact"}
        self.assertEqual(lines, {15})
        t = self.s.cycles()[3]["t0"] + 2
        txt = "   1 " + __import__("time").strftime("%d-%b-%y %H:%M:%S", __import__("time").localtime(t)).upper() + \
              "  SRVO-001 Operator panel E-stop\n   2 01-JAN-20 00:00:00 SRVO-050 Collision Detection Alarm\n"
        r = impacts.import_alarms(self.cfg, self.s, txt)
        self.assertEqual(r, {"parsed": 2, "in_period": 1})
        e = [x for x in self.s.impacts() if x["kind"] == "estop"][0]
        self.assertEqual(e["weight"], self.cfg["pro"]["stop_weight"]["estop"])
        self.assertIsNotNone(e["line"])


class TestDetect(unittest.TestCase):
    def test_impact_detection(self):
        rng = random.Random(1)
        rows = [(i * 0.05, 0.12 + rng.random() * 0.02, 0.04) for i in range(400)]
        rows[300] = (15.0, 2.0, 0.3)
        ev = impacts.detect(rows, 4.0, 5.0)
        self.assertEqual([round(e["t"], 2) for e in ev], [15.0])

    def test_mann_whitney(self):
        self.assertAlmostEqual(compare.mann_whitney([1, 2, 3, 4], [5, 6, 7, 8]), 2 / 70, places=6)
        self.assertGreater(compare.mann_whitney([1, 3, 5, 7], [2, 4, 6, 8]), 0.5)

    def test_concentration(self):
        c = views.concentration([(0, 0.0), (1, 1.0), (2, 8.0), (3, 1.0), (4, 0.0)], 1.0)
        self.assertEqual(c["theta80"], 1.0)
        self.assertEqual((c["lo"], c["hi"]), (2.0, 3.0))


class TestCompareAndSweep(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg, cls.dir = tmp_cfg()
        t0 = 1.79e9
        cls.b, _, _ = run_session(cls.cfg, "before", "before", cycles=36, days=3, seed=11, t_start=t0)
        cls.cb, _, _ = run_session(cls.cfg, "before", "control_before", cycles=36, days=3, seed=21, robot="R02", t_start=t0)
        cls.a, _, _ = run_session(cls.cfg, "after", "after", cycles=36, days=3, seed=12, t_start=t0 + 10 * DAY, drift=1.04)
        cls.ca, _, _ = run_session(cls.cfg, "before", "control_after", cycles=36, days=3, seed=22, robot="R02",
                                   t_start=t0 + 10 * DAY, drift=1.04)

    @classmethod
    def tearDownClass(cls):
        for s in (cls.b, cls.cb, cls.a, cls.ca):
            s.close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_compare(self):
        res = compare.compare(self.cfg, [self.b], [self.a], [self.cb], [self.ca])
        self.assertEqual(res["unit"], "day")
        rows = {r["key"]: r for r in res["rows"]}
        self.assertLess(rows["dmg1"]["effect"], -0.2)
        self.assertLess(rows["dmg6"]["effect"], -0.5)
        self.assertTrue(rows["dmg6"]["significant"])
        self.assertLess(abs(rows["dur"]["effect"]), 0.01)       # 節拍不變
        self.assertNotIn("dmg4", rows)                          # 沒動的軸不比
        d = res["diff"]
        self.assertEqual({tuple(x["after"]) for x in d["lines"]} >= {("13: J P[4:MID] 100% CNT80",)}, True)
        self.assertEqual(d["positions"][0]["point"], "P[7]")
        from dtm.pro import report
        html = report.compare_report(self.cfg, res, {"before": [self.b], "after": [self.a]})
        self.assertIn("P[7]", html)

    def test_sweep_lwi(self):
        A = views.angle_map(self.cfg, self.b, 3)
        dens = {r["deg"]: r["d"] for r in A["bins"]}
        mx = max(dens.values())
        rng = random.Random(2)
        for wk in range(4):
            amp = 0.2 + 1.0 * wk

            def wear(ax, th, amp=amp):
                return amp * dens.get(float(int(th // 1)), 0.0) / mx if ax == 3 else 0.0
            d = sim.sweep(3, 1.79e9 + wk * 7 * DAY, 100, rng, wear=wear)
            sweep.import_cols(self.cfg, "R01", d)
        res = sweep.analyze(self.cfg, "R01", 3)
        r = [x["ratio"] for x in res["runs"]]
        self.assertEqual(len(r), 4)
        self.assertEqual(r[0], 1.0)
        self.assertGreater(r[-1], 1.25)
        self.assertEqual(res["runs"][-1]["level"] >= 1, True)


class TestNoCodes(unittest.TestCase):
    def test_position_only(self):
        """沒有行號回報、教導點是關節值：只用位置對位。"""
        cfg, d = tmp_cfg()
        try:
            s = store.Session.create(cfg, "R01", sim.demo_program(), role="diag", source="csv", t=1.79e9)
            P = analyze.Processor(cfg, s)

            def sink(c):
                c = dict(c)
                c.pop("line")
                P.feed(c)
            sim.run("before", days=1, cycles_per_day=15, hz=100, seed=5, t_start=1.79e9, sink=sink)
            P.flush()
            self.assertEqual((s.get("align") or {}).get("mode"), "pos")
            ev = s.get("eval")
            self.assertGreater(ev["est"] / ev["n"], 0.85)        # 壓入的 ±0.2° 微小動作只靠位置分不清
            s.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestSessionText(unittest.TestCase):
    def test_generated_text_follows_language_user_text_does_not(self):
        """程式產生的說明（演示）依觀看語言顯示；使用者改寫後就照原樣。"""
        from dtm.i18n import _, use
        from dtm.pro.web import session_list
        cfg, d = tmp_cfg()
        cfg["_pro_demo"] = True
        try:
            s = store.Session.create(cfg, "R01", sim.demo_program(), role="after", source="sim", t=1.79e9,
                                     change=_("13 行 FINE 改 CNT80；6、24、25 行用等待餘裕降速；P[7] 不再翻轉手腕"))
            with use("en"):
                en = s.text("change")
                self.assertNotRegex(en, "[\u3400-\u9fff]")
                self.assertEqual(session_list(cfg)[0]["change"], en)
            with use("zh-TW"):
                self.assertIn("不再翻轉手腕", s.text("change"))
            s.save_meta(change="自己寫的說明")
            with use("en"):
                self.assertEqual(s.text("change"), "自己寫的說明")
            s.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestCsvAndWeb(unittest.TestCase):
    def test_csv_import_and_api(self):
        cfg, d = tmp_cfg()
        try:
            chunks = []
            sim.run("before", days=0.1, cycles_per_day=120, hz=50, seed=7, sink=chunks.append)
            sw = sim.sweep(2, chunks[-1]["t"][-1] + 30, 50, random.Random(1))
            path = os.path.join(d, "x.csv")
            sim.write_csv(path, sim.COLS, chunks + [sw])
            s = store.Session.create(cfg, "R01", sim.demo_program(), role="diag", source="csv")
            res = pio.import_csv(cfg, s, path)
            self.assertEqual(res["cycles"].get("ok"), 12)
            self.assertEqual([x["ax"] for x in res["sweeps"]], [2])
            from dtm.pro import web as PW
            cfg["_pro_demo"] = True
            srv = PW.serve_demo(cfg, "127.0.0.1", 0)
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            try:
                get = lambda p: json.loads(urllib.request.urlopen(base + p).read())  # noqa: E731
                L = get("/api/pro/sessions")
                self.assertEqual(L["sessions"][0]["id"], s.id)
                for p in ("summary", "timeline", "angle", "slack", "suggest", "accel", "impacts", "program"):
                    get(f"/api/pro/{p}?id={s.id}")
                html = urllib.request.urlopen(f"{base}/api/pro/report?id={s.id}").read().decode()
                self.assertIn("DTM_PRO_DEMO", html)
                inst = urllib.request.urlopen(f"{base}/api/pro/instrument?id={s.id}").read().decode()
                self.assertIn("R[114:DTM_LINE]=13", inst)
                self.assertEqual(get("/api/pro/sweeps")["items"][0]["ax"], 2)
                page = urllib.request.urlopen(base + "/").read().decode()
                self.assertIn("程式層級診斷", page)
                req = urllib.request.Request(base + "/api/pro/session", data=b"{}", method="POST")
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(req)
                self.assertEqual(e.exception.code, 403)            # 演示模式不能修改
            finally:
                srv.shutdown()
                srv.server_close()
                s.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_listener(self):
        """控制器串流：KAREL 送的每行可能只用 \\r 結尾；欄位數決定軸數；第一筆對齊電腦時間。"""
        import socket
        import time
        cfg, d = tmp_cfg()
        try:
            cfg["source"]["fanuc_http"]["host"] = "127.0.0.1"
            s = store.Session.create(cfg, "R01", sim.demo_program(), role="diag", source="listen")
            chunks = []
            sim.run("before", days=0.01, cycles_per_day=600, hz=50, seed=9, sink=chunks.append)
            L = pio.Listener(cfg)
            srv = L.serve("127.0.0.1", 0)
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            try:
                c = socket.create_connection(("127.0.0.1", srv.server_address[1]))
                t0 = chunks[0]["t"][0]
                buf = []
                for ch in chunks:
                    for i in range(len(ch["t"])):
                        row = [int((ch["t"][i] - t0) * 1000), ch["line"][i]] + [ch[f"q{a}"][i] for a in range(1, 7)] + \
                              [ch[f"tq{a}"][i] for a in range(1, 7)]
                        buf.append(",".join(str(x) for x in row))
                c.sendall(("\r".join(buf) + "\r").encode())
                c.close()
                for _ in range(100):
                    time.sleep(0.1)
                    P = L.proc.get("R01")
                    if P and P.n_samples >= len(buf):
                        break
                P.flush()
                s2 = store.Session.open(cfg, s.id)
                self.assertGreaterEqual(s2.cycle_counts().get("ok", 0), 4)
                self.assertEqual(s2.meta["axes"], [1, 2, 3, 4, 5, 6])
                self.assertLess(abs(s2.meta["t0"] - time.time()), 600)
                s2.close()
                P.s.close()
            finally:
                srv.shutdown()
                srv.server_close()
            s.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_sim_refuses_user_program(self):
        self.assertEqual(len(sim.builtin_versions()), 2)
        with self.assertRaises(ValueError):
            sim.run("other", days=0.01, cycles_per_day=10, hz=50)


if __name__ == "__main__":
    unittest.main()

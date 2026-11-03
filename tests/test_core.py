import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
__import__("_cleanup")                  # 測試結束時刪掉暫存資料夾

from dtm import config as C  # noqa: E402
from dtm.alarms import AlarmManager, RealtimeMonitor  # noqa: E402
from dtm.convert import Converter, calibrate_k  # noqa: E402
from dtm.model import Sample, SegStat, decode_seg, encode_seg  # noqa: E402
from dtm.segment import Segmenter, features_for_axis  # noqa: E402
from dtm.service import Service, simulate_history  # noqa: E402
from dtm.sources import SIM_AXES, SimRobot, decode_words, parse_numreg, sample_from_regs  # noqa: E402
from dtm.store import Store  # noqa: E402


def cfg_sim(db):
    cfg = C.load(None)
    cfg["storage"]["db"] = db
    cfg["axis"] = {"2": {"k_nm_per_raw": 2.0, "rated_nm": 1000}, "3": {"k_nm_per_raw": 1.5, "rated_nm": 1000}}
    return cfg


class TestBasics(unittest.TestCase):
    def test_seg_code(self):
        self.assertEqual(decode_seg(2051), (2, 5, 1))
        self.assertEqual(decode_seg(3202), (3, 20, 2))
        self.assertIsNone(decode_seg(0))
        self.assertIsNone(decode_seg(2053))
        self.assertEqual(encode_seg(3, 10, 2), 3102)

    def test_parse_numreg(self):
        text = "[*NUMREG*]$NUMREG  Storage: CMOS  Access: RW  : ARRAY[200] OF Numeric Reg\n" \
               "  [101] = 12.5  'DT_J1'\n  [102] = -340  'DT_J2'\n  [110] = 2051  ''\n" \
               "  [111] = 1  ''\n  [112] = 77  ''\n  [113] = 1.000000e+02  ''\n"
        regs = parse_numreg(text)
        self.assertEqual(regs[102], -340.0)
        self.assertEqual(regs[113], 100.0)
        s = sample_from_regs(regs, C.load(None), 1.0)
        self.assertEqual(s.dt[2], -340.0)
        self.assertEqual((s.seg, s.run, s.hb, s.ovr), (2051, 1, 77.0, 100.0))

    def test_decode_words(self):
        self.assertEqual(decode_words([0xFFFF], "int16"), -1.0)
        self.assertAlmostEqual(decode_words([0x4148, 0x0000], "float32"), 12.5)
        self.assertAlmostEqual(decode_words([0x0000, 0x4148], "float32", "little"), 12.5)

    def test_calibrate(self):
        r = calibrate_k(10.0, 30.0, 5.0, 1.2)
        self.assertAlmostEqual(r["k"], 5 * 9.80665 * 1.2 / 20)


class TestFeatures(unittest.TestCase):
    def test_recover_friction_and_gravity(self):
        Fc, Fv, G = 50.0, 2.0, 7.0
        segs = []
        for pct in (5, 10, 20):
            F = Fc + Fv * pct
            for d, sgn in ((1, 1), (2, -1)):
                segs.append(SegStat(code=encode_seg(2, pct, d), axis=2, pct=pct, direction=d, t0=0, t1=5, n=20,
                                    mean=sgn * F + G, std=1.0, peak=abs(sgn * F + G), temp=25, valid=True))
        f = features_for_axis(2, segs, 10, 1, 0.0)
        self.assertAlmostEqual(f.Fc, Fc)
        self.assertAlmostEqual(f.Fv, Fv)
        self.assertAlmostEqual(f.F, Fc + Fv * 10)
        self.assertAlmostEqual(f.G, G)

    def test_segmenter_on_sim(self):
        cfg = C.load(None)
        rob = SimRobot([2, 3], seed=1)
        seg = Segmenter(cfg)
        feats = None
        for s in rob.diag_run(0.0, 0, 5.0, const_s=6.0, ramp_s=0.6):
            out = seg.feed(s)
            if out:
                feats = out
        self.assertIsNotNone(feats)
        for f in feats:
            self.assertTrue(f.valid, f.detail)
            temp = f.temp
            true_F = rob.friction(f.axis, 10, 0, temp) / SIM_AXES[f.axis]["k_true"]
            true_G = SIM_AXES[f.axis]["G"] / SIM_AXES[f.axis]["k_true"]
            self.assertAlmostEqual(f.F, true_F, delta=0.06 * true_F)
            self.assertAlmostEqual(f.G, true_G, delta=2.0)


class TestSystem(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.sqlite3")

    def test_history_detects_j3_and_grease_reset(self):
        cfg = cfg_sim(self.db)
        svc = simulate_history(cfg, days=180)
        h3, h2 = svc.health[3], svc.health[2]
        self.assertEqual(h3["level"], 2, h3["reasons"])
        self.assertTrue(h3["baseline_ready"])
        self.assertIn("磨耗", h3["hint"] + "".join(h3["reasons"]) + "減速機磨耗")
        self.assertEqual(h2["level"], 0, h2["reasons"])
        alarms = svc.store.alarms(500)
        j2_health = [a for a in alarms if a["axis"] == 2 and a["kind"] == "健康"]
        self.assertTrue(j2_health, "J2 換脂前應出現油脂劣化注意")
        self.assertTrue(all(a["cleared_ts"] for a in j2_health), "換脂後 J2 健康警報應解除")
        j3 = [a for a in alarms if a["axis"] == 3 and a["kind"] == "健康"]
        first = min(a["ts"] for a in j3)
        days_before_end = (max(r["ts"] for r in svc.store.features(3)) - first) / 86400
        self.assertGreater(days_before_end, 15, "J3 應在達警戒前數週先發注意")

    def test_epochs_and_report(self):
        from dtm.analysis import epochs
        from dtm.config import RESET_KINDS
        from dtm.report import build_report, export_features
        from dtm.web import health, status, trend
        cfg = cfg_sim(self.db)
        svc = simulate_history(cfg, days=150)
        rows = svc.store.features(2)
        resets = [e["ts"] for e in svc.store.events(2) if e["kind"] in RESET_KINDS]
        eps = epochs(rows, resets, cfg)
        self.assertEqual(len(eps), 2)
        self.assertTrue(all(ep["base"] for ep in eps))
        self.assertEqual(sum(len(ep["rows"]) for ep in eps), len(rows))
        tr = trend(svc, 2, "Fv", 365)
        self.assertEqual(len(tr["points"]), len(rows))
        self.assertEqual(len(tr["lines"]), 6)
        html = build_report(svc, 365)
        self.assertIn("<svg", html)
        self.assertIn("換脂", html)
        out = os.path.join(self.tmp, "f.csv")
        export_features(svc, out)
        with open(out, encoding="utf-8-sig") as f:
            self.assertEqual(sum(1 for _ in f), 1 + 2 * 150)
        self.assertEqual(len(health(svc)), 2)
        self.assertEqual(len(status(svc)["axes"]), 2)

    def test_realtime_alarm_debounce(self):
        cfg = cfg_sim(self.db)
        store = Store(self.db)
        conv = Converter(cfg, store)
        am = AlarmManager(store)
        rt = RealtimeMonitor(cfg, conv, am, [2, 3])
        t = 1000.0
        for i in range(10):    # 1.8 s 超過 80%：未滿 2 s 不觸發
            rt.update(Sample(ts=t + i * 0.2, dt={2: 450.0, 3: 10.0}, hb=i))
        self.assertFalse([a for a in store.active_alarms() if a["akey"] == "rt:2"])
        for i in range(10, 16):
            rt.update(Sample(ts=t + i * 0.2, dt={2: 450.0, 3: 10.0}, hb=i))
        act = [a for a in store.active_alarms() if a["akey"] == "rt:2"]
        self.assertEqual(len(act), 1)
        self.assertEqual(act[0]["level"], 1)
        self.assertIn("即時扭矩", act[0]["message"])                   # 訊息在發警報時才組，內容要正確
        self.assertIn("Nm", act[0]["message"])
        for i in range(16, 40):
            rt.update(Sample(ts=t + i * 0.2, dt={2: 20.0, 3: 10.0}, hb=i))
        self.assertFalse([a for a in store.active_alarms() if a["akey"] == "rt:2"])

    def test_stale_heartbeat(self):
        cfg = cfg_sim(self.db)
        store = Store(self.db)
        am = AlarmManager(store)
        rt = RealtimeMonitor(cfg, Converter(cfg, store), am, [2])
        for i in range(40):
            rt.update(Sample(ts=2000.0 + i * 0.2, dt={2: 1.0}, hb=5))
        self.assertTrue([a for a in store.active_alarms() if a["akey"] == "hb"])

    def test_service_flush_and_agg(self):
        cfg = cfg_sim(self.db)
        svc = Service(cfg, notify=False)
        rob = SimRobot([2, 3])
        for s in rob.diag_run(time.time(), 0, 5.0):
            svc.handle(s)
        svc.flush(force=True)
        self.assertGreater(len(svc.store.samples(0, time.time() + 1e6)), 100)
        self.assertTrue(svc.store.agg(2, 0))
        self.assertEqual(len(svc.store.features(2)), 1)


class TestCross(unittest.TestCase):
    """交叉分析：模擬情境應得到正確的維護建議。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cfg = C.load(os.path.join(root, "config.sim.toml"))
        cfg["storage"]["db"] = os.path.join(cls.tmp, "x.sqlite3")
        cls.svc = simulate_history(cfg, days=180)
        cls.t0 = min(r["ts"] for r in cls.svc.store.features(2))

    def test_j2_grease_then_normal(self):
        d = self.svc.decisions[2]
        self.assertEqual((d["grease"]["level"], d["drive"]["level"], d["motor"]["level"]), (0, 0, 0))
        al = [a for a in self.svc.store.alarms(500) if a["akey"] == "dec:2:grease"]
        self.assertTrue(al, "換脂前應建議換脂")
        first = min(a["ts"] for a in al)
        self.assertLess((first - self.t0) / 86400, 120)
        self.assertTrue(all(a["cleared_ts"] for a in al))

    def test_j3_reducer_replace(self):
        d = self.svc.decisions[3]
        self.assertEqual(d["drive"]["level"], 2)
        self.assertEqual(d["primary"], "減速機磨耗")
        self.assertEqual(d["motor"]["level"], 0)
        al = [a for a in self.svc.store.alarms(500) if a["akey"] == "dec:3:drive"]
        prep = min(a["ts"] for a in al if a["level"] == 1)
        repl = min(a["ts"] for a in al if a["level"] == 2)
        self.assertGreater((repl - prep) / 86400, 7, "應先「準備備品」再「安排更換」")

    def test_j4_motor_not_gearbox(self):
        d = self.svc.decisions[4]
        self.assertEqual(d["primary"], "伺服馬達異常")
        self.assertGreaterEqual(d["motor"]["level"], 1)
        self.assertEqual(d["drive"]["level"], 0, d["families"])
        self.assertEqual(d["drive_label"], "齒輪箱")

    def test_weibull_fit(self):
        import random
        from dtm.life import b_life, cond_fail_prob, weibull_fit
        rng = random.Random(3)
        lives, failed = [], []
        for _ in range(400):
            t = 40000 * (-math.log(rng.random())) ** (1 / 2.5)
            c = rng.uniform(10000, 80000)
            lives.append(min(t, c))
            failed.append(t <= c)
        fit = weibull_fit(lives, failed)
        self.assertAlmostEqual(fit["beta"], 2.5, delta=0.35)
        self.assertAlmostEqual(fit["eta_h"], 40000, delta=4000)
        self.assertAlmostEqual(b_life(2.5, 40000), 40000 * (-math.log(0.9)) ** 0.4)
        self.assertAlmostEqual(cond_fail_prob(0, 40000, 2.5, 40000), 1 - math.exp(-1))

    def test_iron_rate(self):
        from dtm.cross import iron_analysis
        from dtm.life import Hours
        cfg = C.load(None)
        cfg["axis"] = {"3": {"grease_mass_g": 2000}}
        cfg["life"]["hours_per_day"] = 24
        st = Store(os.path.join(self.tmp, "iron.sqlite3"))
        t0 = 1_700_000_000.0
        st.install(3, "reducer", t0 - 400 * 86400)
        st.install(3, "grease", t0)
        st.add_iron(t0 + 1 * 86400, 3, 0.010, "after")
        st.add_iron(t0 + 101 * 86400, 3, 0.034, "interim")    # 2400 h：(0.034−0.010)% × 2000 g = 0.48 g → 0.2 g/1000h
        ia = iron_analysis(st, cfg, Hours(st, cfg), 3, t0 + 102 * 86400, None)
        self.assertAlmostEqual(ia["W"], 0.48 / 2.4, delta=0.01)
        self.assertEqual(ia["residual_wt"], 0.010)
        self.assertAlmostEqual(ia["sev_conc"], 0.5 * (0.034 - 0.025) / 0.025, delta=1e-6)


if __name__ == "__main__":
    unittest.main()

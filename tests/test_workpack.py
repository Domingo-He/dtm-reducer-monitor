import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
__import__("_cleanup")                  # 測試結束時刪掉暫存資料夾

from dtm import config as C  # noqa: E402
from dtm import workpack as wp  # noqa: E402
from dtm.notify import Notifier  # noqa: E402
from dtm.store import Store  # noqa: E402


def make_cfg():
    cfg = C.load(os.path.join(ROOT, "config.sim.toml"))
    cfg["storage"]["db"] = os.path.join(tempfile.mkdtemp(), "w.sqlite3")
    return cfg


class TestWorkpack(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_catalog_resolves_relative_to_config(self):
        cat = wp.load_catalog(self.cfg)
        self.assertEqual(cat["error"], "")
        self.assertTrue(cat["path"].endswith("workpacks.r2000ic_165f.toml"))
        self.assertGreaterEqual(len(cat["packs"]), 4)

    def test_j3_reducer_pack(self):
        w = wp.get(self.cfg, 3, wp.comp_for(self.cfg, 3, "drive"))
        self.assertEqual(w["title"], "J3 減速機更換")
        pns = [p["pn"] for p in w["parts"]]
        self.assertIn("A97L-0218-0989#218", pns)
        self.assertIn("A98L-0040-0174#16KG", pns)
        self.assertEqual(w["crew"], 3)
        self.assertAlmostEqual(w["step_hours"], sum(s["hours"] for s in w["steps"]))
        self.assertGreater(w["man_hours"], w["step_hours"])
        # 庫存 0 → 缺料；公開料號 → 待確認
        self.assertTrue(any(p["pn"] == "A97L-0218-0989#218" for p in w["shortages"]))
        self.assertTrue(all(not p.get("verified") for p in w["unverified"]))
        # 給脂量依設定檔自動加註
        g = next(p for p in w["parts"] if p.get("grease"))
        self.assertIn("1,800 g", g["grease_note"])
        self.assertIn("2,700 g", g["grease_note"])
        # 工具組展開
        self.assertTrue(any(t["name"] == "M10 環首螺栓" for t in w["tools"]))

    def test_gearbox_axis_without_pack(self):
        self.assertIsNone(wp.get(self.cfg, 4, wp.comp_for(self.cfg, 4, "drive")))
        self.assertIn("尚未建立作業包", wp.notify_text(self.cfg, "dec:4:motor", 2))

    def test_notify_text_only_for_decisions(self):
        t = wp.notify_text(self.cfg, "dec:3:drive", 1)
        self.assertIn("A97L-0218-0989#218", t)
        self.assertIn("人力 3 人", t)
        self.assertIn("缺料", t)
        self.assertEqual(wp.notify_text(self.cfg, "health:3", 2), "")
        self.assertEqual(wp.notify_text(self.cfg, "dec:3:drive", 0), "")

    def test_line_message_has_pack_but_sms_does_not(self):
        n = self.cfg["notify"]
        n["public_url"] = "http://10.1.2.3:8080"
        nt = Notifier(self.cfg, Store(self.cfg["storage"]["db"]))
        msg = nt.compose(2, "J3 減速機：安排更換", 3, "維護決策", time.time(), wp.notify_text(self.cfg, "dec:3:drive", 2))
        self.assertIn("A97L-0218-0989#218", msg["text"])
        self.assertIn("/workorder?axis=3&comp=reducer", msg["text"])
        self.assertNotIn("A97L", msg["short"])
        self.assertLessEqual(len(msg["short"]), 70)

    def test_workorder_html(self):
        w = wp.get(self.cfg, 3, "reducer")
        d = {"name": "J3", "primary": "減速機磨耗", "drive": {"level": 2, "action": "安排更換（排程停機）"},
             "next": ["鐵粉加做鐵譜"]}
        h = wp.render_workorder(self.cfg, w, d, "drive")
        for s in ("A97L-0218-0989#218", "作業安全指令", "完工驗收", "M10 環首螺栓", "安排更換", "待確認", "☐"):
            self.assertIn(s, h)

    def test_missing_catalog_is_reported(self):
        self.cfg["workpack"]["catalog"] = "no_such_file.toml"
        cat = wp.load_catalog(self.cfg)
        self.assertIn("找不到", cat["error"])
        self.assertIsNone(wp.get(self.cfg, 3, "reducer"))


if __name__ == "__main__":
    unittest.main()

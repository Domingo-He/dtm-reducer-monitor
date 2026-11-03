"""2.1 收件人名單（每個人自己的語言）、簡訊送達回報（手機已接收）、備品單價與保養材料費估算、服務商資訊。"""
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
__import__("_cleanup")                  # 測試結束時刪掉暫存資料夾

from test_notify import CALLS, FakeSerial, Mock  # noqa: E402
from test_settings import Web, users  # noqa: E402
from test_v21 import tmp_cfg  # noqa: E402

from dtm import config as C  # noqa: E402
from dtm import sysconf as SC  # noqa: E402
from dtm import vendors  # noqa: E402
from dtm.central import CentralStore  # noqa: E402
from dtm.fleetviews import maint, material_cost  # noqa: E402
from dtm.notify import Notifier, handle_report, parse_reports, pkey, report_params, report_status  # noqa: E402
from dtm.store import Store  # noqa: E402

PEOPLE = [
    {"id": "a", "name": "王志明", "phone": "0912-000-101", "email": "wang@example.com", "lang": "zh-TW",
     "groups": ["default"], "min_level": 1, "enabled": True},
    {"id": "b", "name": "Nguyễn Văn Hùng", "phone": "+886 912 000 103", "email": "", "lang": "vi",
     "groups": ["default"], "min_level": 2, "enabled": True},
    {"id": "c", "name": "Maria Santos", "phone": "", "email": "maria@example.com", "lang": "en",
     "groups": ["default"], "min_level": 1, "enabled": False},
]


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        CALLS.clear()
        self.store = Store(os.path.join(tempfile.mkdtemp(), "n.sqlite3"))
        cfg = C.load(None)
        cfg["robot"]["name"] = "R01"
        n = cfg["notify"]
        n["sms"].update(enabled=True, provider="http", to=[], min_level=1, max_per_day=20)
        n["sms"]["http"].update(url=self.base + "/b2c/mtk/SmSend?CharsetURL=UTF-8", success="statuscode=[0124]",
                                params={"dstaddr": "{phone}", "smbody": "{message}"})
        n["sms"]["report"].update(enabled=True, key="k-123", id_regex=r"msgid=(\w+)", id_field="msgid",
                                  status_field="statuscode", delivered="^(0|DELIVRD)$")
        n["people"] = [dict(p) for p in PEOPLE]
        self.cfg = cfg


class TestPeopleRouting(Http):
    def test_each_person_gets_own_language(self):
        nt = Notifier(self.cfg, self.store)
        nt.sync = True
        nt.send(2, "J3 減速機：安排更換（排程停機）", 3, "健康", time.time(), "health:3")
        bodies = {urllib.parse.parse_qs(c["body"].decode())["dstaddr"][0]: urllib.parse.parse_qs(c["body"].decode())["smbody"][0]
                  for c in CALLS if "SmSend" in c["path"]}
        self.assertEqual(set(bodies), {"0912000101", "886912000103"})
        self.assertIn("警戒", bodies["0912000101"])
        self.assertNotIn("警戒", bodies["886912000103"])          # 越南文
        rows = {r["person"]: r for r in self.store.notify_log(10)}
        self.assertEqual(rows["Nguyễn Văn Hùng"]["lang"], "vi")
        self.assertEqual(rows["王志明"]["lang"], "zh-TW")
        self.assertNotIn("Maria Santos", rows)                    # 停用的人不發

    def test_person_min_level_and_rule_groups(self):
        n = self.cfg["notify"]
        n["rules"] = [{"name": "值班簡訊", "channels": ["sms"], "min_level": 1, "stage": 1},
                      {"name": "課長", "channels": ["sms"], "min_level": 2, "stage": 1}]
        n["people"][0]["groups"] = ["值班簡訊"]
        nt = Notifier(self.cfg, self.store)
        r = nt.route(1, "R01", "")
        self.assertEqual(r, [("sms", ["0912-000-101"], "值班簡訊", False, None)])   # 越南同仁只收警戒、而且不在這條規則
        self.assertEqual([x[1] for x in nt.route(2, "R01", "")], [["0912-000-101"], ["+886 912 000 103"]])  # 規則沒指定收件人 → 預設收件人
        self.assertEqual(pkey("+886 912 000 103"), pkey("0912000103"))

    def test_test_person_uses_their_language(self):
        nt = Notifier(self.cfg, self.store)
        out = nt.test_person(self.cfg["notify"]["people"][1])
        self.assertEqual([(x["channel"], x["ok"], x["lang"]) for x in out], [("sms", True, "vi")])


class TestDelivery(Http):
    def test_http_report_marks_delivered(self):
        nt = Notifier(self.cfg, self.store)
        r = nt.test("sms")
        self.assertTrue(all(x["ok"] for x in r), r)
        row = self.store.notify_log(1)[0]
        self.assertEqual((row["ref"], row["dlr"]), ("http:1234567890", "pending"))
        self.assertEqual(handle_report(self.cfg, self.store, {"key": "bad", "msgid": "1234567890"})[0], 403)
        code, txt = handle_report(self.cfg, self.store, report_params({"key": "k-123"}, b"msgid=1234567890&statuscode=0",
                                                                      "application/x-www-form-urlencoded"))
        self.assertEqual((code, txt), (200, "OK"))
        rows = [x for x in self.store.notify_log(5) if x["ref"] == "http:1234567890"]
        self.assertEqual(rows[0]["dlr"], "delivered")
        self.cfg["notify"]["sms"]["report"]["key"] = ""
        self.assertEqual(handle_report(self.cfg, self.store, {"key": "", "msgid": "x"})[0], 404)   # 沒設 key 不開放

    def test_modem_report_roundtrip(self):
        s = self.cfg["notify"]["sms"]
        s["provider"] = "modem"
        self.cfg["notify"]["people"] = [dict(PEOPLE[0])]

        class Modem(FakeSerial):
            def write(self, b):
                super().write(b)
                if b.startswith(b'AT+CMGL'):
                    self.pending = (b'\r\n+CMGL: 3,"REC UNREAD",6,12,"+886912000101",145,"26/10/18,14:01:02+32",'
                                    b'"26/10/18,14:01:09+32",0\r\n+CMGL: 4,"REC UNREAD","+886955000000",,"26/10/18,14:00:00+32"'
                                    b'\r\nhello\r\n\r\nOK\r\n')
        fake = Modem()
        nt = Notifier(self.cfg, self.store, serial_factory=lambda: fake)
        nt.sync = True
        self.assertTrue(nt.test("sms")[0]["ok"])
        self.assertIn(b"AT+CSMP=49,167,0,8\r", fake.out)              # 要求送達回報
        row = self.store.notify_log(1)[0]
        self.assertEqual((row["ref"], row["dlr"]), ("modem:12:000101", "pending"))
        nt.tick(time.time() + 120)
        self.assertEqual(self.store.notify_log(1)[0]["dlr"], "delivered")
        self.assertIn(b"AT+CMGD=3\r", fake.out)                       # 只刪送達回報
        self.assertNotIn(b"AT+CMGD=4\r", fake.out)

    def test_parse_reports(self):
        reps, idx = parse_reports('+CDS: 6,201,"+886912345678",145,"26/10/18,10:00:00+32","26/10/18,10:00:05+32",70\r\n')
        self.assertEqual((reps[0]["mr"], reps[0]["st"], idx), (201, 70, []))
        self.assertEqual([report_status(x) for x in (0, 33, 70)], ["delivered", "pending", "failed"])
        self.assertEqual(len(report_params({"key": "k"}, b'[{"msgid":"1"},{"msgid":"2"}]', "application/json")), 2)


class TestPeopleApi(unittest.TestCase):
    def test_validation(self):
        cfg = {"notify": {"rules": [{"name": "值班簡訊"}]}}
        ok = SC.clean_people([{"name": " 林 家豪 ", "phone": "0912345678", "groups": ["值班簡訊"], "lang": "vi"}], cfg)
        self.assertEqual((ok[0]["name"], ok[0]["lang"], ok[0]["min_level"]), ("林 家豪", "vi", 1))
        self.assertTrue(ok[0]["id"])
        for bad in ([{"name": "", "phone": "0912345678", "groups": ["default"]}],
                    [{"name": "A", "groups": ["default"]}],
                    [{"name": "A", "email": "x@", "groups": ["default"]}],
                    [{"name": "A", "phone": "0912345678", "groups": []}],
                    [{"name": "A", "phone": "0912345678", "groups": ["不存在"]}],
                    [{"name": "A", "phone": "0912345678", "groups": ["default"], "lang": "xx"}],
                    [{"name": "A", "phone": "0912345678", "groups": ["default"]},
                     {"name": "B", "phone": "+886912345678", "groups": ["default"]}]):
            with self.assertRaises(ValueError):
                SC.clean_people(bad, cfg)

    def test_web_add_edit_mask_and_audit(self):
        w = Web()
        us = users(w.cfg)
        us.add("adm", "admin-pw-1", "admin", "管理員")
        us.add("op", "operator-pw-1", "operator", "操作員")
        try:
            adm, op = w.login("adm", "admin-pw-1"), w.login("op", "operator-pw-1")
            p = {"name": "Nguyễn Văn Hùng", "phone": "0912000103", "lang": "vi", "groups": ["default"], "min_level": 2}
            self.assertEqual(w.call("/api/sys/notify/people", {"action": "save", "person": p}, op)[0], 403)
            code, j = w.call("/api/sys/notify/people", {"action": "save", "person": p}, adm)
            self.assertEqual(code, 200, j)
            pid = j["id"]
            self.assertEqual(w.cfg["notify"]["people"][0]["lang"], "vi")     # 立即套用
            v = w.call("/api/sys/notify/people", tok=op)[1]
            self.assertEqual(v["people"][0]["phone"], "0912…103")          # 非管理員看不到完整號碼
            self.assertFalse(v["can_edit"])
            self.assertEqual(w.call("/api/sys/notify/people", tok=adm)[1]["people"][0]["phone"], "0912000103")
            code, j = w.call("/api/sys/notify/people", {"action": "save", "person": dict(p, id=pid, lang="en")}, adm)
            self.assertEqual(w.cfg["notify"]["people"][0]["lang"], "en")
            self.assertEqual(w.call("/api/sys/notify/people", {"action": "enable", "id": pid, "enabled": False}, adm)[0], 200)
            self.assertFalse(w.cfg["notify"]["people"][0]["enabled"])
            self.assertEqual(w.call("/api/sys/notify/people", {"action": "delete", "id": pid}, adm)[0], 200)
            self.assertEqual(w.cfg["notify"]["people"], [])
            hist = SC.get(w.cfg).history()
            self.assertEqual(hist[0]["key"], SC.PEOPLE_KEY)
            for r in w.call("/api/sys/audit?limit=50", tok=adm)[1]["rows"]:
                self.assertNotIn("0912000103", r["detail"] or "")              # 操作紀錄不記電話
        finally:
            w.close()


class TestCostAndVendor(unittest.TestCase):
    def cs(self):
        cfg = C.load(None)
        cfg["system"]["role"] = "central"
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cfg["workpack"]["catalog"] = os.path.join(root, "workpacks.r2000ic_165f.toml")
        cs = CentralStore(os.path.join(tempfile.mkdtemp(), "c.sqlite3"), cfg)
        return cs

    def test_material_cost_from_workpack_and_spares(self):
        cs = self.cs()
        cs.save_spare({"part": "RED-J3", "name": "J3 減速機", "stock": 1, "comp": "reducer", "axes": "3", "price": 210000})
        cs.save_spare({"part": "A98L-0040-0174#16KG", "name": "潤滑脂", "stock": 2, "comp": "grease", "price": 9000})
        with self.assertRaises(ValueError):
            cs.save_spare({"part": "X", "name": "x", "price": -1})
        c = material_cost(cs.cfg, {"axis": 3, "kind": "rep"}, cs.q("SELECT * FROM spares"))
        self.assertEqual(c["source"], "pack")
        self.assertEqual(c["total"], 219000)                    # 主件 210,000＋潤滑脂 1 桶 9,000
        self.assertEqual(c["priced"], 2)
        self.assertGreater(c["unpriced"], 0)                    # O 形環等還沒填單價
        c2 = material_cost(cs.cfg, {"axis": 2, "kind": "motor"}, cs.q("SELECT * FROM spares"))
        self.assertEqual((c2["total"], c2["priced"]), (None, 0))   # 馬達作業包的品項都還沒有單價
        self.assertIsNone(material_cost(cs.cfg, {"axis": 2, "kind": "check"}, []))   # 檢查不估材料
        now = time.time()
        with cs.conn() as cn:
            cn.execute("INSERT INTO jobs (site_id,rkey,axis,kind,ts,hours,status,title) VALUES ('P1','P1/r01',3,'rep',?,8,"
                       "'planned','J3')", (now + 3 * 86400,))
        m = maint(cs, now)
        self.assertEqual(m["summary"]["m30_cost"]["total"], 219000)
        self.assertEqual(m["currency"], "TWD")
        self.assertEqual(m["vendor"]["region"], "tw")
        self.assertEqual(next(s for s in m["spares"] if s["part"] == "RED-J3")["price"], 210000)

    def test_vendor_region_and_contact(self):
        cfg = C.load(None)
        self.assertEqual([vendors.region_for(cfg, lg) for lg in ("zh-TW", "zh-CN", "en", "ja", "ko", "vi")],
                         ["tw", "cn", "us", "jp", "kr", "vn"])
        cfg["service"].update(region="tw", contact_name="陳先生", contact_email="svc@example.com")
        v = vendors.view(cfg, "vi")
        self.assertEqual((v["region"], v["pinned"], v["has_contact"]), ("tw", True, True))   # 外籍同仁看到的仍是本地據點
        tw = next(e for e in v["entries"] if e["key"] == "tw")
        self.assertTrue(tw["offices"][0]["addr"] and tw["offices"][0]["addr_en"])
        sc = SC.get(tmp_cfg())
        for k, bad in (("service.region", "mars"), ("service.contact_email", "x@"), ("maint.currency", "NT$")):
            with self.assertRaises(ValueError):
                sc.validate(k, bad)
        self.assertEqual(sc.validate("maint.currency", "usd"), "USD")


if __name__ == "__main__":
    unittest.main()

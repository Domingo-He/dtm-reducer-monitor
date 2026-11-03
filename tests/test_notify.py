import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
__import__("_cleanup")                  # 測試結束時刪掉暫存資料夾

from dtm import config as C  # noqa: E402
from dtm.alarms import AlarmManager  # noqa: E402
from dtm.notify import Notifier, short_text  # noqa: E402
from dtm.store import Store  # noqa: E402

CALLS = []


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        CALLS.append({"path": self.path, "headers": dict(self.headers), "body": body})
        p = urllib.parse.urlparse(self.path).path
        if p.startswith("/v2/bot/message/"):
            code, out = (401, '{"message":"bad token"}') if "BAD" in self.headers.get("Authorization", "") else (200, "{}")
        elif p == "/b2c/mtk/SmSend":
            code, out = 200, "[1]\r\nmsgid=1234567890\r\nstatuscode=1\r\nAccountPoint=98\r\n"
        elif p.endswith("/Messages.json"):
            code, out = 201, '{"sid":"SM123"}'
        elif p == "/generic":
            code, out = 200, "result=OK"
        else:
            code, out = 404, "no"
        self.send_response(code)
        self.end_headers()
        self.wfile.write(out.encode())

    do_GET = do_POST


class FakeSerial:
    def __init__(self):
        self.out, self.pending = [], b""

    def write(self, b):
        self.out.append(b)
        if b.startswith(b"AT+CMGS"):
            self.pending = b"\r\n> "
        elif b.endswith(b"\x1a"):
            self.pending = b"\r\n+CMGS: 12\r\n\r\nOK\r\n"
        else:
            self.pending = b"\r\nOK\r\n"

    def read(self, n):
        p, self.pending = self.pending, b""
        return p

    def close(self):
        pass


class TestNotify(unittest.TestCase):
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
        cfg["robot"]["name"] = "R2000iC#1"
        n = cfg["notify"]
        n["cooldown_s"] = 600
        # Webhook 依模板直接呼叫 LINE Messaging API（README 的接法）
        n["webhook_url"] = self.base + "/v2/bot/message/push"
        n["webhook_headers"] = {"Authorization": "env:DTM_TEST_BEARER"}
        n["webhook_body"] = {"to": "Uaaa111222333", "messages": [{"type": "text", "text": "{text}"}]}
        os.environ["DTM_TEST_BEARER"] = "Bearer TOKEN"
        # 簡訊：通用 HTTP 閘道，用三竹的設定範例
        n["sms"].update(enabled=True, provider="http", to=["0912345678"], min_level=2, max_per_day=3)
        n["sms"]["http"].update(url=self.base + "/b2c/mtk/SmSend?CharsetURL=UTF-8", success="statuscode=[0124]",
                                params={"username": "u", "password": "env:DTM_TEST_PW", "dstaddr": "{phone}",
                                        "smbody": "{message}"})
        os.environ["DTM_TEST_PW"] = "pw123"
        self.cfg = cfg
        self.nt = Notifier(cfg, self.store)

    def wait_log(self, n, timeout=5):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if len(self.store.notify_log(100)) >= n:
                return
            time.sleep(0.05)

    def test_short_text_fits_one_sms(self):
        t = short_text(2, "R2000iC#1", "J3 減速機：安排更換（排程停機）；已有指標達警戒，備品請立即訂購（研判：減速機磨耗）" * 2, 0)
        self.assertLessEqual(len(t), 70)
        self.assertTrue(t.startswith("[警戒]R2000iC#1 J3 減速機"))
        self.assertNotIn("研判", t)

    def test_webhook_template_line(self):
        r = self.nt.test("webhook", "測試")
        self.assertTrue(r[0]["ok"], r)
        c = CALLS[0]
        self.assertEqual(c["path"], "/v2/bot/message/push")
        self.assertEqual(c["headers"]["Authorization"], "Bearer TOKEN")
        body = json.loads(c["body"])
        self.assertEqual(body["to"], "Uaaa111222333")
        self.assertEqual(body["messages"][0]["type"], "text")
        self.assertIn("測試", body["messages"][0]["text"])

    def test_webhook_bad_token_logged(self):
        os.environ["DTM_TEST_BEARER"] = "Bearer BAD"
        try:
            r = Notifier(self.cfg, self.store).test("webhook")
        finally:
            os.environ["DTM_TEST_BEARER"] = "Bearer TOKEN"
        self.assertFalse(r[0]["ok"])
        self.assertIn("401", r[0]["detail"])
        self.assertEqual(self.store.notify_log(5)[0]["ok"], 0)

    def test_webhook_default_json(self):
        self.cfg["notify"]["webhook_body"] = {}
        self.cfg["notify"]["webhook_url"] = self.base + "/generic"
        r = Notifier(self.cfg, self.store).test("webhook", "預設")
        self.assertTrue(r[0]["ok"], r)
        body = json.loads(CALLS[-1]["body"])
        self.assertEqual(body["robot"], "R2000iC#1")
        self.assertIn("預設", body["text"])

    def test_sms_http_mitake_example(self):
        r = self.nt.test("sms")
        self.assertTrue(r[0]["ok"], r)
        form = urllib.parse.parse_qs(CALLS[0]["body"].decode())
        self.assertEqual(form["password"], ["pw123"])
        self.assertEqual(form["dstaddr"], ["0912345678"])
        self.assertIn("CharsetURL=UTF-8", CALLS[0]["path"])
        self.assertLessEqual(len(form["smbody"][0]), 70)

    def test_sms_http_twilio_example_and_get(self):
        s = self.cfg["notify"]["sms"]
        s["http"] = {"method": "POST", "url": self.base + "/2010-04-01/Accounts/AC1/Messages.json", "json": False,
                     "success": "", "basic_user": "AC1", "basic_password": "tok", "headers": {},
                     "params": {"To": "{phone_e164}", "From": "+15550001", "Body": "{message}"}}
        r = Notifier(self.cfg, self.store).test("sms")
        self.assertTrue(r[0]["ok"], r)
        form = urllib.parse.parse_qs(CALLS[-1]["body"].decode())
        self.assertEqual(form["To"], ["+886912345678"])
        self.assertEqual(CALLS[-1]["headers"]["Authorization"], "Basic " + base64.b64encode(b"AC1:tok").decode())
        s["http"] = {"method": "GET", "url": self.base + "/generic", "params": {"to": "{phone}", "msg": "{message}"},
                     "success": "result=OK", "headers": {}}
        r = Notifier(self.cfg, self.store).test("sms")
        self.assertTrue(r[0]["ok"], r)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(CALLS[-1]["path"]).query)
        self.assertEqual(q["to"], ["0912345678"])

    def test_sms_fallback_modem_to_http(self):
        s = self.cfg["notify"]["sms"]
        s["provider"], s["fallback"] = "modem", ["http"]

        class Dead(FakeSerial):
            def write(self, b):
                self.out.append(b)
                self.pending = b"\r\nERROR\r\n"
        r = Notifier(self.cfg, self.store, serial_factory=Dead).test("sms")
        self.assertTrue(r[-1]["ok"], r)
        self.assertIn("http", r[-1]["detail"])

    def test_summary_sms_heartbeat(self):
        nt = Notifier(self.cfg, self.store)
        out = nt.send_text("每日摘要", "機群 3 台｜警戒 0、注意 1\n細節…", ["sms"])
        self.assertTrue(out and out[0][1], out)
        form = urllib.parse.parse_qs(CALLS[-1]["body"].decode())
        self.assertIn("機群 3 台", form["smbody"][0])
        self.assertNotIn("細節", form["smbody"][0])

    def test_modem(self):
        s = self.cfg["notify"]["sms"]
        s["provider"] = "modem"
        fake = FakeSerial()
        r = Notifier(self.cfg, self.store, serial_factory=lambda: fake).test("sms", "J3 警戒")
        self.assertTrue(r[0]["ok"], r)
        self.assertIn(b'AT+CSCS="UCS2"\r', fake.out)
        self.assertTrue(fake.out[-1].endswith(b"\x1a"))
        self.assertIn("J3".encode("utf-16-be").hex().upper().encode(), fake.out[-1])

    def test_routing_cooldown_and_cap(self):
        am = AlarmManager(self.store, self.nt)
        am.set("dec:3:drive", 3, "維護決策", 1, "J3 減速機：準備備品", 0.6, time.time())
        self.wait_log(1)
        chans = [r["channel"] for r in self.store.notify_log(10)]
        self.assertEqual(chans, ["webhook"], "注意等級只發 Webhook，不發簡訊")
        am.set("dec:3:drive", 3, "維護決策", 2, "J3 減速機：安排更換", 1.2, time.time())
        self.wait_log(3)
        chans = sorted(r["channel"] for r in self.store.notify_log(10))
        self.assertEqual(chans, ["sms", "webhook", "webhook"])
        n_before = len(CALLS)
        am.set("dec:3:drive", 3, "維護決策", 0, "", None, time.time())
        am.set("dec:3:drive", 3, "維護決策", 2, "J3 減速機：安排更換", 1.2, time.time())
        time.sleep(0.5)
        self.assertEqual(len(CALLS), n_before, "冷卻時間內同一警報不重發")
        for i in range(5):
            am.set(f"x:{i}", 3, "測試", 2, f"警戒 {i}", None, time.time())
        time.sleep(1.0)
        sms_ok = [r for r in self.store.notify_log(100) if r["channel"] == "sms" and r["ok"]]
        capped = [r for r in self.store.notify_log(100) if "每日簡訊上限" in (r["detail"] or "")]
        self.assertEqual(len(sms_ok), 3)
        self.assertTrue(capped)

    def test_clear_notification_optional(self):
        self.cfg["notify"]["notify_clear"] = True
        nt = Notifier(self.cfg, self.store)
        am = AlarmManager(self.store, nt)
        am.set("k", 2, "健康", 1, "J2 健康注意", None, time.time())
        self.wait_log(1)
        am.set("k", 2, "健康", 0, "", None, time.time())
        self.wait_log(2)
        texts = [json.loads(c["body"])["messages"][0]["text"] for c in CALLS if c["path"].endswith("/push")]
        self.assertTrue(any("已解除" in t for t in texts))


if __name__ == "__main__":
    unittest.main()

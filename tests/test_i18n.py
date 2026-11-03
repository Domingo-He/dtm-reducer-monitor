"""多國語言：翻譯表完整、參數一致，各語言的儀表板 API、報告、工單都能正常產生。"""
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
__import__("_cleanup")                  # 測試結束時刪掉暫存資料夾

from dtm import config as C  # noqa: E402
from dtm import i18n  # noqa: E402
from dtm import workpack as wp  # noqa: E402
from dtm.i18n import LANGS, SOURCE, _, use  # noqa: E402
from dtm.notify import diag_suffix, short_text  # noqa: E402
from dtm.service import simulate_history  # noqa: E402
from dtm.web import serve  # noqa: E402

PH = re.compile(r"\{[^{}]*\}|%%|%[sdfr]|\n")
CJK = re.compile(r"[㐀-鿿]")


def ph(s: str) -> list[str]:
    """參數清單；{{ }} 是跳脫的大括號，不算參數。"""
    return sorted(PH.findall(s.replace("{{", "").replace("}}", "")))


OTHERS = [lg for lg in LANGS if lg != SOURCE]


def msgids():
    with open(os.path.join(ROOT, "dtm", "locales", "_msgids.json"), encoding="utf-8") as f:
        return json.load(f)


class TestCatalogs(unittest.TestCase):
    # 2.1 起 6 種語言都要完整：新增或修改畫面文字時，要同時補上各語言的翻譯（python tools/i18n_tool.py check）。
    def test_every_language_is_complete(self):
        ids = msgids()
        self.assertGreater(len(ids), 400)
        for lg in OTHERS:
            cat = i18n.catalog(lg)
            missing = [m for m in ids if m not in cat]
            self.assertEqual(missing, [], f"{lg} 缺翻譯")
            bad = [m for m in ids if ph(m) != ph(cat[m].replace("!l}", "}"))]
            self.assertEqual(bad, [], f"{lg} 參數不符")

    def test_existing_translations_keep_placeholders(self):
        """已經翻好的項目，參數（{name} 等）必須和原文一致。"""
        ids = set(msgids())
        for lg in OTHERS:
            cat = i18n.catalog(lg)
            bad = [m for m in cat if m in ids and ph(m) != ph(cat[m].replace("!l}", "}"))]
            self.assertEqual(bad, [], f"{lg} 參數不符")
            self.assertGreater(len([m for m in ids if m in cat]), 450, lg)

    def test_non_cjk_languages_have_no_chinese(self):
        for lg in ("en", "ko", "vi"):
            cat = i18n.catalog(lg)
            leaked = [m for m, v in cat.items() if CJK.search(v)]
            self.assertEqual(leaked, [], f"{lg} 翻譯裡還有中文")

    def test_lookup_and_fallback(self):
        with use("en"):
            self.assertEqual(_("隨{comp}更換一併處理", comp=_("減速機")), "Handle together with the reducer replacement")
            self.assertEqual(_("減速機"), "Reducer")
            self.assertEqual(_("{n} 天", n=3), "3 days")
            self.assertEqual(_("沒有這個字串 {x}", x=1), "沒有這個字串 1")   # 不在表裡就回原文
        with use("ja"):
            self.assertEqual(_("減速機"), "減速機")
        with use("zh-CN"):
            self.assertEqual(_("伺服馬達"), "伺服电机")
        self.assertEqual(i18n.norm("zh_tw"), "zh-TW")
        self.assertEqual(i18n.norm("JP"), "ja")
        self.assertIsNone(i18n.norm("fr"))

    def test_sms_drops_diagnosis_and_fits_length_in_every_language(self):
        for lg in LANGS:
            with use(lg):
                suffix = diag_suffix(_("{comp}磨耗", comp=_("減速機")))
                msg = _("{name} {comp}：{action}", name="J3", comp=_("減速機"),
                        action=_("安排更換（排程停機）")) + suffix
                sms = short_text(2, "R-01", msg, 1_700_000_000)
                self.assertNotIn(suffix.strip(), sms, lg)
                self.assertLessEqual(len(sms), 160 if lg == "en" else 70, f"{lg}: {sms}")


    def test_localized_workpack_catalogs_are_up_to_date(self):
        """各語言作業包要跟繁中原檔同步（python tools/workpack_l10n.py 重新產生）：同樣的作業包、同樣的結構。"""
        import tomllib
        base_path = os.path.join(ROOT, "workpacks.r2000ic_165f.toml")
        with open(base_path, "rb") as f:
            base = tomllib.load(f)

        def shape(o):
            if isinstance(o, dict):
                return {k: shape(v) for k, v in o.items()}
            if isinstance(o, list):
                return [shape(v) for v in o]
            return o if not isinstance(o, str) else "s"
        for lg in OTHERS:
            with open(base_path.replace(".toml", f".{lg}.toml"), "rb") as f:
                loc = tomllib.load(f)
            self.assertEqual(shape(loc), shape(base), f"{lg}：作業包跟原檔不同步，請執行 python tools/workpack_l10n.py")

    def test_localized_workpack_catalogs_keep_numbers(self):
        cfg = C.load(os.path.join(ROOT, "config.sim.toml"))
        with use(SOURCE):
            base = wp.get(cfg, 3, "reducer")
        for lg in OTHERS:
            with use(lg):
                w = wp.get(cfg, 3, "reducer")
                self.assertTrue(wp.load_catalog(cfg)["path"].endswith(f".{lg}.toml"), lg)
            self.assertNotEqual(w["title"], base["title"], lg)
            fanuc_pn = re.compile(r"^A\d\d[A-Z]-")
            self.assertEqual([p["pn"] for p in w["parts"] if fanuc_pn.match(p["pn"])],
                             [p["pn"] for p in base["parts"] if fanuc_pn.match(p["pn"])], lg)
            self.assertEqual((w["crew"], w["step_hours"], w["man_hours"], len(w["safety"])),
                             (base["crew"], base["step_hours"], base["man_hours"], len(base["safety"])), lg)
            if lg in ("en", "ko", "vi"):
                text = json.dumps(w, ensure_ascii=False)
                self.assertFalse(CJK.search(text), f"{lg}: {CJK.findall(text)[:10]}")


class TestRenderEveryLanguage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = C.load(os.path.join(ROOT, "config.sim.toml"))
        cfg["storage"]["db"] = os.path.join(tempfile.mkdtemp(), "i18n.sqlite3")
        cls.svc = simulate_history(cfg, days=150)
        cls.httpd = serve(cls.svc, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=30) as r:
            return r.status, r.read().decode("utf-8")

    def test_api_and_pages(self):
        paths = ["/api/i18n", "/api/status", "/api/health", "/api/decision", "/api/alarms?limit=50",
                 "/api/events", "/api/trend?axis=3&metric=F&days=180", "/api/workpack?axis=3&comp=reducer",
                 "/workorder?axis=3&comp=reducer", "/api/report?days=150"]
        for lg in LANGS:
            for p in paths:
                sep = "&" if "?" in p else "?"
                code, body = self.get(f"{p}{sep}lang={lg}")
                self.assertEqual(code, 200, f"{lg} {p}: {body[:200]}")

    def test_live_content_follows_request_language(self):
        _c, body = self.get("/api/decision?lang=en")
        j3 = [d for d in json.loads(body) if d.get("axis") == 3][0]
        text = json.dumps(j3, ensure_ascii=False)
        self.assertIn("Reducer", text)
        _c, body = self.get("/api/i18n?lang=ko")
        info = json.loads(body)
        self.assertEqual(info["lang"], "ko")
        self.assertEqual(info["messages"]["減速機"], "감속기")
        _c, page = self.get("/workorder?axis=3&comp=reducer&lang=vi")
        self.assertIn('lang="vi"', page)
        self.assertIn("Phụ tùng", page)

    def test_stored_alarms_follow_viewer_language(self):
        """2.1 起警報存了訊息模板：已存的警報也依觀看者的語言顯示；系統語言照原文。"""
        rows = json.loads(self.get("/api/alarms?limit=20&lang=en")[1])
        self.assertTrue(rows)
        for r in rows:
            self.assertNotIn("tmpl", r)
            self.assertFalse(CJK.search(r["kind_label"]), r["kind_label"])
            self.assertFalse(CJK.search(r["message"]), r["message"])
        tw = json.loads(self.get("/api/alarms?limit=20&lang=zh-TW")[1])
        raw = {a["id"]: a["message"] for a in self.svc.store.alarms(20)}
        self.assertEqual({r["id"]: r["message"] for r in tw}, raw)

    def test_old_alarms_without_template_keep_text(self):
        aid = self.svc.store.add_alarm(time.time() - 5, 2, "健康", "old:2", 1, "舊版的警報文字", None)
        with self.svc.store.conn() as c:
            c.execute("UPDATE alarms SET tmpl=NULL, cleared_ts=? WHERE id=?", (time.time(), aid))
        try:
            rows = json.loads(self.get("/api/alarms?limit=50&lang=en")[1])
            self.assertEqual([r["message"] for r in rows if r["id"] == aid], ["舊版的警報文字"])
        finally:
            with self.svc.store.conn() as c:
                c.execute("DELETE FROM alarms WHERE id=?", (aid,))


class TestMsgTemplates(unittest.TestCase):
    def test_concat_join_and_render(self):
        m = i18n._("{name} 健康{level}：", name="J2", level=i18n._("注意")) + i18n.join([i18n._("減速機"), "x"])
        t = i18n.pack_json(m)
        self.assertEqual(i18n.render(t, "orig"), str(m))
        with i18n.use("en"):
            self.assertEqual(i18n.render(t, "orig"), "J2 health Caution: Reducer; x")
        self.assertEqual(i18n.render('{"m": "沒有這個字串"}', "orig"), "沒有這個字串")
        with i18n.use("en"):
            self.assertEqual(i18n.render('{"m": "沒有這個字串"}', "orig"), "orig")   # 缺翻譯：用原文
            self.assertEqual(i18n.render("not json", "orig"), "orig")
        self.assertIsNone(i18n.pack("plain"))
        self.assertIsInstance(f"{m}", str)

    def test_templates_and_localize(self):
        d = {"reasons": [i18n._("減速機"), "plain"], "n": 3, "decision": {"grease": {"action": i18n._("注意")}}}
        side = json.loads(json.dumps(i18n.templates(d)))
        copy_ = json.loads(json.dumps(d))
        with i18n.use("ja"):
            i18n.localize(copy_, side)
        self.assertEqual(copy_["reasons"], [i18n.lookup("減速機", "ja"), "plain"])
        self.assertEqual(copy_["decision"]["grease"]["action"], "注意")

    def test_notification_rule_language(self):
        from dtm.notify import Notifier
        cfg = C.load(os.path.join(ROOT, "config.sim.toml"))
        cfg["notify"]["webhook_url"] = "http://127.0.0.1:9/x"
        cfg["notify"]["rules"] = [{"name": "vn", "channels": ["webhook"], "lang": "vi", "to": {"webhook": ["http://127.0.0.1:9/vn"]}},
                                  {"name": "tw", "channels": ["webhook"], "to": {"webhook": ["http://127.0.0.1:9/tw"]}}]
        n = Notifier(cfg)
        sent = []
        n.sync = True
        n._deliver = lambda ch, msg, **kw: sent.append((kw.get("rule"), msg["text"]))
        msg = i18n._("{name} 健康{level}：", name="J2", level=i18n._("警戒"))
        n.send(2, msg, 2, "健康", time.time(), "health:2")
        texts = dict(sent)
        self.assertIn("Cảnh báo", texts["vn"])
        self.assertIn("警戒", texts["tw"])


if __name__ == "__main__":
    unittest.main()

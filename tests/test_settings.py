"""2.1 設定（齒輪）：單一登入、密碼原則、閒置登出、帳號管理、系統設定（網頁可改的項目）、允許連線的電腦、
通知收件人、操作紀錄不記密碼、診斷包遮蔽、DTM 主畫面整合（單機的 / 是主畫面，邊緣主機轉送機器人畫面）。"""
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
__import__("_cleanup")                  # 測試結束時刪掉暫存資料夾

from test_v21 import tmp_cfg  # noqa: E402

from dtm import sysconf as SC  # noqa: E402
from dtm.auth import Auth, UserStore, check_password  # noqa: E402


def users(cfg) -> UserStore:
    return UserStore(cfg["web"]["users_db"])


class TestSessions(unittest.TestCase):
    def test_single_sign_on_between_programs(self):
        """同一台主機的兩個程式（例如廠區畫面與機器人程式）共用登入狀態。"""
        cfg = tmp_cfg()
        users(cfg).add("adm", "admin-pw-1", "admin", "管理員")
        a1, a2 = Auth(cfg), Auth(tmp_cfg())
        a2.cfg = cfg
        a2.users_path = cfg["web"]["users_db"]
        a2.users = UserStore(a2.users_path)
        tok, err, code = a1.login("10.0.0.5", "", "admin-pw-1", username="adm", agent="Mozilla Chrome/120 Windows")
        self.assertEqual(code, 200, err)
        s = a2.session(tok)
        self.assertEqual((s["who"], s["role"], s["username"]), ("管理員", "admin", "adm"))
        with users(cfg).conn() as c:                          # 只存雜湊，不存 token
            row = c.execute("SELECT th FROM sessions").fetchone()
        self.assertNotEqual(row[0], tok)
        a2.logout(tok)
        self.assertIsNone(a1.session(tok))

    def test_idle_logout_and_viewer_exempt(self):
        cfg = tmp_cfg()
        us = users(cfg)
        us.add("op", "operator-pw-1", "operator")
        us.add("tv", "viewer-pw-12", "viewer")
        a = Auth(cfg)
        t0 = time.time()
        op = a.login("1.1.1.1", "", "operator-pw-1", username="op", now=t0)[0]
        tv = a.login("1.1.1.2", "", "viewer-pw-12", username="tv", now=t0)[0]
        self.assertIsNotNone(a.session(op, now=t0 + 29 * 60))
        a.touch(op, now=t0 + 29 * 60)                         # 有操作就延長
        self.assertIsNotNone(a.session(op, now=t0 + 50 * 60))
        self.assertIsNone(a.session(op, now=t0 + 60 * 60))    # 閒置 31 分鐘 → 登出
        self.assertIsNotNone(a.session(tv, now=t0 + 60 * 60))  # 看板用的檢視帳號不受影響
        self.assertIsNone(a.session(tv, now=t0 + 13 * 3600))  # 但登入有效時間照算

    def test_disable_and_role_change_apply_immediately(self):
        cfg = tmp_cfg()
        us = users(cfg)
        us.add("eng", "engineer-pw-1", "engineer")
        a = Auth(cfg)
        tok = a.login("1.1.1.1", "", "engineer-pw-1", username="eng")[0]
        us.update("eng", role="operator")
        self.assertEqual(a.session(tok)["role"], "operator")
        self.assertFalse(a.can(a.session(tok), "/api/event"))
        us.update("eng", active=0)
        self.assertIsNone(a.session(tok))

    def test_account_lockout_follows_the_account(self):
        cfg = tmp_cfg()
        users(cfg).add("adm", "admin-pw-1", "admin")
        a = Auth(cfg)
        for i in range(5):                                    # 每次換一台電腦也照算
            self.assertEqual(a.login(f"10.0.0.{i}", "", "bad", username="adm")[2], 403 if i < 4 else 429)
        self.assertEqual(a.login("10.0.0.99", "", "admin-pw-1", username="adm")[2], 429)
        users(cfg).update("adm", locked_until=0, fails=0)    # 管理員「解除鎖定」
        self.assertEqual(a.login("10.0.0.99", "", "admin-pw-1", username="adm")[2], 200)
        msg = a.login("10.0.0.98", "", "bad", username="nobody")[1]
        self.assertIn("帳號或密碼錯誤", msg)                   # 不透露帳號存不存在


class TestPasswordPolicy(unittest.TestCase):
    def test_rules(self):
        pol = {"pw_min": 10, "pw_mix": True}
        self.assertIn("10", check_password("short1", pol))
        self.assertTrue(check_password("onlyletters", pol))
        self.assertTrue(check_password("1234567890", pol))
        self.assertTrue(check_password("password123", {"pw_min": 8, "pw_mix": True}))   # 常見密碼
        self.assertTrue(check_password("wang12345", {"pw_min": 8, "pw_mix": True}, "wang12345"))
        self.assertEqual(check_password("Reducer-J3-2026", pol), "")
        self.assertEqual(check_password("operator-pw", {"pw_min": 8, "pw_mix": False}), "")

    def test_must_change_blocks_writes_until_changed(self):
        cfg = tmp_cfg()
        us = users(cfg)
        us.add("adm", "admin-pw-1", "admin")
        us.add("op", "Temp-pass-1", "operator", must_change=True)
        a = Auth(cfg)
        tok = a.login("1.1.1.1", "", "Temp-pass-1", username="op")[0]
        s = a.session(tok)
        self.assertTrue(s["must_change"])
        self.assertFalse(a.can(s, "/api/ack"))
        self.assertTrue(a.can(s, "/api/sys/password"))
        other = a.login("1.1.1.2", "", "Temp-pass-1", username="op")[0]
        with self.assertRaises(PermissionError):
            a.change_password("op", "wrong", "New-pass-22", tok)
        with self.assertRaises(ValueError):
            a.change_password("op", "Temp-pass-1", "newpassword", tok)      # 沒有數字
        self.assertEqual(a.change_password("op", "Temp-pass-1", "New-pass-22", tok), 1)
        self.assertFalse(a.session(tok)["must_change"])
        self.assertTrue(a.can(a.session(tok), "/api/ack"))
        self.assertIsNone(a.session(other))                                 # 其他裝置被登出

    def test_expired_password_must_change(self):
        cfg = tmp_cfg()
        users(cfg).add("op", "operator-pw-1", "operator")
        SC.get(cfg).set({"password.max_age_days": 30}, "adm", "127.0.0.1")
        a = Auth(cfg)
        tok = a.login("1.1.1.1", "", "operator-pw-1", username="op", now=time.time() + 40 * 86400)[0]
        with users(cfg).conn() as c:
            c.execute("UPDATE sessions SET expires=?, active=?", (time.time() + 3600, time.time()))
        self.assertTrue(a.session(tok)["must_change"])

    def test_last_admin_is_protected(self):
        cfg = tmp_cfg()
        us = users(cfg)
        us.add("adm", "admin-pw-1", "admin")
        a = Auth(cfg)
        with self.assertRaises(ValueError):
            a.guard_admin("adm", role="operator")
        with self.assertRaises(ValueError):
            a.guard_admin("adm", active=False)
        us.add("adm2", "admin-pw-2", "admin")
        a.guard_admin("adm", active=False)                    # 還有另一個管理員：可以


class TestSysConf(unittest.TestCase):
    def test_set_apply_history_revert_reset(self):
        cfg = tmp_cfg()
        sc = SC.get(cfg)
        self.assertEqual(sc.set({"security.idle_minutes": 15, "ui.display_name": "  三廠  焊接線 "}, "adm", "10.0.0.1"),
                         ["security.idle_minutes", "ui.display_name"])
        self.assertEqual(cfg["web"]["idle_minutes"], 15)
        self.assertEqual(cfg["system"]["display_name"], "三廠 焊接線")
        self.assertEqual(SC.policy(cfg)["idle_minutes"], 15)
        self.assertEqual(sc.set({"security.idle_minutes": 15}, "adm", "10.0.0.1"), [])     # 沒變不記
        with self.assertRaises(ValueError):
            sc.set({"security.idle_minutes": 5000}, "adm", "x")
        with self.assertRaises(ValueError):
            sc.set({"robot.source": "fanuc_http"}, "adm", "x")                           # 不開放的項目
        sc.set({"security.idle_minutes": 45}, "adm", "10.0.0.1")
        h = sc.history()
        self.assertEqual((h[0]["old"], h[0]["new"]), (15, 45))
        self.assertTrue(h[-1]["old_file"])
        sc.revert(h[0]["id"], "adm", "10.0.0.1")
        self.assertEqual(cfg["web"]["idle_minutes"], 15)
        sc.reset("security.idle_minutes", "adm", "10.0.0.1")
        self.assertEqual(cfg["web"]["idle_minutes"], 30)                                  # 設定檔的值

    def test_other_program_picks_up_changes(self):
        cfg = tmp_cfg()
        other = tmp_cfg()
        other["web"]["users_db"] = cfg["web"]["users_db"]
        SC.get(cfg).set({"security.require_login": True}, "adm", "127.0.0.1")
        self.assertTrue(SC.get(other).refresh(force=True))
        self.assertTrue(other["web"]["require_login"])
        self.assertTrue(Auth(other).require_login)

    def test_allow_ips(self):
        self.assertEqual(SC.parse_ips("192.168.10.0/24, 10.1.2.3"), ["192.168.10.0/24", "10.1.2.3"])
        with self.assertRaises(ValueError):
            SC.parse_ips(["192.168.300.1"])
        self.assertTrue(SC.ip_allowed("192.168.10.77", ["192.168.10.0/24"]))
        self.assertFalse(SC.ip_allowed("192.168.11.77", ["192.168.10.0/24"]))
        self.assertTrue(SC.ip_allowed("127.0.0.1", ["192.168.10.0/24"]))
        self.assertTrue(SC.ip_allowed("::ffff:10.1.2.3", ["10.1.2.3"]))
        cfg = tmp_cfg()
        with self.assertRaises(ValueError):                    # 自己會被擋在外面
            SC.get(cfg).set({"security.allow_ips": ["10.9.0.0/16"]}, "adm", "192.168.1.5", client="192.168.1.5")

    def test_notify_recipients_apply_without_restart(self):
        from dtm.notify import Notifier
        cfg = tmp_cfg('[notify]\nemail_enabled = true\nsmtp_host = "mail.example.com"\nmail_to = ["a@example.com"]\n'
                      '[[notify.rules]]\nname = "夜班"\nchannels = ["email"]\nto = { email = ["night@example.com"] }\n')
        nt = Notifier(cfg)
        self.assertEqual(nt.route(2, "R1", "")[0][1], ["night@example.com"])
        sc = SC.get(cfg)
        with self.assertRaises(ValueError):
            sc.set({SC.NOTIFY_KEY: {"rules": {"夜班": {"email": ["not-an-email"]}}}}, "adm", "x")
        with self.assertRaises(ValueError):
            sc.set({SC.NOTIFY_KEY: {"rules": {"不存在": {"email": []}}}}, "adm", "x")
        sc.set({SC.NOTIFY_KEY: {"default": {"email": ["b@example.com"]},
                                "rules": {"夜班": {"email": ["vn@example.com"], "lang": "vi"}}}}, "adm", "x")
        r = nt.route(2, "R1", "")[0]
        self.assertEqual((r[1], r[4]), (["vn@example.com"], "vi"))
        nt._sync_targets()
        self.assertEqual(nt.channels["email"].targets(), ["b@example.com"])
        sc.set({SC.NOTIFY_KEY: {"rules": {"夜班": {"enabled": False}}}}, "adm", "x")
        self.assertEqual(nt.route(2, "R1", ""), [("email", None, "", False, None)])   # 沒有啟用的規則：發給預設收件人


class Web:
    """單機程式＋網頁（用模擬器設定）。"""

    def __init__(self, cfg=None):
        from dtm.service import Service
        from dtm.web import serve
        self.cfg = cfg or tmp_cfg()
        self.svc = Service(self.cfg, notify=False)
        self.httpd = serve(self.svc, "127.0.0.1", 0, auth=Auth(self.cfg))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def call(self, path, body=None, tok=None, raw=False, headers=None):
        h = {"Content-Type": "application/json", **({"X-DTM-Token": tok} if tok else {}), **(headers or {})}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET", headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                if raw:
                    return r.status, data, dict(r.headers)
                return r.status, (json.loads(data) if "json" in r.headers.get("Content-Type", "") else data)
        except urllib.error.HTTPError as e:
            data = e.read() or b"{}"
            try:
                return e.code, json.loads(data)
            except ValueError:
                return e.code, data

    def login(self, u, p):
        code, j = self.call("/api/login", {"username": u, "password": p})
        assert code == 200, j
        return j["token"]

    def close(self):
        self.httpd.shutdown()


class TestWeb(unittest.TestCase):
    def test_shell_pages_and_security_headers(self):
        w = Web()
        try:
            code, body, h = w.call("/", raw=True)
            self.assertIn(b'id="nav"', body)                  # / 是 DTM 主畫面
            self.assertEqual(h.get("X-Frame-Options"), "SAMEORIGIN")
            self.assertEqual(h.get("X-Content-Type-Options"), "nosniff")
            self.assertIn(b"langSel", w.call("/live", raw=True)[1])
            info = w.call("/api/c/info")[1]
            self.assertEqual((info["role"], info["robots"]), ("standalone", 1))
            self.assertEqual(w.call("/api/sys/me")[1]["mode"], "readonly")
        finally:
            w.close()

    def test_settings_and_users_admin_only(self):
        w = Web()
        us = users(w.cfg)
        us.add("adm", "admin-pw-1", "admin", "管理員")
        us.add("op", "operator-pw-1", "operator", "操作員")
        try:
            adm, op = w.login("adm", "admin-pw-1"), w.login("op", "operator-pw-1")
            self.assertEqual(w.call("/api/sys/settings", {"changes": {"security.idle_minutes": 20}}, op)[0], 403)
            code, j = w.call("/api/sys/settings", {"changes": {"security.idle_minutes": 20}}, adm)
            self.assertEqual(code, 200, j)
            v = w.call("/api/sys/settings", tok=op)[1]
            self.assertFalse(v["can_edit"])
            self.assertTrue(v["notify"].get("hidden"))
            self.assertEqual(w.call("/api/sys/users", tok=op)[0], 403)
            code, j = w.call("/api/sys/users", {"action": "add", "username": "lin", "password": "weak",
                                                "role": "engineer"}, adm)
            self.assertEqual(code, 400)                        # 密碼原則
            code, j = w.call("/api/sys/users", {"action": "add", "username": "lin", "password": "Lin-start-01",
                                                "role": "engineer", "display": "林工程師"}, adm)
            self.assertEqual(code, 200, j)
            lin = w.login("lin", "Lin-start-01")
            self.assertTrue(w.call("/api/sys/me", tok=lin)[1]["session"]["must_change"])
            self.assertEqual(w.call("/api/event", {"kind": "inspection", "axis": 2}, lin)[0], 403)
            code, j = w.call("/api/sys/password", {"old": "Lin-start-01", "new": "Lin-own-pass-2"}, lin)
            self.assertEqual(code, 200, j)
            self.assertEqual(w.call("/api/event", {"kind": "inspection", "axis": 2}, lin)[0], 200)
            rows = w.call("/api/sys/audit?limit=50", tok=adm)[1]["rows"]
            pw_rows = [r for r in rows if r["action"] in ("/api/sys/password", "/api/sys/users")]
            self.assertTrue(pw_rows)
            for r in rows:                                     # 操作紀錄不能有密碼
                self.assertNotIn("Lin-start-01", r["detail"] or "")
                self.assertNotIn("Lin-own-pass-2", r["detail"] or "")
            sess = w.call("/api/sys/sessions?all=1", tok=adm)[1]["rows"]
            self.assertEqual({s["username"] for s in sess}, {"adm", "op", "lin"})
            sid = [s["id"] for s in sess if s["username"] == "op"][0]
            self.assertEqual(w.call("/api/sys/sessions/revoke", {"id": sid}, adm)[0], 200)
            self.assertEqual(w.call("/api/ack", {"id": 1}, op)[0], 401)
            code, j = w.call("/api/sys/users", {"action": "disable", "username": "adm"}, adm)
            self.assertEqual(code, 400)                        # 不能停用自己
        finally:
            w.close()

    def test_allow_ips_blocks_other_computers(self):
        w = Web()
        try:
            SC.get(w.cfg).set({"security.allow_ips": ["10.9.9.0/24"]}, "adm", "127.0.0.1")
            self.assertEqual(w.call("/api/status")[0], 200)                        # 本機一律可以
            tok = os.environ.get("DTM_INTERNAL_TOKEN")
            os.environ["DTM_INTERNAL_TOKEN"] = "itok"
            try:
                code, body = w.call("/api/status", headers={"X-DTM-Client": "192.168.5.5", "X-DTM-Internal": "itok"})
                self.assertEqual(code, 403)                    # 轉送來的請求看原始電腦
                code, _b = w.call("/api/status", headers={"X-DTM-Client": "10.9.9.4", "X-DTM-Internal": "itok"})
                self.assertEqual(code, 200)
                code, _b = w.call("/api/status", headers={"X-DTM-Client": "192.168.5.5", "X-DTM-Internal": "bad"})
                self.assertEqual(code, 200)                    # 金鑰不對：不採用轉送的 IP（仍是本機）
            finally:
                if tok is None:
                    os.environ.pop("DTM_INTERNAL_TOKEN", None)
                else:
                    os.environ["DTM_INTERNAL_TOKEN"] = tok
        finally:
            w.close()

    def test_diag_bundle_is_masked(self):
        cfg = tmp_cfg('[notify]\nsmtp_host = "mail.example.com"\nsmtp_password = "SuperSecret9"\n'
                      'mail_to = ["boss@factory.example"]\n[notify.sms]\nto = ["0912345678"]\n')
        w = Web(cfg)
        users(cfg).add("adm", "admin-pw-1", "admin")
        try:
            adm = w.login("adm", "admin-pw-1")
            code, j = w.call("/api/sys/diag", {}, adm)
            self.assertEqual(code, 200, j)
            code, data, _h = w.call(j["url"], tok=adm, raw=True)
            self.assertEqual(code, 200)
            p = os.path.join(os.path.dirname(cfg["_path"]), "diag.zip")
            with open(p, "wb") as f:
                f.write(data)
            with zipfile.ZipFile(p) as z:
                text = z.read("config.toml").decode()
                self.assertIn("system.json", z.namelist())
            for secret in ("SuperSecret9", "boss@factory.example", "0912345678"):
                self.assertNotIn(secret, text)
        finally:
            w.close()


class TestEdgeOneWindow(unittest.TestCase):
    def test_edge_forwards_robot_pages_with_single_sign_on(self):
        """廠區畫面 /r/<機器人>/… 轉送給那台機器人的程式；登入一次就有效，權限與操作紀錄照原本的電腦。"""
        from dtm.central import CentralStore
        from dtm.fleetweb import serve_fleet
        robot = Web()
        users(robot.cfg).add("eng", "engineer-pw-1", "engineer", "工程師乙")
        users(robot.cfg).add("op", "operator-pw-1", "operator", "操作員甲")
        ecfg = tmp_cfg(system={"role": "edge", "site_id": "P9"})
        ecfg["web"]["users_db"] = robot.cfg["web"]["users_db"]
        cs = CentralStore(os.path.join(os.path.dirname(ecfg["_path"]), "edge.sqlite3"), ecfg, local=True)

        class Child:
            id, port, cfg = "R1", robot.httpd.server_address[1], {"robot": {"name": "R1"}}

        class Sup:
            token, children = "itok-edge", [Child]
        old = os.environ.get("DTM_INTERNAL_TOKEN")
        os.environ["DTM_INTERNAL_TOKEN"] = "itok-edge"          # 機器人程式由監督者啟動時帶的金鑰
        httpd = serve_fleet(ecfg, cs, role="edge", supervisor=Sup(), auth=Auth(ecfg), host="127.0.0.1", port=0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        edge = Web.__new__(Web)
        edge.base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            code, body, _h = edge.call("/r/P9%2FR1/", raw=True)
            self.assertEqual(code, 200)
            self.assertIn(b"embed", body)                                   # 機器人的即時監測頁
            self.assertEqual(edge.call("/r/P9%2FR1/api/status")[1]["robot"], robot.cfg["robot"]["name"])
            self.assertEqual(edge.call("/r/P9%2FR9/live", raw=True)[0], 404)
            tok = edge.login("eng", "engineer-pw-1")                       # 在廠區畫面登入
            code, j = edge.call("/r/P9%2FR1/api/event", {"kind": "inspection", "axis": 2, "note": "x"}, tok)
            self.assertEqual(code, 200, j)                                  # 機器人程式認得同一個登入
            op = edge.login("op", "operator-pw-1")
            self.assertEqual(edge.call("/r/P9%2FR1/api/event", {"kind": "inspection", "axis": 2}, op)[0], 403)
            last = robot.svc.store.audit(5)
            self.assertTrue(any(a["who"] == "工程師乙" and a["action"] == "/api/event" for a in last))
        finally:
            httpd.shutdown()
            robot.close()
            if old is None:
                os.environ.pop("DTM_INTERNAL_TOKEN", None)
            else:
                os.environ["DTM_INTERNAL_TOKEN"] = old


if __name__ == "__main__":
    unittest.main()

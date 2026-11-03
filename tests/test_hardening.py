"""2.1 專業化第一批：HTTPS 一鍵啟用、設定檔寫入、現場驗證報告、第一次設定精靈、匯率換算、SBOM、全頁截圖元件。"""
import http.server
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import tomllib
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
__import__("_cleanup")                  # 測試結束時刪掉暫存資料夾

from test_settings import Web, users  # noqa: E402
from test_v21 import ROOT, tmp_cfg  # noqa: E402

from dtm import config as C  # noqa: E402
from dtm import fx as FX  # noqa: E402
from dtm import settings as ST  # noqa: E402
from dtm import tlscert  # noqa: E402
from dtm import validation as VA  # noqa: E402
from dtm import wizard as WZ  # noqa: E402
from dtm.central import CentralStore, day_of  # noqa: E402

DAY = 86400.0
_CERT = {}


def rd(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


def toml(path):
    return tomllib.loads(rd(path))


def shared_cert():
    """測試共用一張憑證（產生 RSA 金鑰約 1 秒）。"""
    if not _CERT:
        d = tempfile.mkdtemp()
        _CERT.update(tlscert.write(d, ["localhost"], ["127.0.0.1"]))
    return _CERT


# ============================================================ 自簽憑證
class TestTlsCert(unittest.TestCase):
    def test_handshake_and_hostname_check(self):
        c = shared_cert()
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("H", (http.server.BaseHTTPRequestHandler,), {
            "do_GET": lambda self: (self.send_response(200), self.send_header("Content-Length", "2"), self.end_headers(),
                                    self.wfile.write(b"ok")),
            "log_message": lambda *a: None}))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(c["cert"], c["key"])
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            cli = ssl.create_default_context(cafile=c["cert"])       # 匯入成信任的根憑證後，主機名稱驗證通過
            url = f"https://localhost:{srv.server_address[1]}/"
            with urllib.request.urlopen(url, context=cli, timeout=10) as r:
                self.assertEqual(r.read(), b"ok")
            with self.assertRaises(Exception):                         # 沒有匯入：驗證失敗
                urllib.request.urlopen(url, context=ssl.create_default_context(), timeout=10)
        finally:
            srv.shutdown()

    def test_describe_and_key_permissions(self):
        c = shared_cert()
        d = tlscert.describe(c["cert"])
        self.assertEqual(d["sha256"], c["sha256"])
        self.assertAlmostEqual((d["not_after"] - time.time()) / DAY, 825, delta=1)
        if os.name == "posix":
            self.assertEqual(os.stat(c["key"]).st_mode & 0o777, 0o600)
        self.assertTrue(rd(c["key"]).startswith("-----BEGIN RSA PRIVATE KEY-----"))

    def test_der_lengths(self):
        self.assertEqual(tlscert._len(5), b"\x05")
        self.assertEqual(tlscert._len(200), b"\x81\xc8")
        self.assertEqual(tlscert._len(300), b"\x82\x01\x2c")
        self.assertEqual(tlscert._int(128), b"\x02\x02\x00\x80")         # 最高位元 1 要補 0
        self.assertEqual(tlscert._oid("1.2.840.113549.1.1.11"), bytes.fromhex("06092a864886f70d01010b"))


# ============================================================ 設定檔寫入
class TestConfigWriter(unittest.TestCase):
    TEXT = '[system]\nrole = "x"\n\n[web]\nhost = "0.0.0.0"   # 對外\nport = 8080\n\n[web.extra]\na = 1\n'

    def test_set_keys_keeps_comments_and_other_tables(self):
        out = ST.set_keys(self.TEXT, "web", {"port": 9090, "tls_cert": "tls/c.pem"})
        self.assertIn('host = "0.0.0.0"   # 對外', out)
        self.assertIn("port = 9090", out)
        d = tomllib.loads(out)
        self.assertEqual(d["web"]["tls_cert"], "tls/c.pem")
        self.assertEqual(d["web"]["extra"], {"a": 1})
        out2 = ST.set_keys(out, "axis.4", {"rated_nm": 1200.0, "name": 'J4 "腕"\\x'})
        d2 = tomllib.loads(out2)
        self.assertEqual(d2["axis"]["4"], {"rated_nm": 1200.0, "name": 'J4 "腕"\\x'})
        self.assertEqual(tomllib.loads(ST.set_keys(out2, "axes", {"enabled": [2, 3, 7]}))["axes"]["enabled"], [2, 3, 7])

    def test_backups_in_same_second_do_not_overwrite(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "config.toml")
        for i in (1, 2):
            with open(p, "w") as f:
                f.write(f"a = {i}\n")
            b = ST.backup_config(p, 1792300000.0)
            if i == 1:
                b1 = b
        self.assertNotEqual(b1, b)
        self.assertEqual(rd(b1), "a = 1\n")

    def test_config_editable_rules(self):
        cfg = tmp_cfg()
        self.assertTrue(ST.config_editable(cfg, "127.0.0.1")[0])
        self.assertFalse(ST.config_editable(cfg, "10.0.0.5")[0])
        cfg["_demo"] = True
        self.assertFalse(ST.config_editable(cfg, "127.0.0.1")[0])
        self.assertFalse(ST.config_editable(C.load(None), "127.0.0.1")[0])


# ============================================================ HTTPS 一鍵啟用
def https_get(url, timeout=10):
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(url, context=ctx, timeout=timeout) as r:
        return r.status, r.read(), dict(r.headers)


class TestHttps(unittest.TestCase):
    def test_enable_restart_serve_and_disable(self):
        cfg = tmp_cfg()
        info = ST.enable_tls(cfg, "127.0.0.1", ["dtm.plant.local", "10.9.8.7"])
        self.assertIn("dtm.plant.local", info["names"])
        self.assertIn("10.9.8.7", info["ips"])
        raw = toml(cfg["_path"])
        self.assertEqual((raw["web"]["tls_cert"], raw["web"]["tls_key"]), ("tls/dtm-cert.pem", "tls/dtm-key.pem"))
        with self.assertRaises(PermissionError):
            ST.enable_tls(cfg, "10.0.0.5")
        # 「重新啟動」：用寫好的設定檔啟動，網頁改成 HTTPS
        cfg2 = C.load(cfg["_path"])
        for k in ("storage", "web"):
            cfg2[k]["users_db" if k == "web" else "db"] = cfg[k]["users_db" if k == "web" else "db"]
        w = Web(cfg2)
        try:
            base = w.base.replace("http://", "https://")
            code, body, h = https_get(base + "/api/sys/tls/cert.crt")
            self.assertEqual(code, 200)
            self.assertTrue(body.startswith(b"-----BEGIN CERTIFICATE-----"))
            self.assertIn("dtm-cert.crt", h.get("Content-Disposition", ""))
            # 連上卻不送資料（不做 TLS 交握）的連線，不會擋住其他人
            idle = socket.create_connection(("127.0.0.1", w.httpd.server_address[1]))
            try:
                t0 = time.time()
                self.assertEqual(https_get(base + "/api/c/info")[0], 200)
                self.assertLess(time.time() - t0, 5)
            finally:
                idle.close()
        finally:
            w.close()
        ST.disable_tls(cfg, "127.0.0.1")
        self.assertEqual(toml(cfg["_path"])["web"]["tls_cert"], "")

    def test_api_admin_only_and_status(self):
        cfg = tmp_cfg()
        users(cfg).add("adm", "admin-pw-1", "admin")
        users(cfg).add("eng", "engineer-pw-1", "engineer")
        w = Web(cfg)
        try:
            self.assertEqual(w.call("/api/sys/tls", tok=w.login("eng", "engineer-pw-1"))[0], 403)
            tok = w.login("adm", "admin-pw-1")
            code, v = w.call("/api/sys/tls", tok=tok)
            self.assertEqual(code, 200)
            self.assertFalse(v["running"])
            self.assertTrue(v["can_edit"])
            code, r = w.call("/api/sys/tls", {"action": "self_signed", "names": "dtm.local"}, tok=tok)
            self.assertEqual(code, 200, r)
            v = w.call("/api/sys/tls", tok=tok)[1]
            self.assertTrue(v["configured"] and v["restart"] and not v["running"])
            self.assertTrue(v["cert"]["self_signed"])
            self.assertGreater(v["cert"]["days_left"], 800)
            self.assertEqual(w.call("/api/sys/tls", {"action": "bogus"}, tok=tok)[0], 400)
            rows = w.svc.store.audit_rows(10) if hasattr(w.svc.store, "audit_rows") else None
            if rows is not None:
                self.assertTrue(any(r["action"] == "/api/sys/tls" for r in rows))
        finally:
            w.close()

    def test_robot_children_never_inherit_tls(self):
        from dtm.fleet import apply_robot
        cfg = C.load(os.path.join(ROOT, "config.edge.example.toml"))
        cfg["web"].update(tls_cert="tls/dtm-cert.pem", tls_key="tls/dtm-key.pem")
        rid = cfg["robots"][0]["id"]
        child = apply_robot(cfg, rid)
        self.assertEqual((child["web"]["tls_cert"], child["web"]["tls_key"]), ("", ""))


class _Tls(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        b = json.dumps({"path": self.path, "tls": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


class TestProxyHttpsFallback(unittest.TestCase):
    def test_central_reaches_edge_that_switched_to_https(self):
        from dtm.auth import Auth
        from dtm.fleetweb import serve_fleet
        c = shared_cert()
        edge = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Tls)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(c["cert"], c["key"])
        edge.socket = ctx.wrap_socket(edge.socket, server_side=True)
        threading.Thread(target=edge.serve_forever, daemon=True).start()
        d = tempfile.mkdtemp()
        cfg = C.load(os.path.join(ROOT, "config.central.example.toml"))
        cfg["_path"] = os.path.join(d, "config.toml")
        cfg["web"]["users_db"] = os.path.join(d, "users.sqlite3")
        cs = CentralStore(os.path.join(d, "c.sqlite3"), cfg)
        with cs.conn() as cn:
            cn.execute("INSERT INTO sites (site_id, name, info) VALUES ('P1', 'P1', ?)", (json.dumps({"edge_host": "127.0.0.1"}),))
            cn.execute("INSERT INTO robots (rkey, site_id, id, name, port) VALUES ('P1/r01', 'P1', 'r01', 'R01', ?)",
                       (edge.server_address[1],))
        srv = serve_fleet(cfg, cs, role="central", auth=Auth(cfg), host="127.0.0.1", port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/r/P1%2Fr01/api/status"
            with urllib.request.urlopen(url, timeout=20) as r:
                j = json.loads(r.read())
            self.assertEqual(j, {"path": "/api/status", "tls": True})
        finally:
            srv.shutdown()
            edge.shutdown()


# ============================================================ 現場驗證報告
def _central(now):
    d = tempfile.mkdtemp()
    cfg = C.load(os.path.join(ROOT, "config.central.example.toml"))
    cs = CentralStore(os.path.join(d, "c.sqlite3"), cfg)
    put = "INSERT OR REPLACE INTO axis_daily (day,rkey,axis,level,ratio,motion_level,dec_level) VALUES (?,?,?,?,?,-1,0)"

    def days(c, rk, ax, t_end, n, lv_of):
        for k in range(n, 0, -1):
            c.execute(put, (day_of(t_end - k * DAY), rk, ax, lv_of(k), 1.0))

    with cs.conn() as c:
        c.execute("INSERT INTO sites (site_id, name) VALUES ('P1', '一廠')")
        for rk in ("P1/a", "P1/b", "P1/c", "P1/d", "P1/e", "P1/f"):
            c.execute("INSERT INTO robots (rkey, site_id, id, name) VALUES (?, 'P1', ?, ?)", (rk, rk[-1], rk[-1].upper()))
        comp = "INSERT INTO components (rkey,edge_id,axis,comp,install_ts,removed_ts,life_h,failed,reason) VALUES (?,?,?,?,?,?,?,?,?)"
        # a：注意 60 天前、警戒 20 天前 → 失效（警戒偵測，提早 60／20 天）
        ra = now - 200 * DAY
        days(c, "P1/a", 2, ra, 120, lambda k: 2 if k <= 20 else 1 if k <= 60 else 0)
        c.execute(comp, ("P1/a", 1, 2, "reducer", now - 900 * DAY, ra, 15000, 1, "磨耗"))
        c.execute("INSERT INTO events (rkey,edge_id,ts,axis,kind,note) VALUES ('P1/a',1,?,2,'reducer_replace','')", (ra,))
        # b：有資料但一直正常就壞了 → 漏報
        rb = now - 150 * DAY
        days(c, "P1/b", 3, rb, 90, lambda k: 0)
        c.execute(comp, ("P1/b", 2, 3, "reducer", now - 900 * DAY, rb, 14000, 1, "磨耗"))
        # c：壞在開始監測之前 → 資料不足
        c.execute(comp, ("P1/c", 3, 2, "reducer", now - 900 * DAY, now - 500 * DAY, 9000, 1, "磨耗"))
        # d：注意後預防更換，沒有失效 → 警報後更換、未確認失效；計畫性預防更換（沒有警報）不列入
        rd = now - 100 * DAY
        days(c, "P1/d", 4, rd, 60, lambda k: 1 if k <= 25 else 0)
        c.execute(comp, ("P1/d", 4, 4, "gearbox", now - 900 * DAY, rd, 12000, 0, "預防性更換"))
        c.execute(comp, ("P1/d", 5, 5, "gearbox", now - 900 * DAY, rd, 12000, 0, "預防性更換"))
        # e：警戒後沒有處置（>90 天前）→ 誤報候選；另一軸只重設基準 → 誤報候選（非劣化原因）
        days(c, "P1/e", 2, now - 200 * DAY, 5, lambda k: 2)
        days(c, "P1/e", 3, now - 300 * DAY, 4, lambda k: 2)
        c.execute("INSERT INTO events (rkey,edge_id,ts,axis,kind,note) VALUES ('P1/e',9,?,3,'baseline_reset','')",
                  (now - 298 * DAY,))
        # f：最近的警戒（未滿 90 天）→ 觀察中；警戒後換脂 → 有處置
        days(c, "P1/f", 2, now, 10, lambda k: 2)
        days(c, "P1/f", 3, now - 150 * DAY, 3, lambda k: 2)
        c.execute("INSERT INTO events (rkey,edge_id,ts,axis,kind,note) VALUES ('P1/f',10,?,3,'grease_change','')",
                  (now - 140 * DAY,))
    return cs


class TestValidation(unittest.TestCase):
    def test_central_report(self):
        now = time.time()
        r = VA.for_central(_central(now), now=now)
        f = {x["rkey"]: x for x in r["failures"]}
        self.assertEqual(f["P1/a"]["result"], "alert")
        self.assertAlmostEqual(f["P1/a"]["lead_caution"], 60, delta=1)
        self.assertAlmostEqual(f["P1/a"]["lead_alert"], 20, delta=1)
        self.assertEqual(f["P1/b"]["result"], "missed")
        self.assertEqual(f["P1/c"]["result"], "nodata")
        self.assertEqual(f["P1/d"]["result"], "prev_alarm")
        self.assertEqual(sum(1 for x in r["failures"] if x["rkey"] == "P1/d"), 1)   # 沒警報的預防更換不列
        s = r["summary"]
        self.assertEqual((s["failures"], s["evaluable"], s["detected"], s["missed"], s["nodata"]), (3, 2, 1, 1, 1))
        self.assertEqual(s["rate"], 0.5)
        lo, hi = s["rate_ci"]
        self.assertTrue(0 < lo < 0.5 < hi < 1)
        out = {(x["rkey"], x["axis"]): x["outcome"] for x in r["episodes"]}
        self.assertEqual(out[("P1/a", 2)], "confirmed")
        self.assertEqual(out[("P1/e", 2)], "unresolved")
        self.assertEqual(out[("P1/e", 3)], "reset")
        self.assertEqual(out[("P1/f", 2)], "pending")
        self.assertEqual(out[("P1/f", 3)], "maint")
        self.assertEqual(s["false_alarm"], 2)
        self.assertTrue(s["small_sample"])
        html = VA.build_html(r, "測試")
        self.assertIn("<title>", html)
        self.assertIn("一廠", html)
        self.assertIn("@page", html)
        json.loads(VA.to_json(r))

    def test_wilson(self):
        lo, hi = VA.wilson(9, 10)
        self.assertAlmostEqual(lo, 0.596, places=2)
        self.assertAlmostEqual(hi, 0.982, places=2)
        self.assertIsNone(VA.wilson(0, 0))

    def test_standalone_with_teardown_no_fault(self):
        from dtm.store import Store
        cfg = tmp_cfg()
        st = Store(cfg["storage"]["db"])
        now = time.time()
        rem = now - 30 * DAY
        with st.conn() as c:
            for k in range(80, 0, -1):
                c.execute("INSERT INTO features (ts, axis, F, valid) VALUES (?, 2, 1.0, 1)", (rem - k * DAY,))
            c.execute("INSERT INTO alarms (ts, axis, kind, akey, level, message, cleared_ts) VALUES (?, 2, '健康', 'health:2', 1, 'x', ?)",
                      (rem - 40 * DAY, rem - 30 * DAY))
            c.execute("INSERT INTO alarms (ts, axis, kind, akey, level, message, cleared_ts) VALUES (?, 2, '健康', 'health:2', 2, 'x', ?)",
                      (rem - 10 * DAY, rem))
            c.execute("INSERT INTO components (robot, axis, comp, install_ts, removed_ts, life_h, failed, reason) "
                      "VALUES ('R', 2, 'reducer', ?, ?, 9000, 1, '失效')", (now - 800 * DAY, rem))
            c.execute("INSERT INTO components (robot, axis, comp, install_ts, removed_ts, life_h, failed, reason, teardown) "
                      "VALUES ('R', 2, 'reducer', ?, ?, 9000, 0, '失效', ?)",
                      (rem, now - 5 * DAY, json.dumps({"no_fault": True})))
        r = VA.for_store(cfg, st, now=now)
        res = [x["result"] for x in r["failures"]]
        self.assertEqual(res[0], "alert")
        self.assertAlmostEqual(r["failures"][0]["lead_caution"], 40, delta=1)
        self.assertEqual(r["summary"]["evaluable"], 1)

    def test_endpoints(self):
        cfg = tmp_cfg()
        w = Web(cfg)
        try:
            code, body = w.call("/api/report/validation")
            self.assertEqual(code, 200)
            self.assertIn("<h1>", body.decode())
            code, j = w.call("/api/report/validation.json?days=365")
            self.assertEqual((code, j["days"]), (200, 365))
        finally:
            w.close()


# ============================================================ 第一次設定精靈
class TestWizard(unittest.TestCase):
    FORM = {"name": "R-2000iC #3", "line": "L2", "source": "fanuc_http", "host": "192.168.10.23", "poll_hz": 4,
            "axes": [2, 3, 7], "rated": {"2": "4900", "3": "", "7": "1200", "5": "99"}}

    def test_normalize_errors(self):
        for bad, msg in (({"source": "ftp"}, "資料來源"), ({"host": "http://x"}, "IP"), ({"axes": []}, "軸"),
                         ({"poll_hz": 100}, "0.5"), ({"rated": {"2": "abc"}}, "數字")):
            with self.assertRaises(ValueError) as e:
                WZ.normalize(dict(self.FORM, **bad))
            self.assertIn(msg, str(e.exception))
        v = WZ.normalize(self.FORM)
        self.assertEqual(v["rated"], {2: 4900.0, 7: 1200.0})               # 空白不改、沒選的軸不寫

    def test_save_keeps_rest_of_file_and_needs_local(self):
        cfg = tmp_cfg()
        before = rd(cfg["_path"])
        with self.assertRaises(PermissionError):
            WZ.save(cfg, "10.0.0.8", self.FORM)
        r = WZ.save(cfg, "127.0.0.1", self.FORM)
        self.assertTrue(r["ok"])
        raw = toml(cfg["_path"])
        self.assertEqual(raw["robot"]["source"], "fanuc_http")
        self.assertEqual(raw["robot"]["name"], "R-2000iC #3")
        self.assertEqual(raw["source"]["fanuc_http"]["host"], "192.168.10.23")
        self.assertEqual(raw["axes"]["enabled"], [2, 3, 7])
        self.assertEqual(raw["axis"]["7"]["rated_nm"], 1200.0)
        comments = [ln.strip() for ln in before.splitlines() if ln.strip().startswith("#")]
        self.assertEqual(comments, [ln.strip() for ln in rd(cfg["_path"]).splitlines() if ln.strip().startswith("#")])
        self.assertTrue(cfg["_setup_pending"])
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(cfg["_path"]), "config_backups")))

    def test_api_test_with_simulator_and_setup_hint(self):
        cfg = tmp_cfg()
        users(cfg).add("adm", "admin-pw-1", "admin")
        w = Web(cfg)
        try:
            tok = w.login("adm", "admin-pw-1")
            self.assertTrue(w.call("/api/c/info", tok=tok)[1]["setup_hint"])         # 還在用模擬器
            v = w.call("/api/sys/wizard", tok=tok)[1]
            self.assertTrue(v["can_edit"])
            self.assertEqual(v["values"]["source"], "sim")
            code, r = w.call("/api/sys/wizard", dict(self.FORM, action="test", source="sim"), tok=tok)
            self.assertEqual(code, 200, r)
            self.assertTrue(r["ok"], r)
            code, r = w.call("/api/sys/wizard", dict(self.FORM, action="save"), tok=tok)
            self.assertEqual(code, 200, r)
            self.assertTrue(w.call("/api/c/info", tok=tok)[1]["setup_pending"])
        finally:
            w.close()


# ============================================================ 匯率換算
class TestFx(unittest.TestCase):
    def _cfg(self, **m):
        cfg = tmp_cfg()
        cfg["maint"].update(currency="TWD", **m)
        return cfg

    def test_auto_fallback_cache_and_stale(self):
        calls = []

        def fetch(url, proxy="", timeout=10):
            calls.append(url)
            if "er-api" in url:
                raise OSError("down")
            return {"date": time.strftime("%Y-%m-%d"), "twd": {"usd": 0.031, "jpy": 4.7}}
        cfg = self._cfg(fx="auto")
        fx = FX.FX(cfg, fetch=fetch)
        fx.refresh(wait=True)
        self.assertEqual(len(calls), 2)                                        # 第一個來源失敗，改用第二個
        v = fx.view("en")
        self.assertEqual((v["display"], v["rate"], v["source"]), ("USD", 0.031, "currency-api"))
        self.assertFalse(v["stale"])
        self.assertEqual(fx.view("zh-TW")["display"], "TWD")
        self.assertIn("KRW", fx.view("ko")["error"])                           # 匯率表沒有 KRW
        fx2 = FX.FX(cfg, fetch=lambda *a, **k: self.fail("不應該再抓"))       # 快取：新的實例直接讀檔
        self.assertEqual(fx2.view("ja")["rate"], 4.7)
        fx2.data["updated"] = time.time() - 10 * DAY
        self.assertTrue(fx2.view("en")["stale"])

    def test_all_sources_fail_and_manual_and_off(self):
        def boom(*a, **k):
            raise OSError("offline")
        fx = FX.FX(self._cfg(fx="auto"), fetch=boom)
        fx.refresh(wait=True)
        v = fx.view("en")
        self.assertEqual((v["display"], v["rate"]), ("TWD", 1.0))
        self.assertIn("ExchangeRate-API", v["error"])
        fx = FX.FX(self._cfg(fx="manual", fx_rates={"usd": 0.03}), fetch=boom)
        self.assertEqual(fx.view("en")["rate"], 0.03)
        self.assertEqual(fx.view("en")["display"], "USD")
        fx = FX.FX(self._cfg(fx="off"), fetch=boom)
        self.assertEqual(fx.view("en")["display"], "TWD")
        self.assertEqual(FX.outbound(self._cfg(fx="off")), [])


# ============================================================ SBOM、全頁截圖元件
class TestSupplyChain(unittest.TestCase):
    def test_sbom(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import sbom
        b = sbom.build()
        self.assertEqual((b["bomFormat"], b["specVersion"]), ("CycloneDX", "1.5"))
        names = {c["name"]: c for c in b["components"]}
        self.assertEqual(set(names), {"pymodbus", "pyserial", "html2canvas", "WinSW", "python"})
        import hashlib
        h = hashlib.sha256(rd(os.path.join(ROOT, "dtm", "static", "vendor", "html2canvas.min.js"), "rb")).hexdigest()
        self.assertEqual(names["html2canvas"]["hashes"][0]["content"], h)
        self.assertEqual(sbom.build()["serialNumber"], b["serialNumber"])        # 同一版本固定

    def test_screenshot_component_served_without_login(self):
        w = Web()
        try:
            code, body, h = w.call("/vendor/html2canvas.min.js", raw=True)
            self.assertEqual(code, 200)
            self.assertIn("javascript", h.get("Content-Type", ""))
            self.assertIn(b"html2canvas", body[:400])
            self.assertIn(b'id="shotBtn"', w.call("/", raw=True)[1])
            self.assertIn(b'id="shotBtn"', w.call("/live", raw=True)[1])
            self.assertIn(b'id="clk"', w.call("/pro", raw=True)[1])
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()

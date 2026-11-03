"""1.5 版：事件撤銷、寫入權限與操作紀錄、資料庫備份、設定檔檢查、不該報警的情境。"""
import json
import math
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
__import__("_cleanup")                  # 測試結束時刪掉暫存資料夾

from dtm import config as C  # noqa: E402
from dtm.auth import MAX_FAILS, WriteAuth  # noqa: E402
from dtm.service import Service  # noqa: E402
from dtm.store import Store  # noqa: E402
from dtm.web import serve  # noqa: E402

DAY = 86400.0


def cfg_tmp(**web):
    cfg = C.load(os.path.join(ROOT, "config.sim.toml"))
    d = tempfile.mkdtemp()
    cfg["storage"]["db"] = os.path.join(d, "t.sqlite3")
    cfg["web"]["users_db"] = os.path.join(d, "users.sqlite3")
    cfg["web"].update(web)
    return cfg


def snapshot(store):
    comps = [(c["axis"], c["comp"], c["install_ts"], c["removed_ts"], c["life_h"], c["failed"], c["reason"])
             for c in store.components()]
    return comps, [(h["ts"], h["hours"]) for h in store.hours()]


class TestUndo(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_tmp()
        self.svc = Service(self.cfg, notify=False)
        self.t0 = time.time() - 300 * DAY
        for ax in self.svc.axes:
            self.svc.store.install(ax, "reducer", self.t0, "R", "init")
            self.svc.store.install(ax, "grease", self.t0, "R", "init")
            self.svc.store.install(ax, "motor", self.t0, "R", "init")

    def test_undo_restores_components_hours_and_baseline(self):
        st = self.svc.store
        before = snapshot(st)
        reset_before = st.last_reset(3)
        eid = self.svc.register_event(self.t0 + 100 * DAY, 3, "reducer_replace", "錯登", failed=True,
                                      teardown="剝離", hours=12345, who="王小明")
        self.assertGreater(st.last_reset(3), reset_before)
        self.assertNotEqual(snapshot(st), before)
        ev = self.svc.undo_event(eid, "李大華", "軸別選錯")
        self.assertEqual(snapshot(st), before)                       # 元件與時數完全還原
        self.assertEqual(st.last_reset(3), reset_before)             # 基準回到登錄前
        self.assertTrue(ev["undone_ts"])
        self.assertEqual((ev["who"], ev["undo_who"], ev["undo_note"]), ("王小明", "李大華", "軸別選錯"))
        self.assertNotIn(eid, [e["id"] for e in st.events()])
        self.assertIn(eid, [e["id"] for e in st.events(include_undone=True)])
        with self.assertRaises(ValueError):
            self.svc.undo_event(eid)                                 # 不能撤銷兩次
        with self.assertRaises(ValueError):
            self.svc.undo_event(99999)

    def test_later_replacement_must_be_undone_first(self):
        st = self.svc.store
        before = snapshot(st)
        e1 = self.svc.register_event(self.t0 + 50 * DAY, 2, "grease_change", "換脂")
        e2 = self.svc.register_event(self.t0 + 80 * DAY, 2, "reducer_replace", "換減速機")   # 也換掉 e1 裝的油脂
        with self.assertRaises(ValueError) as cm:
            self.svc.undo_event(e1)
        self.assertIn(f"#{e2}", str(cm.exception))
        self.svc.undo_event(e2)
        self.svc.undo_event(e1)
        self.assertEqual(snapshot(st), before)

    def test_all_axes_event_and_baseline_reset(self):
        st = self.svc.store
        before = snapshot(st)
        eid = self.svc.register_event(self.t0 + 10 * DAY, None, "grease_change", "全軸換脂")
        self.assertEqual(len([c for c in st.components("grease") if c["removed_ts"] is None]), len(self.svc.axes))
        self.svc.undo_event(eid)
        self.assertEqual(snapshot(st), before)
        rid = self.svc.register_event(self.t0 + 20 * DAY, 3, "baseline_reset")
        self.assertEqual(st.last_reset(3), self.t0 + 20 * DAY)
        self.svc.undo_event(rid)
        self.assertEqual(st.last_reset(3), 0.0)

    def test_undo_brings_back_health_after_mistaken_reset(self):
        """模擬資料跑完後撤銷 J2 第 120 天的換脂：J2 的基準期與油脂紀錄回到換脂前。"""
        from dtm.service import simulate_history
        from dtm.sources import SIM_GREASE_DAY
        cfg = cfg_tmp()
        svc = simulate_history(cfg, days=SIM_GREASE_DAY + 20)
        g = [e for e in svc.store.events(2) if e["kind"] == "grease_change"][0]
        n_after = svc.health[2]["n_runs"]
        svc.undo_event(g["id"], "test")
        self.assertEqual(svc.store.last_reset(2), 0.0)
        self.assertGreater(svc.health[2]["n_runs"], n_after)          # 換脂前的診斷又算進來
        cur = svc.store.current_component(2, "grease")
        self.assertLess(cur["install_ts"], g["ts"])


class TestMigration(unittest.TestCase):
    def test_old_database_is_upgraded_in_place(self):
        path = os.path.join(tempfile.mkdtemp(), "old.sqlite3")
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, axis INTEGER, kind TEXT, note TEXT);
            CREATE TABLE alarms (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, axis INTEGER, kind TEXT, akey TEXT,
              level INTEGER, message TEXT, value REAL, acked INTEGER DEFAULT 0, cleared_ts REAL);
            INSERT INTO events (ts,axis,kind,note) VALUES (1000, 2, 'grease_change', 'old');
            INSERT INTO events (ts,axis,kind,note) VALUES (2000, 3, 'baseline_reset', 'old');""")
        c.commit()
        c.close()
        cfg = cfg_tmp()
        cfg["storage"]["db"] = path
        svc = Service(cfg, notify=False)
        c = sqlite3.connect(path)
        cols = {r[1] for r in c.execute("PRAGMA table_info(events)")}
        c.close()
        self.assertTrue({"who", "detail", "undone_ts", "undo_who", "undo_note"} <= cols)
        self.assertEqual(len(svc.store.events()), 2)                  # 舊資料還在
        with self.assertRaises(ValueError):
            svc.undo_event(1)                                       # 舊版換脂沒有變動紀錄 → 拒絕，不亂改
        svc.undo_event(2)                                           # 重設基準沒有元件，可以撤銷
        self.assertEqual(svc.store.last_reset(3), 0.0)


class TestWriteAuth(unittest.TestCase):
    def test_password_lockout_and_expiry(self):
        a = WriteAuth({"web": {"write_password": "s3cret", "session_hours": 1}})
        self.assertTrue(a.enabled)
        self.assertEqual(a.login("1.2.3.4", "", "s3cret")[2], 400)       # 沒填姓名
        t0 = 1_000_000.0
        for i in range(MAX_FAILS):
            self.assertEqual(a.login("1.2.3.4", "王", "x", now=t0 + i)[2], 403)
        self.assertEqual(a.login("1.2.3.4", "王", "s3cret", now=t0 + 10)[2], 429)   # 暫停中，密碼對也不行
        self.assertEqual(a.login("5.6.7.8", "王", "s3cret", now=t0 + 10)[2], 200)   # 別台電腦不受影響
        tok, _err, code = a.login("1.2.3.4", "王 小明", "s3cret", now=t0 + 700)
        self.assertEqual(code, 200)
        self.assertEqual(a.who(tok, now=t0 + 800), "王 小明")
        self.assertIsNone(a.who(tok, now=t0 + 700 + 3601))             # 過期
        self.assertIsNone(a.who("bogus"))

    def test_env_password_overrides_config(self):
        os.environ["DTM_WRITE_PASSWORD"] = "fromenv"
        try:
            a = WriteAuth({"web": {"write_password": "fromfile"}})
            self.assertEqual(a.login("h", "w", "fromenv")[2], 200)
        finally:
            del os.environ["DTM_WRITE_PASSWORD"]
        os.environ["DTM_PW_X"] = "indirect"
        try:
            a = WriteAuth({"web": {"write_password": "env:DTM_PW_X"}})
            self.assertEqual(a.login("h", "w", "indirect")[2], 200)
        finally:
            del os.environ["DTM_PW_X"]
        self.assertFalse(WriteAuth({"web": {}}).enabled)


class Web:
    def start(self, cfg):
        self.svc = Service(cfg, notify=False)
        self.httpd = serve(self.svc, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, path, body=None, token=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["X-DTM-Token"] = token
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


class TestWebWrites(unittest.TestCase):
    def test_writes_need_login_and_are_audited(self):
        w = Web()
        w.start(cfg_tmp(write_password="pw"))
        try:
            code, st = w.call("/api/status")
            self.assertTrue(st["write_auth"])
            self.assertTrue(st["local_only"])
            for path, body in (("/api/event", {"axis": 3, "kind": "inspection"}), ("/api/ack", {"id": 1}),
                               ("/api/iron", {"axis": 3, "wt_pct": 0.05}), ("/api/hours", {"hours": 1}),
                               ("/api/vibration", {"axis": 3, "rms": 0.1}), ("/api/event/undo", {"id": 1}),
                               ("/api/notify/test", {})):
                self.assertEqual(w.call(path, body)[0], 401, path)
            self.assertEqual(w.call("/api/login", {"who": "王小明", "password": "no"})[0], 403)
            code, j = w.call("/api/login", {"who": "王小明", "password": "pw"})
            self.assertEqual(code, 200)
            tok = j["token"]
            code, j = w.call("/api/event", {"axis": 3, "kind": "baseline_reset", "note": "測試"}, tok)
            self.assertEqual(code, 200)
            eid = j["id"]
            code, j = w.call("/api/event/undo", {"id": eid, "note": "誤按"}, tok)
            self.assertEqual(code, 200)
            code, j = w.call("/api/event/undo", {"id": eid}, tok)
            self.assertEqual(code, 400)                              # 已撤銷過 → 400 與中文說明
            self.assertIn("撤銷", j["error"])
            _c, evs = w.call("/api/events")
            ev = [e for e in evs if e["id"] == eid][0]
            self.assertEqual((ev["who"], ev["undo_who"], ev["undo_note"]), ("王小明", "王小明", "誤按"))
            self.assertNotIn("detail", ev)
            _c, audit = w.call("/api/audit")
            acts = [(a["action"], a["who"], a["ok"]) for a in audit]
            self.assertIn(("/api/event", "王小明", 1), acts)
            self.assertIn(("/api/event/undo", "", 0), acts)          # 未登入的嘗試也有紀錄
            self.assertIn(("login", "王小明", 0), acts)
            self.assertFalse(any("pw" in (a["detail"] or "") for a in audit if a["action"] == "login"))
            w.call("/api/logout", {}, tok)
            self.assertEqual(w.call("/api/event", {"axis": 3, "kind": "inspection"}, tok)[0], 401)
        finally:
            w.stop()

    def test_no_account_is_readonly_until_local_setup(self):
        w = Web()
        w.start(cfg_tmp(write_password=""))
        try:
            st = w.call("/api/status")[1]
            self.assertEqual(st["auth_mode"], "readonly")
            self.assertTrue(st["setup_needed"])
            self.assertTrue(st["setup_local"])
            code, j = w.call("/api/event", {"axis": 3, "kind": "inspection"})
            self.assertEqual((code, j["auth"]), (403, "setup"))             # 2.1：沒有帳號一律唯讀
            self.assertEqual(w.call("/api/login", {"who": "阿明"})[0], 403)
            self.assertEqual(w.call("/api/setup", {"username": "ming", "password": "short"})[0], 400)
            code, j = w.call("/api/setup", {"username": "ming", "display": "阿明", "password": "long-enough-1"})
            self.assertEqual(code, 200, j)
            self.assertEqual(j["role"], "admin")
            self.assertEqual(w.call("/api/event", {"axis": 3, "kind": "inspection"}, j["token"])[0], 200)
            self.assertEqual(w.svc.store.events()[-1]["who"], "阿明")
            self.assertEqual(w.call("/api/setup", {"username": "x2", "password": "long-enough-2"})[0], 400)
            self.assertEqual(w.call("/api/status")[1]["auth_mode"], "accounts")
        finally:
            w.stop()

    def test_setup_refused_from_other_computers(self):
        from dtm.auth import Auth
        from dtm.web import first_admin
        cfg = cfg_tmp(write_password="")
        logs = []
        obj, code = first_admin(Auth(cfg), "10.1.2.3", {"username": "a", "password": "long-enough-1"},
                                lambda *a: logs.append(a))
        self.assertEqual(code, 403)
        self.assertFalse(logs[-1][3])


class TestBackup(unittest.TestCase):
    def setUp(self):
        from dtm.model import Sample
        self.cfg = cfg_tmp()
        self.cfg["storage"]["backup_keep"] = 3
        self.svc = Service(self.cfg, notify=False)
        self.svc.store.add_samples([Sample(ts=time.time(), dt={1: 1.0, 2: 2.0}) for _ in range(50)])
        self.svc.register_event(time.time() - DAY, 3, "inspection", "備份前")

    def test_backup_skips_raw_samples_and_prunes(self):
        from dtm import backup as B
        import zipfile
        t = datetime(2026, 10, 1, 3, 0).timestamp()
        paths = [B.backup_now(self.cfg, now=t + i * DAY) for i in range(5)]
        left = B.list_backups(self.cfg)
        self.assertEqual([b["path"] for b in left], paths[:1:-1])          # 只留最新 3 份，新到舊
        with zipfile.ZipFile(paths[-1]) as z, tempfile.TemporaryDirectory() as tmp:
            z.extractall(tmp)
            c = sqlite3.connect(os.path.join(tmp, "t.sqlite3"))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT note FROM events").fetchone()[0], "備份前")
            c.close()

    def test_due(self):
        from dtm import backup as B
        self.assertTrue(B.due(self.cfg))                                  # 從沒備份過
        t = datetime(2026, 10, 1, 3, 5).timestamp()
        B.backup_now(self.cfg, now=t)
        self.assertFalse(B.due(self.cfg, now=t + 3600))                  # 同一天
        self.assertFalse(B.due(self.cfg, now=datetime(2026, 10, 2, 2, 0).timestamp()))   # 隔天還沒到 3 點
        self.assertTrue(B.due(self.cfg, now=datetime(2026, 10, 2, 3, 1).timestamp()))
        self.assertTrue(B.due(self.cfg, now=t + 27 * 3600))               # 超過 26 小時（電腦夜間關機）
        self.cfg["storage"]["backup_dir"] = ""
        self.assertFalse(B.due(self.cfg))

    def test_restore_round_trip_and_rejects_foreign_file(self):
        from dtm import backup as B
        path = B.backup_now(self.cfg)
        self.svc.register_event(time.time(), 3, "other", "備份後才登錄")
        saved = B.restore(self.cfg, path)
        st = Store(self.cfg["storage"]["db"])
        self.assertEqual([e["note"] for e in st.events()], ["備份前"])
        self.assertEqual([e["note"] for e in Store(saved).events()], ["備份前", "備份後才登錄"])
        junk = os.path.join(tempfile.mkdtemp(), "junk.sqlite3")
        c = sqlite3.connect(junk)
        c.execute("CREATE TABLE x (a)")
        c.commit()
        c.close()
        with self.assertRaises(ValueError):
            B.restore(self.cfg, junk)
        self.assertEqual([e["note"] for e in Store(self.cfg["storage"]["db"]).events()], ["備份前"])


class TestCheck(unittest.TestCase):
    def write(self, text: bytes | str) -> str:
        path = os.path.join(tempfile.mkdtemp(), "config.toml")
        with open(path, "wb") as f:
            f.write(text.encode("utf-8") if isinstance(text, str) else text)
        return path

    def run_check(self, text, **kw):
        from dtm.check import run_check
        rep = run_check(self.write(text), **kw)
        return rep, "\n".join(rep.lines())

    def test_bundled_configs_have_no_errors(self):
        from dtm.check import ERROR, run_check
        for name in ("config.sim.toml", "config.example.toml"):
            rep = run_check(os.path.join(ROOT, name))
            self.assertEqual(rep.count(ERROR), 0, "\n".join(rep.lines()))

    def test_common_mistakes(self):
        from dtm.check import ERROR
        rep, out = self.run_check('[robot]\npoll_hz = "4"\n[web]\npasswrod = "x"\n[health]\ncaution_ratio = 2.5\n'
                                  '[registres]\nseg = 1\n[axis.2]\nreducer_typ = "rv"\n')
        self.assertIn('把 "4" 改成 4', out)
        self.assertIn("write_password", out)                       # 拼錯 → 建議正確名稱
        self.assertIn("registers", out)
        self.assertIn("reducer_type", out)
        self.assertIn("caution_ratio（2.5）要小於 alert_ratio", out)   # 型別錯了，其他數值照樣檢查
        self.assertEqual(rep.count(ERROR), 2)
        _r, out = self.run_check("[robot]\nname = R1\n")
        self.assertIn("第 2 行", out)
        self.assertIn("雙引號", out)
        _r, out = self.run_check("[web]\nport ＝ 8080\n")
        self.assertIn("全形", out)
        _r, out = self.run_check("[robot]\nname = \"中文\"\n".encode("cp950"))
        self.assertIn("UTF-8", out)
        _r, out = self.run_check('[robot]\nsource = "fanuc_http"\n[registers]\nseg = 102\n')
        self.assertIn("R[102] 跟 J2", out)
        _r, out = self.run_check('[notify]\nwebhook_url = "env:NO_SUCH_VAR_X"\n')
        self.assertIn("NO_SUCH_VAR_X", out)
        for ok in ('"auto"', "true", "false"):
            self.assertEqual(self.run_check(f"[health]\ncommon_mode = {ok}\n")[0].count(ERROR), 0, ok)
        self.assertIn("common_mode", self.run_check('[health]\ncommon_mode = "yes"\n')[1])

    def test_bom_is_accepted_everywhere(self):
        from dtm.check import ERROR
        path = self.write(b'\xef\xbb\xbf[robot]\nname = "BOM-01"\n')
        self.assertEqual(C.load(path)["robot"]["name"], "BOM-01")
        rep, _out = self.run_check(b'\xef\xbb\xbf[robot]\nname = "BOM-01"\n')
        self.assertEqual(rep.count(ERROR), 0)

    def test_connection_test_finds_dead_bg_logic(self):
        """假的 Fanuc 網頁伺服器：心跳不動、J2 扭矩一直 0、J3 正常。"""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from dtm.check import ERROR, OK, WARN
        state = {"n": 0}

        class Fake(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                state["n"] += 1
                body = (f"  [101] = 0  ''\n  [102] = 0  ''\n  [103] = {10 + state['n'] % 7}  ''\n"
                        f"  [110] = 0  ''\n  [111] = 0  ''\n  [112] = 77  ''\n  [113] = 50  ''\n").encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            text = (f'[robot]\nsource = "fanuc_http"\npoll_hz = 10\n[source.fanuc_http]\n'
                    f'host = "127.0.0.1:{srv.server_address[1]}"\n[web]\nhost = "127.0.0.1"\n')
            rep, out = self.run_check(text, connect=True, seconds=1.5, out=lambda *a: None)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertIn("BG Logic 沒有執行", out)
        self.assertIn("$CUR_DIS_TRQ[2]", out)
        self.assertIn("倍率 50%", out)
        self.assertTrue(any(lv == OK and w == "J3" for lv, w, _m in rep.items))
        self.assertGreaterEqual(rep.count(ERROR), 1)
        self.assertGreaterEqual(rep.count(WARN), 1)

        rep, out = self.run_check('[robot]\nsource = "fanuc_http"\n[source.fanuc_http]\nhost = "127.0.0.1:9"\n'
                                  'timeout_s = 0.3\n[web]\nhost = "127.0.0.1"\n', connect=True, seconds=1.0,
                                  out=lambda *a: None)
        self.assertIn("讀不到資料", out)
        self.assertIn("NUMREG.VA", out)


class TestTemperatureAndCommonMode(unittest.TestCase):
    def feats(self, svc, ax, ts, F, Fc, Fv, temp=None):
        from dtm.model import RunFeatures
        svc.store.add_features([RunFeatures(ts=ts, axis=ax, F=F, Fc=Fc, Fv=Fv, G=0.0, ripple=3.0, peak=0.0,
                                            temp=temp, valid=True, detail={})])

    def cold(self, i):
        """共同的「變冷」：第 i 次比第 0 次冷 0.6·i °C；黏性摩擦每 °C +2.5%、固定摩擦 +0.4%（和預設係數一致）。"""
        dT = -0.6 * i
        return math.exp(0.025 * -dT), math.exp(0.004 * -dT)

    def test_common_index_follows_shared_change_and_ignores_peer_maintenance(self):
        from dtm.analysis import common_index, load_rows
        cfg = cfg_tmp()
        svc = Service(cfg, notify=False)
        t0 = time.time() - 100 * DAY
        for i in range(40):
            kv, kc = self.cold(i)
            for ax in (2, 3, 4):
                step = 0.6 if (ax == 4 and i >= 20) else 1.0       # J4 第 20 次換脂後黏性摩擦掉 40%
                self.feats(svc, ax, t0 + i * DAY + ax * 120, 60 * kc + 20 * kv * step, 60 * kc, 2 * kv * step)
        svc.store.add_event(t0 + 19.5 * DAY, 4, "grease_change")
        idx = common_index(svc.store, cfg, 2)
        rows = svc.store.feature_values(2)
        self.assertAlmostEqual(idx[rows[-1]["id"]]["Fv"], self.cold(39)[0], places=3)   # 不受 J4 換脂影響
        self.assertAlmostEqual(idx[rows[-1]["id"]]["Fc"], self.cold(39)[1], places=3)
        for m in ("Fv", "Fc", "F"):
            adj = [r[m] / r["cm"][m] for r in load_rows(svc.store, cfg, 2)]
            self.assertLess(max(adj) / min(adj), 1.01, m)                        # 扣完之後 J2 是平的
        cfg["health"]["common_mode"] = False
        self.assertEqual(common_index(svc.store, cfg, 2), {})

    def test_single_axis_change_is_not_removed(self):
        from dtm.analysis import evaluate_axis, load_rows
        cfg = cfg_tmp()
        svc = Service(cfg, notify=False)
        t0 = time.time() - 100 * DAY
        for i in range(40):
            kv, kc = self.cold(i)
            for ax in (2, 3, 4):
                own = 1.0 + (0.03 * (i - 15) if ax == 2 and i > 15 else 0.0)   # 只有 J2 的黏性摩擦再往上
                wear = 1.0 + (0.02 * (i - 15) if ax == 3 and i > 15 else 0.0)  # 只有 J3 的固定摩擦往上
                self.feats(svc, ax, t0 + i * DAY + ax * 120, 60 * kc * wear + 20 * kv * own, 60 * kc * wear, 2 * kv * own)
        res = evaluate_axis(load_rows(svc.store, cfg, 2), cfg)
        self.assertGreater(res["metrics"]["Fv"]["ratio"], 1.6)
        self.assertTrue(res["common_mode"])
        self.assertIsNone(res["temp_mode"])
        self.assertGreaterEqual(res["level"], 1)
        res3 = evaluate_axis(load_rows(svc.store, cfg, 3), cfg)
        self.assertGreater(res3["metrics"]["Fc"]["ratio"], 1.4)                  # 磨耗不會被扣掉
        self.assertLess(res3["metrics"]["Fv"]["ratio"], 1.1)
        self.assertEqual(evaluate_axis(load_rows(svc.store, cfg, 4), cfg)["level"], 0)

    def test_auto_mode_skips_common_mode_when_temperature_is_available(self):
        """common_mode = "auto" 且有溫度訊號時只做溫度補償：所有軸同時劣化（同一天換的油脂一起老化）才不會被扣掉。"""
        from dtm.analysis import common_index, evaluate_axis, load_rows
        cfg = cfg_tmp()
        cfg["health"]["common_mode"] = "auto"
        svc = Service(cfg, notify=False)
        t0 = time.time() - 100 * DAY
        for i in range(40):
            age = 1.0 + (0.03 * (i - 15) if i > 15 else 0.0)            # 三軸一起老化，溫度不變
            for ax in (2, 3, 4):
                self.feats(svc, ax, t0 + i * DAY + ax * 120, 60 + 20 * age, 60, 2 * age, temp=35.0)
        self.assertEqual(common_index(svc.store, cfg, 2), {})              # auto + 有溫度 → 不扣
        res = evaluate_axis(load_rows(svc.store, cfg, 2), cfg)
        self.assertGreater(res["metrics"]["Fv"]["ratio"], 1.6)
        self.assertFalse(res["common_mode"])
        cfg["health"]["common_mode"] = True                                # 預設：一律扣（這種情況會被扣掉，是已知代價）
        self.assertTrue(common_index(svc.store, cfg, 2))
        self.assertLess(evaluate_axis(load_rows(svc.store, cfg, 2), cfg)["metrics"]["Fv"]["ratio"], 1.1)

    def test_estimate_temp_coef_recovers_known_value(self):
        import math
        import random
        from dtm.analysis import estimate_temp_coef
        rng = random.Random(3)
        rows = []
        for i in range(120):
            T = 30 + 10 * math.sin(i / 120 * math.pi) + rng.uniform(-3, 3)
            rows.append({"ts": i * DAY, "temp": T, "Fv": 2.0 * math.exp(-0.033 * (T - 30)) * (1 + 0.001 * i)
                         * (1 + rng.gauss(0, 0.01)), "Fc": 60 * math.exp(-0.004 * (T - 30))})
        est = estimate_temp_coef(rows)
        self.assertTrue(est["Fv"]["ok"])
        self.assertAlmostEqual(est["Fv"]["k"], 0.033, delta=0.003)
        self.assertFalse(estimate_temp_coef(rows[:10])["Fv"]["ok"])            # 資料不足


class TestScenarios(unittest.TestCase):
    """模擬一年中的干擾；每個情境約 20–30 秒。"""

    @classmethod
    def setUpClass(cls):
        import logging
        logging.disable(logging.WARNING)

    @classmethod
    def tearDownClass(cls):
        import logging
        logging.disable(logging.NOTSET)

    def run_sc(self, key, days, **health):
        from dtm import scenarios as S
        cfg = C.load(os.path.join(ROOT, "config.sim.toml"))
        cfg["health"].update(health)
        return S.run(cfg, S.BY_KEY[key], days)

    def test_cold_start_without_sensor_no_false_alarm(self):
        r = self.run_sc("cold_start_nosensor", 200)
        self.assertEqual(r["false"], 0, r)
        legacy = self.run_sc("cold_start_nosensor", 200, temp_coef_fv=0.0, temp_coef_fc=0.0, common_mode=False)
        self.assertGreater(legacy["false"], 0)          # 1.4.1 的作法在這個情境會誤報：確認情境真的有壓力

    def test_seasonal_with_sensor_no_false_alarm(self):
        self.assertEqual(self.run_sc("seasonal", 240)["false"], 0)

    def test_grease_fault_still_detected_without_sensor(self):
        r = self.run_sc("grease_nosensor", 330)
        self.assertEqual(r["missed"], [], r)
        self.assertEqual(r["false"], 0, r)
        self.assertIn("grease", r["axes"][2]["decision_keys"])


if __name__ == "__main__":
    unittest.main()

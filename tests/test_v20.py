"""2.0 版：E1 行走軸、生產循環監測、感測器、看門狗、自我檢查、帳號與角色、通知中心、
邊緣主機（多台）、邊緣 → 中央同步、中央 8 個畫面的 API、演示資料、服務安裝檔。"""
import gzip
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
__import__("_cleanup")                  # 測試結束時刪掉暫存資料夾

from dtm import config as C  # noqa: E402
from dtm import runtime as R  # noqa: E402
from dtm import model as Mdl  # noqa: E402

DAY = 86400.0


def tmp_cfg(path=None, **over):
    """設定檔放在暫存資料夾（run/、logs/、data/ 都會建在那裡）。"""
    d = tempfile.mkdtemp()
    cfg = C.load(path or os.path.join(ROOT, "config.sim.toml"))
    cfg["_path"] = os.path.join(d, "config.toml")
    cfg["storage"]["db"] = os.path.join(d, "t.sqlite3")
    cfg["storage"]["backup_dir"] = ""
    for k, v in over.items():
        cfg[k] = C._merge(cfg.get(k, {}), v) if isinstance(v, dict) else v
    return cfg


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def http(method, url, body=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}") if "json" in r.headers.get("Content-Type", "") else r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


# ============================================================ E1、動作代碼
class TestAxesAndCodes(unittest.TestCase):
    def test_e1_helpers(self):
        cfg = C.load(None)
        self.assertEqual(C.axis_name(cfg, 7), "E1")
        self.assertEqual(C.axis_name(cfg, 2), "J2")
        self.assertEqual(C.drive_comp(cfg, 7), "gearbox")          # 行走軸齒輪箱＋齒條
        self.assertTrue(C.axis_cfg(cfg, 7)["linear"])
        self.assertEqual(C.drive_comp(cfg, 2), "reducer")

    def test_code_ranges(self):
        self.assertEqual(Mdl.decode_seg(2501), (2, 50, 1))
        self.assertEqual(Mdl.decode_seg(7302)[0], 7)                # E1 診斷段
        self.assertIsNone(Mdl.decode_seg(9001))                     # 生產動作不是診斷段
        self.assertTrue(Mdl.is_motion(9001))
        self.assertFalse(Mdl.is_motion(9902))
        self.assertEqual(Mdl.vib_window_axis(9903), 3)


# ============================================================ 生產循環監測＋感測器（模擬 40 天）
class TestMotionAndSensors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from dtm.service import simulate_history
        cls.cfg = tmp_cfg()
        cls.cfg["axes"]["enabled"] = [2, 3, 7]
        cls.cfg["sensors"]["enabled"] = True
        cls.cfg["sensors"]["source"] = "sim"
        cls.svc = simulate_history(cls.cfg, days=40)

    def test_cycles_and_daily_medians(self):
        st = self.svc.store
        codes = {c for c, _a in st.cycle_codes()}
        self.assertTrue({9001, 9002} <= codes, codes)
        self.assertTrue(st.daily_since(0), "沒有每日中位數")
        m = self.svc.motion_health.get(3)
        self.assertIsNotNone(m)
        self.assertIn("codes", m)

    def test_e1_is_monitored_without_gravity_check(self):
        h = self.svc.health.get(7)
        self.assertIsNotNone(h)
        self.assertNotIn("G", {k for k, v in (h.get("metrics") or {}).items() if v.get("level")})

    def test_status_summary_has_new_fields(self):
        s = R.service_summary(self.svc)
        ax = {a["axis"]: a for a in s["axes"]}
        self.assertEqual(ax[7]["name"], "E1")
        self.assertIn("motion", ax[3])
        self.assertIn("decision", ax[3])
        self.assertIn("disk", s)


class TestSensorWindows(unittest.TestCase):
    def test_window_slicing(self):
        from dtm.sensors import SensorHub
        cfg = C.load(None)
        cfg["sensors"]["enabled"] = True
        cfg["sensors"]["source"] = "sim"
        cfg["axes"]["enabled"] = [2, 3]
        hub = SensorHub(cfg)
        t0 = time.time()
        for i in range(40):
            hub.poll_once(t0 + i * 0.1)
        self.assertGreater(hub.status()["reads"], 0)
        w = hub.window(2, t0, t0 + 1.0)
        if hub.map:                                                    # 模擬來源有對應表時，時間窗要切得出來
            self.assertIsNotNone(w)
            self.assertGreater(w["n"], 5)
        self.assertIsNone(hub.window(2, t0 + 100, t0 + 101))           # 時間窗外沒有資料


# ============================================================ 自我檢查、未正常結束、磁碟
class TestRuntime(unittest.TestCase):
    def test_run_marker_detects_unclean_shutdown(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "x.sqlite3")
        m = R.RunMarker(db)
        self.assertFalse(m.start())
        m2 = R.RunMarker(db)                                         # 沒有 stop() 就再啟動 = 上次沒正常結束
        self.assertTrue(m2.start())
        m2.stop()
        self.assertFalse(R.RunMarker(db).start())

    def test_corrupt_database_blocks_start(self):
        cfg = tmp_cfg()
        with open(cfg["storage"]["db"], "wb") as f:
            f.write(b"SQLite format 3\x00" + b"\xff" * 5000)
        checks = R.self_check(cfg, unclean=True)
        db = [c for c in checks if c["name"] == "資料庫完整性"][0]
        self.assertEqual(db["level"], "error")

    def test_stop_signal_runs_once_in_thread(self):
        import signal as S
        calls = []
        done = threading.Event()
        old = {n: S.getsignal(getattr(S, n)) for n in ("SIGINT", "SIGTERM") if hasattr(S, n)}
        try:
            h = R.on_stop_signals(lambda: (calls.append(threading.current_thread().name), done.set()))
            h()
            h()                                                        # 服務管理員連送兩次也只停一次、不互鎖
            done.wait(2)
            time.sleep(0.1)
            self.assertEqual(calls, ["dtm-stop"])
        finally:
            for n, f in old.items():
                S.signal(getattr(S, n), f)

    def test_atomic_status_json(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "s.json")
        R.write_json(p, {"a": 1})
        self.assertEqual(R.read_json(p), {"a": 1})
        self.assertEqual(R.read_json(os.path.join(d, "none.json"), {"x": 0}), {"x": 0})

    def test_disk_alarm(self):
        from dtm.service import Service
        cfg = tmp_cfg()
        svc = Service(cfg, notify=False)
        orig = R.disk_info
        try:
            R.disk_info = lambda p: {"free_pct": 2.0, "free_gb": 1.0, "total_gb": 50.0}
            svc.disk_tick()
        finally:
            R.disk_info = orig
        act = [a for a in svc.store.active_alarms() if a["akey"] == "sys:disk"]
        self.assertTrue(act)
        self.assertEqual(act[0]["level"], 2)


# ============================================================ 看門狗
class FakeFeeder:
    kind = "fake"
    can_disable = True

    def __init__(self):
        self.feeds = 0
        self.disabled = 0
        self.enabled = 0
        self.error = ""

    def feed(self):
        self.feeds += 1

    def disable(self):
        self.disabled += 1

    def enable(self):
        self.enabled += 1


class TestWatchdog(unittest.TestCase):
    def setUp(self):
        from dtm.watchdog import Watchdog
        self.cfg = tmp_cfg()
        self.cfg["watchdog"].update(grace_s=60, stale_s=120, data_stale_s=900, max_restarts=3, window_s=3600)
        self.cfg["watchdog"]["hw"].update(feed_s=10, max_reboots_day=2)
        self.clock = Clock(time.time())                                # 維護模式用真實時間寫入
        self.path = R.status_path(self.cfg)
        self.restarts = []
        self.feeder = FakeFeeder()
        self.wd = Watchdog(self.cfg, restart=lambda: self.restarts.append(self.clock()), feeder=self.feeder,
                           clock=self.clock)

    def status(self, age=0, data_age=0):
        R.write_json(self.path, {"ts": self.clock() - age, "loop_ts": self.clock() - age,
                                 "last_ts": self.clock() - data_age, "robot": "R1"})

    def test_ok_stuck_and_anti_loop(self):
        self.clock.t += 100
        self.status()
        self.assertEqual(self.wd.tick(), "ok")
        self.assertGreater(self.feeder.feeds, 0)
        for i in range(5):                                            # 一直卡住
            self.clock.t += 200
            self.status(age=500)
            self.wd.tick()
        self.assertEqual(len(self.restarts), 3)                       # 每小時最多 3 次
        self.assertTrue(self.wd.gave_up)
        feeds = self.feeder.feeds
        self.clock.t += 30
        self.wd.tick()
        self.assertEqual(self.feeder.feeds, feeds)                    # 救不回來 → 停止餵硬體看門狗

    def test_data_stale_only_notifies(self):
        self.clock.t += 100
        self.status(data_age=2000)
        self.assertEqual(self.wd.tick(), "data")
        self.assertEqual(self.restarts, [])                           # 機器人關機時重啟沒用

    def test_maintenance_pauses_everything(self):
        from dtm.watchdog import clear_maintenance, maintenance, set_maintenance
        self.clock.t += 100
        set_maintenance(self.cfg, 30, "更新", "tester")
        self.assertIsNotNone(maintenance(self.cfg))
        self.status(age=900)
        self.assertEqual(self.wd.tick(), "maintenance")
        self.assertEqual(self.restarts, [])
        self.assertEqual(self.feeder.disabled, 1)
        clear_maintenance(self.cfg)
        self.status()
        self.wd.tick()
        self.assertEqual(self.feeder.enabled, 1)

    def test_hw_reboot_limit_per_day(self):
        self.clock.t += 100
        self.wd.hw_stops = [self.clock() - 3600, self.clock() - 7200]   # 今天已重開 2 次
        self.wd.restarts = [self.clock() - 60, self.clock() - 120, self.clock() - 180]   # 重啟次數也用完了
        self.wd.gave_up = True
        self.status(age=900)
        self.wd.tick()
        self.assertEqual(self.wd.hw_mode, "forced")                     # 不再停止餵狗，改通知人


# ============================================================ 帳號與角色
class TestAccounts(unittest.TestCase):
    def test_roles_and_login(self):
        from dtm.auth import Auth, UserStore, need_role
        cfg = tmp_cfg()
        us = UserStore(R.resolve(cfg, cfg["web"]["users_db"]))
        us.add("wang", "password123", "operator", "王小明")
        us.add("lin", "password456", "engineer", "林工程師")
        with self.assertRaises(ValueError):
            us.add("wang", "password789", "admin")
        with self.assertRaises(ValueError):
            us.add("short", "123", "viewer")
        a = Auth(cfg)
        self.assertEqual(a.mode, "accounts")
        tok, err, code = a.login("1.1.1.1", "", "password123", username="wang")
        self.assertEqual(code, 200, err)
        s = a.session(tok)
        self.assertEqual((s["who"], s["role"]), ("王小明", "operator"))
        self.assertTrue(a.can(s, "/api/ack"))
        self.assertTrue(a.can(s, "/api/c/ack"))
        self.assertFalse(a.can(s, "/api/event"))
        self.assertFalse(a.can(s, "/api/c/job"))
        self.assertEqual(need_role("/api/c/maintenance"), "admin")
        _t, _e, code = a.login("1.1.1.1", "", "wrong", username="lin")
        self.assertEqual(code, 403)
        us.update("lin", active=0)
        _t, _e, code = a.login("1.1.1.2", "", "password456", username="lin")
        self.assertEqual(code, 403)
        self.assertNotIn("pw_hash", us.list()[0])

    def test_password_is_hashed(self):
        from dtm.auth import hash_pw, verify_pw
        h, s, it = hash_pw("secret-pass")
        self.assertNotIn("secret", h)
        self.assertTrue(verify_pw("secret-pass", h, s, it))
        self.assertFalse(verify_pw("secret-pasS", h, s, it))


# ============================================================ 通知中心：規則、逐級、值班、夜間、備援
class FakeHttp:
    def __init__(self):
        self.calls = []

    def request(self, method, url, data=None, headers=None, timeout=15):
        self.calls.append((method, url, data))
        return 200, '{"errcode":0}'


class TestNotifyCenter(unittest.TestCase):
    def make(self, **notify):
        from dtm.notify import Notifier
        cfg = tmp_cfg()
        n = cfg["notify"]
        n["webhook_url"] = "https://chat.example.com/hook/group"
        n["min_level"] = 1
        n["email_enabled"] = False
        n["sms"].update(enabled=True, provider="http", to=["0911000111"], min_level=2,
                        http={"method": "POST", "url": "https://sms.example.com/send", "params": {"to": "{phone}"},
                              "headers": {}, "success": ""})
        for k, v in notify.items():
            n[k] = v
        self.clock = Clock(1_800_000_000.0)
        self.acked = set()
        self.http = FakeHttp()
        nt = Notifier(cfg, None, http=self.http, clock=self.clock, is_acked=lambda k, r, s: k in self.acked)
        nt.sync = True
        return nt

    def urls(self):
        return [u for _m, u, _d in self.http.calls]

    def test_payload_formats(self):
        nt = self.make(webhook_body={"msgtype": "text", "text": {"content": "{text}"}})   # 企業微信群機器人的格式
        nt.send(2, "J3 F 比值 2.1", 3, "健康", self.clock(), "health:3")
        hooks = [json.loads(d) for _m, u, d in self.http.calls if "chat.example.com" in u]
        self.assertEqual(hooks[0]["msgtype"], "text")
        self.assertIn("J3 F 比值 2.1", hooks[0]["text"]["content"])
        self.assertTrue(any("sms.example.com" in u for u in self.urls()))   # 警戒也發簡訊

    def test_escalation_stops_when_acked(self):
        nt = self.make(rules=[{"name": "群組", "channels": ["webhook"], "stage": 1, "min_level": 1},
                              {"name": "值班", "channels": ["sms"], "stage": 2, "min_level": 2}],
                       escalation={"enabled": True, "min_level": 2, "stage2_after_s": 1800, "stage3_after_s": 7200})
        nt.send(2, "J3 警戒", 3, "健康", self.clock(), "health:3")
        n1 = len(self.http.calls)
        self.assertTrue(all("chat.example.com" in u for u in self.urls()))
        self.clock.t += 1000
        nt.tick()
        self.assertEqual(len(self.http.calls), n1)                    # 還沒到 30 分鐘
        self.clock.t += 900
        nt.tick()
        self.assertTrue(any("sms.example.com" in u for u in self.urls()))   # 第 2 階段
        n2 = len(self.http.calls)
        self.acked.add("health:3")
        self.clock.t += 8000
        nt.tick()
        self.assertEqual(len(self.http.calls), n2)                    # 確認後停止升級
        self.assertEqual(nt.escalations(), [])

    def test_quiet_hours_defer_caution_into_summary(self):
        import datetime as dt
        night = dt.datetime(2027, 1, 5, 23, 30).timestamp()
        nt = self.make(quiet_hours="22:00-07:00", summary_time="07:30", summary_channels=["webhook"],
                       rules=[{"name": "群組", "channels": ["webhook"], "stage": 1, "min_level": 1}])
        self.clock.t = night
        nt.send(1, "J2 注意", 2, "健康", self.clock(), "health:2")
        self.assertEqual(self.http.calls, [])                         # 夜間注意不即時發
        nt.send(2, "J3 警戒", 3, "健康", self.clock(), "health:3")
        self.assertEqual(len(self.http.calls), 1)                     # 警戒照發
        self.clock.t = dt.datetime(2027, 1, 6, 7, 31).timestamp()
        nt.tick()
        self.assertTrue(any("J2 注意" in (d or b"").decode() for _m, _u, d in self.http.calls[1:]), "摘要沒帶到延後的注意")

    def test_edge_fallback_gate(self):
        from dtm.notify import Notifier
        cfg = tmp_cfg()
        cfg["notify"].update(webhook_url="https://chat.example.com/hook", min_level=1)
        state = {"g": "none"}
        h = FakeHttp()
        nt = Notifier(cfg, None, http=h, gate=lambda: state["g"])
        nt.sync = True
        nt.send(2, "x", 2, "健康", time.time(), "k1")
        self.assertEqual(h.calls, [])                                  # 連得上中央：由中央發
        state["g"] = "alert_only"
        nt.send(1, "注意", 2, "健康", time.time(), "k2")
        self.assertEqual(h.calls, [])                                  # 備援只發警戒
        nt.send(2, "警戒", 2, "健康", time.time(), "k3")
        self.assertEqual(len(h.calls), 1)

    def test_duty_roster_csv(self):
        import datetime as dt
        from dtm.notify import on_duty
        cfg = tmp_cfg()
        p = os.path.join(R.base_dir(cfg), "duty.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("date,start,end,name,phone,email\n2027-01-05,08:00,20:00,日班王,0911000111,\n"
                    "2027-01-05,20:00,08:00,夜班林,0922000222,\n")
        cfg["notify"]["duty_file"] = "duty.csv"
        day = on_duty(cfg, dt.datetime(2027, 1, 5, 10, 0).timestamp())
        self.assertEqual([d["name"] for d in day], ["日班王"])
        night = on_duty(cfg, dt.datetime(2027, 1, 6, 3, 0).timestamp())   # 跨夜班的後半段
        self.assertEqual([d["name"] for d in night], ["夜班林"])


# ============================================================ 邊緣主機：監督者
class FakeProc:
    n = 1000

    def __init__(self):
        FakeProc.n += 1
        self.pid = FakeProc.n
        self.code = None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -15

    def kill(self):
        self.code = -9

    def wait(self, timeout=None):
        return self.code


class TestSupervisor(unittest.TestCase):
    def setUp(self):
        from dtm.fleet import Supervisor
        self.cfg = tmp_cfg(system={"role": "edge", "site_id": "P9", "site_name": "測試廠"})
        self.cfg["robots"] = [{"id": "R01", "host": "10.0.0.1"}, {"id": "R02", "host": "10.0.0.2", "e1": True}]
        self.cfg["fleet"].update(grace_s=60, stale_s=120, max_restarts=2, window_s=3600)
        self.clock = Clock()
        self.procs = []
        self.alarms = []

        class A:
            def set(_s, key, axis, kind, level, msg, value, ts):
                self.alarms.append((key, level, msg))

        def spawn(ch):
            p = FakeProc()
            self.procs.append((ch.id, p))
            return p
        self.sup = Supervisor(self.cfg, spawn=spawn, clock=self.clock, alarms=A())

    def test_children_config(self):
        a, b = self.sup.children
        self.assertEqual((a.port, b.port), (8101, 8102))
        self.assertIn(7, b.cfg["axes"]["enabled"])
        self.assertNotEqual(a.cfg["storage"]["db"], b.cfg["storage"]["db"])
        self.assertEqual(b.cfg["source"]["fanuc_http"]["host"], "10.0.0.2")
        self.assertTrue(a.cfg["_edge"])

    def test_restart_backoff_and_give_up(self):
        self.sup.start_all()
        self.assertEqual(len(self.procs), 2)
        ch = self.sup.children[0]
        for i in range(4):
            self.clock.t += 100
            for c in self.sup.children:                            # 第二台正常更新狀態檔
                R.write_json(c.status_path, {"ts": self.clock()})
            ch.proc.code = 1                                       # 第一台一直當掉
            self.sup.tick()
        self.assertTrue(ch.gave_up)
        self.assertEqual(sum(1 for rid, _p in self.procs if rid == "R01"), 3)   # 啟動 1 次＋重啟 2 次
        self.assertTrue(any(k == "child:R01" and lv == 2 for k, lv, _m in self.alarms))
        st = R.read_json(self.sup.status_path)
        self.assertEqual(st["role"], "supervisor")
        self.assertEqual(len(st["children"]), 2)

    def test_stuck_child_is_killed(self):
        self.sup.start_all()
        ch = self.sup.children[1]
        self.clock.t += 200
        R.write_json(ch.status_path, {"ts": self.clock() - 500})
        R.write_json(self.sup.children[0].status_path, {"ts": self.clock()})
        states = self.sup.tick()
        self.assertEqual(states["R02"], "restarted")


# ============================================================ 同步：機器人資料庫 → 中央
class TestSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from dtm.service import simulate_history
        cls.rcfg = tmp_cfg(system={"site_id": "P9", "site_name": "測試廠"})
        cls.rcfg["robot"].update(id="R07", name="R07", line="L1")
        cls.rcfg["axes"]["enabled"] = [2, 3]
        cls.svc = simulate_history(cls.rcfg, days=60)
        R.StatusWriter(cls.svc).write_once()

    def central(self):
        from dtm.central import CentralStore
        ccfg = tmp_cfg(system={"role": "central"})
        ccfg["central"]["token"] = "tok-123"
        return CentralStore(os.path.join(R.base_dir(ccfg), "central.sqlite3"), ccfg)

    def collect(self, cursor_file):
        from dtm.sync import Collector, robot_key
        col = Collector(robot_key(self.rcfg), self.rcfg["storage"]["db"], R.status_path(self.rcfg), cursor_file)
        return col, col.collect()

    def test_round_trip_cursors_and_views(self):
        from dtm import fleetviews as V
        cs = self.central()
        cur = os.path.join(tempfile.mkdtemp(), "c.json")
        col, d = self.collect(cur)
        self.assertEqual(d["key"], "P9/R07")
        self.assertTrue(d["features"])
        self.assertTrue(d["alarms"])
        d["meta"] = {"id": "R07", "name": "R07", "axes": [2, 3], "line": "L1"}
        res = cs.ingest({"site": {"site_id": "P9", "site_name": "測試廠"}, "robots": [d]}, notify=False)
        self.assertTrue(res["ok"])
        col.commit(d["cursors"])
        _col2, d2 = self.collect(cur)
        self.assertEqual(d2.get("features", []), [])                  # 游標往前了，不會重送
        n_alarm = cs.one("SELECT COUNT(*) AS n FROM alarms")["n"]
        cs.ingest({"site": {"site_id": "P9"}, "robots": [dict(d, meta=d["meta"])]}, notify=False)
        self.assertEqual(cs.one("SELECT COUNT(*) AS n FROM alarms")["n"], n_alarm)   # 重送不會重複
        for name, fn in V.VIEWS.items():
            if name == "robot":
                out = fn(cs, "P9/R07")
                self.assertTrue(out["axes"])
                self.assertTrue(out["diag"], "機器人詳情沒有趨勢")
            else:
                fn(cs)
        ov = V.overview(cs)
        self.assertEqual(ov["kpi"]["robots"], 1)

    def test_edge_text_follows_viewer_language(self):
        """邊緣主機產生的警報與研判文字，中央依觀看者的語言重新產生（2.1）。"""
        import re
        from dtm import fleetviews as V
        from dtm.i18n import use
        han = re.compile(r"[\u3400-\u9fff]")
        cs = self.central()
        _c, d = self.collect(os.path.join(tempfile.mkdtemp(), "c.json"))
        d["meta"] = {"id": "R07", "name": "R07", "axes": [2, 3]}
        cs.ingest({"site": {"site_id": "P9", "site_name": "測試廠"}, "robots": [d]}, notify=False)
        self.assertTrue(cs.one("SELECT COUNT(*) AS n FROM alarms WHERE tmpl IS NOT NULL")["n"])
        self.assertTrue(cs.one("SELECT COUNT(*) AS n FROM axis_state WHERE tmpl IS NOT NULL")["n"])
        with use("en"):
            rows = V.alerts(cs, "all")["rows"]
            self.assertTrue(rows)
            for r in rows:
                self.assertFalse(han.search(r["message"]), r["message"])
            out = V.robot(cs, "P9/R07")
            sel = out["sel"]
            for t in sel["reasons"] + [sel["hint"]]:
                self.assertFalse(han.search(t or ""), t)
        with use("zh-TW"):
            tw = {r["id"]: r["message"] for r in V.alerts(cs, "all")["rows"]}
        raw = {r["id"]: r["message"] for r in cs.q("SELECT id, message FROM alarms")}
        self.assertEqual(tw, {k: raw[k] for k in tw})

    def test_commands_and_rules_flow_back(self):
        from dtm.sync import apply_rules, execute_command
        cs = self.central()
        _c, d = self.collect(os.path.join(tempfile.mkdtemp(), "c.json"))
        d["meta"] = {"id": "R07", "name": "R07", "axes": [2, 3]}
        cs.ingest({"site": {"site_id": "P9"}, "robots": [d]}, notify=False)
        q = cs.command("P9/R07", "iron", {"axis": 3, "wt_pct": 0.07, "sample": "interim"}, "王小明")
        self.assertTrue(q["queued"])
        res = cs.ingest({"site": {"site_id": "P9"}, "robots": []}, notify=False)
        cmds = res["commands"]
        self.assertEqual([c["kind"] for c in cmds], ["iron"])
        self.assertEqual(cmds[0]["robot_id"], "R07")
        n_iron = len(self.svc.store.iron(3)) if hasattr(self.svc.store, "iron") else None
        r = execute_command(self.svc, cmds[0])
        self.assertTrue(r["ok"], r)
        if n_iron is not None:
            self.assertEqual(len(self.svc.store.iron(3)), n_iron + 1)
        cs.ingest({"site": {"site_id": "P9"}, "robots": [], "results": [dict(r, cmd_id=cmds[0]["id"])]}, notify=False)
        self.assertEqual(cs.one("SELECT status FROM commands WHERE id=?", (cmds[0]["id"],))["status"], "done")
        # 規則：中央機群壽命模型 → 機器人的先驗
        cfg = C.load(None)
        self.assertTrue(apply_rules(cfg, {"life_prior": {"reducer": {"beta": 2.5, "eta_h": 50000}}}))
        self.assertEqual(cfg["life"]["prior"]["reducer"]["eta_h"], 50000)
        self.assertFalse(apply_rules(cfg, {"life_prior": {"reducer": {"beta": 2.5, "eta_h": 50000}}}))

    def test_central_notifies_new_and_cleared_alarms_once(self):
        cs = self.central()
        sent = []

        class N:
            def send(_s, level, message, axis, kind, ts, key, cleared=False, robot=None, site=None):
                sent.append((level, cleared, key, robot, site))
        cs.notifier = N()
        now = time.time()
        a = {"id": 1, "ts": now - 60, "axis": 3, "kind": "健康", "akey": "health:3", "level": 2, "message": "J3 警戒",
             "value": 2.1, "acked": 0, "cleared_ts": None}
        old = dict(a, id=2, ts=now - 86400, akey="health:2")             # 補傳的舊警報只存不發
        robot = {"key": "P9/R07", "meta": {"id": "R07", "name": "R07"}, "status": {}, "alarms": [a, old]}
        cs.ingest({"site": {"site_id": "P9", "site_name": "測試廠"}, "robots": [robot]}, notify=True)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][3:], ("R07", "測試廠"))
        cs.ingest({"site": {"site_id": "P9"}, "robots": [robot]}, notify=True)
        self.assertEqual(len(sent), 1)                                   # 同一筆不重發
        robot["alarms"] = [dict(a, cleared_ts=now)]
        cs.ingest({"site": {"site_id": "P9"}, "robots": [robot]}, notify=True)
        self.assertEqual(sent[-1][1], True)                              # 解除
        self.assertTrue(cs.is_acked("P9/R07|health:3"))                  # 已解除 → 不再升級

    def test_edge_offline_alarm(self):
        cs = self.central()
        sent = []

        class N:
            def send(_s, *a, **k):
                sent.append((a, k))
        cs.notifier = N()
        t = time.time()
        cs.ingest({"site": {"site_id": "P9", "site_name": "測試廠"}, "robots": []}, notify=True, now=t)
        cs.check_edges(t + 60)
        self.assertEqual(sent, [])
        cs.check_edges(t + 1000)
        act = cs.q("SELECT * FROM alarms WHERE akey='edge:offline' AND cleared_ts IS NULL")
        self.assertEqual(len(act), 1)
        cs.check_edges(t + 1100)
        self.assertEqual(len(cs.q("SELECT * FROM alarms WHERE akey='edge:offline'")), 1)
        cs.ingest({"site": {"site_id": "P9"}, "robots": []}, notify=True, now=t + 1200)
        cs.check_edges(t + 1210)
        self.assertEqual(cs.q("SELECT * FROM alarms WHERE akey='edge:offline' AND cleared_ts IS NULL"), [])


# ============================================================ 中央網頁：/api/sync、8 個畫面、寫入權限
class TestCentralWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from dtm.auth import Auth, UserStore
        from dtm.demo import build_demo
        from dtm.fleetweb import serve_fleet
        cls.cfg = tmp_cfg(system={"role": "central"})
        cls.cfg["central"]["token"] = "tok-abc"
        cls.cfg["central"]["tokens"] = {"P7": "tok-p7"}
        for ax in (4, 5, 6):
            cls.cfg.setdefault("axis", {})[str(ax)] = {"reducer_type": "gearbox"}
        cls.cs = build_demo(cls.cfg, os.path.join(R.base_dir(cls.cfg), "central.sqlite3"))
        us = UserStore(R.resolve(cls.cfg, cls.cfg["web"]["users_db"]))
        us.add("op", "operator-pw", "operator", "操作員甲")
        us.add("eng", "engineer-pw", "engineer", "工程師乙")
        cls.httpd = serve_fleet(cls.cfg, cls.cs, role="central", auth=Auth(cls.cfg), host="127.0.0.1", port=0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def login(self, user, pw):
        code, j = http("POST", self.base + "/api/login", {"username": user, "password": pw})
        self.assertEqual(code, 200, j)
        return {"X-DTM-Token": j["token"]}

    def test_all_screens(self):
        for p in ["/", "/api/c/info", "/api/c/robots", "/api/c/overview", "/api/c/plant?site=P3",
                  "/api/c/robot?rkey=P3%2Fr14&axis=2", "/api/c/risk", "/api/c/alerts?tab=all", "/api/c/maint",
                  "/api/c/life?axis=2", "/api/c/system", "/api/c/export.csv?view=risk",
                  "/workorder?axis=2&comp=reducer&rkey=P3%2Fr14"]:
            with urllib.request.urlopen(self.base + p, timeout=30) as r:
                self.assertEqual(r.status, 200, p)
        _c, ov = http("GET", self.base + "/api/c/overview")
        self.assertEqual(ov["kpi"]["robots"], 160)
        self.assertEqual(ov["kpi"]["e1"], 10)
        self.assertTrue(ov["demo"])
        _c, rk = http("GET", self.base + "/api/c/risk")
        self.assertTrue(any(s["act_key"] == "urgent" for s in rk["spares"]))

    def test_sync_needs_token(self):
        payload = {"site": {"site_id": "P7", "site_name": "七廠"}, "robots": []}
        body = gzip.compress(json.dumps(payload).encode())
        code, _j = http("POST", self.base + "/api/sync", raw=body, headers={"Content-Encoding": "gzip",
                                                                             "Authorization": "Bearer tok-abc"})
        self.assertEqual(code, 403)                                     # 七廠有自己的金鑰
        code, j = http("POST", self.base + "/api/sync", raw=body, headers={"Content-Encoding": "gzip",
                                                                            "Authorization": "Bearer tok-p7"})
        self.assertEqual(code, 200, j)
        self.assertTrue(j["ok"])
        self.assertIn("rules", j)

    def test_write_permissions(self):
        a = self.cs.one("SELECT id FROM alarms WHERE cleared_ts IS NULL AND acked=0 AND origin='edge' LIMIT 1")
        code, _j = http("POST", self.base + "/api/c/ack", {"id": a["id"]})
        self.assertEqual(code, 401)
        op = self.login("op", "operator-pw")
        code, j = http("POST", self.base + "/api/c/job", {"rkey": "P3/r14", "axis": 2, "kind": "grease",
                                                           "date": "2030-01-02"}, op)
        self.assertEqual(code, 403)
        self.assertIn("工程師", j["error"])
        code, j = http("POST", self.base + "/api/c/ack", {"id": a["id"]}, op)
        self.assertEqual(code, 200, j)
        self.assertTrue(j["alarm"]["dispatch"]["queued"])                # 確認會送回邊緣
        eng = self.login("eng", "engineer-pw")
        code, j = http("POST", self.base + "/api/c/job", {"rkey": "P3/r14", "axis": 2, "kind": "rep",
                                                           "date": "2030-01-02", "hours": 6}, eng)
        self.assertEqual(code, 200, j)
        code, j = http("POST", self.base + "/api/c/job/done", {"id": j["id"], "failed": True}, eng)
        self.assertEqual(code, 200, j)
        cmd = self.cs.one("SELECT * FROM commands WHERE id=?", (j["dispatch"]["queued"],))
        self.assertEqual(json.loads(cmd["payload"])["kind"], "reducer_replace")
        code, j = http("POST", self.base + "/api/c/notify/test", {"channel": "all"}, eng)
        self.assertEqual(code, 403)                                     # 管理員才能測試通知
        audit = self.cs.q("SELECT action, ok FROM audit WHERE action='/api/c/job'")
        self.assertTrue(any(x["ok"] == 0 for x in audit) and any(x["ok"] == 1 for x in audit))


# ============================================================ 機器人程式的內部指令 API（邊緣主機轉中央指令）
class TestInternalCommand(unittest.TestCase):
    def test_internal_api_requires_token(self):
        from dtm.service import Service
        from dtm.web import serve
        cfg = tmp_cfg()
        cfg["web"]["write_password"] = "pw-xyz"
        svc = Service(cfg, notify=False)
        os.environ["DTM_INTERNAL_TOKEN"] = "inner-1"
        try:
            httpd = serve(svc, "127.0.0.1", 0)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{httpd.server_address[1]}/api/internal/cmd"
            cmd = {"kind": "hours", "payload": {"hours": 12345}, "who": "中央"}
            code, _j = http("POST", url, cmd)
            self.assertEqual(code, 403)
            code, j = http("POST", url, cmd, {"X-DTM-Internal": "inner-1"})
            self.assertEqual(code, 200, j)
            self.assertTrue(j["ok"])
            self.assertEqual(svc.store.hours()[-1]["hours"], 12345)
            httpd.shutdown()
        finally:
            os.environ.pop("DTM_INTERNAL_TOKEN", None)


# ============================================================ 設定檢查、服務安裝檔、演示
class TestConfigAndService(unittest.TestCase):
    def test_example_configs(self):
        from dtm.check import ERROR, run_check
        os.environ["DTM_SYNC_TOKEN"] = "x"
        try:
            for name in ("config.central.example.toml", "config.edge.example.toml"):
                rep = run_check(os.path.join(ROOT, name))
                self.assertEqual(rep.count(ERROR), 0, (name, [i for i in rep.items if i[0] == ERROR]))
        finally:
            os.environ.pop("DTM_SYNC_TOKEN", None)

    def test_edge_duplicates_are_errors(self):
        from dtm.check import ERROR, run_check
        d = tempfile.mkdtemp()
        p = os.path.join(d, "edge.toml")
        with open(p, "w", encoding="utf-8") as f:
            f.write('[system]\nrole = "edge"\n\n[[robots]]\nid = "R1"\nhost = "1.1.1.1"\n'
                    '[[robots]]\nid = "R1"\nhost = "1.1.1.2"\nport = 8080\n')
        rep = run_check(p)
        errs = " ".join(m for lv, _w, m in rep.items if lv == ERROR)
        self.assertIn("site_id", " ".join(w for lv, w, _m in rep.items if lv == ERROR))
        self.assertIn("重複", errs)

    def test_service_files(self):
        from dtm import winservice as W
        cfg = tmp_cfg()
        xml = W.winsw_xml("DTM", "DTM", "desc", "C:\\Python\\python.exe", ["-m", "dtm", "-c", "C:\\a b\\c.toml", "run"],
                          "C:\\DTM", "C:\\DTM\\logs")
        self.assertIn('<onfailure action="restart" delay="10 sec"/>', xml)
        self.assertIn('"C:\\a b\\c.toml"', xml)
        unit = W.systemd_unit("DTM", "/usr/bin/python3", ["-m", "dtm", "run"], "/opt/dtm")
        self.assertIn("Restart=always", unit)
        self.assertLess(unit.index("StartLimitBurst"), unit.index("[Service]"))
        files = W.build_files(cfg)
        self.assertEqual(len(files), 2)                                 # 主程式＋看門狗
        self.assertTrue(os.path.exists(W.WINSW_EXE))

    def test_demo_is_marked_and_consistent(self):
        from dtm import fleetviews as V
        from dtm.demo import build_demo, demo_tick
        cfg = tmp_cfg(system={"role": "central"})
        cs = build_demo(cfg, os.path.join(R.base_dir(cfg), "d.sqlite3"))
        self.assertTrue(V.demo_flag(cs))
        ov = V.overview(cs)
        self.assertEqual([p["robots"] for p in ov["plants"]], [32, 28, 27, 25, 26, 22])
        demo_tick(cs, time.time() + 600)
        cs.check_edges(time.time() + 600)
        self.assertEqual(cs.q("SELECT * FROM alarms WHERE akey='edge:offline'"), [])   # 演示心跳持續

    def test_demo_follows_viewer_language(self):
        """演示資料每種語言各一份：依請求語言挑資料庫，畫面上的資料文字不會混到中文。"""
        import re
        from dtm import fleetviews as V
        from dtm.demo import DemoStores, ensure_demo
        from dtm.central import CentralStore
        from dtm.i18n import use
        han = re.compile(r"[\u3400-\u9fff]")
        cfg = tmp_cfg(system={"role": "central"})
        cfg["central"]["demo_db"] = os.path.join(R.base_dir(cfg), "demo.sqlite3")
        with use("zh-TW"):
            base = CentralStore(ensure_demo(cfg), cfg)
        ds = DemoStores(cfg, base, "zh-TW")
        ds.notifier = None
        with use("en"):
            self.assertTrue(ds.store().path.endswith("demo.en.sqlite3"))
            ov = V.overview(ds)
            self.assertEqual([p["name"] for p in ov["plants"]][:2], ["Plant 1", "Plant 2"])
            texts = [r["message"] for r in V.alerts(ds, "all")["rows"]] + [s["name"] for s in V.maint(ds)["spares"]]
            self.assertTrue(texts)
            self.assertEqual([t for t in texts if han.search(t)], [])
        with use("zh-TW"):
            self.assertIs(ds.store(), base)
            self.assertEqual(V.overview(ds)["plants"][0]["name"], "一廠")
        self.assertEqual(len(ds.all()), 2)
        self.assertIsNone(ds.store("en").notifier)


if __name__ == "__main__":
    unittest.main()

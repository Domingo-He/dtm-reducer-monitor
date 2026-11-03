"""2.1 新功能測試：設備設定、感測器更新間隔、備份還原、溫度與鐵粉判斷、匯出、維運輔助。"""
import json
import os
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
__import__("_cleanup")                  # 測試結束時刪掉暫存資料夾
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from dtm import config as C  # noqa: E402

DAY = 86400.0


def tmp_cfg(text: str = "", **over):
    """在暫存資料夾寫一份 config.toml（設定頁會寫回這個檔案）。"""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "config.toml")
    with open(os.path.join(ROOT, "config.sim.toml"), encoding="utf-8") as f:
        base = f.read()
    with open(p, "w", encoding="utf-8") as f:
        f.write(base + "\n" + text)
    cfg = C.load(p)
    cfg["storage"]["db"] = os.path.join(d, "t.sqlite3")
    cfg["storage"]["backup_dir"] = ""
    cfg["web"]["users_db"] = os.path.join(d, "users.sqlite3")
    for k, v in over.items():
        cfg[k] = C._merge(cfg.get(k, {}), v) if isinstance(v, dict) else v
    return cfg


# ============================================================ 設備設定
class TestDevices(unittest.TestCase):
    def test_flags_default_from_sensor_map(self):
        cfg = tmp_cfg('[sensors]\nenabled = true\n[sensors.map]\n"2.radial.rms" = { addr = 1 }\n"2.temp" = { addr = 3 }\n')
        fl = C.device_flags(cfg)
        self.assertEqual(fl["axes"][2], {"torque": True, "temp": True, "radial": True, "axial": False})
        self.assertFalse(fl["ambient"])
        self.assertTrue(C.device_key_on(fl, "2.radial.env"))
        self.assertFalse(C.device_key_on(fl, "2.axial.rms"))

    def test_save_writes_backup_and_keeps_other_sections(self):
        from dtm import settings as S
        cfg = tmp_cfg('[devices]\nambient = true\n[devices.axis.2]\ntemp = true\n')
        dev, bak = S.save(cfg, {"ambient": False, "axes": {"2": {"radial": True}, "3": {"temp": True}}}, "127.0.0.1")
        self.assertTrue(os.path.exists(bak))
        with open(cfg["_path"], "rb") as f:
            t = tomllib.load(f)
        self.assertEqual(t["devices"]["axis"]["2"], {"temp": False, "radial": True, "axial": False})
        self.assertFalse(t["devices"]["ambient"])
        self.assertEqual(t["robot"]["source"], "sim")                  # 其他區段不動
        self.assertTrue(C.device_flags(cfg)["axes"][3]["temp"])         # 記憶體中的設定也更新
        with self.assertRaises(PermissionError):
            S.save(cfg, {"axes": {}}, "10.1.2.3")                       # 只能本機
        with self.assertRaises(ValueError):
            S.save(cfg, {"axes": {"6": {"temp": True}}}, "127.0.0.1")  # 沒有監測的軸

    def test_edge_robot_is_view_only(self):
        from dtm import settings as S
        cfg = tmp_cfg()
        cfg["_edge"] = True
        self.assertFalse(S.view(cfg, None, "127.0.0.1")["editable"])

    def test_slow_sensor_detected_and_only_changes_recorded(self):
        from dtm.sensors import SensorHub
        cfg = tmp_cfg('[sensors]\nenabled = true\nsource = "modbus"\nslow_s = 20\n'
                      '[sensors.map]\n"2.radial.rms" = { addr = 1 }\n"2.temp" = { addr = 3 }\n')
        hub = SensorHub(cfg)
        seq = {"t": 0}

        def fake_read(now):
            seq["t"] += 1
            fast = 0.1 + 0.001 * seq["t"]                                # 有線：每次都變
            slow = 40.0 + (now // 120)                                   # 無線：每 2 分鐘才變一次
            return {"2.radial.rms": fast, "2.temp": slow}
        hub._sim_values = fake_read
        hub.kind = "sim"
        t0 = 1_800_000_000.0
        for i in range(0, 900, 2):
            hub.poll_once(t0 + i)
        self.assertTrue(hub.is_slow("2.temp"))
        self.assertFalse(hub.is_slow("2.radial.rms"))
        st = hub.status()
        self.assertGreater(st["update_s"]["2.temp"], 100)
        recs = hub.readings(t0 + 600, t0 + 900)
        temps = [r for r in recs if "2.temp" in r]
        self.assertLessEqual(len(temps), 4)                             # 慢速鍵只在變化時記
        self.assertGreater(len([r for r in recs if "2.radial.rms" in r]), 100)

    def test_disabled_kind_is_ignored(self):
        from dtm.sensors import SensorHub
        cfg = tmp_cfg('[sensors]\nenabled = true\nsource = "modbus"\n[sensors.map]\n"2.radial.rms" = { addr = 1 }\n'
                      '"2.temp" = { addr = 3 }\n[devices.axis.2]\ntemp = false\nradial = true\n')
        hub = SensorHub(cfg)
        hub.kind = "sim"
        hub._sim_values = lambda now: {"2.radial.rms": 0.2, "2.temp": 45.0}
        v = hub.poll_once(1_800_000_000.0)
        self.assertNotIn("2.temp", v)


# ============================================================ 備份到網路資料夾、還原精靈
def _feat(ts, ax, F=10.0):
    from dtm.model import RunFeatures
    return RunFeatures(ts, ax, F, 1.0, 0.1, 0.0, 0.5, 20.0, 35.0, True, {})


class TestBackupMirror(unittest.TestCase):
    def setUp(self):
        from dtm.store import Store
        self.cfg = tmp_cfg()
        d = os.path.dirname(self.cfg["storage"]["db"])
        self.cfg["storage"].update(backup_dir=os.path.join(d, "bk"), backup_mirror_dir=os.path.join(d, "nas"), backup_keep=2)
        self.store = Store(self.cfg["storage"]["db"])
        self.store.add_features([_feat(1_800_000_000.0, 2)])

    def test_mirror_copy_verified_and_pruned(self):
        from dtm import backup as B
        for i in range(3):
            B.backup_now(self.cfg, now=1_800_000_000.0 + i * 86400)
        r = B.mirror_sync(self.cfg)
        self.assertEqual(len(r["copied"]), 2)                          # 本機只留 2 份
        m = B.list_backups(self.cfg, "mirror")
        self.assertEqual([x["name"] for x in m], [x["name"] for x in B.list_backups(self.cfg)])
        self.assertEqual(B.sha256(m[0]["path"]), B.sha256(B.list_backups(self.cfg)[0]["path"]))
        self.assertFalse([f for f in os.listdir(B.mirror_dir(self.cfg)) if f.endswith(".part")])
        B.backup_now(self.cfg, now=1_800_000_000.0 + 5 * 86400)
        r = B.mirror_sync(self.cfg)
        self.assertEqual(len(r["copied"]), 1)
        self.assertEqual(len(r["pruned"]), 1)

    def test_mirror_down_keeps_local_backup(self):
        import threading
        from dtm import backup as B
        bad = os.path.join(os.path.dirname(self.cfg["storage"]["db"]), "afile")
        with open(bad, "w") as f:
            f.write("x")
        self.cfg["storage"]["backup_mirror_dir"] = os.path.join(bad, "sub")   # 檔案底下不能建資料夾 → 模擬連不上
        sch = B.BackupScheduler(self.cfg, threading.Event())
        B.backup_now(self.cfg)
        sch.mirror(now=1000.0)
        self.assertTrue(sch.info["mirror_error"])
        self.assertEqual(len(B.list_backups(self.cfg)), 1)
        self.cfg["storage"]["backup_mirror_dir"] = os.path.join(os.path.dirname(bad), "nas2")
        sch.info["mirror"] = B.mirror_dir(self.cfg)
        sch.mirror(now=1500.0)                                          # 30 分鐘內不重試
        self.assertTrue(sch.info["mirror_error"])
        sch.mirror(now=1000.0 + 1801)
        self.assertEqual(sch.info["mirror_error"], "")
        self.assertEqual(sch.info["mirror_count"], 1)

    def test_bad_copy_rejected(self):
        from dtm import backup as B
        src = B.backup_now(self.cfg)
        real = B.sha256
        calls = {"n": 0}

        def flaky(p):
            calls["n"] += 1
            return "0" * 64 if p.endswith(".part") else real(p)
        B.sha256 = flaky
        try:
            with self.assertRaises(RuntimeError):
                B.mirror_copy(self.cfg, src)
        finally:
            B.sha256 = real
        self.assertEqual(B.list_backups(self.cfg, "mirror"), [])


class TestRestore(unittest.TestCase):
    T0 = 1_800_000_000.0

    def setUp(self):
        from dtm.service import Service
        self.cfg = tmp_cfg()
        d = os.path.dirname(self.cfg["storage"]["db"])
        self.cfg["storage"]["backup_dir"] = os.path.join(d, "bk")
        self.svc = Service(self.cfg, notify=False)
        st = self.svc.store
        for i in range(10):
            st.add_features([_feat(self.T0 + i * DAY, ax, 10 + i * 0.1) for ax in (2, 3)])
        st.add_agg([(int((self.T0 + m * 60) // 60), 2, 10, 1.0, 0.0, 2.0, 2.0) for m in range(50)])
        st.install(2, "reducer", self.T0 - 100 * DAY, "R", "導入時的減速機")
        st.install(2, "grease", self.T0 - 100 * DAY, "R", "導入時的潤滑脂")
        self.eid = self.svc.register_event(self.T0 + 3 * DAY, 2, "reducer_replace", "換減速機", hours=1234.0, who="王")
        st.add_alarm(self.T0 + 4 * DAY, 2, "健康", "health:2", 1, "J2 注意", 1.1)
        from dtm import backup as B
        self.bak = B.backup_now(self.cfg, now=self.T0 + 10 * DAY)

    def tearDown(self):
        from dtm import restore as RS
        for k in list(RS._CACHE):
            RS.cached(k, pop=True).close()

    def counts(self, sql, args=()):
        import sqlite3
        c = sqlite3.connect(self.cfg["storage"]["db"])
        try:
            return c.execute(sql, args).fetchone()[0]
        finally:
            c.close()

    def test_scope_rules(self):
        from dtm import restore as RS
        with self.assertRaises(ValueError):
            RS.parse_scope({"axis": 2, "groups": ["raw"]})               # 原始取樣不做單軸還原
        sc = RS.parse_scope({"axis": 2, "groups": ["maint", "alarm"]})
        self.assertNotIn("hours", sc.tables)                            # 沒有軸欄位的只在整台處理
        self.assertNotIn("notify_log", sc.tables)
        self.assertIn("events", sc.tables)
        sc = RS.parse_scope({"since": "2027-01-01", "until": "2027-01-01"})
        self.assertEqual(sc.until - sc.since, DAY)                      # 結束日含當天

    def test_merge_after_db_lost_remaps_event_detail(self):
        """資料庫整個不見、重新開始累積後，用補回遺失把舊資料補回；事件明細裡的元件編號要對應到補回後的編號。"""
        from dtm import restore as RS
        from dtm.service import Service
        db = self.cfg["storage"]["db"]
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(db + ext):
                os.remove(db + ext)
        svc = Service(self.cfg, notify=False)
        svc.store.install(3, "reducer", self.T0 + 20 * DAY, "R", "新資料庫的第一筆")  # 佔掉編號 1
        svc.store.add_features([_feat(self.T0 + 20 * DAY, 2)])
        o = RS.open_source(self.cfg, "local", keep=True)
        pv = RS.preview(self.cfg, o, RS.parse_scope({}), "merge")
        by = {r["table"]: r for r in pv["tables"]}
        self.assertEqual(by["features"]["add"], 20)
        self.assertEqual(by["features"]["remove"], 0)
        self.assertEqual(by["agg_1min"]["add"], 50)
        self.assertEqual(by["events"]["add"], 1)
        self.assertGreaterEqual(by["components"]["add"], 1)
        res = RS.run(self.cfg, o, o.scope, o.mode, who="測試", svc=svc)
        self.assertTrue(os.path.exists(res["pre_backup"]))
        self.assertEqual(self.counts("SELECT COUNT(*) FROM features"), 21)
        self.assertIn(2, res["recomputed"])
        ev = [e for e in svc.store.events() if e["kind"] == "reducer_replace"][0]
        self.assertEqual(len(ev["detail"]["installed"]), 2)
        for cid in ev["detail"]["installed"]:
            c = svc.store.component(cid)
            self.assertEqual(c["axis"], 2)                               # 不會指到新資料庫的那筆（J3）
            self.assertEqual(c["install_ts"], self.T0 + 3 * DAY)
        for old in ev["detail"]["closed"]:
            self.assertEqual(svc.store.component(old["id"])["install_ts"], self.T0 - 100 * DAY)
        h = [x for x in svc.store.hours() if x["id"] == ev["detail"]["hours_id"]]
        self.assertEqual(h[0]["hours"], 1234.0)
        svc.undo_event(ev["id"], "測試", "還原後撤銷")                      # 補回的事件還能正常撤銷
        self.assertEqual(self.counts("SELECT COUNT(*) FROM components WHERE comp='reducer'"), 2)
        self.assertEqual(self.counts("SELECT COUNT(*) FROM components WHERE axis=2 AND removed_ts IS NULL"), 2)
        pv2 = RS.preview(self.cfg, RS.open_source(self.cfg, "local"), RS.parse_scope({}), "merge")
        self.assertEqual(pv2["tables"][[r["table"] for r in pv2["tables"]].index("features")]["add"], 0)  # 再做一次不會重複
        self.assertTrue(any(a["action"] == "restore" for a in svc.store.audit(20)))

    def test_overwrite_one_axis_needs_confirm(self):
        from dtm import restore as RS
        st = self.svc.store
        st.add_features([_feat(self.T0 + 12 * DAY, 2), _feat(self.T0 + 13 * DAY, 2), _feat(self.T0 + 12 * DAY, 3)])
        self.svc.store.ack_alarm(st.alarms(5)[0]["id"], "李")
        o = RS.open_source(self.cfg, "local", keep=True)
        sc = RS.parse_scope({"axis": 2, "groups": ["trend", "alarm"]})
        pv = RS.preview(self.cfg, o, sc, "overwrite")
        by = {r["table"]: r for r in pv["tables"]}
        self.assertEqual(by["features"]["remove"], 2)
        self.assertEqual(by["features"]["add"], 0)
        self.assertEqual(pv["confirm_phrase"], RS.phrase(self.cfg, sc))
        with self.assertRaises(PermissionError):
            RS.run(self.cfg, o, sc, "overwrite", confirm="y")
        res = RS.run(self.cfg, o, sc, "overwrite", confirm=pv["confirm_phrase"], svc=self.svc)
        self.assertEqual(res["removed"], 2)
        self.assertEqual(res["added"], 0)
        self.assertEqual(self.counts("SELECT COUNT(*) FROM features WHERE axis=2"), 10)
        self.assertEqual(self.counts("SELECT COUNT(*) FROM features WHERE axis=3"), 11)   # 其他軸不動
        self.assertEqual(self.counts("SELECT acked FROM alarms WHERE akey='health:2'"), 0)  # 回到備份當時（未確認）
        self.assertEqual(res["recomputed"], [2])

    def test_time_range_and_snapshot_source(self):
        from dtm import restore as RS
        self.svc.store.add_features([_feat(self.T0 + 12 * DAY, 2)])
        o = RS.open_source(self.cfg, "local")
        since = datetime_str(self.T0 + 5 * DAY)
        sc = RS.parse_scope({"groups": ["trend"], "since": since})
        pv = RS.preview(self.cfg, o, sc, "overwrite")
        self.assertEqual({r["table"]: r for r in pv["tables"]}["features"]["remove"], 1)
        RS.run(self.cfg, o, sc, "overwrite", confirm=pv["confirm_phrase"])
        self.assertEqual(self.counts("SELECT COUNT(*) FROM features"), 20)
        snaps = RS.list_snapshots(self.cfg)
        self.assertEqual(len(snaps), 1)
        o2 = RS.open_source(self.cfg, "snapshot")                       # 還原錯了：用還原前自動備份回到還原前
        pv2 = RS.preview(self.cfg, o2, sc, "merge")
        self.assertEqual({r["table"]: r for r in pv2["tables"]}["features"]["add"], 1)
        o.close()
        o2.close()

    def test_central_rebuild_over_sync(self):
        """中央保存的資料透過同步金鑰取回，只給自己廠區的機器人。"""
        import gzip as gz
        import threading
        import urllib.request
        from dtm import restore as RS
        from dtm.auth import Auth
        from dtm.central import CentralStore
        from dtm.fleetweb import serve_fleet
        from dtm.sync import robot_key
        ccfg = tmp_cfg(system={"role": "central"})
        ccfg["central"]["token"] = "tok-s1"
        cs = CentralStore(os.path.join(os.path.dirname(ccfg["storage"]["db"]), "c.sqlite3"), ccfg, local=True)
        self.cfg["system"]["site_id"] = "S1"
        key = robot_key(self.cfg)
        feats = self.svc.store.features(2, valid_only=False) + self.svc.store.features(3, valid_only=False)
        cs.ingest({"site": {"site_id": "S1"}, "robots": [{
            "key": key, "meta": {"name": "R", "axes": [2, 3]}, "status": {},
            "features": feats, "events": self.svc.store.events(include_undone=True),
            "components": self.svc.store.components(), "alarms": []}]}, notify=False)
        httpd = serve_fleet(ccfg, cs, role="central", auth=Auth(ccfg), host="127.0.0.1", port=0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            self.cfg["central"].update(url=f"http://127.0.0.1:{httpd.server_address[1]}", token="tok-s1")
            db = self.cfg["storage"]["db"]
            for ext in ("", "-wal", "-shm"):
                if os.path.exists(db + ext):
                    os.remove(db + ext)
            o = RS.open_source(self.cfg, "central")
            pv = RS.preview(self.cfg, o, RS.parse_scope({}), "merge")
            by = {r["table"]: r for r in pv["tables"]}
            self.assertEqual(by["features"]["add"], 20)
            self.assertEqual(by["agg_1min"]["backup"], 0)                  # 中央沒有每分鐘統計
            self.assertTrue(any("中央只保存" in w for w in pv["warnings"]))
            RS.run(self.cfg, o, RS.parse_scope({}), "merge")
            self.assertEqual(self.counts("SELECT COUNT(*) FROM features"), 20)
            o.close()
            # 別的廠區的金鑰不能拿這台的資料
            ccfg["central"]["tokens"] = {"S2": "tok-s2"}
            body = gz.compress(json.dumps({"site": {"site_id": "S2"}, "rebuild": {"robot": key}}).encode())
            req = urllib.request.Request(self.cfg["central"]["url"] + "/api/sync", data=body, method="POST", headers={
                "Content-Encoding": "gzip", "Authorization": "Bearer tok-s2", "Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(e.exception.code, 403)
            # 匯出檔（離線帶過去）也能用
            p = os.path.join(os.path.dirname(db), "exp.json.gz")
            with gz.open(p, "wt", encoding="utf-8") as f:
                json.dump(cs.export_robot(key), f)
            o = RS.open_source(self.cfg, "file", path=p)
            self.assertEqual({r["table"]: r for r in RS.preview(self.cfg, o, RS.parse_scope({}))["tables"]}["features"]["add"], 0)
            o.close()
        finally:
            httpd.shutdown()

    def test_web_run_only_local_admin(self):
        import threading
        import urllib.request
        from dtm.auth import Auth, UserStore
        from dtm.web import serve
        UserStore(self.cfg["web"]["users_db"]).add("adm", "admin-pw-1", "admin", "管理員")
        httpd = serve(self.svc, "127.0.0.1", 0, auth=Auth(self.cfg))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def call(path, body=None, tok=None):
            h = {"Content-Type": "application/json"}
            if tok:
                h["X-DTM-Token"] = tok
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                         method="POST" if body is not None else "GET", headers=h)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")
        try:
            code, src = call("/api/restore/sources")
            self.assertEqual(code, 200)
            self.assertEqual(len(src["local"]), 1)
            self.assertNotIn("path", src["local"][0])
            self.assertTrue(src["editable"])
            code, _j = call("/api/restore/preview", {"source": "local"})
            self.assertEqual(code, 401)
            code, j = call("/api/login", {"username": "adm", "password": "admin-pw-1"})
            tok = j["token"]
            code, j = call("/api/restore/preview", {"source": "file", "file": "/etc/passwd"}, tok)
            self.assertEqual(code, 400)                                  # 網頁不能指定任意檔案
            self.svc.store.add_features([_feat(self.T0 + 15 * DAY, 2)])
            code, pv = call("/api/restore/preview", {"source": "local", "mode": "overwrite",
                                                      "scope": {"axis": 2, "groups": ["trend"]}}, tok)
            self.assertEqual(code, 200, pv)
            code, j = call("/api/restore/run", {"preview_id": pv["preview_id"], "confirm": ""}, tok)
            self.assertEqual(code, 400)
            code, j = call("/api/restore/run", {"preview_id": pv["preview_id"], "confirm": pv["confirm_phrase"]}, tok)
            self.assertEqual(code, 200, j)
            self.assertEqual(j["removed"], 1)
            self.assertEqual(j["recomputed"], [2])
            code, j = call("/api/restore/run", {"preview_id": pv["preview_id"], "confirm": pv["confirm_phrase"]}, tok)
            self.assertEqual(code, 400)                                  # 同一份預覽只能執行一次
        finally:
            httpd.shutdown()


# ============================================================ 趨勢加速、溫度、鐵粉、證據比重
class TestTrend(unittest.TestCase):
    def test_linear_vs_accelerating(self):
        from dtm.trend import extrapolate
        xs = [-29 + i for i in range(30)]
        lin = extrapolate(xs, [10 + 0.05 * (x + 29) for x in xs], {"d": 13.0})
        self.assertFalse(lin["accel"])
        self.assertAlmostEqual(lin["d"], (13.0 - (10 + 0.05 * 29)) / 0.05, delta=1.0)
        acc_ys = [10 + 0.002 * (x + 29) ** 2 for x in xs]               # 越走越快
        acc = extrapolate(xs, acc_ys, {"d": 13.0})
        lin_only = extrapolate(xs, acc_ys, {"d": 13.0}, accel_ratio=1e9)
        self.assertTrue(acc["accel"])
        self.assertEqual(acc["method"], "conservative")
        self.assertLess(acc["d"], lin_only["d"])                        # 保守估計比直線外推短

    def test_noise_is_not_acceleration(self):
        import random
        from dtm.trend import extrapolate
        rnd = random.Random(7)
        hits = 0
        for _k in range(40):
            xs = [-29 + i for i in range(30)]
            r = extrapolate(xs, [10 + 0.02 * (x + 29) + rnd.gauss(0, 0.05) for x in xs], {"d": 13.0})
            hits += r["accel"]
        self.assertLessEqual(hits, 6)                                   # 純雜訊很少被當成加速


def _cycles(store, t0, days, temps, ambs=None, code=9001, per_day=6):
    """每天每軸 per_day 個循環；temps(day, axis) → 外殼溫度、ambs(day) → 環境溫度（None = 沒有感測器）。"""
    rows = []
    for d in range(days):
        for k in range(per_day):
            ts = t0 + d * DAY + 8 * 3600 + k * 600
            for ax, tf in temps.items():
                rows.append({"ts": ts, "t0": ts - 30, "code": code, "axis": ax, "dur": 30.0, "ovr": 100.0,
                             "temp": tf(d), "amb": ambs(d) if ambs else None, "n": 300, "mean": 1.0, "mean_abs": 1.0,
                             "rms": 1.0, "peak": 2.0, "std": 0.1, "vib_r": None, "vib_a": None, "valid": 1, "reason": ""})
    store.add_cycles(rows)


class TestThermal(unittest.TestCase):
    def setUp(self):
        from datetime import datetime
        from dtm.store import Store
        self.cfg = tmp_cfg()
        self.store = Store(self.cfg["storage"]["db"])
        self.t0 = datetime(2027, 3, 1).timestamp()
        self.now = self.t0 + 19 * DAY + 12 * 3600                       # 第 20 天中午（今天的循環即時算）

    def ev(self, ax=2, axes=(2,)):
        from dtm import motion as M
        from dtm import thermal as T
        M.refresh_daily(self.store, since=self.t0, now=self.now)
        return T.evaluate_axis(self.store, self.cfg, ax, list(axes), self.now)

    def test_sudden_rise_with_ambient(self):
        _cycles(self.store, self.t0, 20, {2: lambda d: 45.0 + (9.0 if d == 19 else 0)}, lambda d: 25.0)
        r = self.ev()
        self.assertTrue(r["rise"])
        self.assertEqual(r["level"], 1)
        self.assertTrue(r["alarm"])
        self.assertEqual(r["ref_kind"], "ambient")
        self.assertAlmostEqual(r["jump"], 9.0, places=3)
        self.assertGreaterEqual(r["sev"], 0.5)
        self.assertIn("溫度突升", r["message"])

    def test_ambient_rise_is_not_alarm(self):
        _cycles(self.store, self.t0, 20, {2: lambda d: 45.0 + (9.0 if d == 19 else 0)}, lambda d: 25.0 + (9.0 if d == 19 else 0))
        r = self.ev()
        self.assertFalse(r["rise"])
        self.assertEqual(r["level"], 0)

    def test_stable_high_temp_is_not_rise(self):
        _cycles(self.store, self.t0, 20, {2: lambda d: 70.0}, lambda d: 25.0)
        r = self.ev()
        self.assertFalse(r["rise"])
        self.assertEqual(r["level"], 0)

    def test_other_axes_as_reference_and_no_reference(self):
        temps = {2: lambda d: 45.0 + (9.0 if d == 19 else 0), 3: lambda d: 44.0, 4: lambda d: 43.0}
        _cycles(self.store, self.t0, 20, temps)
        r = self.ev(2, (2, 3, 4))
        self.assertEqual(r["ref_kind"], "axes")
        self.assertTrue(r["alarm"])
        r1 = self.ev(2, (2,))                                            # 只有這一軸、沒有環境溫度
        self.assertTrue(r1["rise"])
        self.assertFalse(r1["alarm"])
        self.assertIsNone(r1["sev"])
        self.assertIn("無法排除氣溫影響", r1["suppressed"])

    def test_one_day_jump(self):
        cfg = self.cfg["thermal"]
        cfg["rise_c"] = 20.0                                              # 只測一天內升 5°C 的條件
        _cycles(self.store, self.t0, 20, {2: lambda d: 45.0 + (6.0 if d == 19 else 0)}, lambda d: 25.0)
        r = self.ev()
        self.assertTrue(r["rise"])
        self.assertIn("一天內升", r["message"])

    def test_post_grease_window_and_hold(self):
        _cycles(self.store, self.t0, 20, {2: lambda d: 45.0 + (9.0 if d >= 15 else 0)}, lambda d: 25.0)
        self.store.add_event(self.now - 30 * 3600, 2, "grease_change", "換脂")
        r = self.ev()
        self.assertFalse(r["alarm"])                                     # 72 小時內不判突升
        self.assertIn("72", r["suppressed"])
        self.store.add_event(self.now - 5 * DAY, 2, "grease_change", "換脂")  # 換脂已 5 天，溫度還沒降
        import sqlite3
        c = sqlite3.connect(self.cfg["storage"]["db"])
        c.execute("DELETE FROM events WHERE ts > ?", (self.now - 2 * DAY,))
        c.commit()
        c.close()
        r = self.ev()
        self.assertTrue(r["hold"])
        self.assertTrue(r["alarm"])
        self.assertIn("油封", r["message"])

    def test_single_temperature_register_is_not_per_axis(self):
        same = lambda d: 40.0 + (9.0 if d == 19 else 0)  # noqa: E731
        _cycles(self.store, self.t0, 20, {2: same, 3: same, 4: same})   # 一個溫度暫存器：各軸完全相同
        r = self.ev(2, (2, 3, 4))
        self.assertIsNone(r["dT"])
        self.assertFalse(r["alarm"])

    def test_overheat_absolute(self):
        self.cfg["thermal"]["overheat_c"] = 80.0
        _cycles(self.store, self.t0, 20, {2: lambda d: 82.0}, lambda d: 30.0)
        r = self.ev()
        self.assertTrue(r["overheat"])
        self.assertEqual(r["level"], 2)

    def test_shared_series_cache_same_result_fewer_queries(self):
        """同一輪各軸判斷共用每日序列：結果一樣，讀資料庫的次數從「軸數²」降成「軸數」。"""
        from unittest import mock
        from dtm import motion as M
        from dtm import thermal as T
        temps = {2: lambda d: 45.0 + (9.0 if d == 19 else 0), 3: lambda d: 44.0, 4: lambda d: 43.0}
        _cycles(self.store, self.t0, 20, temps)
        M.refresh_daily(self.store, since=self.t0, now=self.now)
        axes = [2, 3, 4]
        plain = [T.evaluate_axis(self.store, self.cfg, a, axes, self.now) for a in axes]
        real = self.store.daily
        with mock.patch.object(self.store, "daily", side_effect=real) as spy:
            cache: dict = {}
            shared = [T.evaluate_axis(self.store, self.cfg, a, axes, self.now, cache) for a in axes]
            for a in axes:
                M.evaluate_axis(self.store, self.cfg, a, self.now, cache)
        for p, s in zip(plain, shared):
            self.assertEqual({k: v for k, v in p.items() if k != "message"}, {k: v for k, v in s.items() if k != "message"})
        self.assertEqual(spy.call_count, len(axes))                        # 每個（軸, 代碼）只讀一次

    def test_motion_tick_rewrites_past_days_only_after_midnight(self):
        """已結束的日子只在啟動、換日、換日後一小時內重寫；平常每 10 分鐘只重算今天（即時）。"""
        from datetime import datetime
        from unittest import mock
        from dtm.service import Service
        svc = Service(self.cfg, notify=False)
        calls = []
        real = __import__("dtm.motion", fromlist=["refresh_daily"]).refresh_daily

        def spy(store, since=None, now=None):
            calls.append((since, now))
            return real(store, since=since, now=now)
        t = datetime(2027, 3, 5, 10, 0).timestamp()
        with mock.patch("dtm.service.M.refresh_daily", side_effect=spy):
            svc.last_motion_refresh = 0.0
            svc.motion_tick(t)                                           # 啟動：寫最近 3 天
            svc.motion_tick(t + 700)                                     # 同一天上午：不重寫
            svc.motion_tick(t + 1400)
            self.assertEqual(len(calls), 1)
            mid = datetime(2027, 3, 6, 0, 5).timestamp()
            svc.motion_tick(mid)                                         # 換日：寫最近 3 天
            svc.motion_tick(mid + 700)                                   # 換日後一小時內：補寫昨天
            svc.motion_tick(mid + 2 * 3600)                              # 之後：不重寫
        self.assertEqual(len(calls), 3)
        self.assertIsNone(calls[1][0])
        self.assertAlmostEqual(calls[2][0], mid + 700 - DAY)


class TestIronAndWeights(unittest.TestCase):
    def setUp(self):
        from dtm.service import Service
        self.cfg = tmp_cfg()
        self.cfg["axis"] = {"2": {"grease_mass_g": 2000.0}}
        self.svc = Service(self.cfg, notify=False)
        self.T0 = 1_800_000_000.0
        self.svc.store.install(2, "grease", self.T0, "R", "")
        self.svc.store.add_hours(self.T0, 10000.0)

    def test_default_sample_is_drain_mid(self):
        from dtm.config import IRON_SAMPLES
        self.assertEqual(self.cfg["iron"]["default_sample"], "drain_mid")
        self.assertIn("中段", IRON_SAMPLES["drain_mid"])
        self.svc.add_iron(self.T0 + 30 * DAY, 2, 0.01)
        self.assertEqual(self.svc.store.iron(2)[0]["sample"], "drain_mid")
        with self.assertRaises(ValueError):
            self.svc.store.add_iron(self.T0, 2, 0.01, "bogus")

    def test_past_knee_no_rul_and_spares(self):
        from dtm.cross import decide
        self.svc.store.add_hours(self.T0 + 100 * DAY, 12000.0)
        self.svc.add_iron(self.T0 + 100 * DAY, 2, 0.07, "interim")       # RV 注意 0.05、警戒 0.10
        d = decide(self.svc, 2, self.T0 + 101 * DAY)
        self.assertTrue(d["iron"]["past_knee"])
        self.assertNotIn("days_to_alert", d["iron"])
        self.assertGreaterEqual(d["drive"]["level"], 1)
        self.assertIn("已過轉折點", d["drive"]["action"])
        self.assertEqual(d["iron"]["sample_interval_days"], 60)
        self.assertAlmostEqual(d["iron"]["next_sample_ts"], self.T0 + 160 * DAY)
        self.assertTrue(any("每 60 天" in x for x in d["next"]))
        self.svc.add_iron(self.T0 + 110 * DAY, 2, 0.12, "interim")
        d = decide(self.svc, 2, self.T0 + 111 * DAY)
        self.assertEqual(d["iron"]["sample_interval_days"], 21)

    def test_w_acceleration_escalates(self):
        from dtm.cross import iron_analysis
        from dtm.life import Hours
        st = self.svc.store
        hrs = [12000, 13000, 14000, 15000]
        for i, (h, wt) in enumerate(zip(hrs, [0.010, 0.012, 0.0145, 0.0185])):  # 每 1000 h 增量 2→2.5→4（加速）
            ts = self.T0 + (i + 1) * 50 * DAY
            st.add_hours(ts, h)
            st.add_iron(ts, 2, wt, "interim")
        res = iron_analysis(st, self.cfg, Hours(st, self.cfg), 2, self.T0 + 210 * DAY, None)
        self.assertTrue(res.get("w_accel"))
        self.assertGreaterEqual(res["sev"], 0.5)

    def test_temperature_alone_caps_at_observe(self):
        from dtm.cross import decide
        self.svc.thermal[2] = {"sev": 1.5, "level": 1, "rise": True, "alarm": True, "message": "溫度突升", "dT": 30.0}
        d = decide(self.svc, 2, self.T0 + DAY)
        self.assertEqual(d["drive"]["level"], 0)
        self.assertEqual(d["drive"]["action"], "觀察")
        fam = d["families"]["溫度突升（ΔT）"]
        self.assertAlmostEqual(fam, 0.9)                                  # 1.5 × 比重 0.6

    def test_weights_from_config_and_check(self):
        from dtm.check import ERROR, run_check
        from dtm.cross import weights
        self.cfg["decision"]["weights"]["iron"] = 0.5
        self.assertEqual(weights(self.cfg)["iron"], 0.5)
        p = os.path.join(os.path.dirname(self.cfg["storage"]["db"]), "bad.toml")
        with open(os.path.join(ROOT, "config.sim.toml"), encoding="utf-8") as f:
            base = f.read()
        with open(p, "w", encoding="utf-8") as f:
            f.write(base + "\n[decision.weights]\nironn = 1.0\ntemp_rise = 3\n")
        rep = run_check(p, False, 1)
        errs = [x for x in rep.items if x[0] == ERROR] if hasattr(rep, "items") else []
        txt = "\n".join(rep.lines())
        self.assertIn("ironn", txt)
        self.assertIn("temp_rise", txt)
        self.assertGreaterEqual(rep.count(ERROR), 2, errs)


class TestCentralIronJob(unittest.TestCase):
    def test_auto_job_created_moved_and_removed(self):
        from dtm.central import CentralStore
        cfg = tmp_cfg(system={"role": "central"})
        cs = CentralStore(os.path.join(os.path.dirname(cfg["storage"]["db"]), "c.sqlite3"), cfg, local=True)

        def push(nxt):
            dec = {"iron_next_ts": nxt, "iron_interval_days": 60} if nxt else {}
            cs.ingest({"site": {"site_id": "S1"}, "robots": [{"key": "S1/R1", "meta": {"name": "R1", "axes": [2]},
                       "status": {"axes": [{"axis": 2, "name": "J2", "decision": dec}]}}]}, notify=False)
            return cs.q("SELECT * FROM jobs WHERE kind='iron'")
        j = push(1_900_000_000.0)
        self.assertEqual(len(j), 1)
        self.assertEqual(j[0]["created_by"], "DTM")
        self.assertIn("60", j[0]["title"])
        j = push(1_900_000_000.0 + 10 * DAY)
        self.assertEqual(len(j), 1)
        self.assertEqual(j[0]["ts"], 1_900_000_000.0 + 10 * DAY)
        self.assertEqual(push(None), [])                                  # 恢復正常：自動排的撤掉
        cs.save_job({"rkey": "S1/R1", "axis": 2, "kind": "iron", "ts": 1_900_000_000.0, "title": "人工"}, "王")
        self.assertEqual(len(push(None)), 1)                              # 人工排的不動


# ============================================================ 小型模式（邊緣主機沒接中央）
class TestSmallMode(unittest.TestCase):
    def test_small_config_is_valid(self):
        from dtm.check import ERROR, run_check
        rep = run_check(os.path.join(ROOT, "config.small.example.toml"), False, 1)
        self.assertEqual(rep.count(ERROR), 0, "\n".join(rep.lines()))
        self.assertIn("小型模式", "\n".join(rep.lines()))

    def test_launcher_is_cp950_crlf(self):
        with open(os.path.join(ROOT, "start_small.bat"), "rb") as f:
            b = f.read()
        self.assertEqual(b.count(b"\n"), b.count(b"\r\n"))
        self.assertIn("config.small.toml", b.decode("cp950"))

    def test_robot_programs_leave_notifications_to_hub(self):
        import json as js
        from dtm import runtime as R
        from dtm.fleet import apply_robot
        from dtm.service import Service
        cfg = C.load(os.path.join(ROOT, "config.small.example.toml"))
        d = tempfile.mkdtemp()
        cfg["_path"] = os.path.join(d, "config.small.toml")
        cfg["robot"]["source"] = "sim"
        rc = apply_robot(cfg, "R01")
        rc["web"]["users_db"] = os.path.join(d, "u.sqlite3")
        svc = Service(rc)
        gate = svc.notifier.gate
        self.assertIsNotNone(gate)
        sup = os.path.join(R.run_dir(rc), "status__supervisor.json")
        os.makedirs(os.path.dirname(sup), exist_ok=True)
        with open(sup, "w") as f:
            js.dump({"loop_ts": __import__("time").time()}, f)
        self.assertEqual(gate(), "none")                                 # 主程式正常：機器人程式不各自發
        with open(sup, "w") as f:
            js.dump({"loop_ts": 1.0}, f)
        self.assertEqual(gate(), "alert_only")                           # 主程式停擺：自己發警戒當備援

    def test_hub_store_notifies_and_writes_site_rules(self):
        from dtm.central import CentralStore
        from dtm.sync import apply_rules
        cfg = tmp_cfg(system={"role": "edge", "site_id": "S1"})
        cs = CentralStore(os.path.join(os.path.dirname(cfg["storage"]["db"]), "_fleet.sqlite3"), cfg, local=True)
        sent = []

        class Fake:
            def send(self, *a, **k):
                sent.append((a, k))
        cs.notifier = Fake()
        payload = {"site": {"site_id": "S1"}, "robots": [{"key": "S1/R01", "meta": {"name": "R01", "axes": [2]},
                   "status": {}, "alarms": [{"id": 1, "ts": __import__("time").time(), "axis": 2, "kind": "健康",
                                             "akey": "health:2", "level": 2, "message": "J2 警戒", "value": 1.0,
                                             "acked": 0, "cleared_ts": None, "mtime": 1.0}]}]}
        cs.ingest(payload, notify=True)
        self.assertEqual(sent, [])                                        # 一般邊緣：本機資料庫不發
        cs.hub = True
        payload["robots"][0]["alarms"][0].update(id=2, akey="health:3", axis=3)
        cs.ingest(payload, notify=True)
        self.assertEqual(len(sent), 1)                                    # 小型模式：由本機廠區資料庫統一發
        self.assertEqual(sent[0][1]["robot"], "R01")
        comps = [{"id": i, "axis": 2, "comp": "reducer", "install_ts": 0, "removed_ts": 1, "life_h": h, "failed": 1}
                 for i, h in enumerate([20000, 25000, 30000, 35000], 1)]
        cs.ingest({"site": {"site_id": "S1"}, "robots": [{"key": "S1/R01", "meta": {"name": "R01"}, "status": {},
                                                         "components": comps}]}, notify=False)
        rules = cs.compute_rules()
        self.assertEqual(rules["scope"], "site")
        self.assertIn("reducer", rules["life_prior"])
        rc = tmp_cfg()
        self.assertTrue(apply_rules(rc, rules))
        self.assertEqual(rc["life"]["prior"]["reducer"]["from"], "site")
        from dtm.life import Hours, life_model
        from dtm.store import Store
        st = Store(rc["storage"]["db"])
        m = life_model(st, rc, "reducer", Hours(st, rc), 1_800_000_000.0)
        self.assertIn("本廠機群", m["source"])


# ============================================================ 原始片段保留、分析匯出、拆檢表單
JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")


def _samples(store, t0, n, step=0.2):
    from dtm.model import Sample
    store.add_samples([Sample(ts=t0 + i * step, dt={2: 1.0, 3: 2.0}, seg=0, run=0, ovr=100.0, temp=40.0) for i in range(n)])


class TestRawKeep(unittest.TestCase):
    def test_purge_keeps_marked_spans(self):
        from dtm.store import Store
        cfg = tmp_cfg()
        st = Store(cfg["storage"]["db"])
        now = 1_800_000_000.0
        old = now - 20 * DAY
        _samples(st, old, 100)                                            # 20 秒
        st.add_raw_keep("alarm", old + 5, old + 10, 2, "health:2")
        st.add_raw_keep("diag", now - 200 * DAY, now - 199 * DAY)         # 超過 180 天的標記會被清掉
        st.purge(7, 180, now=now)
        with st.conn() as c:
            n = c.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            k = c.execute("SELECT COUNT(*) FROM raw_keep").fetchone()[0]
        self.assertEqual(n, 26)                                           # 5 秒 ÷ 0.2 + 1
        self.assertEqual(k, 1)

    def test_incremental_purge_matches_full(self):
        """每小時只清新過期的時段與剛到期的保留片段；結果要跟每次完整掃描一樣，每天仍完整掃一次。"""
        from dtm.store import Store
        cfg = tmp_cfg()
        st = Store(cfg["storage"]["db"])
        now = 1_800_000_000.0

        def cnt():
            with st.conn() as c:
                return c.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        _samples(st, now - 8 * DAY, 3000, step=60.0)                      # 8 天前起，每分鐘一筆，共 50 小時
        st.add_raw_keep("alarm", now - 8 * DAY + 600, now - 8 * DAY + 1200)  # 10 分鐘的保留片段（11 筆）
        st.purge(7, 180, now=now)                                         # 第一次：完整掃描
        self.assertEqual(cnt(), 3000 - 1440 + 11)                         # 超過 7 天的刪掉、保留片段留著
        st.purge(7, 180, now=now + 3600)                                  # 一小時後：只清新過期的那一小時
        self.assertEqual(cnt(), 3000 - 1500 + 11)
        _samples(st, now - 30 * DAY, 5, step=60.0)                        # 晚到的舊資料：增量清理不會掃到
        st.purge(7, 180, now=now + 7200)
        self.assertEqual(cnt(), 3000 - 1560 + 11 + 5)
        st.purge(7, 8, now=now + 3 * 3600)                                # 保留片段到期：片段內的樣本跟著清掉
        self.assertEqual(cnt(), 3000 - 1620 + 5)
        self.assertEqual(st.raw_keeps(), [])
        st.purge(7, 180, now=now + 2 * DAY)                               # 隔天：完整掃描，晚到的舊資料也清掉
        self.assertEqual(cnt(), 0)
        st.forget_caches()
        self.assertEqual(st.purge(7, 180, now=now + 2 * DAY), 0)

    def test_keep_lookup_with_mixed_span_lengths(self):
        """查「在不在保留片段內」用片段最大長度限定範圍：長短片段混在一起時結果要跟逐一比對一樣。"""
        from dtm.store import Store, outside_keep
        st = Store(tmp_cfg()["storage"]["db"])
        now = 1_800_000_000.0
        old = now - 20 * DAY
        _samples(st, old, 2000, step=1.0)
        st.add_raw_keep("diag", old + 100, old + 105)
        st.add_raw_keep("alarm", old + 300, old + 1500)                    # 長片段：裡面的樣本離短片段很遠
        st.add_raw_keep("alarm", old + 1400, old + 1410)
        with st.conn() as c:
            cond, p = outside_keep(c)
            fast = c.execute(f"SELECT COUNT(*) FROM samples WHERE {cond}", p).fetchone()[0]
            slow = c.execute("SELECT COUNT(*) FROM samples WHERE NOT EXISTS (SELECT 1 FROM raw_keep k "
                             "WHERE k.t0<=samples.ts AND k.t1>=samples.ts)").fetchone()[0]
        self.assertEqual(fast, slow)
        self.assertEqual(fast, 2000 - 6 - 1201)
        st.purge(7, 180, now=now)
        with st.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 6 + 1201)

    def test_cycle_codes_cached_scan(self):
        """動作代碼清單：每天完整掃一次逐筆循環表，其他時候只看最近 3 天＋每日中位數表，新代碼馬上出現。"""
        from dtm.store import Store
        st = Store(tmp_cfg()["storage"]["db"])
        now = time_now()

        def cyc(ts, code, ax):
            st.add_cycles([{"ts": ts, "t0": ts - 10, "code": code, "axis": ax, "dur": 10.0, "n": 50, "rms": 1.0,
                            "mean": 0.0, "mean_abs": 0.8, "peak": 2.0, "std": 0.5, "valid": 1}])
        cyc(now - 100 * DAY, 9001, 2)                                     # 很久以前、沒有每日中位數
        self.assertEqual(st.cycle_codes(), [(9001, 2)])                   # 第一次：完整掃描
        cyc(now - 50, 9002, 3)
        self.assertEqual(st.cycle_codes(), [(9001, 2), (9002, 3)])        # 新代碼：看最近 3 天就找得到
        st.forget_caches()
        self.assertEqual(st.cycle_codes(), [(9001, 2), (9002, 3)])

    def test_alarm_marks_span_and_backup_keeps_it(self):
        import sqlite3
        import zipfile
        from dtm import backup as B
        from dtm.service import Service
        cfg = tmp_cfg()
        cfg["storage"]["backup_dir"] = os.path.join(os.path.dirname(cfg["storage"]["db"]), "bk")
        svc = Service(cfg, notify=False)
        t = 1_800_000_000.0
        _samples(svc.store, t - 1000, 10000)                              # 2000 秒
        svc.alarms.set("health:2", 2, "健康", 1, "J2 注意", 1.2, t)
        keeps = svc.store.raw_keeps()
        self.assertEqual((keeps[0]["kind"], keeps[0]["t0"], keeps[0]["t1"]), ("alarm", t - 600, t + 600))
        z = B.backup_now(cfg)
        d = tempfile.mkdtemp()
        with zipfile.ZipFile(z) as zf:
            zf.extractall(d)
        c = sqlite3.connect(os.path.join(d, os.path.basename(cfg["storage"]["db"])))
        n = c.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        c.close()
        self.assertEqual(n, 6001)                                         # 只備份警報前後 10 分鐘


class TestTeardown(unittest.TestCase):
    def setUp(self):
        from dtm.service import Service
        self.cfg = tmp_cfg()
        self.svc = Service(self.cfg, notify=False)
        self.T0 = 1_800_000_000.0
        self.svc.store.install(3, "reducer", self.T0 - 400 * DAY, "R", "")
        self.eid = self.svc.register_event(self.T0, 3, "reducer_replace", "換減速機", failed=True, who="王")

    def comp(self):
        return [c for c in self.svc.store.components("reducer") if c["removed_ts"]][0]

    def test_photo_and_form(self):
        from dtm import teardown as TD
        import base64
        name = TD.save_photo(self.cfg, "data:image/jpeg;base64," + base64.b64encode(JPEG).decode(), f"ev{self.eid}")
        self.assertTrue(TD.photo_path(self.cfg, name))
        with self.assertRaises(ValueError):
            TD.save_photo(self.cfg, base64.b64encode(b"<svg></svg>").decode())     # 不是照片
        self.assertIsNone(TD.photo_path(self.cfg, "../config.toml"))
        td = self.svc.set_teardown(self.eid, {"location": ["crank_bearing"], "modes": ["spalling", "wear"],
                                              "grease": "metal", "photos": [name], "note": "外環剝離"}, "李")
        self.assertIn("剝離", TD.text(td))
        c = self.comp()
        self.assertEqual(json.loads(c["teardown"])["modes"], ["spalling", "wear"])
        self.assertEqual(c["failed"], 1)
        self.assertEqual(self.svc.store.event(self.eid)["detail"]["teardown"]["grease"], "metal")
        with self.assertRaises(ValueError):
            self.svc.set_teardown(self.eid, {"modes": ["bogus"]}, "李")
        with self.assertRaises(ValueError):
            self.svc.set_teardown(self.eid, {}, "李")                     # 什麼都沒填

    def test_no_fault_becomes_suspension_and_undo_restores(self):
        self.svc.set_teardown(self.eid, {"no_fault": True, "note": "拆開無損傷"}, "李")
        self.assertEqual(self.comp()["failed"], 0)                        # 壽命統計改當預防更換
        self.svc.undo_event(self.eid, "李", "登錄錯誤")
        old = [c for c in self.svc.store.components("reducer")][0]
        self.assertIsNone(old["removed_ts"])
        self.assertFalse(old["teardown"])

    def test_only_replacement_events(self):
        eid = self.svc.register_event(self.T0, 3, "grease_change", "換脂")
        with self.assertRaises(ValueError):
            self.svc.set_teardown(eid, {"note": "x"}, "李")


class TestAnalysisExport(unittest.TestCase):
    def test_zip_contents_sanitized_and_anonymized(self):
        import base64
        import zipfile
        from dtm import export as EX
        from dtm import teardown as TD
        from dtm.service import Service
        cfg = tmp_cfg()
        cfg["robot"].update(name="三廠R14", line="L3 堆疊")
        cfg["system"].update(site_name="三廠")
        cfg["notify"]["sms"].update(enabled=True, to=["0912345678"])
        cfg["notify"]["mail_to"] = ["boss@example.com"]
        cfg["notify"]["smtp_password"] = "pw-123"
        cfg["source"]["fanuc_http"]["host"] = "192.168.10.24"
        svc = Service(cfg, notify=False)
        T0 = 1_800_000_000.0
        svc.store.add_features([_feat(T0 + i * DAY, 2) for i in range(5)])
        svc.store.install(2, "reducer", T0 - 300 * DAY, "三廠R14", "")
        eid = svc.register_event(T0 + 2 * DAY, 2, "reducer_replace", "三廠R14 換件", failed=True, who="王小明")
        ph = TD.save_photo(cfg, base64.b64encode(JPEG).decode(), "ev")
        svc.set_teardown(eid, {"modes": ["pitting"], "photos": [ph]}, "王小明")
        _samples(svc.store, T0 + 3 * DAY, 50)
        svc.store.add_raw_keep("alarm", T0 + 3 * DAY, T0 + 3 * DAY + 5, 2, "health:2")
        out = os.path.join(tempfile.mkdtemp(), "a.zip")
        res = EX.export_robot_db(cfg, out, None, None, anonymize=True)
        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
            for f in ("features.csv", "motion_daily.csv", "events.csv", "alarms.csv", "iron.csv", "vibration.csv",
                      "components.csv", "hours.csv", "calibration.csv", "system_events.csv", "config_sanitized.json",
                      "raw_segments.csv", "README.txt", f"photos/{ph}"):
                self.assertIn(f, names)
            blob = b"".join(z.read(n) for n in names if not n.startswith("photos/")).decode("utf-8")
            conf = json.loads(z.read("config_sanitized.json"))
            ev = z.read("events.csv").decode("utf-8-sig")
        for secret in ("0912345678", "boss@example.com", "pw-123", "192.168.10.24", "三廠", "王小明",
                       os.path.dirname(cfg["storage"]["db"])):
            self.assertNotIn(secret, blob, secret)
        self.assertEqual(conf["robot"]["name"], "ROBOT-01")
        self.assertIn("ROBOT-01 換件", ev)
        self.assertIn("操作人1", ev)
        self.assertEqual(res["counts"]["raw_segments.csv"], 26)
        self.assertEqual(res["counts"]["features.csv"], 5)
        res2 = EX.export_robot_db(cfg, out, datetime_str(T0 + 2 * DAY), datetime_str(T0 + 3 * DAY))
        self.assertEqual(res2["counts"]["features.csv"], 2)

    def test_web_admin_only_and_one_time_link(self):
        import threading
        import urllib.request
        from dtm.auth import Auth, UserStore
        from dtm.service import Service
        from dtm.web import serve
        cfg = tmp_cfg()
        svc = Service(cfg, notify=False)
        us = UserStore(cfg["web"]["users_db"])
        us.add("adm", "admin-pw-1", "admin", "管理員")
        us.add("eng", "engineer-pw", "engineer", "工程師")
        httpd = serve(svc, "127.0.0.1", 0, auth=Auth(cfg))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def call(path, body=None, tok=None):
            h = {"Content-Type": "application/json", **({"X-DTM-Token": tok} if tok else {})}
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                         method="POST" if body is not None else "GET", headers=h)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                    return r.status, (json.loads(data) if "json" in r.headers.get("Content-Type", "") else data)
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")
        try:
            eng = call("/api/login", {"username": "eng", "password": "engineer-pw"})[1]["token"]
            adm = call("/api/login", {"username": "adm", "password": "admin-pw-1"})[1]["token"]
            self.assertEqual(call("/api/export/analysis", {}, eng)[0], 403)
            code, j = call("/api/export/analysis", {"anonymize": True}, adm)
            self.assertEqual(code, 200, j)
            code, body = call(j["url"])
            self.assertEqual(code, 200)
            self.assertTrue(body.startswith(b"PK"))
            self.assertEqual(call(j["url"])[0], 404)                       # 一次性
            eid = svc.register_event(1_800_000_000.0, 2, "reducer_replace", "x")
            import base64
            code, ph = call("/api/teardown/photo", {"event_id": eid, "data": base64.b64encode(JPEG).decode()}, eng)
            self.assertEqual(code, 200, ph)
            code, j = call("/api/teardown", {"event_id": eid, "modes": ["wear"], "photos": [ph["file"]]}, eng)
            self.assertEqual(code, 200, j)
            code, evs = call("/api/events")
            e = [x for x in evs if x["id"] == eid][0]
            self.assertIn("磨耗", e["teardown_text"])
            self.assertEqual(call("/api/photo?name=" + ph["file"])[0], 200)
        finally:
            httpd.shutdown()


# ============================================================ 計畫停機、時間偏差、導入檢查報告、門檻回放
class TestDowntime(unittest.TestCase):
    def test_parse_until(self):
        from dtm import downtime as DT
        now = 1_800_000_000.0
        self.assertEqual(DT.parse_until("8h", now), now + 8 * 3600)
        self.assertEqual(DT.parse_until(now + 5), now + 5)
        self.assertTrue(DT.parse_until("2027-01-15 08:00") > 0)
        with self.assertRaises(ValueError):
            DT.parse_until("明天")

    def test_suppresses_comm_alarms_and_watchdog(self):
        from dtm import downtime as DT
        from dtm.model import Sample
        from dtm.service import Service
        from dtm.watchdog import Watchdog
        cfg = tmp_cfg()
        svc = Service(cfg, notify=False)
        now = time_now()
        svc.alarms.set("conn", None, "通訊", 2, "無法讀取", None, now)
        d = svc.set_downtime("4h", "歲修", "王")
        self.assertNotIn("conn", svc.alarms.active)                      # 原本的連線警報解除
        self.assertTrue(svc.rt.quiet())
        svc.rt.last_sample = now - 999
        svc.rt.check_conn(now)
        self.assertNotIn("conn", svc.alarms.active)                      # 停機中不發
        for i in range(30):                                              # 心跳停住也不發
            svc.rt.update(Sample(ts=now + i, dt={2: None, 3: 1.0, 4: 1.0}, hb=1.0))
        self.assertFalse([k for k in svc.alarms.active if k in ("hb",) or k.startswith("inv:")])
        self.assertIn("歲修", DT.label(d))
        from dtm.web import status
        self.assertEqual(status(svc)["downtime"]["note"], "歲修")
        from dtm import runtime as R
        self.assertEqual(R.service_summary(svc)["downtime"]["note"], "歲修")
        wd = Watchdog(cfg)
        p = R.status_path(cfg)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        R.write_json(p, {"ts": now, "loop_ts": now, "last_ts": now - 5000, "robot": "R", "downtime": d})
        wd.started = 0
        self.assertNotEqual(wd.judge()[0], "data")                       # 看門狗不報資料中斷
        self.assertTrue(svc.cancel_downtime("王"))
        self.assertIsNone(DT.get(cfg))
        with self.assertRaises(ValueError):
            svc.set_downtime(str(now + 100 * DAY), "", "王")              # 最長 60 天


def time_now():
    import time as _t
    return _t.time()


class TestClockDrift(unittest.TestCase):
    def test_offset_and_alarm(self):
        from dtm.service import Service
        from dtm.sync import check_clock, clock_offset
        self.assertAlmostEqual(clock_offset(1000.0, 1002.0, {"server_time": 900.0}), 101.0)
        self.assertIsNone(clock_offset(1.0, 2.0, {}))
        cfg = tmp_cfg()
        svc = Service(cfg, notify=False)
        check_clock(cfg, svc.alarms, 95.0)
        self.assertIn("clock", svc.alarms.active)
        self.assertIn("+95", svc.alarms.active["clock"]["message"])
        check_clock(cfg, svc.alarms, 3.0)
        self.assertNotIn("clock", svc.alarms.active)

    def test_central_shows_site_offset(self):
        from dtm import fleetviews as V
        from dtm.central import CentralStore
        cfg = tmp_cfg(system={"role": "central"})
        cs = CentralStore(os.path.join(os.path.dirname(cfg["storage"]["db"]), "c.sqlite3"), cfg, local=True)
        now = time_now()
        cs.ingest({"site": {"site_id": "S1", "sent_ts": now + 120}, "robots": []}, notify=False, now=now)
        e = V.system(cs, now)["edges"][0]
        self.assertAlmostEqual(e["clock_offset_s"], 120, delta=1)
        self.assertTrue(e["clock_bad"])


class TestCommissioning(unittest.TestCase):
    def test_report_and_auto(self):
        import json as js
        from dtm import commissioning as CM
        from dtm.model import RunFeatures, Sample
        from dtm.store import Store
        cfg = tmp_cfg()
        st = Store(cfg["storage"]["db"])
        t0 = 1_800_000_000.0
        rows, t = [], t0
        for i in range(3000):
            t += 0.2 if i % 50 else 0.35                                   # 一點抖動
            if i == 1500:
                t += 60                                                    # 一次 60 秒中斷
            rows.append(Sample(ts=t, dt={2: 1.0, 3: 1.0, 4: 1.0}))
        st.add_samples(rows)
        st.add_features([RunFeatures(t0 + i * 3600, 2, 10 + 0.1 * (i % 3), 1, 0.1, 0, 0.5, 2, None, True, {}) for i in range(6)] +
                        [RunFeatures(t0 + 7 * 3600, 2, None, None, None, None, None, None, None, False,
                                     {"invalid": [{"reason": "等速段太短"}]})])
        st.add_cycles([{"ts": t0 + k * 600, "t0": t0 + k * 600 - 20, "code": 9001, "axis": ax, "dur": 20.0, "ovr": 100.0,
                        "temp": None, "amb": None, "n": 100, "mean": 1, "mean_abs": 1, "rms": 1, "peak": 2, "std": 0.1,
                        "vib_r": None, "vib_a": None, "valid": 1, "reason": ""} for k in range(30) for ax in (2, 3, 4)])
        r = CM.collect(cfg, days=1, until=t0 + DAY)
        s = r["sampling"]
        self.assertEqual(len(s["gaps"]), 1)
        self.assertAlmostEqual(s["gap_max_s"], 60.2, delta=0.5)
        self.assertAlmostEqual(s["rate_hz"], 4.9, delta=0.3)
        self.assertEqual(r["diag"]["total"], 7)
        self.assertEqual(r["diag"]["reasons"][0][0], "等速段太短")
        self.assertEqual(r["motion"]["codes"][0]["cycles"], 30)
        html_ = CM.render(cfg, r)
        for word in ("取樣頻率", "資料中斷", "診斷段切割", "動作代碼涵蓋", "F 變異係數"):
            self.assertIn(word, html_)
        self.assertFalse(CM.auto_due(cfg, st, t0 + 3 * DAY))
        self.assertTrue(CM.auto_due(cfg, st, t0 + 8 * DAY))
        p = CM.auto_generate(cfg, st, None, t0 + 8 * DAY)
        self.assertTrue(os.path.exists(p))
        self.assertFalse(CM.auto_due(cfg, st, t0 + 9 * DAY))             # 只自動產生一次
        self.assertIn("commissioning", js.dumps([e["kind"] for e in st.sys_events()]))


class TestReplay(unittest.TestCase):
    def test_lower_threshold_reports_more(self):
        from dtm import replay as RP
        from dtm.model import RunFeatures
        from dtm.store import Store
        cfg = tmp_cfg()
        st = Store(cfg["storage"]["db"])
        t0 = 1_800_000_000.0
        feats = []
        for i in range(60):
            f = 10.0 + (0.02 * ((i * 7) % 5)) + (0 if i < 20 else 0.08 * (i - 20))   # 第 20 次以後慢慢上升到 1.3 倍
            feats.append(RunFeatures(t0 + i * DAY, 2, f, f * 0.6, 0.01 * f, 0.0, 0.3 + 0.002 * (i % 3), 2.0, None, True, {}))
        st.add_features(feats)
        over = RP.parse_sets(["health.caution_ratio=1.15", "health.alert_ratio=1.25", "decision.weights.iron=0.8"])
        self.assertEqual(over["decision"]["weights"]["iron"], 0.8)
        res = RP.run(st, cfg, over, since=t0, until=t0 + 61 * DAY, axes=[2])
        rows = {r["kind"]: r for r in RP.summary(res)}
        h = rows["health"]
        self.assertGreaterEqual(h["alt_caution"] + h["alt_alert"], h["base_caution"] + h["base_alert"])
        self.assertGreaterEqual(h["alt_alert"], 1)
        self.assertIn("門檻回放", RP.text(res))
        with self.assertRaises(ValueError):
            RP.parse_sets(["health.caution_ratio"])


class TestStaticDemo(unittest.TestCase):
    def test_static_key_matches_page(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from build_static_demo import inject_script, static_key
        self.assertEqual(static_key("/api/c/robot?rkey=P3%2Fr14&days=180"), "robot__days-180__rkey-P3_r14.json")
        self.assertEqual(static_key("/api/c/alerts?tab=active&site=&level="), "alerts__tab-active.json")
        self.assertEqual(static_key("/api/c/teardown/options"), "teardown_options.json")
        with open(os.path.join(ROOT, "dtm", "static", "central.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertIn("window.DTM_STATIC", page)
        self.assertIn("</head>", page)
        self.assertIn('"langs": ["zh-TW", "en"]', inject_script("zh-TW", ["zh-TW", "en"]))

    def test_build_one_language(self):
        """只產生英文：快照放在 data/，i18n.json 是英文、語言選單只列英文，資料文字沒有中文。"""
        import re
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from build_static_demo import build
        out = os.path.join(tempfile.mkdtemp(), "site")
        st = build(out, n_robots=1, langs=["en"])
        self.assertEqual(st["langs"], ["en"])
        with open(os.path.join(out, "data", "i18n.json"), encoding="utf-8") as f:
            i18n = json.load(f)
        self.assertEqual((i18n["lang"], list(i18n["langs"])), ("en", ["en"]))
        with open(os.path.join(out, "index.html"), encoding="utf-8") as f:
            self.assertIn('"lang": "en"', f.read())
        with open(os.path.join(out, "data", "alerts__tab-all.json"), encoding="utf-8") as f:
            rows = json.load(f)["rows"]
        self.assertTrue(rows)
        self.assertEqual([r["message"] for r in rows if re.search(r"[\u3400-\u9fff]", r["message"])], [])


def datetime_str(ts):
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


if __name__ == "__main__":
    unittest.main()

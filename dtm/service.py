"""主服務：讀取 → 即時緩衝 → 儲存 → 即時警報 → 診斷切段 → 健康判定 → 健康警報。"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque

from .alarms import AlarmManager, RealtimeMonitor
from .notify import Notifier, diag_suffix
from .analysis import LEVEL_NAMES, evaluate_axis, load_rows
from .config import COMP_LABELS, EVENT_COMPONENTS, axis_cfg, drive_comp, enabled_axes
from .i18n import N_, _, cat, join
from .convert import Converter
from .model import RunFeatures, Sample
from . import motion as M
from .segment import Segmenter
from .sources import SIM_GREASE_DAY, SIM_GREASE_MASS, SimRobot, make_source
from .store import Store

log = logging.getLogger("dtm")


class Service:
    def __init__(self, cfg: dict, store: Store | None = None, notify: bool = True):
        self.cfg = cfg
        self.axes = enabled_axes(cfg)
        self.store = store or Store(cfg["storage"]["db"])
        self.conv = Converter(cfg, self.store)
        self.notifier = Notifier(cfg, self.store, gate=self._notify_gate(), summary_fn=self.summary_text,
                                 is_acked=self.alarm_acked) if notify else None
        self.alarms = AlarmManager(self.store, self.notifier,
                                   raw_keep_s=float(cfg["storage"].get("raw_keep_alarm_s", 600)))
        self.rt = RealtimeMonitor(cfg, self.conv, self.alarms, self.axes)
        self._dt_cache = (0.0, None)               # 計畫停機（每 10 秒讀一次檔案）
        self.rt.quiet = lambda: self.downtime() is not None
        self.seg = Segmenter(cfg)
        self.sensors = None                     # 感測器閘道（振動、溫度），start() 時依設定啟動
        self.motion = M.MotionTracker(cfg, self.axes, window=self.sensor_window)
        self.motion_health: dict[int, dict] = {}
        self.thermal: dict[int, dict] = {}       # 2.1：溫度突升、換脂後未回降、過熱
        self.last_motion_refresh = 0.0
        self.last_cycle_purge = 0.0
        self._known_codes = {c for c, _a in self.store.cycle_codes()}
        hz = float(cfg["robot"]["poll_hz"])
        n = int(float(cfg["web"]["live_window_s"]) * hz) + 10
        self.live = {ax: deque(maxlen=n) for ax in self.axes}
        self.live_meta: deque = deque(maxlen=n)
        self.lock = threading.RLock()
        self.batch: list[Sample] = []
        self.aggbuf: dict[tuple[int, int], list] = {}
        self.last_flush = time.time()
        self.last_purge = 0.0
        self.health: dict[int, dict] = {}
        self.decisions: dict[int, dict] = {}
        self._dec_hist: dict[str, deque] = {}
        self.status = {"connected": False, "source": "", "last_ts": None, "error": "", "samples": 0,
                       "started": time.time(), "last_run_ts": None, "cycles": 0, "last_cycle_ts": None}
        self.source = None
        self.backup = None
        self.selfcheck: list[dict] = []
        self.finished = False                    # 資料來源正常結束（CSV 重播完）
        self.disk_level = 0
        self.last_disk_check = 0.0
        self._stop = threading.Event()
        for ax in self.axes:
            self.refresh_health(ax)
            self.refresh_motion(ax, raise_alarms=False)
            self.refresh_thermal(ax, raise_alarms=False)
            self.refresh_decision(ax, raise_alarms=False)

    # ------------------------------------------------------------ 通知：備援閘門、確認狀態、每日摘要
    def _notify_gate(self):
        """有接中央（邊緣主機底下的機器人，或單機設定了 [central] url）：平常由中央統一發通知；
        連不上中央超過 edge_fallback_s 才自己發「警戒」。"""
        from .runtime import read_json, run_dir
        cfg = self.cfg
        limit = float(cfg["notify"].get("edge_fallback_s", 300))
        if cfg.get("_edge") and not (cfg.get("central") or {}).get("url"):
            # 邊緣主機沒接中央（小型模式）：由主程式的本機廠區資料庫統一發通知與逐級通報；
            # 主程式停擺超過 edge_fallback_s，機器人程式才自己發「警戒」
            sup = os.path.join(run_dir(cfg), "status__supervisor.json")

            def hub_gate():
                st = read_json(sup, {}) or {}
                ok = st.get("loop_ts")
                return "none" if ok and time.time() - float(ok) < limit else "alert_only"
            return hub_gate
        if cfg["system"].get("role") == "central" or not (cfg.get("central") or {}).get("url"):
            return None
        path = os.path.join(run_dir(cfg), "central_sync.json")

        def gate():
            st = read_json(path, {}) or {}
            ok = st.get("last_ok")
            return "none" if ok and time.time() - float(ok) < limit else "alert_only"
        return gate

    def _commissioning_tick(self, now: float):
        """跑滿 7 天自動產生一次導入檢查報告（data/reports/）。"""
        from . import commissioning as CM
        try:
            if CM.auto_due(self.cfg, self.store, now):
                p = CM.auto_generate(self.cfg, self.store, self.sensors.status() if self.sensors else None, now)
                log.info(_("已產生導入檢查報告：%s"), p)
        except Exception as e:
            log.warning("commissioning report: %s", e)

    # ------------------------------------------------------------ 計畫停機（2.1）
    def downtime(self, now: float | None = None) -> dict | None:
        from . import downtime as DT
        now = now or time.time()
        t, d = self._dt_cache
        if now - t > 10:
            d = DT.get(self.cfg, now)
            self._dt_cache = (now, d)
        return d if d and float(d["until"]) > now else None

    def set_downtime(self, until, note: str = "", who: str = "", now: float | None = None) -> dict:
        from . import downtime as DT
        now = now or time.time()
        d = DT.set_(self.cfg, until, note, who, now)
        self._dt_cache = (now, d)
        for key in DT.COMM_KEYS + tuple(f"inv:{ax}" for ax in self.axes):
            if key in self.alarms.active:                  # 原本作用中的資料中斷類警報直接解除
                self.alarms.set(key, None, N_("通訊"), 0, "", None, now)
        self.store.add_sys_event("downtime", 0, DT.label(d) + (f"｜{who}" if who else ""))
        log.info("downtime until %s by %s", d["until"], who or "-")
        return d

    def cancel_downtime(self, who: str = "", now: float | None = None) -> bool:
        from . import downtime as DT
        now = now or time.time()
        had = DT.clear(self.cfg)
        self._dt_cache = (now, None)
        if had:
            self.store.add_sys_event("downtime", 0, _("取消計畫停機") + (f"｜{who}" if who else ""))
        return had

    def alarm_acked(self, akey: str, robot: str = "", site: str = "") -> bool:
        a = self.alarms.active.get(akey)
        if not a:
            return True                                 # 已解除
        r = self.store.alarm(a["id"])
        return bool(r and r["acked"])

    def summary_text(self) -> str:
        from .analysis import LEVEL_NAMES
        act = self.store.active_alarms()
        lines = [_("{robot}：作用中警報 警戒 {a}、注意 {c}", robot=self.cfg["robot"]["name"],
                   a=sum(1 for x in act if x["level"] >= 2), c=sum(1 for x in act if x["level"] == 1))]
        for x in sorted(act, key=lambda r: -r["level"])[:8]:
            lines.append(f"・[{LEVEL_NAMES.get(x['level'])}] {x['message'][:70]}")
        age = time.time() - self.status["last_ts"] if self.status.get("last_ts") else None
        dt = self.downtime()
        if dt:
            from . import downtime as DT
            lines.append(DT.label(dt))
        elif age is None or age > 600:
            lines.append(_("注意：超過 10 分鐘沒有收到機器人資料"))
        return "\n".join(lines)

    # ------------------------------------------------------------ 感測器時間窗（給動作代碼與診斷段切片）
    def sensor_window(self, ax: int, t0: float, t1: float) -> dict | None:
        return self.sensors.window(ax, t0, t1) if self.sensors else None

    # ------------------------------------------------------------ 生產中監測（動作代碼）
    def refresh_motion(self, ax: int, now: float | None = None, raise_alarms: bool = True,
                       cache: dict | None = None) -> dict:
        res = M.evaluate_axis(self.store, self.cfg, ax, now, cache)
        with self.lock:
            self.motion_health[ax] = res
        if raise_alarms and res["codes"]:
            lv = max(0, res["level"])
            name = axis_cfg(self.cfg, ax)["name"]
            msg = _("{name} 生產監測{level}：", name=name, level=M.LEVEL_NAMES.get(lv)) + res["message"]
            if res.get("days_to_alert") not in (None, 0.0) and lv < 2:
                msg += _("。依趨勢約 {days:.0f} 天達警戒", days=res["days_to_alert"])
            self.alarms.set(f"motion:{ax}", ax, N_("生產監測"), lv, msg, res.get("ratio"), now or time.time())
        return res

    def refresh_thermal(self, ax: int, now: float | None = None, raise_alarms: bool = True,
                        cache: dict | None = None) -> dict:
        from . import thermal as T
        try:
            res = T.evaluate_axis(self.store, self.cfg, ax, self.axes, now, cache)
        except Exception as e:                       # 溫度判斷出錯不影響其他監測
            log.warning("thermal J%s: %s", ax, e)
            return self.thermal.get(ax) or {}
        with self.lock:
            self.thermal[ax] = res
        if raise_alarms:
            name = axis_cfg(self.cfg, ax)["name"]
            lv = res["level"] if res["alarm"] else 0
            self.alarms.set(f"temp:{ax}", ax, N_("溫度"), lv, cat(name, " ", res["message"]) if lv else "",
                            res.get("jump"), now or time.time())
        return res

    def on_cycles(self, rows: list[dict]):
        self.store.add_cycles(rows)
        if rows and rows[0]["code"] not in self._known_codes:      # 第一次看到這個動作代碼：馬上建立清單
            self._known_codes.add(rows[0]["code"])
            self.last_motion_refresh = 0.0
        with self.lock:
            self.status["cycles"] += 1
            self.status["last_cycle_ts"] = rows[0]["ts"] if rows else None

    def motion_tick(self, now: float | None = None, force: bool = False):
        """每 10 分鐘（或換日時）把已結束的日子寫入每日中位數並重新判定。"""
        now = now or time.time()
        new_day = bool(self.last_motion_refresh) and M.day_of(now) != M.day_of(self.last_motion_refresh)
        if not force and not new_day and now - self.last_motion_refresh < 600:
            return
        prev = self.last_motion_refresh
        self.last_motion_refresh = now
        if force or not prev or new_day:
            M.refresh_daily(self.store, now=now)             # 已結束的日子只在換日（或啟動、強制）時重算最近 3 天
        elif now - M.day_start(now) < 3600:
            M.refresh_daily(self.store, since=now - 86400, now=now)   # 換日後一小時內再補寫昨天（換日前剛結束的循環）
        if new_day and prev:
            self._motion_vib(prev)
        series_cache: dict = {}                              # 這一輪生產監測與各軸溫度判斷共用的每日序列
        for ax in self.axes:
            self.refresh_motion(ax, now, cache=series_cache)
            self.refresh_thermal(ax, now, cache=series_cache)
            if self.motion_health.get(ax, {}).get("codes"):
                self.refresh_decision(ax, now)

    def _motion_vib(self, day_ts: float):
        """vib_source 設成動作代碼時：前一天這個動作的振動中位數寫成一筆振動資料（交叉判斷用）。"""
        src = str(self.cfg["sensors"].get("vib_source", "diag")).strip()
        if not self.sensors or not src.isdigit():
            return
        code, day = int(src), M.day_of(day_ts)
        for ax in self.axes:
            r = next((x for x in self.store.daily(ax, code, day) if x["day"] == day), None)
            if not r or (r["n"] or 0) < int(self.cfg["motion"]["min_cycles_day"]):
                continue
            for d, k in (("radial", "vib_r"), ("axial", "vib_a")):
                if r.get(k) is not None:
                    self.store.add_vib(r["ts"], ax, d, {"rms": r[k]}, r.get("temp"), _("動作 {code}", code=code))

    # ------------------------------------------------------------ 健康
    def refresh_health(self, ax: int, now: float | None = None) -> dict:
        rows = load_rows(self.store, self.cfg, ax, since=self.store.last_reset(ax, now), until=now)
        res = evaluate_axis(rows, self.cfg, now)
        res["axis"] = ax
        res["name"] = axis_cfg(self.cfg, ax)["name"]
        with self.lock:
            self.health[ax] = res
        return res

    def _sensor_features(self, feats: list[RunFeatures]) -> list[tuple]:
        """診斷時間窗裡的感測器讀值：補溫度（沒有溫度暫存器時），並整理成要寫入的振動資料。"""
        if not self.sensors:
            return []
        wins = getattr(self.seg, "last_windows", []) or []
        vib_rows = []
        use_diag = str(self.cfg["sensors"].get("vib_source", "diag")).lower() == "diag"
        for f in feats:
            spans = [(t0, t1) for ax, t0, t1, _k in wins if ax == f.axis]
            w = self.sensors.windows(f.axis, spans) if spans else None
            if not w:
                continue
            if f.temp is None and w.get("temp") is not None:
                f.temp = round(w["temp"], 2)
            if use_diag:
                for d in ("radial", "axial"):
                    if w[d]:
                        vib_rows.append((f.ts, f.axis, d, dict(w[d]), w.get("temp")))
        return vib_rows

    def on_features(self, feats: list[RunFeatures]):
        vib_rows = self._sensor_features(feats)
        self.store.add_features(feats)
        wins = getattr(self.seg, "last_windows", []) or []
        if feats:                                        # 診斷段的原始波形長期保留（raw_keep_days）
            t1 = max(f.ts for f in feats)
            t0 = min((w[1] for w in wins), default=t1 - float(self.cfg["diag"]["max_run_s"]))
            self.store.add_raw_keep("diag", t0 - 5, t1 + 5, None, "")
        for ts, ax, d, vals, temp in vib_rows:
            self.store.add_vib(ts, ax, d, vals, temp, _("感測器（診斷）"))
        for f in feats:
            if f.axis not in self.axes:
                continue
            name = axis_cfg(self.cfg, f.axis)["name"]
            if not f.valid:
                bad = join(sorted({i["reason"] for i in f.detail.get("invalid", [])})) or _("缺少正反轉配對")
                self.alarms.set(f"diag:{f.axis}", f.axis, N_("診斷"), 1, _("{name} 診斷資料無效：{bad}", name=name, bad=bad),
                                None, f.ts)
                continue
            self.alarms.set(f"diag:{f.axis}", f.axis, N_("診斷"), 0, "", None, f.ts)
            self._health_alarm(f.axis, self.refresh_health(f.axis, f.ts), f.ts)
            self.refresh_decision(f.axis, f.ts)
        with self.lock:
            self.status["last_run_ts"] = feats[0].ts if feats else None

    def _health_alarm(self, ax: int, h: dict, ts: float):
        name = axis_cfg(self.cfg, ax)["name"]
        lv = h["level"] if h["baseline_ready"] else 0
        msg = _("{name} 健康{level}：", name=name, level=LEVEL_NAMES.get(lv)) + join(h["reasons"])
        if h.get("hint"):
            msg += _("。研判：{hint}", hint=h["hint"])
        if h.get("days_to_alert") not in (None, 0.0) and lv < 2:
            msg += _("。依趨勢約 {days:.0f} 天達警戒", days=h["days_to_alert"])
        ratio = (h["metrics"].get("F") or {}).get("ratio")
        self.alarms.set(f"health:{ax}", ax, N_("健康"), lv, msg, ratio, ts)

    # ------------------------------------------------------------ 交叉判斷與維護事件
    def refresh_decision(self, ax: int, now: float | None = None, raise_alarms: bool = True,
                         force: bool = False) -> dict:
        """force=True：人工修正（例如撤銷事件）後直接採用目前判斷，不等連續 N 次確認。"""
        from .cross import ACTIONS as ACTION_TEXT, decide
        d = decide(self, ax, now)
        with self.lock:
            self.decisions[ax] = d
        if raise_alarms:
            ts = now or time.time()
            confirm = max(1, int(self.cfg["health"]["confirm_runs"]))
            for key, comp in (("grease", COMP_LABELS["grease"]), ("drive", d["drive_label"]), ("motor", COMP_LABELS["motor"])):
                akey = f"dec:{ax}:{key}"
                hist = self._dec_hist.setdefault(akey, deque(maxlen=confirm))
                if force:
                    hist.clear()
                    hist.extend([d[key]["level"]] * confirm)
                else:
                    hist.append(d[key]["level"])
                cur = (self.alarms.active.get(akey) or {}).get("level", 0)
                lv = cur
                if len(hist) >= confirm and min(hist) > cur:        # 連續 N 次達到才升級
                    lv = min(hist)
                elif len(hist) >= confirm and max(hist) < cur:      # 連續 N 次低於才降級
                    lv = max(hist)
                if lv == cur and lv > 0:
                    continue
                msg = _("{name} {comp}：{action}", name=d["name"], comp=comp,
                        action=ACTION_TEXT[key][lv] if lv != d[key]["level"] else d[key]["action"]) + diag_suffix(d["primary"])
                self.alarms.set(akey, ax, N_("維護決策"), lv, msg, d["scores"][key], ts)
        return d

    def register_event(self, ts: float, axis: int | None, kind: str, note: str = "", failed: bool = False,
                       teardown: str = "", hours: float | None = None, who: str = "", code: int | None = None) -> int:
        """登錄保養事件；換脂、換減速機／齒輪箱、換馬達會結束舊元件壽命紀錄並建立新紀錄。
        做了哪些變動記在事件的 detail 裡，登錄錯了可以用 undo_event 整筆還原。"""
        from .life import Hours
        eid = self.store.add_event(ts, axis, kind, note, who)
        detail = {"installed": [], "closed": [], "hours_id": None}
        if kind == "motion_reset":
            detail["code"] = int(code) if code not in (None, "", "all") else None
        if hours is not None:
            detail["hours_id"] = self.store.add_hours(ts, hours)
        axes = [axis] if axis else self.axes
        hrs = Hours(self.store, self.cfg)
        for ax in axes:
            for comp in EVENT_COMPONENTS.get(kind, []):
                cur = self.store.current_component(ax, comp, ts)
                if cur:
                    detail["closed"].append({k: cur[k] for k in ("id", "removed_ts", "life_h", "failed", "reason", "teardown")})
                    is_main = comp == EVENT_COMPONENTS[kind][0]
                    self.store.close_component(cur["id"], ts, hrs.between(cur["install_ts"], ts),
                                               failed and is_main, _("失效") if failed and is_main else _("預防／例行"),
                                               teardown if is_main else "")
                detail["installed"].append(self.store.install(ax, comp, ts, self.cfg["robot"]["name"],
                                                              _("{comp}：{note}", comp=COMP_LABELS[comp], note=note)))
            if ax in self.axes:
                if kind in EVENT_COMPONENTS or kind == "baseline_reset":
                    self.alarms.set(f"health:{ax}", ax, N_("健康"), 0, "", None, ts)
                for comp in EVENT_COMPONENTS.get(kind, []):           # 已處理的元件：解除對應的維護決策警報
                    key = {"grease": "grease", "motor": "motor"}.get(comp, "drive")
                    self._dec_hist.pop(f"dec:{ax}:{key}", None)
                    self.alarms.set(f"dec:{ax}:{key}", ax, N_("維護決策"), 0, "", None, ts)
                self.refresh_health(ax, ts)
        self.store.set_event_detail(eid, detail)
        for ax in axes:
            if ax in self.axes:
                if kind == "motion_reset" or kind in EVENT_COMPONENTS or kind == "baseline_reset":
                    self.alarms.set(f"motion:{ax}", ax, N_("生產監測"), 0, "", None, ts)
                    self.refresh_motion(ax, ts, raise_alarms=False)
                self.refresh_decision(ax, ts)
        return eid

    def undo_event(self, eid: int, who: str = "", note: str = "", now: float | None = None) -> dict:
        """撤銷登錄錯誤的事件：元件壽命紀錄、時數、健康基準、維護決策都回到登錄前的狀態。
        事件本身保留並標記「已撤銷」，留下誰、何時、為什麼撤銷的紀錄。"""
        now = now or time.time()
        ev = self.store.event(eid)
        if not ev:
            raise ValueError(_("找不到事件 #{id}", id=eid))
        if ev.get("undone_ts"):
            raise ValueError(_("事件 #{id} 已經撤銷過了", id=eid))
        det = ev["detail"]
        if ev["kind"] in EVENT_COMPONENTS and not det:
            raise ValueError(_("事件 #{id} 是舊版登錄的，沒有變動紀錄，無法自動還原元件壽命；請改用「重設基準」或手動修正",
                               id=eid))
        for cid in det.get("installed", []):          # 新裝的元件後來又被換掉 → 要先撤銷後面那一筆
            c = self.store.component(cid)
            if c and c["removed_ts"] is not None:
                later = [e for e in self.store.events()
                         if any(o["id"] == cid for o in e["detail"].get("closed", []))]
                ref = f"#{later[0]['id']}" if later else ""
                raise ValueError(_("這次安裝的{comp}之後又被更換過，請先撤銷後面的事件{ref}",
                                   comp=COMP_LABELS.get(c["comp"], c["comp"]), ref=ref))
        self.store.undo_event(eid, now, who, note)
        axes = [ev["axis"]] if ev["axis"] else self.axes
        for ax in axes:
            if ax not in self.axes:
                continue
            for key in ("grease", "drive", "motor"):
                self._dec_hist.pop(f"dec:{ax}:{key}", None)
            self._health_alarm(ax, self.refresh_health(ax), now)
            self.refresh_motion(ax, now)
            self.refresh_decision(ax, now, force=True)
        log.info("undo event #%s (%s) by %s", eid, ev["kind"], who or "-")
        return self.store.event(eid)

    def set_teardown(self, eid: int, form: dict, who: str = "", now: float | None = None) -> dict:
        """拆檢表單：寫進被這筆事件換下來的主要元件（減速機／齒輪箱／馬達）與事件明細。
        「檢查無異常」→ 那筆元件改當預防更換（壽命統計的設限資料）。撤銷事件時會還原。"""
        import json as _json
        from . import teardown as TD
        ev = self.store.event(eid)
        if not ev:
            raise ValueError(_("找不到事件 #{id}", id=eid))
        if ev["kind"] not in TD.REPLACE_KINDS:
            raise ValueError(_("只有更換減速機、齒輪箱、馬達的事件可以填拆檢表單"))
        if ev.get("undone_ts"):
            raise ValueError(_("事件 #{id} 已經撤銷", id=eid))
        td = TD.normalize(self.cfg, form, who, now)
        main = EVENT_COMPONENTS[ev["kind"]][0]
        det = ev["detail"]
        for old in det.get("closed", []):
            c = self.store.component(old["id"])
            if c and c["comp"] == main:
                self.store.set_teardown(c["id"], _json.dumps(td, ensure_ascii=False),
                                        failed=False if td["no_fault"] else None)
        det["teardown"] = td
        self.store.set_event_detail(eid, det)
        axes = [ev["axis"]] if ev["axis"] else self.axes
        for ax in axes:
            if ax in self.axes:
                self.refresh_decision(ax, now, raise_alarms=False)
        log.info("teardown event #%s by %s: %s", eid, who or "-", TD.text(td))
        return td

    def add_iron(self, ts: float, axis: int, wt_pct: float, sample: str = "drain_mid", note: str = ""):
        self.store.add_iron(ts, axis, wt_pct, sample, note)
        if axis in self.axes:
            self.refresh_decision(axis, ts)

    def add_vib(self, ts: float, axis: int, direction: str, vals: dict, temp=None, source: str = "",
                refresh: bool = True):
        self.store.add_vib(ts, axis, direction, vals, temp, source)
        if refresh and axis in self.axes:
            self.refresh_decision(axis, ts)

    # ------------------------------------------------------------ 即時
    def handle(self, s: Sample):
        with self.lock:
            for ax in self.axes:
                self.live[ax].append((s.ts, s.dt.get(ax)))
            self.live_meta.append((s.ts, s.seg, s.run, s.temp))
            self.status.update(connected=True, last_ts=s.ts, error="")
            self.status["samples"] += 1
            self.batch.append(s)
        minute = int(s.ts // 60)
        for ax in self.axes:
            v = s.dt.get(ax)
            if v is None or abs(v) >= float(self.cfg["realtime"]["invalid_abs"]):
                continue
            a = self.aggbuf.setdefault((minute, ax), [0, 0.0, v, v, abs(v)])
            a[0] += 1
            a[1] += v
            a[2], a[3], a[4] = min(a[2], v), max(a[3], v), max(a[4], abs(v))
        self.rt.update(s)
        rows = self.motion.feed(s)
        if rows:
            self.on_cycles(rows)
        feats = self.seg.feed(s)
        if feats:
            self.on_features(feats)

    def flush(self, force: bool = False):
        now = time.time()
        if not force and now - self.last_flush < float(self.cfg["storage"]["batch_s"]):
            return
        with self.lock:
            batch, self.batch = self.batch, []
        if self.disk_level < 2:                           # 磁碟快滿：暫停寫原始樣本，保住特徵、事件、警報
            self.store.add_samples(batch)
        cur_min = int(now // 60)
        done = [k for k in self.aggbuf if force or k[0] < cur_min]
        rows = [(k[0], k[1], a[0], a[1] / a[0], a[2], a[3], a[4]) for k in done for a in [self.aggbuf.pop(k)]]
        self.store.add_agg(rows)
        self.last_flush = now
        if now - self.last_purge > 3600:
            self._commissioning_tick(now)
            self.store.purge(float(self.cfg["storage"]["raw_retention_days"]),
                             float(self.cfg["storage"].get("raw_keep_days", 180)))
            self.store.purge_cycles(float(self.cfg["storage"]["cycle_retention_days"]))
            self.last_purge = now
        self.motion_tick(now)
        if self.notifier:
            self.notifier.tick(now)
        if now - self.last_disk_check > 600:
            self.disk_tick(now)

    def disk_tick(self, now: float | None = None) -> int:
        """磁碟剩餘空間：低於 disk_alarm_pct 注意、低於 disk_critical_pct 警戒（並暫停寫原始樣本）。"""
        from .runtime import disk_info
        now = now or time.time()
        self.last_disk_check = now
        sc = self.cfg["system"]
        di = disk_info(self.cfg["storage"]["db"])
        pct = di["free_pct"]
        lv = 0 if pct is None else 2 if pct < float(sc["disk_critical_pct"]) else 1 if pct < float(sc["disk_alarm_pct"]) else 0
        if lv != self.disk_level:
            self.store.add_sys_event("disk", lv, _("磁碟剩 {pct}%", pct=pct))
        self.disk_level = lv
        msg = _("資料庫所在磁碟只剩 {pct}%（{gb} GB）", pct=pct, gb=di["free_gb"]) + (
            _("，已暫停寫入原始樣本") if lv == 2 else _("，請清理或擴充硬碟"))
        self.alarms.set("sys:disk", None, N_("系統"), lv, msg, pct, now)
        return lv

    # ------------------------------------------------------------ 主迴圈
    def loop(self):
        while not self._stop.is_set():
            self.status["loop_ts"] = time.time()          # 看門狗用：迴圈有在轉
            try:
                if self.source is None:
                    self.source = make_source(self.cfg)
                    self.status["source"] = self.source.name
                s = self.source.read()
                if s is not None:
                    self.handle(s)
            except EOFError:
                log.info(_("資料來源結束"))
                self.finished = True
                break
            except Exception as e:
                log.warning(_("讀取失敗：%s"), e)
                with self.lock:
                    self.status.update(connected=False, error=str(e))
                if self.source is not None and self.cfg["robot"]["source"] in ("modbus",):
                    try:
                        self.source.close()
                    finally:
                        self.source = None
                time.sleep(1.0)
            self.rt.check_conn(time.time())
            self.flush()
        self.flush(force=True)

    def start(self) -> threading.Thread:
        from .backup import BackupScheduler
        if self.cfg["sensors"].get("enabled") and self.sensors is None:
            from .sensors import SensorHub
            self.sensors = SensorHub(self.cfg)
            threading.Thread(target=self.sensors.run, args=(self._stop,), name="dtm-sensors", daemon=True).start()
        t = threading.Thread(target=self.loop, name="dtm-acq", daemon=True)
        t.start()
        self.backup = BackupScheduler(self.cfg, self._stop)
        threading.Thread(target=self.backup.run, name="dtm-backup", daemon=True).start()
        return t

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------ 給網頁用
    def live_snapshot(self, seconds: float) -> dict:
        with self.lock:
            if not self.live_meta:
                return {"axes": {}, "meta": []}
            t_end = self.live_meta[-1][0]
            out = {}
            for ax in self.axes:
                pts = [(t, self.conv.value(ax, v)) for t, v in self.live[ax] if t >= t_end - seconds]
                out[ax] = pts
            meta = [m for m in self.live_meta if m[0] >= t_end - seconds]
        return {"axes": out, "meta": meta}


def simulate_history(cfg: dict, days: int = 180, store: Store | None = None) -> Service:
    """用模擬器產生 N 天的資料，走與現場相同的流程：
    每日一次診斷（干擾扭矩）＋徑向／軸向振動；每 60 天中途鐵粉取樣；J2 第 120 天換脂（換脂前後各取樣）。
    情境：J2 油脂老化 → 換脂；J3 減速機磨耗 → 更換；J4 齒輪箱的馬達軸承劣化 → 檢查馬達。
    另匯入一份「模擬機群」的更換紀錄，示範 Weibull 壽命擬合（數字為示意，不是實際資料）。
    """
    svc = Service(cfg, store=store, notify=False)
    robot = SimRobot(svc.axes, seed=int(cfg["source"]["sim"]["seed"]))
    hz = float(cfg["robot"]["poll_hz"])
    hpd = float(cfg["life"]["hours_per_day"])
    t_base = time.time() - days * 86400
    st = svc.store
    name = cfg["robot"]["name"]
    fleet = {"reducer": ([31000, 38500, 44000, 52000], [36000, 47000]),
             "gearbox": ([28000, 35000, 41000], [30000]),
             "motor": ([26000, 47000, 63000], [40000, 52000])}
    for comp, (fails, cens) in fleet.items():
        for h in fails:
            st.add_history(comp, h, True, robot=_("模擬機群"), reason=_("失效"), note=_("示意資料"))
        for h in cens:
            st.add_history(comp, h, False, robot=_("模擬機群"), reason=_("預防更換"), note=_("示意資料"))
    grease_start = {2: -20, 3: -200, 4: -150}
    acc = {}
    for ax in svc.axes:
        drive = drive_comp(cfg, ax)
        st.install(ax, drive, t_base - 900 * 86400, name, _("模擬：初始安裝"))
        st.install(ax, "motor", t_base - 900 * 86400, name, _("模擬：初始安裝"))
        g0 = grease_start.get(ax, -100)
        st.install(ax, "grease", t_base + g0 * 86400, name, _("模擬：上次換脂"))
        acc[ax] = robot.wear_rate(ax, 0) * (-g0) * hpd / 1000
    rng = robot.rng
    cpd = int(cfg["source"]["sim"].get("cycles_per_day", 40)) if cfg["source"]["sim"].get("motion_codes", True) else 0
    for d in range(days):
        day0 = t_base + d * 86400
        t0 = day0 + 9 * 3600
        for ax in svc.axes:
            acc[ax] += robot.wear_rate(ax, d) * hpd / 1000
            Mg = SIM_GREASE_MASS.get(ax, 1000.0)
            wt = lambda: round(acc[ax] / Mg * 100 * (1 + rng.gauss(0, 0.04)), 4)  # noqa: E731
            if d == SIM_GREASE_DAY and ax == 2:
                svc.add_iron(day0 + 7 * 3600, ax, wt(), "change", _("模擬：換脂排出的舊脂"))
                svc.register_event(day0 + 8 * 3600, 2, "grease_change", _("模擬：J2 換脂"))
                acc[ax] *= 0.3                                   # 換脂完全率 70%
            if d == SIM_GREASE_DAY + 1 and ax == 2:
                svc.add_iron(day0 + 7 * 3600, ax, wt(), "after", _("模擬：換脂後 24 小時殘留"))
            if d in (30, 90, 150, days - 2):
                svc.add_iron(day0 + 7 * 3600, ax, wt(), "interim", _("模擬：中途取樣"))
            for direction, vals in robot.vib(ax, d).items():
                svc.add_vib(t0 - 600, ax, direction, vals, source=_("模擬"), refresh=False)
        seg = Segmenter(cfg)
        for s in robot.diag_run(t0, d, hz, const_s=6.0, ramp_s=0.6):
            feats = seg.feed(s)
            if feats:
                svc.on_features(feats)
        rows = []                                   # 生產循環：動作代碼 9001／9002，分散在一天的生產時段
        for k in range(cpd):
            tc = day0 + 10 * 3600 + k * (12 * 3600 / max(1, cpd))
            temp = robot.temp(d) + rng.uniform(-1.5, 1.5)
            for code in (9001, 9002):
                for s in robot.motion_cycle(tc, d, hz, code, temp=temp):
                    out = svc.motion.feed(s)
                    if out:
                        rows += out
                tc += 4.0
        svc.store.add_cycles(rows)
    M.refresh_daily(svc.store, since=t_base, now=time.time())
    for ax in svc.axes:
        svc.refresh_health(ax)
        svc.refresh_motion(ax)
        svc.refresh_decision(ax, raise_alarms=False)
    svc.last_motion_refresh = time.time()
    return svc

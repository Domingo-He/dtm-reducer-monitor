"""生產中監測：依「動作代碼」逐循環擷取，同一個動作只跟同一個動作比。

生產程式在固定動作前把 R[110] 設成 9000–9899（例如 9001 = 有工件搬運、9002 = 空手回程），動作完寫回 0。
機器人等產品的時間再不規律，動作本身（路徑、速度、加速度）每次都一樣，所以：
1. 每個循環記一筆：各軸干擾扭矩的平均、均方根（RMS）、峰值、標準差，動作長度、倍率、溫度；
   有接感測器閘道時，同一時間窗的振動也一起記（vib_r 徑向、vib_a 軸向）。
2. 單筆會因工件位置、溫度、雜訊上下跳，判定看「每日中位數」：一天幾百筆取中間值，雜訊互相抵消。
3. 每個（代碼, 軸）各自用重設後前 N 個有效日建立基準，之後的每日中位數除以基準得到比值。
4. 改了那段動作的點位或速度，基準就不能比了：登錄「重設動作基準」（可指定代碼）或任何會重設基準的保養事件。

生產扭矩裡有慣性與重力，摩擦只占一部分，所以門檻（預設注意 1.15、警戒 1.30 倍）比診斷程式（1.5／2.0）低。
這些是起始值，請用現場資料修正。
"""
from __future__ import annotations

import functools
import math
import statistics
import time
from datetime import datetime

from .config import RESET_KINDS, axis_cfg
from .i18n import TDict, _, join
from .model import Sample, is_motion
from .trend import extrapolate

MOTION_METRICS = TDict({"rms": "扭矩 RMS", "mean_abs": "扭矩平均絕對值", "peak": "扭矩峰值"})
LEVEL_NAMES = TDict({-1: "建立基準中", 0: "正常", 1: "注意", 2: "警戒"})


def code_name(cfg: dict, code: int) -> str:
    names = (cfg.get("motion") or {}).get("codes") or {}
    n = names.get(str(code)) or names.get(code)
    return f"{code} {n}" if n else str(code)


def day_of(ts: float) -> str:
    """時間戳 → 本地日期字串。每天的中位數要對每一筆循環算一次，用 15 分鐘一格快取
    （各地時區與日光節約的切換點都是 15 分鐘的倍數，同一格裡一定是同一天）。"""
    return _day_of_slot(int(ts // 900))


@functools.lru_cache(maxsize=8192)
def _day_of_slot(slot: int) -> str:
    return datetime.fromtimestamp(slot * 900).strftime("%Y-%m-%d")


def day_start(ts: float) -> float:
    """ts 所在那一天 00:00（本地時間）的時間戳。"""
    return datetime.strptime(day_of(ts), "%Y-%m-%d").timestamp()


class MotionTracker:
    """餵樣本；動作代碼從 A 變成別的值時，回傳剛結束那個循環的各軸資料（list[dict]，一軸一筆）。"""

    def __init__(self, cfg: dict, axes: list[int], window=None):
        self.cfg = cfg
        self.axes = axes
        self.window = window                  # 感測器時間窗查詢：window(軸, t0, t1) -> {"vib_r":…, "vib_a":…, "temp":…}
        self.invalid_abs = float(cfg["realtime"]["invalid_abs"])
        self.code = 0
        self.buf: list[Sample] = []
        self.typical: dict[int, list[float]] = {}   # 每個代碼最近 50 次的長度，用來排除暫停或改速度的循環
        self.last_close = 0.0

    @property
    def state(self) -> dict:
        return {"code": self.code, "t0": self.buf[0].ts if self.buf else None}

    def feed(self, s: Sample) -> list[dict] | None:
        mc = self.cfg["motion"]
        code = int(round(s.seg)) if mc.get("enabled", True) and is_motion(s.seg) and not s.run else 0
        out = None
        if code != self.code:
            if self.code and self.buf:
                out = self._close(s.ts)
            self.code, self.buf = code, []
        if code:
            self.buf.append(s)
        return out

    def _close(self, t_end: float) -> list[dict]:
        mc = self.cfg["motion"]
        buf, code = self.buf, self.code
        t0 = buf[0].ts
        dur = max(t_end - t0, buf[-1].ts - t0)
        reason = ""
        if dur < float(mc["min_s"]):
            reason = _("動作太短（{d:.1f}s），可能中斷", d=dur)
        elif dur > float(mc["max_s"]):
            reason = _("動作太長（{d:.1f}s），可能中途暫停", d=dur)
        if not reason and mc.get("require_full_override", True) and any(
                x.ovr is not None and x.ovr < 99.5 for x in buf):
            reason = _("速度倍率不是 100%")
        hist = self.typical.setdefault(code, [])
        if not reason and len(hist) >= 5:
            typ = statistics.median(hist)
            if typ > 0 and abs(dur - typ) / typ > float(mc["dur_tol"]):
                reason = _("長度 {d:.1f}s 跟平常（{t:.1f}s）差太多", d=dur, t=typ)
        if not reason or len(hist) < 5:
            hist.append(dur)
            del hist[:-50]
        temps = [x.temp for x in buf if x.temp is not None]
        ovrs = [x.ovr for x in buf if x.ovr is not None]
        rows = []
        for ax in self.axes:
            vals = [x.dt.get(ax) for x in buf]
            vals = [v for v in vals if v is not None and abs(v) < self.invalid_abs]
            r = {"ts": t_end, "t0": t0, "code": code, "axis": ax, "dur": round(dur, 3),
                 "ovr": min(ovrs) if ovrs else None, "temp": round(statistics.fmean(temps), 2) if temps else None,
                 "n": len(vals), "valid": 0, "reason": reason, "vib_r": None, "vib_a": None}
            if len(vals) >= 3:
                n = len(vals)
                mean = sum(vals) / n
                r.update(mean=mean, mean_abs=sum(abs(v) for v in vals) / n,
                         rms=math.sqrt(sum(v * v for v in vals) / n), peak=max(abs(v) for v in vals),
                         std=math.sqrt(max(0.0, sum((v - mean) ** 2 for v in vals) / n)), valid=0 if reason else 1)
            else:
                r["reason"] = reason or _("樣本太少（{n} 筆）", n=len(vals))
            if self.window:
                w = self.window(ax, t0, t_end) or {}
                r["vib_r"], r["vib_a"] = w.get("vib_r"), w.get("vib_a")
                if w.get("temp") is not None:            # 這一軸的外殼溫度感測器優先（溫度暫存器只有一個值）
                    r["temp"] = round(w["temp"], 2)
                if w.get("amb") is not None:
                    r["amb"] = round(w["amb"], 2)
            rows.append(r)
        self.last_close = t_end
        return rows


# ------------------------------------------------------------ 每日中位數
def _median(xs):
    xs = [x for x in xs if x is not None and math.isfinite(x)]
    return statistics.median(xs) if xs else None


def aggregate_days(cycles: list[dict]) -> list[dict]:
    """逐筆循環 → 每日（代碼, 軸）中位數。"""
    by: dict[tuple, list[dict]] = {}
    for c in cycles:
        by.setdefault((day_of(c["ts"]), c["code"], c["axis"]), []).append(c)
    out = []
    for (day, code, ax), rs in sorted(by.items()):
        out.append({"day": day, "code": code, "axis": ax, "n": len(rs), "ts": rs[-1]["ts"],
                    **{m: _median([r.get(m) for r in rs]) for m in ("rms", "mean_abs", "peak", "vib_r", "vib_a",
                                                                     "temp", "amb", "dur")}})
    return out


def refresh_daily(store, since: float | None = None, now: float | None = None) -> int:
    """把已結束的日子（不含今天）寫入 motion_daily；今天的即時算，不寫入。回傳寫入筆數。"""
    now = now or time.time()
    today = day_of(now)
    start = datetime.strptime(day_of(since if since is not None else now - 3 * 86400), "%Y-%m-%d").timestamp()
    rows = [r for r in aggregate_days(store.cycles(since=start, until=now)) if r["day"] < today]
    if rows:
        store.upsert_daily(rows)
    return len(rows)


def _resets(store, ax: int, code: int, now: float) -> float:
    t = 0.0
    for e in store.events(ax):
        if e["ts"] > now:
            continue
        if e["kind"] in RESET_KINDS:
            t = max(t, e["ts"])
        elif e["kind"] == "motion_reset":
            c = (e.get("detail") or {}).get("code")
            if c in (None, "", code, str(code)):
                t = max(t, e["ts"])
    return t


def cycle_codes(store, cache: dict | None = None) -> list[tuple[int, int]]:
    """（代碼, 軸）清單；cache：同一輪判斷共用。"""
    if cache is None:
        return store.cycle_codes()
    if "codes" not in cache:
        cache["codes"] = store.cycle_codes()
    return cache["codes"]


def daily_series(store, cfg: dict, ax: int, code: int, now: float | None = None,
                 cache: dict | None = None) -> list[dict]:
    """這個（代碼, 軸）每個有效日的中位數：過去的日子讀 motion_daily，今天即時算。
    cache：同一輪判斷（生產監測＋各軸溫度）共用，同一個（軸, 代碼, 時間）只算一次。呼叫端不能修改回傳的資料。"""
    now = now or time.time()
    if cache is not None:
        hit = cache.get(("daily", ax, code, now))
        if hit is not None:
            return hit
        rows = daily_series(store, cfg, ax, code, now)
        cache[("daily", ax, code, now)] = rows
        return rows
    today = day_of(now)
    rows = [r for r in store.daily(ax, code) if r["day"] < today]
    t_today = datetime.strptime(today, "%Y-%m-%d").timestamp()
    rows += [r for r in aggregate_days(store.cycles(axis=ax, code=code, since=t_today, until=now))]
    have = {r["day"] for r in rows}
    if rows and min(have) > day_of(now - 2 * 86400):          # 剛升級或還沒寫入：補算最近幾天
        extra = aggregate_days(store.cycles(axis=ax, code=code, since=now - 3 * 86400, until=t_today))
        rows = sorted([r for r in extra if r["day"] not in have] + rows, key=lambda r: r["day"])
    return rows


def evaluate_code(store, cfg: dict, ax: int, code: int, now: float | None = None, cache: dict | None = None) -> dict:
    mc = cfg["motion"]
    now = now or time.time()
    metric = mc.get("metric", "rms")
    reset = _resets(store, ax, code, now)
    all_rows = daily_series(store, cfg, ax, code, now, cache)
    rows = [r for r in all_rows if r["ts"] >= reset and (r["n"] or 0) >= int(mc["min_cycles_day"]) and r.get(metric)]
    need = int(mc["baseline_days"])
    res = {"axis": ax, "code": code, "name": code_name(cfg, code), "metric": metric, "level": -1,
           "level_name": LEVEL_NAMES[-1], "days": len(rows), "need_days": need, "baseline": None, "latest": None,
           "ratio": None, "vib_ratio": None, "days_to_alert": None, "days_to_caution": None, "reasons": [],
           "series": [], "reset_ts": reset, "today_n": 0}
    today = day_of(now)
    res["today_n"] = next((r["n"] for r in all_rows if r["day"] == today), 0)
    if not rows:
        return res
    res["latest"] = rows[-1][metric]
    res["latest_day"] = rows[-1]["day"]
    if len(rows) < need:
        res["series"] = [{"day": r["day"], "ts": r["ts"], "v": r[metric], "n": r["n"], "level": -1} for r in rows]
        return res
    base = statistics.median(r[metric] for r in rows[:need])
    vb = _median([r.get("vib_r") for r in rows[:need]])
    cr, ar = float(mc["caution_ratio"]), float(mc["alert_ratio"])
    vcr, var = float(mc["vib_caution_ratio"]), float(mc["vib_alert_ratio"])
    levels = []
    for r in rows:
        ratio = r[metric] / base if base else None
        lv = 2 if ratio is not None and ratio >= ar else 1 if ratio is not None and ratio >= cr else 0
        vr = r["vib_r"] / vb if vb and r.get("vib_r") else None
        if vr is not None:
            lv = max(lv, 2 if vr >= var else 1 if vr >= vcr else 0)
        levels.append(lv)
        res["series"].append({"day": r["day"], "ts": r["ts"], "v": r[metric], "n": r["n"], "level": lv,
                              "ratio": ratio, "vib_r": r.get("vib_r"), "vib_ratio": vr, "temp": r.get("temp")})
    confirm = max(1, int(mc["confirm_days"]))
    overall = 0
    for i in range(len(levels)):
        win = levels[max(0, i - confirm + 1):i + 1]
        if len(win) < confirm:
            continue
        if min(win) > overall:
            overall = min(win)
        elif max(win) < overall:
            overall = max(win)
    last = res["series"][-1]
    res.update(level=overall, level_name=LEVEL_NAMES[overall], baseline=base, ratio=last["ratio"],
               vib_ratio=last["vib_ratio"], vib_base=vb, level_latest=levels[-1])
    if last["ratio"] is not None and last["ratio"] >= cr:
        res["reasons"].append(_("動作 {code} {metric} 每日中位數為基準 {r:.2f} 倍", code=res["name"],
                                metric=MOTION_METRICS.get(metric, metric), r=last["ratio"]))
    if last["vib_ratio"] is not None and last["vib_ratio"] >= vcr:
        res["reasons"].append(_("動作 {code} 振動每日中位數為基準 {r:.2f} 倍", code=res["name"], r=last["vib_ratio"]))
    win = rows[-int(mc["trend_days"]):]
    if len(win) >= 5:
        ex = extrapolate([(r["ts"] - now) / 86400 for r in win], [r[metric] for r in win],
                         {"days_to_caution": cr * base, "days_to_alert": ar * base})
        if ex:
            res["slope_pct_month"] = ex["slope"] * 30 / base * 100 if base else None
            res["days_to_caution"], res["days_to_alert"] = ex["days_to_caution"], ex["days_to_alert"]
            res["trend_accel"] = ex["accel"]
            if ex["accel"] and res["level"] >= 1:
                res["reasons"].append(_("動作 {code} 上升在加速，剩餘天數改用保守估計", code=res["name"]))
    return res


def evaluate_axis(store, cfg: dict, ax: int, now: float | None = None, cache: dict | None = None) -> dict:
    """一個軸所有動作代碼的判定，取最嚴重的那個。"""
    now = now or time.time()
    codes = sorted({c for c, a in cycle_codes(store, cache) if a == ax})
    items = [evaluate_code(store, cfg, ax, c, now, cache) for c in codes]
    ready = [i for i in items if i["level"] >= 0]
    out = {"axis": ax, "name": axis_cfg(cfg, ax)["name"], "codes": items, "level": -1 if not items else
           (max(i["level"] for i in ready) if ready else -1), "ratio": None, "vib_ratio": None,
           "days_to_alert": None, "reasons": []}
    out["level_name"] = LEVEL_NAMES[out["level"]]
    if ready:
        out["ratio"] = max((i["ratio"] for i in ready if i["ratio"] is not None), default=None)
        out["vib_ratio"] = max((i["vib_ratio"] for i in ready if i["vib_ratio"] is not None), default=None)
        d = [i["days_to_alert"] for i in ready if i["days_to_alert"] is not None]
        out["days_to_alert"] = min(d) if d else None
        out["reasons"] = [r for i in ready for r in i["reasons"]]
    out["message"] = join(out["reasons"])
    return out

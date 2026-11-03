"""溫度（2.1）：穩定的高溫只縮短換脂週期；「突然升溫」才當成異常證據。

- 溫升 ΔT＝外殼溫度－參考溫度，用同一個動作代碼的每日中位數（同樣的動作、同樣的負載才能比）。
  參考溫度：有環境溫度感測器就用環境溫度；沒有時用「其他軸外殼溫度的中位數」（至少另外 2 軸有溫度），
  氣溫、冷機、廠房空調造成的共同變化會一起扣掉。連這個也沒有：只顯示、不發警報、不算進判斷。
- 溫度突升（注意）：ΔT 比自己最近 14 天的中位數高 rise_c（預設 8°C），或一天內升 day_rise_c（預設 5°C）
  而且參考溫度沒有同步升。
- 換脂後 post_grease_h（預設 72 小時）內不判突升：新脂攪拌、殘壓都會讓溫度暫時升高。
  超過 72 小時、14 天內仍比換脂前高 post_grease_hold_c → 提醒確認殘壓有沒有釋放、填脂量、油封有沒有滲漏。
- 過熱絕對門檻 overheat_c（預設 0＝停用）：外殼溫度達到就直接發警戒。
- 在維護判斷裡，溫升算在扭矩（摩擦）家族，比重 decision.weights.temp_rise（預設 0.6）；只有溫度異常時最多「觀察」。
"""
from __future__ import annotations

import statistics
import time

from .config import EVENT_COMPONENTS
from .i18n import _, join

GREASE_KINDS = tuple(k for k, comps in EVENT_COMPONENTS.items() if "grease" in comps)
DAY = 86400.0


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def sev_rise(jump: float | None, rise_c: float) -> float | None:
    """ΔT 突升幅度 → 嚴重度：rise_c → 0.5（注意）、2×rise_c → 1.0。"""
    if jump is None:
        return None
    lo = 0.5 * rise_c
    if jump <= lo:
        return 0.0
    if jump <= rise_c:
        return 0.5 * (jump - lo) / (rise_c - lo)
    return min(1.5, 0.5 + 0.5 * (jump - rise_c) / rise_c)


def last_grease_ts(store, ax: int, now: float) -> float | None:
    ts = [e["ts"] for e in store.events(ax) if e["kind"] in GREASE_KINDS and e["ts"] <= now]
    return max(ts) if ts else None


def _series(store, cfg: dict, ax: int, code: int, now: float, cache: dict | None = None) -> dict[str, dict]:
    """（軸, 代碼）的每日中位數。cache：同一輪判斷共用（每一軸都要讀其他軸的序列，不共用會重複算「軸數²」次）。"""
    from .motion import daily_series
    if cache is not None and ("thermal", ax, code, now) in cache:
        return cache[("thermal", ax, code, now)]
    out = {r["day"]: r for r in daily_series(store, cfg, ax, code, now, cache)}
    if cache is not None:
        cache[("thermal", ax, code, now)] = out
    return out


def evaluate_code(store, cfg: dict, ax: int, code: int, axes: list[int], now: float,
                  cache: dict | None = None) -> dict | None:
    tc = cfg["thermal"]
    min_n = int(tc["min_cycles_day"])
    mine = _series(store, cfg, ax, code, now, cache)
    others = {a: _series(store, cfg, a, code, now, cache) for a in axes if a != ax}
    rows = []
    for day in sorted(mine):
        r = mine[day]
        if r.get("temp") is None or (r.get("n") or 0) < min_n:
            continue
        ref, kind = r.get("amb"), "ambient"
        if ref is None:
            oth = [o[day]["temp"] for o in others.values() if day in o and o[day].get("temp") is not None]
            ref, kind = (statistics.median(oth), "axes") if len(oth) >= 2 else (None, None)
        rows.append({"day": day, "ts": r["ts"], "n": r["n"], "temp": r["temp"], "ref": ref, "ref_kind": kind,
                     "dT": r["temp"] - ref if ref is not None else r["temp"]})
    if not rows:
        return None
    if all(r["ref_kind"] == "axes" and abs(r["temp"] - r["ref"]) < 1e-6 for r in rows):
        return None          # 各軸溫度完全相同＝只有一個溫度暫存器，沒有各軸外殼溫度，不能判斷單軸溫升
    last = rows[-1]
    base = [r for r in rows[:-1] if last["ts"] - r["ts"] <= float(tc["baseline_days"]) * DAY]
    out = {"code": code, "day": last["day"], "temp": last["temp"], "dT": last["dT"], "ref": last["ref"],
           "ref_kind": last["ref_kind"], "base": None, "jump": None, "day_jump": None, "rise": False, "days": len(base)}
    if len(base) >= int(tc["min_days"]):
        out["base"] = _med(r["dT"] for r in base)
        out["jump"] = last["dT"] - out["base"]
        prev = base[-1]
        if last["ts"] - prev["ts"] <= 1.5 * DAY:
            out["day_jump"] = last["dT"] - prev["dT"]
            ref_up = (last["ref"] - prev["ref"]) if last["ref"] is not None and prev["ref"] is not None else 0.0
            day_hit = out["day_jump"] >= float(tc["day_rise_c"]) and ref_up < 0.5 * float(tc["day_rise_c"])
        else:
            day_hit = False
        out["rise"] = out["jump"] >= float(tc["rise_c"]) or day_hit
        out["day_hit"] = day_hit
    out["rows"] = rows
    return out


def evaluate_axis(store, cfg: dict, ax: int, axes: list[int], now: float | None = None,
                  cache: dict | None = None) -> dict:
    """一個軸的溫度判斷（取各動作代碼裡升最多的那個）。level：0 正常、1 注意（突升／換脂後未回降）、2 過熱。"""
    now = now or time.time()
    tc = cfg["thermal"]
    rise_c = float(tc["rise_c"])
    from .motion import cycle_codes
    codes = sorted({c for c, a in cycle_codes(store, cache) if a == ax})
    items = [x for x in (evaluate_code(store, cfg, ax, c, axes, now, cache) for c in codes) if x]
    res = {"axis": ax, "level": 0, "rise": False, "hold": False, "overheat": False, "sev": None, "alarm": False,
           "jump": None, "dT": None, "temp": None, "ref_kind": None, "code": None, "message": "", "suppressed": "",
           "codes": len(items)}
    if not items:
        return res
    ready = [x for x in items if x["jump"] is not None]
    top = max(ready, key=lambda x: x["jump"]) if ready else max(items, key=lambda x: x["day"])
    res.update(jump=top["jump"], dT=top["dT"], temp=top["temp"], ref_kind=top["ref_kind"], code=top["code"],
               base=top["base"], day_jump=top.get("day_jump"))
    hot = max((x["temp"] for x in items if x["day"] == max(i["day"] for i in items)), default=None)
    oh = float(tc.get("overheat_c") or 0)
    if oh > 0 and hot is not None and hot >= oh:
        res.update(overheat=True, level=2, alarm=True,
                   message=_("外殼溫度 {t:.1f}°C 達過熱門檻 {oh:.0f}°C", t=hot, oh=oh))
    g_ts = last_grease_ts(store, ax, now)
    since_g = (now - g_ts) / 3600 if g_ts else None
    in_post = since_g is not None and since_g < float(tc["post_grease_h"])
    # 換脂後超過 72 小時、14 天內：比換脂前的中位數還高很多 → 提醒內壓／油封
    if since_g is not None and not in_post and since_g <= 14 * 24 and top["ref_kind"]:
        pre = [r["dT"] for r in top["rows"] if g_ts - 14 * DAY <= r["ts"] < g_ts]
        if len(pre) >= 3 and top["dT"] - statistics.median(pre) >= float(tc["post_grease_hold_c"]):
            res.update(hold=True, hold_c=top["dT"] - statistics.median(pre))
    if top["rise"] and not in_post:
        res["rise"] = True
        res["sev"] = sev_rise(top["jump"], rise_c)
        if top.get("day_hit"):
            res["sev"] = max(res["sev"] or 0.0, 0.5)
    elif top["jump"] is not None and not in_post:
        res["sev"] = sev_rise(top["jump"], rise_c)
    if in_post and top["rise"]:
        res["suppressed"] = _("換脂後 {h:.0f} 小時內不判溫度突升", h=float(tc["post_grease_h"]))
    if not top["ref_kind"]:
        res["sev"] = None                                 # 沒有參考溫度：只顯示，不算進判斷、不發警報
        if res["rise"]:
            res["suppressed"] = _("沒有環境溫度，也沒有其他軸外殼溫度可比，無法排除氣溫影響：只顯示、不發警報")
    msgs = [res["message"]] if res["message"] else []
    if res["hold"]:
        msgs.append(_("換脂後超過 {h:.0f} 小時溫升仍未回降（比換脂前高 {d:.1f}°C）：確認排出口殘壓已釋放、填脂量是否過多、"
                      "油封有無滲漏", h=float(tc["post_grease_h"]), d=res["hold_c"]))
    if res["rise"]:
        how = _("一天內升 {d:.1f}°C", d=top["day_jump"]) if top.get("day_hit") and (top["jump"] or 0) < rise_c \
            else _("比最近 {n} 天中位數高 {d:.1f}°C", n=int(tc["baseline_days"]), d=top["jump"])
        msgs.append(_("溫度突升：溫升 ΔT {dt:.1f}°C，{how}", dt=top["dT"], how=how))
    res["message"] = join(msgs)
    if (res["rise"] or res["hold"]) and top["ref_kind"]:
        res["level"] = max(res["level"], 1)
        res["alarm"] = True
    return res

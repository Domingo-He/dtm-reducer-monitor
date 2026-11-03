"""情境模擬：量測誤報與漏報。

python -m dtm -c config.sim.toml scenarios [--days 365] [--only seasonal_nosensor,grease_nosensor]

- 「不該報警」：機器人健康，只有現場常見的干擾（季節溫差、冷機、換夾具、生產程式變更）。
- 「應該報警」：干擾之外再加上單一軸的真實劣化，確認補償沒有把真的問題一起扣掉。
油脂壽命提醒會在一年內正常出現（夏天高溫加速老化），它是保養提醒、不是誤報，所以情境裡把換脂週期設得很長，
只量干擾本身造成的警報。

模擬器裡的溫度係數、負載影響是「合理的假設」，不是量測值，而且刻意和判定程式的預設係數不同；
目的是檢查判定邏輯在這些干擾下會不會亂報。實際數字仍要用現場資料驗證。
"""
from __future__ import annotations

import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import axis_cfg, drive_comp, enabled_axes
from .i18n import N_, TDict, _
from .segment import Segmenter
from .sources import SIM_VIB_BASE, SimRobot

# 模擬用的「真實」物理（和判定程式裡的預設值刻意不同，避免自己出題自己答）
TRUE_FV_TEMP = 0.030      # 黏性摩擦：溫度每降 1°C 約 +3%
TRUE_FC_TEMP = 0.004      # 固定摩擦：溫度影響很小
WARM = 12.0               # 生產中減速機比環境溫度高的度數
HEALTHY = {2: dict(Fc=60.0, Fv=2.0, G=8.0, ripple=4.0, k_true=2.0),
           3: dict(Fc=40.0, Fv=1.5, G=-5.0, ripple=3.0, k_true=1.5),
           4: dict(Fc=20.0, Fv=0.8, G=1.0, ripple=1.5, k_true=1.0)}
DAY = 86400.0


def ambient_const(day: float) -> float:
    return 25.0


def ambient_season(day: float) -> float:
    """台灣廠房：一月約 16°C、七月約 36°C。第 0 天 = 十月中。"""
    return 26.0 + 10.0 * math.sin(2 * math.pi * (day - 290) / 365.0 + math.pi / 2)


@dataclass
class Scenario:
    key: str
    title: str
    desc: str
    expect: tuple = ()                                        # 應該報警的軸；空的 = 都不該報
    ambient: Callable[[float], float] = ambient_const
    report_temp: bool = True
    warm: Callable[[float], float] = lambda d: WARM          # 診斷當下比環境高幾度（冷機時較低）
    fc_mult: Callable[[float], float] = lambda d: 1.0         # 負載變更造成的固定摩擦變化
    g_add: Callable[[float, int], float] = lambda d, ax: 0.0  # 負載設定錯誤造成的同向偏移
    skip: Callable[[float], bool] = lambda d: False           # 停機（不做診斷）
    fault: Callable[[float, int], tuple] = lambda d, ax: (1.0, 1.0)   # 真實劣化：(Fc 倍數, Fv 倍數)
    events: list = field(default_factory=list)                # (第幾天, 事件類別, 備註)
    known_gap: str = ""                                       # 已知盲點：漏報是預期中的，說明怎麼補
    health: dict = field(default_factory=dict)                # 這個情境要改的 [health] 設定


def _cold(d: float) -> bool:
    return int(d) % 7 == 0 or 129 <= d < 131           # 每週一早上，以及長假（第 120–128 天）後兩天


SCENARIOS = [
    Scenario("control", N_("對照組：恆溫、無變化"), N_("確認健康的機器人在理想條件下不會報警")),
    Scenario("seasonal", N_("季節溫差（有溫度訊號）"), N_("環境 16–36°C 一年循環，診斷時讀得到外殼溫度"),
             ambient=ambient_season),
    Scenario("seasonal_nosensor", N_("季節溫差（沒有溫度訊號）"), N_("同上，但沒有接溫度（設定檔 temp = 0，現場最常見）"),
             ambient=ambient_season, report_temp=False),
    Scenario("cold_start", N_("冷機診斷"), N_("每週一與長假後第一次診斷時減速機還沒熱，比平常低 10°C；含 9 天停機"),
             ambient=ambient_season, warm=lambda d: 2.0 if _cold(d) else WARM,
             skip=lambda d: 120 <= d < 129),
    Scenario("cold_start_nosensor", N_("冷機診斷（沒有溫度訊號）"), N_("同上，沒有接溫度"),
             ambient=ambient_season, report_temp=False, warm=lambda d: 2.0 if _cold(d) else WARM,
             skip=lambda d: 120 <= d < 129),
    Scenario("payload", N_("更換夾具（負載設定已更新）"), N_("第 150 天換較重的夾具：固定摩擦 +5%，同向偏移殘差很小"),
             ambient=ambient_season, fc_mult=lambda d: 1.05 if d >= 150 else 1.0,
             g_add=lambda d, ax: 0.03 * HEALTHY[ax]["Fc"] if d >= 150 else 0.0),
    Scenario("program_change", N_("生產程式變更"), N_("第 200 天起生產節拍變慢，診斷時減速機比以前冷 8°C"),
             ambient=ambient_season, warm=lambda d: 4.0 if d >= 200 else WARM),
    Scenario("payload_wrong", N_("更換夾具但負載設定沒改（應該報警）"),
             N_("第 150 天換夾具卻沒更新負載設定：正反轉同向偏移大增。這是真的設定錯誤，應提示檢查負載設定"),
             expect=(2, 3, 4), ambient=ambient_season,
             g_add=lambda d, ax: 0.8 * HEALTHY[ax]["Fc"] if d >= 150 else 0.0),
    Scenario("grease_nosensor", N_("J2 油脂劣化＋季節溫差＋冷機（沒有溫度訊號，應該報警）"),
             N_("第 120 天起只有 J2 的黏性摩擦逐漸上升到 1.8 倍；確認跨軸共模扣除不會把單軸劣化一起扣掉"),
             expect=(2,), ambient=ambient_season, report_temp=False, warm=lambda d: 2.0 if _cold(d) else WARM,
             fault=lambda d, ax: (1.0, 1.0 + 0.8 * min(1.0, max(0.0, d - 120) / 200) ** 1.5) if ax == 2 else (1.0, 1.0)),
    Scenario("wear_seasonal", N_("J3 減速機磨耗＋季節溫差（有溫度訊號，應該報警）"),
             N_("第 150 天起只有 J3 的固定摩擦逐漸上升到 1.6 倍；確認溫度補償不會把磨耗一起扣掉"),
             expect=(3,), ambient=ambient_season,
             fault=lambda d, ax: (1.0 + 0.6 * min(1.0, max(0.0, d - 150) / 180) ** 2, 1.0) if ax == 3 else (1.0, 1.0)),
]
_ALL_GREASE = lambda d, ax: (1.0, 1.0 + 0.8 * min(1.0, max(0.0, d - 120) / 200) ** 1.5)   # noqa: E731
SCENARIOS += [
    Scenario("grease_all", N_("所有軸油脂一起劣化＋季節溫差（預設設定：已知盲點）"),
             N_("同一天換的油脂一起老化，各軸黏性摩擦同時上升到 1.8 倍；跨軸共模扣除分不出「一起變冷」和「一起老化」"),
             expect=(2, 3, 4), ambient=ambient_season, fault=_ALL_GREASE,
             known_gap=N_("靠油脂壽命（時數／年限）與鐵粉把關；有溫度訊號並校正溫度係數後，改 common_mode = \"auto\" 可補上")),
    Scenario("grease_all_auto", N_("所有軸油脂一起劣化＋季節溫差（有溫度、係數已校正、common_mode = auto，應該報警）"),
             N_("同上，但已用 temp-coef 把溫度係數校正到實際值，並改成只在沒有溫度時才扣共同變化"),
             expect=(2, 3, 4), ambient=ambient_season, fault=_ALL_GREASE,
             health={"common_mode": "auto", "temp_coef_fv": TRUE_FV_TEMP, "temp_coef_fc": TRUE_FC_TEMP}),
]
BY_KEY = {s.key: s for s in SCENARIOS}


class ScenarioRobot(SimRobot):
    """健康的機器人：沒有磨耗、沒有油脂劣化，只有情境指定的干擾。"""

    def __init__(self, axes, sc: Scenario, seed: int = 11):
        super().__init__(axes, seed)
        self.sc = sc

    def p(self, ax):
        return HEALTHY.get(ax, HEALTHY[4])

    def temp(self, day: float) -> float:                       # 診斷時的外殼溫度
        return self.sc.ambient(day) + self.sc.warm(day)

    def state(self, ax, day, temp):
        p = self.p(ax)
        dT = temp - (25.0 + WARM)
        kc, kv = self.sc.fault(day, ax)
        Fc = p["Fc"] * math.exp(-TRUE_FC_TEMP * dT) * self.sc.fc_mult(day) * kc
        Fv = p["Fv"] * math.exp(-TRUE_FV_TEMP * dT) * kv
        return Fc, Fv, p["G"] + self.sc.g_add(day, ax), p["ripple"]

    def vib(self, ax, day):
        out = {}
        for d, base in SIM_VIB_BASE.items():
            v = {k: b * (1 + self.rng.gauss(0, 0.05)) for k, b in base.items()}
            v["kurt"] = base["kurt"] + self.rng.gauss(0, 0.1)
            out[d] = {k: round(val, 4) for k, val in v.items()}
        return out

    @staticmethod
    def wear_rate(ax, day):
        return 0.06

    def diag_run(self, *a, **kw):
        out = super().diag_run(*a, **kw)
        if not self.sc.report_temp:
            for s in out:
                s.temp = None
        return out


def run(cfg: dict, sc: Scenario, days: int = 365, db: str | None = None) -> dict:
    """跑一個情境，回傳各軸的警報統計。"""
    from .service import Service
    from .store import Store
    cfg = dict(cfg)
    cfg["storage"] = dict(cfg["storage"], db=db or os.path.join(tempfile.mkdtemp(), f"sc_{sc.key}.sqlite3"))
    cfg["life"] = dict(cfg["life"], grease_interval_h=1e7, grease_interval_years=100.0)   # 只量干擾，不量油脂壽命提醒
    if sc.health:
        cfg["health"] = dict(cfg["health"], **sc.health)
    svc = Service(cfg, store=Store(cfg["storage"]["db"]), notify=False)
    robot = ScenarioRobot(svc.axes, sc)
    hz = float(cfg["robot"]["poll_hz"])
    t_base = time.time() - days * DAY
    st = svc.store
    name = cfg["robot"]["name"]
    for ax in svc.axes:
        drive = drive_comp(cfg, ax)
        st.install(ax, drive, t_base - 900 * DAY, name, "sim")
        st.install(ax, "motor", t_base - 900 * DAY, name, "sim")
        st.install(ax, "grease", t_base - 10 * DAY, name, "sim")
    acc = {ax: 0.0 for ax in svc.axes}
    hpd = float(cfg["life"]["hours_per_day"])
    runs = 0
    for d in range(days):
        day0 = t_base + d * DAY
        for day_ev, kind, note in sc.events:
            if day_ev == d:
                svc.register_event(day0 + 8 * 3600, None, kind, _(note))
        if sc.skip(d):
            continue
        t0 = day0 + 9 * 3600
        for ax in svc.axes:
            acc[ax] += robot.wear_rate(ax, d) * hpd / 1000
            if d % 60 == 30:
                mass = float(axis_cfg(cfg, ax)["grease_mass_g"]) or 2000.0
                svc.add_iron(day0 + 7 * 3600, ax, round(acc[ax] / mass * 100, 4), "interim", "sim")
            for direction, vals in robot.vib(ax, d).items():
                svc.add_vib(t0 - 600, ax, direction, vals, source="sim", refresh=False)
        seg = Segmenter(cfg)
        for s in robot.diag_run(t0, d, hz, const_s=6.0, ramp_s=0.6):
            feats = seg.feed(s)
            if feats:
                svc.on_features(feats)
        runs += 1
    return summarize(svc, sc, days, runs)


def summarize(svc, sc: Scenario, days: int, runs: int) -> dict:
    rows = svc.store.alarms(limit=100000)
    out = {"key": sc.key, "title": sc.title, "desc": sc.desc, "expect": list(sc.expect), "days": days,
           "runs": runs, "axes": {}, "known_gap": sc.known_gap}
    for ax in svc.axes:
        mine = [r for r in rows if r["axis"] == ax and r["level"] >= 1 and r["kind"] in ("健康", "維護決策")]
        health = [r for r in mine if r["kind"] == "健康"]
        dec = [r for r in mine if r["kind"] == "維護決策"]
        end = time.time()
        spans = sorted((r["ts"], r["cleared_ts"] or end) for r in mine)
        dur, cur = 0.0, None                       # 有警報的天數（各警報期間的聯集）
        for a, b in spans:
            if cur is None or a > cur[1]:
                if cur:
                    dur += cur[1] - cur[0]
                cur = [a, b]
            else:
                cur[1] = max(cur[1], b)
        if cur:
            dur += cur[1] - cur[0]
        out["axes"][ax] = {"health": len(health), "decision": len(dec),
                           "health_max": max([r["level"] for r in health], default=0),
                           "decision_keys": sorted({r["akey"].split(":")[-1] for r in dec}),
                           "alarm_days": round(dur / DAY, 1),
                           "messages": [r["message"] for r in mine][:3]}
    out["false"] = sum(a["health"] + a["decision"] for ax, a in out["axes"].items() if ax not in sc.expect)
    out["missed"] = [ax for ax in sc.expect if ax in out["axes"] and not (out["axes"][ax]["health"] + out["axes"][ax]["decision"])]
    out["per_axis_year"] = round(out["false"] / max(1, len(svc.axes) - len(sc.expect)) * 365 / max(1, days), 2)
    return out


KEY_LABELS = TDict({"grease": "潤滑脂", "drive": "減速機", "motor": "馬達"})


def report_lines(res: list[dict]) -> list[str]:
    lines = []
    for r in res:
        bits = []
        if r["expect"]:
            if r["missed"] and r.get("known_gap"):
                bits.append(_("漏報 {axes}（預期中：{how}）", axes=_("、").join(f"J{a}" for a in r["missed"]),
                              how=_(r["known_gap"])))
            elif r["missed"]:
                bits.append(_("漏報 {axes}", axes=_("、").join(f"J{a}" for a in r["missed"])))
            else:
                bits.append(_("該報的 {axes} 都有報", axes=_("、").join(f"J{a}" for a in r["expect"])))
        bits.append(_("無誤報") if r["false"] == 0 else _("誤報 {n} 次（每軸每年 {y} 次）", n=r["false"], y=r["per_axis_year"]))
        verdict = _("，").join(bits)
        lines.append(f"■ {_(r['title'])}：{verdict}")
        lines.append("    " + _(r["desc"]) + _("（{days} 天、診斷 {runs} 次）", days=r["days"], runs=r["runs"]))
        for ax, a in r["axes"].items():
            if a["health"] or a["decision"]:
                keys = _("、").join(KEY_LABELS.get(k, k) for k in a["decision_keys"])
                lines.append("    " + _("J{ax}：健康警報 {h} 次（最高{lv}）、維護決策 {d} 次{keys}，累計 {days} 天",
                                          ax=ax, h=a["health"], lv=_("警戒") if a["health_max"] >= 2 else _("注意"),
                                          d=a["decision"], keys=f"（{keys}）" if keys else "", days=a["alarm_days"]))
                for m in a["messages"][:2]:
                    lines.append("      · " + m[:110])
    return lines


def run_all(cfg: dict, days: int = 365, only: list[str] | None = None, out=print) -> list[dict]:
    res = []
    for sc in SCENARIOS:
        if only and sc.key not in only:
            continue
        t = time.time()
        r = run(cfg, sc, days)
        r["seconds"] = round(time.time() - t, 1)
        res.append(r)
        if out:
            for line in report_lines([r]):
                out(line)
    return res


__all__ = ["SCENARIOS", "BY_KEY", "Scenario", "ScenarioRobot", "run", "run_all", "report_lines", "enabled_axes"]

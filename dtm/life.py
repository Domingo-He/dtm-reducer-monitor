"""壽命模型：運轉時數、油脂有效老化（含溫度）、Weibull 壽命（含設限資料）。

Weibull：R(t) = exp(−(t/η)^β)。
- 失效更換（failed=1）是完整壽命；預防性更換或仍在使用（failed=0）是「設限」資料，
  只知道「至少活到 t」，擬合時必須納入，否則會低估壽命。
- 已用 t 小時、接下來 Δ 小時內失效的條件機率：1 − R(t+Δ)/R(t)。
- B10：10% 失效的時數 = η·(−ln 0.9)^(1/β)。
"""
from __future__ import annotations

import math
import time

from .config import drive_comp
from .i18n import _


class Hours:
    """運轉時數模型：有時數表紀錄就內插／外推，沒有就用每日運轉時數估算。"""

    def __init__(self, store, cfg: dict):
        self.readings = [(r["ts"], r["hours"]) for r in store.hours()]
        self.hpd = float(cfg["life"]["hours_per_day"])

    def rate_per_day(self) -> float:
        r = self.readings
        if len(r) >= 2 and r[-1][0] > r[0][0]:
            rate = (r[-1][1] - r[0][1]) / ((r[-1][0] - r[0][0]) / 86400)
            return min(24.0, max(0.0, rate))
        return self.hpd

    def at(self, ts: float) -> float:
        r = self.readings
        if not r:
            return ts / 86400 * self.hpd
        if len(r) == 1 or ts >= r[-1][0]:
            return r[-1][1] + (ts - r[-1][0]) / 86400 * self.rate_per_day()
        if ts <= r[0][0]:
            return r[0][1] - (r[0][0] - ts) / 86400 * self.rate_per_day()
        for (t0, h0), (t1, h1) in zip(r, r[1:]):
            if t0 <= ts <= t1:
                return h0 + (h1 - h0) * (ts - t0) / (t1 - t0)
        return r[-1][1]

    def between(self, t0: float, t1: float) -> float:
        return max(0.0, self.at(t1) - self.at(t0))


def grease_age(store, cfg: dict, hours: Hours, ax: int, now: float) -> dict | None:
    """目前這批油脂的年齡；外殼溫度高於參考值時，每 +10°C 有效老化加倍（保守，不因低溫延長）。"""
    g = store.current_component(ax, "grease", now)
    if not g:
        return None
    lc = cfg["life"]
    age_h = hours.between(g["install_ts"], now)
    temps = [t for _, t in store.temps(ax, g["install_ts"], now)]
    tref = float(lc["grease_temp_ref_c"])
    factor = (sum(max(1.0, 2 ** ((t - tref) / 10)) for t in temps) / len(temps)) if temps else 1.0
    eff_h = age_h * factor
    years = (now - g["install_ts"]) / (365.25 * 86400)
    frac_h = eff_h / float(lc["grease_interval_h"]) if float(lc["grease_interval_h"]) > 0 else 0.0
    frac_y = years / float(lc["grease_interval_years"]) if float(lc["grease_interval_years"]) > 0 else 0.0
    frac = max(frac_h, frac_y)
    rate = hours.rate_per_day() * factor
    days_left_h = (float(lc["grease_interval_h"]) - eff_h) / rate if rate > 0 else float("inf")
    days_left_y = (float(lc["grease_interval_years"]) - years) * 365.25
    return {"install_ts": g["install_ts"], "age_h": age_h, "eff_h": eff_h, "temp_factor": factor,
            "interval_h": float(lc["grease_interval_h"]),
            "interval_adj_h": float(lc["grease_interval_h"]) / factor if factor > 0 else None,  # 依溫度補正後的換脂週期
            "years": years, "frac": frac, "basis": _("時數") if frac_h >= frac_y else _("年限"),
            "due_ts": now + max(0.0, min(days_left_h, days_left_y)) * 86400}


# ------------------------------------------------------------ Weibull
def weibull_fit(lives: list[float], failed: list[bool]) -> dict | None:
    """含右設限的最大概似估計：對 β 做格點搜尋，η 有封閉解。"""
    data = [(t, bool(f)) for t, f in zip(lives, failed) if t and t > 0]
    r = sum(1 for _, f in data if f)
    if r < 2:
        return None
    sum_ln_fail = sum(math.log(t) for t, f in data if f)
    best = None
    b = 0.3
    while b <= 10.0:
        s = sum(t ** b for t, _ in data)
        eta = (s / r) ** (1 / b)
        ll = r * math.log(b) - r * b * math.log(eta) + (b - 1) * sum_ln_fail - s / eta ** b
        if best is None or ll > best[0]:
            best = (ll, b, eta)
        b = round(b + 0.01, 2)
    _, beta, eta = best
    return {"beta": beta, "eta_h": eta, "n": len(data), "failures": r}


def b_life(beta: float, eta: float, p: float = 0.10) -> float:
    return eta * (-math.log(1 - p)) ** (1 / beta)


def cond_fail_prob(age_h: float, dt_h: float, beta: float, eta: float) -> float:
    a = (age_h / eta) ** beta
    b = ((age_h + dt_h) / eta) ** beta
    return 1 - math.exp(a - b)


def life_model(store, cfg: dict, comp: str, hours: Hours, now: float) -> dict | None:
    """優先用自己的更換紀錄（含仍在使用的設限資料）擬合；失效筆數不足時用設定的先驗。"""
    lives, failed = [], []
    for c in store.components(comp):
        if c["life_h"] is not None:
            lives.append(c["life_h"])
            failed.append(bool(c["failed"]))
        elif c["install_ts"] is not None and c["removed_ts"] is None and c["install_ts"] <= now:
            lives.append(hours.between(c["install_ts"], now))    # 仍在使用：設限
            failed.append(False)
    nfail = sum(failed)
    fit = weibull_fit(lives, failed) if nfail >= int(cfg["life"]["weibull_min_failures"]) else None
    if fit:
        fit["source"] = _("自有紀錄擬合（{nf} 筆失效、{nc} 筆設限）", nf=fit["failures"], nc=fit["n"] - fit["failures"])
        return fit
    pr = cfg["life"]["prior"].get(comp, {})
    if float(pr.get("beta", 0)) > 0 and float(pr.get("eta_h", 0)) > 0:
        frm = pr.get("from")
        src = _("中央機群壽命模型（自有失效紀錄 {n} 筆，不足擬合）", n=nfail) if frm == "central" else \
            _("本廠機群壽命模型（自有失效紀錄 {n} 筆，不足擬合）", n=nfail) if frm == "site" else \
            _("設定檔先驗（自有失效紀錄 {n} 筆，不足擬合）", n=nfail)
        return {"beta": float(pr["beta"]), "eta_h": float(pr["eta_h"]), "n": len(lives), "failures": nfail,
                "source": src}
    return None


def component_life(store, cfg: dict, hours: Hours, ax: int, comp: str, now: float) -> dict:
    cur = store.current_component(ax, comp, now)
    out = {"comp": comp, "installed": bool(cur), "install_ts": cur["install_ts"] if cur else None,
           "age_h": hours.between(cur["install_ts"], now) if cur else None, "model": None,
           "b10_h": None, "p_fail_lead": None, "lead_days": int(cfg["life"]["spare_lead_days"])}
    m = life_model(store, cfg, comp, hours, now)
    if m and cur:
        out["model"] = m
        out["b10_h"] = b_life(m["beta"], m["eta_h"])
        lead_h = out["lead_days"] * hours.rate_per_day()
        out["p_fail_lead"] = cond_fail_prob(out["age_h"], lead_h, m["beta"], m["eta_h"])
        out["life_used_b10"] = out["age_h"] / out["b10_h"] if out["b10_h"] else None
    return out


def axis_life(store, cfg: dict, ax: int, now: float | None = None) -> dict:
    now = now or time.time()
    h = Hours(store, cfg)
    return {"grease": grease_age(store, cfg, h, ax, now),
            "drive": component_life(store, cfg, h, ax, drive_comp(cfg, ax), now),
            "motor": component_life(store, cfg, h, ax, "motor", now),
            "hours_rate": h.rate_per_day()}

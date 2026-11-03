"""門檻回放試算（2.1）：用已存的診斷特徵與每日動作中位數，套另一組門檻與比重，算過去會報幾次、什麼時候報。

調門檻不用等：先回放「如果當初 caution_ratio 是 1.3，過去一年會報幾次」，再決定要不要改設定檔。
- 健康判定（診斷扭矩）：每次診斷的等級，套連續 confirm_runs 次的遲滯，計算升到注意／警戒的時間。
- 生產監測（動作代碼每日中位數）：每個代碼每天的等級，套連續 confirm_days 天的遲滯。
- 維護決策（選用 --decision，比較慢）：每天用當時為止的資料重算一次換脂／減速機／馬達建議（含證據比重）。
結果是「回放」不是預測：換脂、換件等事件照實際發生的時間重設基準；感測器與鐵粉只用當時已登錄的資料。
"""
from __future__ import annotations

import copy
import time
import tomllib
from datetime import datetime

from .analysis import epochs, load_rows
from .config import RESET_KINDS, _merge, axis_name, enabled_axes
from .i18n import _

DAY = 86400.0


def parse_sets(items: list[str]) -> dict:
    """["health.caution_ratio=1.3", "decision.weights.iron=0.8"] → 巢狀 dict（值用 TOML 語法解析）。"""
    out: dict = {}
    for it in items or []:
        if "=" not in it:
            raise ValueError(_("格式要像 health.caution_ratio=1.3：{x}", x=it))
        k, v = it.split("=", 1)
        try:
            val = tomllib.loads(f"v = {v.strip()}")["v"]
        except tomllib.TOMLDecodeError:
            val = v.strip()
        cur = out
        parts = [p for p in k.strip().split(".") if p]
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = val
    return out


def with_overrides(cfg: dict, over: dict) -> dict:
    alt = copy.deepcopy(cfg)
    for k, v in over.items():
        alt[k] = _merge(alt.get(k, {}), v) if isinstance(v, dict) and isinstance(alt.get(k), dict) else v
    return alt


def _transitions(series: list[tuple[float, int]], confirm: int) -> list[tuple[float, int]]:
    """（時間, 單次等級）→ 套遲滯後，等級「升高」的時間點與新等級。"""
    out, overall, lv_hist = [], 0, []
    for ts, lv in series:
        lv_hist.append(lv)
        win = lv_hist[-confirm:]
        if len(win) < confirm:
            continue
        if min(win) > overall:
            overall = min(win)
            out.append((ts, overall))
        elif max(win) < overall:
            overall = max(win)
    return out


def health_events(store, cfg: dict, ax: int, since: float, until: float) -> list[tuple[float, int]]:
    rows = [r for r in load_rows(store, cfg, ax, 0, until)]
    resets = [e["ts"] for e in store.events(ax) if e["kind"] in RESET_KINDS and e["ts"] <= until]
    confirm = max(1, int(cfg["health"]["confirm_runs"]))
    out = []
    for ep in epochs(rows, resets, cfg):
        series = [(r["ts"], ep["levels"].get(r["id"], -1)) for r in ep["rows"]]
        series = [(t, max(0, lv)) for t, lv in series]
        out += [x for x in _transitions(series, confirm) if since <= x[0] <= until]
    return out


def motion_events(store, cfg: dict, ax: int, since: float, until: float) -> list[tuple[float, int, int]]:
    from . import motion as M
    out = []
    confirm = max(1, int(cfg["motion"]["confirm_days"]))
    for code in sorted({c for c, a in store.cycle_codes() if a == ax}):
        res = M.evaluate_code(store, cfg, ax, code, until)
        series = [(p["ts"], max(0, p["level"])) for p in res["series"]]
        out += [(t, lv, code) for t, lv in _transitions(series, confirm) if since <= t <= until]
    return sorted(out)


class _Svc:
    """回放用的精簡服務物件：只提供 decide() 需要的欄位。"""

    def __init__(self, cfg, store):
        self.cfg, self.store, self.axes = cfg, store, enabled_axes(cfg)
        self.health, self.motion_health, self.thermal = {}, {}, {}

    def refresh_health(self, ax, now=None):
        from .analysis import evaluate_axis
        res = evaluate_axis(load_rows(self.store, self.cfg, ax, since=self.store.last_reset(ax, now), until=now),
                            self.cfg, now)
        self.health[ax] = res
        return res


def decision_events(store, cfg: dict, ax: int, since: float, until: float, step_days: float = 1.0) -> dict:
    """每 step_days 天重算一次維護決策；回傳各建議（grease／drive／motor）升級的時間點。"""
    from . import motion as M
    from .cross import decide
    svc = _Svc(cfg, store)
    out = {"grease": [], "drive": [], "motor": []}
    prev = {"grease": 0, "drive": 0, "motor": 0}
    t = since
    while t <= until:
        svc.refresh_health(ax, t)
        svc.motion_health[ax] = M.evaluate_axis(store, cfg, ax, t)
        d = decide(svc, ax, t)
        for k in out:
            lv = d[k]["level"]
            if lv > prev[k]:
                out[k].append((t, lv))
            prev[k] = lv
        t += step_days * DAY
    return out


def run(store, cfg: dict, over: dict, since: float | None = None, until: float | None = None,
        axes: list[int] | None = None, decision: bool = False, step_days: float = 1.0) -> dict:
    until = until or time.time()
    since = since or until - 365 * DAY
    alt = with_overrides(cfg, over)
    axes = axes or enabled_axes(cfg)
    res = {"since": since, "until": until, "over": over, "axes": []}
    for ax in axes:
        item = {"axis": ax, "name": axis_name(cfg, ax)}
        for key, fn in (("health", health_events), ("motion", motion_events)):
            item[key] = {"base": fn(store, cfg, ax, since, until), "alt": fn(store, alt, ax, since, until)}
        if decision:
            item["decision"] = {"base": decision_events(store, cfg, ax, since, until, step_days),
                                "alt": decision_events(store, alt, ax, since, until, step_days)}
        res["axes"].append(item)
    return res


def _cnt(evs, lv) -> int:
    return sum(1 for e in evs if e[1] == lv)


def summary(res: dict) -> list[dict]:
    """畫面／指令用：每軸每類的「原設定 vs 新設定」次數與第一次時間。"""
    rows = []
    for it in res["axes"]:
        for key, lab in (("health", _("健康判定")), ("motion", _("生產監測"))):
            b, a = it[key]["base"], it[key]["alt"]
            rows.append({"axis": it["axis"], "name": it["name"], "kind": key, "label": lab,
                         "base_caution": _cnt(b, 1), "base_alert": _cnt(b, 2),
                         "alt_caution": _cnt(a, 1), "alt_alert": _cnt(a, 2),
                         "base_first": b[0][0] if b else None, "alt_first": a[0][0] if a else None,
                         "alt_events": [list(x) for x in a][:50], "base_events": [list(x) for x in b][:50]})
        if "decision" in it:
            for k, lab in (("grease", _("換脂建議")), ("drive", _("減速機建議")), ("motor", _("馬達建議"))):
                b, a = it["decision"]["base"][k], it["decision"]["alt"][k]
                rows.append({"axis": it["axis"], "name": it["name"], "kind": f"decision.{k}", "label": lab,
                             "base_caution": _cnt(b, 1), "base_alert": _cnt(b, 2),
                             "alt_caution": _cnt(a, 1), "alt_alert": _cnt(a, 2),
                             "base_first": b[0][0] if b else None, "alt_first": a[0][0] if a else None,
                             "alt_events": [list(x) for x in a][:50], "base_events": [list(x) for x in b][:50]})
    return rows


def text(res: dict) -> str:
    f = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d") if t else "—"  # noqa: E731
    lines = [_("門檻回放：{a} ～ {b}", a=f(res["since"]), b=f(res["until"])),
             _("新設定：{x}", x=", ".join(f"{k}={v}" for k, v in _flat(res["over"]))), "",
             _("軸    項目          注意（原→新）  警戒（原→新）  第一次（原 → 新）")]
    for r in summary(res):
        lines.append(f"{r['name']:<5} {r['label']:<10}  {r['base_caution']:>4} → {r['alt_caution']:<4}   "
                     f"{r['base_alert']:>4} → {r['alt_alert']:<4}   {f(r['base_first'])} → {f(r['alt_first'])}")
    lines += ["", _("回放用的是當時已存的資料；換脂、換件依實際時間重設基準。改設定前請對照拆檢紀錄與現場經驗。")]
    return "\n".join(lines)


def _flat(d: dict, pre: str = ""):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _flat(v, f"{pre}{k}.")
        else:
            yield f"{pre}{k}", v

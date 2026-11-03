"""中央／廠區儀表板的資料（8 個畫面）。只讀 CentralStore；中央伺服器與邊緣主機共用同一套。

畫面：全廠總覽、廠區詳情、機器人詳情、風險與預測、警報與通知、保養與備品、壽命統計、系統健康。
每台機器人每軸的「綜合等級」＝診斷扭矩、生產循環監測、交叉判定（油脂／減速機／馬達）三者最嚴重的一個；
三者都還在建立基準（沒有任何判定）時才算「建立基準中」。
"""
from __future__ import annotations

import math
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .central import DRIVE_PARTS, CentralStore, jload, weibull_points
from .config import EVENT_LABELS, RESET_KINDS, axis_name, drive_comp
from .i18n import N_, TDict, _, localize, render

LV = TDict({-1: "建立基準中", 0: "正常", 1: "注意", 2: "警戒"})
AXES_ALL = (1, 2, 3, 4, 5, 6, 7)
REPLACE_KINDS = ("reducer_replace", "gearbox_replace", "motor_replace")
IMPACTS = ("高", "中", "低")


def _r(v, nd=2):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if math.isfinite(f) else None


def ax_label(cfg, ax) -> str:
    return axis_name(cfg, int(ax)) if ax not in (None, "") else ""


def day_start(ts: float) -> float:
    d = datetime.fromtimestamp(ts)
    return datetime(d.year, d.month, d.day).timestamp()


def eff_level(a: dict) -> int:
    base = max(a.get("level", -1) if a.get("level") is not None else -1, (a.get("motion") or {}).get("level", -1) or -1)
    d = a.get("decision") or {}
    dec = max([(d.get(k) or {}).get("level", 0) or 0 for k in ("grease", "drive", "motor")] or [0]) if d else -1
    if base < 0 and dec <= 0:
        return -1
    return max(base, dec)


def eta_days(a: dict, lv: int) -> float | None:
    if lv >= 2:
        return 0.0
    c = [a.get("days_to_alert"), (a.get("motion") or {}).get("days_to_alert"), (a.get("decision") or {}).get("rul_days")]
    c = [float(x) for x in c if x is not None and math.isfinite(float(x)) and 0 <= float(x) <= 730]   # 兩年以上的外推不可信
    return min(c) if c else None


MONTHS = [N_("1月"), N_("2月"), N_("3月"), N_("4月"), N_("5月"), N_("6月"), N_("7月"), N_("8月"), N_("9月"),
          N_("10月"), N_("11月"), N_("12月")]


def action_text(a: dict) -> str:
    d = a.get("decision") or {}
    for k in ("drive", "motor", "grease"):
        x = d.get(k) or {}
        if (x.get("level") or 0) >= 1:
            return x.get("action", "")
    if a.get("hint"):
        return a["hint"]
    if (a.get("motion") or {}).get("level", -1) >= 1:
        return _("生產循環扭矩偏高：下次停機做診斷程式確認")
    return _("持續觀察")


class Fleet:
    """一次讀進全部機器人與各軸狀態（每個請求一次）。"""

    def __init__(self, cs, now: float | None = None, site: str | None = None, series: bool = False,
                 rkey: str | None = None):
        self.cs = cs
        self.cfg = cs.cfg
        self.now = now or time.time()
        self.sites = {s["site_id"]: s for s in cs.q("SELECT * FROM sites ORDER BY site_id")}
        q, args = "SELECT * FROM robots WHERE 1=1", []
        if site:
            q += " AND site_id=?"
            args.append(site)
        if rkey:
            q += " AND rkey=?"
            args.append(rkey)
        self.robots = cs.q(q + " ORDER BY site_id, name", args)
        by = {r["rkey"]: r for r in self.robots}
        cols = "rkey, axis, name, level, ratio, metrics, days_to_caution, days_to_alert, slope, motion, decision, hint, " \
               "n_runs, need_runs, baseline_ready, temp, updated, tmpl" + (", series, reasons" if series else "")
        for r in self.robots:
            r["axes"] = []
            r["status"] = jload(r.get("status"), {}) or {}
            r["axes_cfg"] = jload(r.get("axes"), []) or []
            s = self.sites.get(r["site_id"]) or {}
            r["site_name"] = s.get("name") or r["site_id"]
            r["disp"] = self.disp(r)
        aq, aargs = f"SELECT {cols} FROM axis_state", []
        if rkey:
            aq += " WHERE rkey=?"
            aargs.append(rkey)
        for a in cs.q(aq + " ORDER BY rkey, axis", aargs):
            r = by.get(a["rkey"])
            if r is None:
                continue
            a["metrics"] = jload(a.get("metrics"), {}) or {}
            a["motion"] = jload(a.get("motion"), {}) or {}
            a["decision"] = jload(a.get("decision"), {}) or {}
            if "reasons" in a:
                a["reasons"] = jload(a.get("reasons"), []) or []
            localize(a, a.pop("tmpl", None))          # 邊緣主機產生的文字依觀看者的語言重新產生
            a["label"] = ax_label(self.cfg, a["axis"])
            a["lv"] = eff_level(a)
            a["ratio_eff"] = a["ratio"] if a["ratio"] is not None else (a["motion"] or {}).get("ratio")
            a["z"] = ((a["metrics"].get("F") or {}).get("z"))
            a["eta"] = eta_days(a, a["lv"])
            a["robot"] = r
            r["axes"].append(a)
        for r in self.robots:
            lvs = [a["lv"] for a in r["axes"]]
            r["lv"] = max(lvs) if lvs else -1
            r["worst"] = max(r["axes"], key=lambda a: (a["lv"], a["ratio_eff"] or 0), default=None)
            r["fresh"] = bool(r.get("last_ts")) and self.now - float(r["last_ts"]) < 600
            dt = r["status"].get("downtime") or {}
            r["downtime"] = dt if float(dt.get("until") or 0) > self.now else None
            r["e1"] = any(a["axis"] == 7 for a in r["axes"]) or 7 in r["axes_cfg"]

    def disp(self, r: dict) -> str:
        nm = r.get("name") or r.get("id") or r["rkey"]
        site = r.get("site_name") or ""
        return nm if not site or str(nm).startswith(site) else f"{site}-{nm}"

    @property
    def axes(self) -> list[dict]:
        return [a for r in self.robots for a in r["axes"]]

    def site_state(self, s: dict) -> dict:
        timeout = float(self.cfg["central"].get("edge_timeout_s", 300))
        hb = self.now - float(s.get("last_seen") or 0)
        lag = s.get("lag_s")
        if hb > timeout:
            st, key = _("離線"), "off"
        elif hb > 180 or (lag is not None and lag > 180):
            st, key = _("延遲"), "late"
        else:
            st, key = _("在線"), "ok"
        return {"state": st, "key": key, "heartbeat_s": round(hb), "lag_s": _r(lag, 0), "last_seen": s.get("last_seen")}


def axis_brief(a: dict) -> dict:
    return {"rkey": a["robot"]["rkey"], "robot": a["robot"]["disp"], "site": a["robot"]["site_id"], "axis": a["axis"],
            "label": a["label"], "lv": a["lv"], "lv_name": LV.get(a["lv"]), "ratio": _r(a["ratio_eff"]),
            "z": _r(a["z"], 1), "eta": _r(a["eta"], 0), "action": action_text(a),
            "primary": (a.get("decision") or {}).get("primary", ""), "impact": a["robot"].get("impact") or "中"}


def alarm_rows(cs, where: str = "1=1", args=(), limit: int = 200) -> list[dict]:
    rows = cs.q("SELECT a.*, r.name AS rname, r.id AS rid, s.name AS sname FROM alarms a "
                "LEFT JOIN robots r ON r.rkey=a.rkey LEFT JOIN sites s ON s.site_id=a.site_id "
                f"WHERE {where} ORDER BY (a.cleared_ts IS NULL) DESC, a.level DESC, a.ts DESC LIMIT ?", (*args, limit))
    stages = {r["akey"]: r["st"] for r in cs.q("SELECT akey, MAX(stage) AS st FROM notify_log GROUP BY akey")}
    out = []
    for a in rows:
        site = a.get("sname") or a["site_id"]
        if a["rkey"].endswith("/_edge"):
            src = _("{site} 邊緣主機", site=site)
        else:
            nm = a.get("rname") or a["rkey"].split("/")[-1]
            src = nm if str(nm).startswith(site or "\0") else f"{site}-{nm}"
        out.append({"id": a["id"], "rkey": a["rkey"], "site": a["site_id"], "src": src, "axis": a["axis"],
                    "axis_label": ax_label(cs.cfg, a["axis"]) if a["axis"] else "", "ts": a["ts"],
                    "level": a["level"], "kind": a["kind"], "kind_label": _(a["kind"]) if a.get("kind") else "",
                    "sys": a["kind"] == "系統", "message": render(a.get("tmpl"), a["message"]), "acked": bool(a["acked"]),
                    "acked_by": a["acked_by"], "acked_ts": a["acked_ts"], "cleared_ts": a["cleared_ts"],
                    "active": a["cleared_ts"] is None, "stage": stages.get(f"{a['rkey']}|{a['akey']}", 0) or 0,
                    "origin": a["origin"]})
    return out


def upcoming_jobs(cs, t0: float, t1: float, site: str | None = None) -> list[dict]:
    q = ("SELECT j.*, r.name AS rname, s.name AS sname FROM jobs j LEFT JOIN robots r ON r.rkey=j.rkey "
         "LEFT JOIN sites s ON s.site_id=j.site_id WHERE j.ts>=? AND j.ts<? ")
    args = [t0, t1]
    if site:
        q += "AND j.site_id=? "
        args.append(site)
    out = []
    for j in cs.q(q + "ORDER BY j.ts", args):
        out.append(job_view(cs, j))
    return out


def job_view(cs, j: dict) -> dict:
    site = j.get("sname") or j.get("site_id") or ""
    nm = j.get("rname") or (j.get("rkey") or "").split("/")[-1]
    robot = (nm if str(nm).startswith(site or "\0") else f"{site}-{nm}") if nm else site
    prep = jload(j.get("prep"), {}) or {}
    return {"id": j["id"], "ts": j["ts"], "date": datetime.fromtimestamp(j["ts"]).strftime("%m/%d"),
            "rkey": j.get("rkey"), "site": j.get("site_id"), "robot": robot, "axis": j.get("axis"),
            "axis_label": ax_label(cs.cfg, j["axis"]) if j.get("axis") else "", "kind": j["kind"],
            "kind_label": CentralStore.JOB_KINDS.get(j["kind"], j["kind"]), "hours": j.get("hours"),
            "status": j.get("status"), "title": j.get("title") or "", "note": j.get("note") or "",
            "workpack": j.get("workpack") or "", "prep": prep, "done_ts": j.get("done_ts"), "done_by": j.get("done_by"),
            "created_by": j.get("created_by")}


def axis_daily_series(cs, rkey: str, axis: int, days: int, now: float) -> list[list]:
    d0 = datetime.fromtimestamp(now - days * 86400).strftime("%Y-%m-%d")
    return [[r["day"], _r(r["ratio"], 3), r["lv"]] for r in cs.q(
        "SELECT day, ratio, MAX(level, motion_level, dec_level) AS lv FROM axis_daily WHERE rkey=? AND axis=? AND day>=? "
        "ORDER BY day", (rkey, axis, d0))]


def demo_flag(cs) -> bool:
    return cs.meta("demo") == "1"


# ============================================================ 1 全廠總覽
def overview(cs, now: float | None = None) -> dict:
    F = Fleet(cs, now)
    now = F.now
    robots = F.robots
    axes = F.axes
    dist = Counter(r["lv"] for r in robots)
    act = alarm_rows(cs, "a.cleared_ts IS NULL", (), 500)
    unacked = [a for a in act if not a["acked"]]
    eta30 = [a for a in axes if a["lv"] < 2 and a["eta"] is not None and a["eta"] <= 30]
    eta90 = [a for a in axes if a["lv"] < 2 and a["eta"] is not None and a["eta"] <= 90]
    em = cs.one("SELECT SUM(n_online) AS on_, SUM(n_robots) AS tot FROM edge_metrics WHERE ts>=?", (now - 86400,)) or {}
    completeness = (100.0 * em["on_"] / em["tot"]) if em.get("tot") else None
    sites_state = [dict(sid=sid, name=s["name"], **F.site_state(s)) for sid, s in F.sites.items()]
    plants = []
    for sid, s in F.sites.items():
        rs = [r for r in robots if r["site_id"] == sid]
        if not rs and s.get("role") == "central":
            continue
        d = Counter(r["lv"] for r in rs)
        worst = max((a for r in rs for a in r["axes"]), key=lambda a: (a["lv"], a["ratio_eff"] or 0), default=None)
        plants.append({"site": sid, "name": s["name"] or sid, "robots": len(rs), "axes": sum(len(r["axes"]) for r in rs),
                       "e1": sum(1 for r in rs if r["e1"]), "n": {str(k): d.get(k, 0) for k in (-1, 0, 1, 2)},
                       "lv": max(d) if d else -1, "worst": axis_brief(worst) if worst else None,
                       "alarms": sum(1 for a in act if a["site"] == sid and a["level"] >= 1),
                       **F.site_state(s)})
    ranked = sorted([a for a in axes if a["lv"] >= 1], key=lambda a: (-a["lv"], a["eta"] if a["eta"] is not None else 9e9,
                                                                       -(a["ratio_eff"] or 0)))[:8]
    risks = []
    for a in ranked:
        b = axis_brief(a)
        b["spark"] = [p[1] for p in axis_daily_series(cs, a["robot"]["rkey"], a["axis"], 60, now)]
        risks.append(b)
    hc = cs.cfg["health"]
    return {"now": now, "demo": demo_flag(cs),
            "kpi": {"robots": len(robots), "axes": len(axes), "e1": sum(1 for r in robots if r["e1"]),
                    "dist": {str(k): dist.get(k, 0) for k in (-1, 0, 1, 2)},
                    "alarm_alert": sum(1 for a in act if a["level"] >= 2), "alarm_caution": sum(1 for a in act if a["level"] == 1),
                    "unacked": len(unacked), "eta30": len(eta30), "eta90": len(eta90),
                    "eta30_list": [axis_brief(a)["robot"] + " " + a["label"] for a in eta30][:4],
                    "completeness": _r(completeness, 1), "edges_on": sum(1 for s in sites_state if s["key"] != "off"),
                    "edges": len(sites_state), "late": [s["name"] for s in sites_state if s["key"] == "late"],
                    "stage2_min": round(float((cs.cfg["notify"].get("escalation") or {}).get("stage2_after_s", 1800)) / 60)},
            "plants": plants, "alarms": act[:6], "risks": risks,
            "thresholds": {"caution": hc["caution_ratio"], "alert": hc["alert_ratio"]},
            "trend": fleet_trend(cs, 90, now), "jobs": upcoming_jobs(cs, day_start(now), now + 14 * 86400)[:6]}


def fleet_trend(cs, days: int, now: float) -> dict:
    d0 = datetime.fromtimestamp(now - days * 86400).strftime("%Y-%m-%d")
    rows = cs.q("SELECT day, SUM(MAX(level, motion_level, dec_level)=1) AS c, SUM(MAX(level, motion_level, dec_level)>=2) AS a "
                "FROM axis_daily WHERE day>=? GROUP BY day ORDER BY day", (d0,))
    evs = []
    for e in cs.q("SELECT e.ts, e.kind, e.axis, r.name, s.name AS sname FROM events e LEFT JOIN robots r ON r.rkey=e.rkey "
                  "LEFT JOIN sites s ON s.site_id=r.site_id WHERE e.kind IN (?,?,?) AND e.ts>=? AND e.undone_ts IS NULL "
                  "ORDER BY e.ts", (*REPLACE_KINDS, now - days * 86400)):
        evs.append({"ts": e["ts"], "label": f"{e['sname'] or ''}-{e['name'] or ''} {ax_label(cs.cfg, e['axis'])} "
                    f"{EVENT_LABELS.get(e['kind'], e['kind'])}"})
    return {"days": [r["day"] for r in rows], "caution": [r["c"] for r in rows], "alert": [r["a"] for r in rows],
            "events": evs}


# ============================================================ 2 廠區詳情
def plant(cs, site: str | None = None, now: float | None = None) -> dict:
    allF = Fleet(cs, now)
    now = allF.now
    sites = [s for s in allF.sites.values() if any(r["site_id"] == s["site_id"] for r in allF.robots)]
    if not sites:
        return {"tabs": [], "site": None, "demo": demo_flag(cs)}
    site = site if site in {s["site_id"] for s in sites} else sites[0]["site_id"]
    act = alarm_rows(cs, "a.cleared_ts IS NULL", (), 1000)
    tabs = [{"site": s["site_id"], "name": s["name"] or s["site_id"],
             "robots": sum(1 for r in allF.robots if r["site_id"] == s["site_id"]),
             "alarms": sum(1 for a in act if a["site"] == s["site_id"] and a["level"] >= 1),
             "lv": max([r["lv"] for r in allF.robots if r["site_id"] == s["site_id"]] or [-1])} for s in sites]
    robots = [r for r in allF.robots if r["site_id"] == site]
    s = allF.sites[site]
    info = jload(s.get("info"), {}) or {}
    grease = {r["rkey"]: r["ts"] for r in cs.q("SELECT rkey, MAX(ts) AS ts FROM events WHERE kind='grease_change' "
                                               "AND undone_ts IS NULL GROUP BY rkey")}
    present = sorted({a["axis"] for r in robots for a in r["axes"]} | {1, 2, 3, 4, 5, 6})
    rows = []
    for r in robots:
        cells = []
        by = {a["axis"]: a for a in r["axes"]}
        for ax in present:
            a = by.get(ax)
            if not a:
                cells.append({"axis": ax, "lv": None})
                continue
            cells.append({"axis": ax, "lv": a["lv"], "ratio": _r(a["ratio_eff"]), "z": _r(a["z"], 1),
                          "tip": f"{r['disp']} {a['label']}：{LV.get(a['lv'])}"
                                 + (f"，F {a['ratio_eff']:.2f}" if a["ratio_eff"] is not None else "")
                                 + (f"，z {a['z']:.1f}" if a["z"] is not None else "")
                                 + (_("，預估 {d:.0f} 天達警戒", d=a["eta"]) if a["eta"] and a["lv"] < 2 else "")})
        rows.append({"rkey": r["rkey"], "name": r.get("name"), "disp": r["disp"], "line": r.get("line") or "",
                     "lv": r["lv"], "cells": cells, "hours": _r(r.get("hours"), 0), "fresh": r["fresh"],
                     "downtime": r.get("downtime"),
                     "connected": bool(r.get("connected")), "grease_ts": grease.get(r["rkey"]),
                     "port": r.get("port"), "max_ratio": max([c.get("ratio") or 0 for c in cells] or [0])})
    todo = sorted([a for r in robots for a in r["axes"] if a["lv"] >= 1],
                  key=lambda a: (-a["lv"], a["eta"] if a["eta"] is not None else 9e9))[:8]
    # 今日每小時生產循環
    midnight = day_start(now)
    hourly = [0] * 24
    total = 0
    for r in robots:
        for h in (r["status"].get("cycles_hourly") or []):
            t = float(h["hour"]) * 3600
            if t >= midnight:
                hourly[datetime.fromtimestamp(t).hour] += int(h["n"])
                total += int(h["n"])
    temps = [a["temp"] for r in robots for a in r["axes"] if a.get("temp") is not None]
    jobs = upcoming_jobs(cs, day_start(now), now + 30 * 86400, site)
    scheduled = {(j["rkey"], j["axis"]) for j in jobs}
    pred = [axis_brief(a) for r in robots for a in r["axes"]
            if a["eta"] is not None and 0 < a["eta"] <= 30 and (r["rkey"], a["axis"]) not in scheduled]
    d = Counter(r["lv"] for r in robots)
    return {"now": now, "demo": demo_flag(cs), "tabs": tabs, "site": site, "name": s["name"] or site,
            "summary": {"robots": len(robots), "axes": sum(len(r["axes"]) for r in robots),
                        "e1": sum(1 for r in robots if r["e1"]), "dist": {str(k): d.get(k, 0) for k in (-1, 0, 1, 2)},
                        "cycles_today": total, "temp": _r(statistics.mean(temps), 1) if temps else None,
                        "model": robots[0].get("model") if robots else "",
                        "host": s.get("host") or "", "edge": allF.site_state(s), "version": s.get("version"),
                        "role": s.get("role")},
            "axes": present, "axis_labels": [ax_label(cs.cfg, ax) for ax in present], "rows": rows,
            "todo": [axis_brief(a) for a in todo], "hourly": hourly, "hour_now": datetime.fromtimestamp(now).hour,
            "jobs": jobs, "predicted": pred, "thresholds": {"caution": cs.cfg["health"]["caution_ratio"],
                                                          "alert": cs.cfg["health"]["alert_ratio"]},
            "children": (info.get("supervisor") or {}).get("children", [])}


# ============================================================ 3 機器人詳情
def robot(cs, rkey: str, axis: int | None = None, days: int = 180, now: float | None = None) -> dict:
    from .life import b_life
    F = Fleet(cs, now, series=True, rkey=rkey)
    now = F.now
    r = next((x for x in F.robots if x["rkey"] == rkey), None)
    if not r:
        raise ValueError(_("找不到機器人 {r}", r=rkey))
    if not r["axes"]:
        return {"rkey": rkey, "robot": r["disp"], "axes": [], "demo": demo_flag(cs), "status": r["status"]}
    sel = next((a for a in r["axes"] if a["axis"] == axis), None) or r["worst"] or r["axes"][0]
    cards = []
    for a in r["axes"]:
        sp = [p[1] for p in axis_daily_series(cs, rkey, a["axis"], 30, now)]
        if len(sp) < 3:
            ser = jload(a.get("series"), []) or []
            sp = [p[1] for p in ser[-30:]]
        cards.append({"axis": a["axis"], "label": a["label"], "lv": a["lv"], "lv_name": LV.get(a["lv"]),
                      "ratio": _r(a["ratio_eff"]), "z": _r(a["z"], 1), "spark": sp, "eta": _r(a["eta"], 0),
                      "note": (a.get("decision") or {}).get("primary") or a.get("hint") or "",
                      "n_runs": a.get("n_runs"), "need_runs": a.get("need_runs")})
    ax = sel["axis"]
    since = now - days * 86400
    # 診斷扭矩：每次診斷的 F 比值 → 每日中位數、10–90% 範圍、溫度
    ser = [p for p in (jload(sel.get("series"), []) or []) if p and p[0] and p[0] >= since and p[1] is not None]
    byday = defaultdict(list)
    for p in ser:
        byday[day_start(p[0])].append(p)
    diag = []
    for d0 in sorted(byday):
        ps = byday[d0]
        vs = sorted(p[1] for p in ps)
        tt = [p[3] for p in ps if len(p) > 3 and p[3] is not None]
        diag.append({"ts": d0 + 43200, "v": _r(statistics.median(vs), 3), "lo": _r(vs[int(0.1 * (len(vs) - 1))], 3),
                     "hi": _r(vs[int(round(0.9 * (len(vs) - 1)))], 3), "n": len(vs), "lv": max(p[2] for p in ps),
                     "temp": _r(statistics.mean(tt), 1) if tt else None})
    if not diag:
        for dday, v, lv in axis_daily_series(cs, rkey, ax, days, now):
            t = datetime.strptime(dday, "%Y-%m-%d").timestamp() + 43200
            diag.append({"ts": t, "v": v, "lo": None, "hi": None, "n": None, "lv": lv, "temp": None})
    motion = motion_series(cs, rkey, ax, since)
    evs = cs.q("SELECT * FROM events WHERE rkey=? ORDER BY ts DESC LIMIT 60", (rkey,))
    marks = [{"ts": e["ts"], "label": EVENT_LABELS.get(e["kind"], e["kind"]), "note": e["note"] or ""}
             for e in evs if (e["axis"] in (None, ax)) and e["ts"] >= since and not e["undone_ts"]]
    for i in cs.q("SELECT ts, wt_pct FROM iron WHERE rkey=? AND axis=? AND ts>=? ORDER BY ts", (rkey, ax, since)):
        marks.append({"ts": i["ts"], "label": _("鐵粉 {wt} wt%", wt=f"{i['wt_pct']:g}"), "note": "", "iron": True})
    feats = []
    from .analysis import METRIC_LABELS
    for m in ("F", "Fc", "Fv", "ripple"):
        x = (sel["metrics"] or {}).get(m) or {}
        if not x:
            continue
        feats.append({"k": m, "name": METRIC_LABELS.get(m, m), "v": _r(x.get("value"), 3), "b": _r(x.get("base"), 3),
                      "r": _r(x.get("ratio"), 2), "z": _r(x.get("z"), 1), "lv": x.get("level", 0)})
    d = sel.get("decision") or {}
    comp = d.get("drive_comp") or drive_comp(cs.cfg, ax)
    lf = (d.get("life") or {}).get("drive") or {}
    model = life_fit_axis(cs, comp, ax, now)
    age = lf.get("age_h")
    life = {"comp": comp, "part": DRIVE_PARTS.get(ax, ""), "age_h": _r(age, 0), "b10_h": _r(lf.get("b10_h"), 0),
            "install_ts": lf.get("install_ts"), "model": model}
    if model:
        b, e = model["beta"], model["eta_h"]
        life["curve"] = [[round(t), round(math.exp(-((t / e) ** b)), 4)] for t in
                         [i * e * 1.6 / 40 for i in range(41)]]
        life["b10_fleet"] = round(b_life(b, e))
        if age:
            from .central import cond_fail
            life["rel_now"] = round(math.exp(-((age / e) ** b)), 3)
            life["p_1000h"] = round(cond_fail(age, 1000, b, e), 4)
    jobs = [job_view(cs, j) for j in cs.q("SELECT * FROM jobs WHERE rkey=? ORDER BY ts DESC LIMIT 20", (rkey,))]
    wp_sum = None
    try:
        from . import workpack as wp
        key = "grease" if (d.get("grease") or {}).get("level", 0) > max((d.get("drive") or {}).get("level", 0),
                                                                          (d.get("motor") or {}).get("level", 0)) else \
            ("motor" if (d.get("motor") or {}).get("level", 0) > (d.get("drive") or {}).get("level", 0) else "drive")
        wcomp = wp.comp_for(cs.cfg, ax, key)
        wp_sum = wp.summary(wp.get(cs.cfg, ax, wcomp))
        if wp_sum:
            wp_sum["url"] = f"/workorder?axis={ax}&comp={wcomp}&rkey={rkey}"
    except Exception:
        wp_sum = None
    st = r["status"]
    hc = cs.cfg["health"]
    mc = cs.cfg["motion"]
    fam = [{"name": k, "sev": _r(v, 2)} for k, v in (d.get("families") or {}).items() if v is not None]
    return {
        "now": now, "demo": demo_flag(cs), "rkey": rkey, "robot": r["disp"], "name": r.get("name"), "site": r["site_id"],
        "site_name": r["site_name"], "line": r.get("line") or "", "model": r.get("model") or "", "impact": r.get("impact"),
        "hours": _r(r.get("hours"), 0), "last_ts": r.get("last_ts"), "connected": bool(r.get("connected")),
        "fresh": r["fresh"], "downtime": r.get("downtime"), "error": r.get("error") or "", "version": r.get("version"), "port": r.get("port"),
        "edge_host": (jload((F.sites.get(r["site_id"]) or {}).get("info"), {}) or {}).get("edge_host") or "",
        "lv": r["lv"], "worst": axis_brief(r["worst"]) if r["worst"] else None, "cycles": r.get("cycles"),
        "axes": cards, "sel": {"axis": ax, "label": sel["label"], "lv": sel["lv"], "lv_name": LV.get(sel["lv"]),
                               "ratio": _r(sel["ratio_eff"]), "z": _r(sel["z"], 1), "eta": _r(sel["eta"], 0),
                               "slope": _r(sel.get("slope"), 1), "reasons": jload(sel.get("reasons"), []) or [],
                               "hint": sel.get("hint") or "", "n_runs": sel.get("n_runs"), "need_runs": sel.get("need_runs")},
        "days": days, "diag": diag, "motion": motion, "marks": sorted(marks, key=lambda m: m["ts"]),
        "thresholds": {"caution": hc["caution_ratio"], "alert": hc["alert_ratio"],
                       "m_caution": mc["caution_ratio"], "m_alert": mc["alert_ratio"]},
        "decision": {"primary": d.get("primary") or "", "drive_label": d.get("drive_label") or "",
                     "actions": [{"k": k, "label": {"grease": _("油脂"), "drive": d.get("drive_label") or _("減速機"),
                                                    "motor": _("馬達")}[k],
                                  "level": (d.get(k) or {}).get("level", 0), "action": (d.get(k) or {}).get("action", ""),
                                  "score": _r((d.get("scores") or {}).get(k), 2)} for k in ("grease", "drive", "motor")],
                     "families": sorted(fam, key=lambda f: -(f["sev"] or 0)), "next": d.get("next", []),
                     "notes": d.get("notes", []), "rul_days": _r(d.get("rul_days"), 0),
                     "grease_due_ts": d.get("grease_due_ts"), "grease_frac": _r(d.get("grease_frac"), 3),
                     "grease_temp_factor": _r(d.get("grease_temp_factor"), 2),
                     "grease_interval_adj_h": _r(d.get("grease_interval_adj_h"), 0),
                     "iron_wt": d.get("iron_wt"), "iron_knee": d.get("iron_knee"), "iron_next_ts": d.get("iron_next_ts"),
                     "thermal": d.get("thermal")},
        "feats": feats, "life": life, "workpack": wp_sum, "jobs": jobs,
        "events": [{"id": e["edge_id"], "ts": e["ts"], "axis": e["axis"], "axis_label": ax_label(cs.cfg, e["axis"]) if e["axis"] else _("全部"),
                    "kind": e["kind"], "label": EVENT_LABELS.get(e["kind"], e["kind"]), "note": e["note"] or "",
                    "who": e["who"] or "", "undone": bool(e["undone_ts"]), "undo_who": e["undo_who"]} for e in evs[:30]],
        "alarms": alarm_rows(cs, "a.rkey=?", (rkey,), 20),
        "status": {k: st.get(k) for k in ("source", "poll_hz", "samples", "selfcheck", "disk", "backup", "sensors",
                                           "loop_ts", "started", "db_mb")},
    }


def motion_series(cs, rkey: str, ax: int, since: float) -> list[dict]:
    """生產循環每日中位數 → 相對該動作代碼「最後一次重設之後前 N 天」的比值。"""
    mc = cs.cfg["motion"]
    n0 = int(mc.get("baseline_days", 10))
    resets = [e["ts"] for e in cs.q("SELECT ts, kind FROM events WHERE rkey=? AND (axis=? OR axis IS NULL) "
                                    "AND undone_ts IS NULL", (rkey, ax)) if e["kind"] in RESET_KINDS]
    last_reset = max(resets) if resets else 0
    out = []
    rows = cs.q("SELECT * FROM motion_daily WHERE rkey=? AND axis=? ORDER BY code, day", (rkey, ax))
    for code in sorted({r["code"] for r in rows}):
        rs = [r for r in rows if r["code"] == code and (r["ts"] or 0) >= last_reset and r["rms"]]
        if len(rs) < n0:
            continue
        base = statistics.median([r["rms"] for r in rs[:n0]])
        vb = [r["vib_r"] for r in rs[:n0] if r["vib_r"]]
        vbase = statistics.median(vb) if vb else None
        pts = [{"ts": r["ts"], "v": _r(r["rms"] / base, 3), "n": r["n"], "temp": r["temp"],
                "vib": _r(r["vib_r"] / vbase, 3) if vbase and r["vib_r"] else None}
               for r in rs if (r["ts"] or 0) >= since]
        out.append({"code": code, "points": pts, "base_rms": _r(base, 3)})
    return out


# ============================================================ 壽命模型
def life_fit_axis(cs, comp: str, ax: int | None, now: float) -> dict | None:
    """同軸的機群紀錄夠（失效 ≥ weibull_min_failures）就用同軸；不夠用同元件全部；再不夠用中央規則或設定先驗。"""
    from .life import weibull_fit
    minf = int(cs.cfg["life"].get("weibull_min_failures", 3))
    for a, src in ((ax, _("機群同軸紀錄")), (None, _("機群同元件紀錄"))):
        lives, failed = cs.life_data(comp, a, now)
        if sum(failed) >= minf:
            fit = weibull_fit(lives, failed)
            if fit:
                return {"beta": round(fit["beta"], 2), "eta_h": round(fit["eta_h"]), "n": fit["n"],
                        "failures": fit["failures"], "source": src}
    pr = ((cs.rules() or {}).get("life_prior") or {}).get(comp) or cs.cfg["life"]["prior"].get(comp) or {}
    if float(pr.get("beta", 0) or 0) > 0 and float(pr.get("eta_h", 0) or 0) > 0:
        return {"beta": float(pr["beta"]), "eta_h": float(pr["eta_h"]), "n": pr.get("n"), "failures": pr.get("failures"),
                "source": _("中央規則／設定先驗")}
    return None


# ============================================================ 4 風險與預測
def needs_drive(a: dict, horizon: float) -> bool:
    """這一軸在 horizon 天內需要傳動件（減速機／齒輪箱）：判定已指向傳動件，或趨勢會達警戒而且不是油脂問題。"""
    d = a.get("decision") or {}
    if ((d.get("drive") or {}).get("level") or 0) >= 1:
        return True
    if ((d.get("grease") or {}).get("level") or 0) >= 1:
        return False                          # 油脂問題先換脂，不算備品需求
    return a["lv"] >= 0 and a["eta"] is not None and a["eta"] <= horizon


def spare_demand(cs, axes: list[dict]) -> list[dict]:
    """備品需求：依達警戒時間排序，庫存（含已預留）依序分配；第一個分不到的那一軸若比交期還早到 → 緊急。"""
    out = []
    for s in cs.q("SELECT * FROM spares ORDER BY id"):
        axs = {int(x) for x in str(s.get("axes") or "").replace(" ", "").split(",") if x.strip().isdigit()}
        comp = s.get("comp") or ""
        match = [a for a in axes if (not axs or a["axis"] in axs)
                 and comp == ((a.get("decision") or {}).get("drive_comp") or drive_comp(cs.cfg, a["axis"]))]
        d90 = sorted([a for a in match if needs_drive(a, 90)], key=lambda a: a["eta"] if a["eta"] is not None else 0)
        d180 = [a for a in match if needs_drive(a, 180)]
        stock = float(s.get("stock") or 0)
        lead_d = float(s.get("lead_weeks") or 0) * 7
        short = [a for a in d90[int(stock):]] if stock < len(d90) else []
        if short:
            first = short[0]["eta"] if short[0]["eta"] is not None else 0
            if lead_d > first:
                act, key = _("緊急：交期 {w:g} 週長於預估時程，缺 {n} 個", w=s.get("lead_weeks") or 0, n=len(short)), "urgent"
            else:
                act, key = _("立即下單 {n} 個", n=len(short)), "order"
        elif stock < len(d180):
            act, key = _("本月下單 {n} 個", n=int(len(d180) - stock)), "plan"
        elif stock - len(d180) < float(s.get("safety") or 0):
            act, key = _("補到安全庫存"), "plan"
        else:
            act, key = _("足夠"), "ok"
        out.append({"id": s["id"], "part": s["part"], "name": s["name"], "comp": comp, "axes": sorted(axs),
                    "stock": s.get("stock"), "reserved": s.get("reserved"), "safety": s.get("safety"),
                    "free": stock - float(s.get("reserved") or 0), "d90": len(d90), "d180": len(d180),
                    "lead_weeks": s.get("lead_weeks"), "unit": s.get("unit") or "", "location": s.get("location") or "",
                    "note": s.get("note") or "", "act": act, "act_key": key, "price": s.get("price"),
                    "for": [axis_brief(a)["robot"] + " " + a["label"] for a in d90][:4]})
    return out


def risk(cs, now: float | None = None, horizon: int = 180) -> dict:
    F = Fleet(cs, now)
    now = F.now
    axes = F.axes
    cand = [a for a in axes if a["lv"] >= 1 or (a["eta"] is not None and a["eta"] <= horizon)]
    cand.sort(key=lambda a: (a["eta"] if a["eta"] is not None else 9e9, -a["lv"], -(a["ratio_eff"] or 0)))
    jobs = cs.q("SELECT rkey, axis, ts, kind, status FROM jobs WHERE status!='done' AND ts>=?", (day_start(now),))
    jmap = {}
    for j in jobs:
        jmap.setdefault((j["rkey"], j["axis"]), j)
    rows = []
    for i, a in enumerate(cand[:40]):
        b = axis_brief(a)
        e = a["eta"]
        if e is not None and e > 0:
            spread = 0.3 * e + 7               # 趨勢外推的不確定性（經驗值：±30% 再加一週）
            b["lo"], b["hi"] = round(max(0.0, e - spread)), round(e + spread)
        else:
            b["lo"] = b["hi"] = None
        j = jmap.get((a["robot"]["rkey"], a["axis"]))
        b["job_day"] = round((j["ts"] - now) / 86400, 1) if j else None
        b["job_kind"] = j["kind"] if j else None
        b["rank"] = i + 1
        rows.append(b)
    months = []
    d = datetime.fromtimestamp(now)
    for k in range(1, 7):
        m = (d.month - 1 + k) % 12 + 1
        y = d.year + (d.month - 1 + k) // 12
        t = datetime(y, m, 1).timestamp()
        if (t - now) / 86400 <= horizon:
            months.append({"label": _(MONTHS[m - 1]), "day": round((t - now) / 86400, 1)})
    def bucket(a):
        if a["lv"] >= 2 or (a["eta"] is not None and a["eta"] <= 30):
            return 0
        if a["eta"] is not None and a["eta"] <= 90:
            return 1
        return 2 if a["eta"] is not None and a["eta"] <= 180 else None
    prio = {("高", 0): 1, ("高", 1): 1, ("高", 2): 2, ("中", 0): 1, ("中", 1): 2, ("中", 2): 3,
            ("低", 0): 2, ("低", 1): 3, ("低", 2): 3}
    matrix = []
    for imp in IMPACTS:
        for bk in (2, 1, 0):
            chips = [{"t": f"{axis_brief(a)['robot']} {a['label']}", "rkey": a["robot"]["rkey"], "axis": a["axis"]}
                     for a in cand if (a["robot"].get("impact") or "中") == imp and bucket(a) == bk]
            matrix.append({"impact": imp, "bucket": bk, "prio": prio[(imp, bk)], "chips": chips})
    spares = spare_demand(cs, axes)
    by_part = defaultdict(int)
    for a in axes:
        if needs_drive(a, 90):
            by_part[DRIVE_PARTS.get(a["axis"], a["label"])] += 1
    return {"now": now, "demo": demo_flag(cs), "horizon": horizon, "rows": rows, "months": months, "matrix": matrix,
            "spares": spares,
            "summary": {"alert": sum(1 for a in axes if a["lv"] >= 2),
                        "alert_scheduled": sum(1 for a in axes if a["lv"] >= 2 and (a["robot"]["rkey"], a["axis"]) in jmap),
                        "d30": [axis_brief(a)["robot"] + " " + a["label"] for a in axes
                                if a["lv"] < 2 and a["eta"] is not None and a["eta"] <= 30],
                        "d90": sum(1 for a in axes if a["lv"] < 2 and a["eta"] is not None and a["eta"] <= 90),
                        "d180": sum(1 for a in axes if a["lv"] < 2 and a["eta"] is not None and a["eta"] <= 180),
                        "parts90": dict(sorted(by_part.items())),
                        "urgent": [s["name"] for s in spares if s["act_key"] == "urgent"]}}


# ============================================================ 5 警報與通知
def dlr_state(g: dict, now: float, timeout_s: float) -> str:
    """簡訊送達回報狀態：delivered 已送達、failed 送不到、pending 等回報、timeout 逾時未回報、"" 沒有追蹤。"""
    d = g.get("dlr") or ""
    if d == "pending" and now - float(g.get("ts") or now) > timeout_s:
        return "timeout"
    return d


# ============================================================ 5 警報與通知
def alerts(cs, tab: str = "active", site: str = "", level: str = "", limit: int = 200, notifier=None,
           now: float | None = None) -> dict:
    now = now or time.time()
    t_today = day_start(now)
    where = {"active": "a.cleared_ts IS NULL", "unacked": "a.cleared_ts IS NULL AND a.acked=0",
             "today": "a.ts>=?", "all": "a.ts>=?"}.get(tab, "a.cleared_ts IS NULL")
    args: list = [t_today] if tab == "today" else ([now - 90 * 86400] if tab == "all" else [])
    if site:
        where += " AND a.site_id=?"
        args.append(site)
    if level in ("1", "2"):
        where += " AND a.level=?"
        args.append(int(level))
    if level == "sys":
        where += " AND a.kind='系統'"
    rows = alarm_rows(cs, where, tuple(args), limit)
    cnt = lambda w, a=(): (cs.one(f"SELECT COUNT(*) AS n FROM alarms a WHERE {w}", a) or {}).get("n", 0)  # noqa: E731
    tabs = {"active": cnt("a.cleared_ts IS NULL"), "unacked": cnt("a.cleared_ts IS NULL AND a.acked=0"),
            "today": cnt("a.ts>=?", (t_today,)), "all": cnt("a.ts>=?", (now - 90 * 86400,))}
    n = cs.cfg["notify"]
    esc = n.get("escalation") or {}
    rules = n.get("rules") or []
    act_un = [r for r in alarm_rows(cs, "a.cleared_ts IS NULL AND a.acked=0 AND a.level>=2", (), 500)]
    def chans(stage):
        cs_ = sorted({c for r in rules if int(r.get("stage", 1)) == stage and r.get("enabled", True) for c in r.get("channels", [])})
        return cs_
    from .notify import CHANNEL_LABELS
    lab = lambda xs: _("、").join(CHANNEL_LABELS.get(c, c) for c in xs) or _("（未設定規則：各通道依自己的最低等級）")  # noqa: E731
    first = [k for k, on in (("sms", (n.get("sms") or {}).get("enabled")), ("email", n.get("email_enabled")),
                             ("webhook", bool(n.get("webhook_url")))) if on]
    steps = [{"n": 1, "when": _("立即"), "what": lab(chans(1) or first),
              "who": _("規則第 1 階段收件人"), "now": sum(1 for a in act_un if a["stage"] <= 1)}]
    if esc.get("enabled", True):
        steps.append({"n": 2, "when": _("{m:.0f} 分鐘未確認", m=float(esc.get("stage2_after_s", 1800)) / 60),
                      "what": lab(chans(2)), "who": _("值班人員"), "now": sum(1 for a in act_un if a["stage"] == 2)})
        steps.append({"n": 3, "when": _("{m:.0f} 分鐘未確認", m=float(esc.get("stage3_after_s", 7200)) / 60),
                      "what": lab(chans(3)), "who": _("主管"), "now": sum(1 for a in act_un if a["stage"] >= 3)})
    steps.append({"n": len(steps) + 1, "when": _("確認或解除"), "what": _("停止升級"), "who": "", "now": None})
    logs = cs.q("SELECT * FROM notify_log WHERE ts>=? ORDER BY id DESC LIMIT 100", (t_today,))
    rep = (n.get("sms") or {}).get("report") or {}
    rep_to = float(rep.get("timeout_min", 30) or 30) * 60
    preview = None
    if notifier:
        top = next((r for r in rows if r["level"] >= 2 and r["active"]), None)
        try:
            preview = notifier.preview(2, top["message"] if top else "", top["axis"] if top else None,
                                       top["kind"] if top else "健康", top["src"] if top else None, "")
        except Exception as e:
            preview = {"text": str(e)}
    duty = None
    try:
        from .notify import on_duty
        duty = on_duty(cs.cfg, now)
    except Exception:
        duty = None
    return {"now": now, "demo": demo_flag(cs), "tab": tab, "tabs": tabs, "rows": rows, "steps": steps,
            "quiet_hours": n.get("quiet_hours", ""), "summary_time": n.get("summary_time", ""),
            "log": [{"ts": g["ts"], "channel": g["channel"], "label": CHANNEL_LABELS.get(g["channel"], g["channel"]),
                     "target": g["target"], "level": g["level"], "ok": bool(g["ok"]), "detail": g["detail"],
                     "robot": g.get("robot") or "", "site": g.get("site") or "", "stage": g.get("stage") or 1,
                     "message": (g["message"] or "")[:120], "person": g.get("person") or "", "lang": g.get("lang") or "",
                     "dlr": dlr_state(g, now, rep_to), "dlr_ts": g.get("dlr_ts"), "dlr_detail": g.get("dlr_detail") or ""}
                    for g in logs],
            "sms_report": {"enabled": bool(rep.get("enabled")), "timeout_min": rep_to / 60,
                           "sent": sum(1 for g in logs if g["channel"] == "sms" and g["ok"] and g.get("dlr")),
                           "delivered": sum(1 for g in logs if g.get("dlr") == "delivered")},
            "channels": notifier.status() if notifier else [], "preview": preview,
            "rules": [{"name": _(r.get("name", "")) if r.get("name") else "", "sites": r.get("sites", []), "robots": r.get("robots", []),
                       "min_level": r.get("min_level", 1), "channels": r.get("channels", []), "stage": r.get("stage", 1),
                       "duty": bool(r.get("duty")), "quiet": bool(r.get("quiet", True)), "enabled": r.get("enabled", True)}
                      for r in rules],
            "duty": duty, "escalating": notifier.escalations() if notifier else []}


# ============================================================ 6 保養與備品
def fx_view(cfg: dict) -> dict:
    from . import fx
    return fx.get(cfg).view()


def vendors_view(cfg: dict) -> dict:
    from . import vendors
    return vendors.view(cfg)


JOB_COMP = {"motor": "motor", "grease": "grease"}          # rep = 依軸型式為 reducer／gearbox


def _norm_pn(v) -> str:
    return "".join(str(v or "").split()).upper()


def _grease_kg(cfg: dict, ax) -> float | None:
    """換脂用量（kg）：本軸給脂量 × 1.5（含沖洗）；沒設定給脂量就不估。"""
    from .config import axis_cfg
    mass = float(axis_cfg(cfg, ax).get("grease_mass_g") or 0) if ax else 0
    return round(mass * 1.5 / 1000, 1) if mass > 0 else None


def _spare_for(spares: list[dict], comp: str, ax) -> dict | None:
    for sp in spares:
        axs = {int(x) for x in str(sp.get("axes") or "").replace(" ", "").split(",") if x.strip().isdigit()}
        if (sp.get("comp") or "") == comp and (not axs or (ax and int(ax) in axs)):
            return sp
    return None


def material_cost(cfg: dict, j: dict, spares: list[dict]) -> dict | None:
    """一項保養的預估材料費 = 作業包的備品清單 × 單價。
    單價來源依序：作業包目錄的 price → 備品庫存同料號 → 同名稱 → 主件（main = true）用庫存裡同元件、同軸那一項。
    沒有作業包時，換件／換馬達用庫存裡同元件、同軸那一項；換脂依本軸給脂量（×1.5）估潤滑脂用量。"""
    ax, kind = j.get("axis"), j.get("kind")
    comp = JOB_COMP.get(kind) or (drive_comp(cfg, ax) if kind == "rep" and ax else "")
    if not comp:
        return None
    by_pn = {_norm_pn(sp.get("part")): sp for sp in spares if sp.get("part")}
    by_name = {str(sp.get("name") or "").strip(): sp for sp in spares if sp.get("name")}
    items = []
    try:
        from . import workpack as wp
        w = wp.get(cfg, ax, comp) if ax else None
    except Exception:
        w = None
    if w and w.get("parts"):
        for pt in w["parts"]:
            qty, unit = float(pt.get("qty") or 1), pt.get("unit", "")
            price, src = pt.get("price"), "pack" if pt.get("price") is not None else ""
            sp = by_pn.get(_norm_pn(pt.get("pn"))) or by_name.get(str(pt.get("name") or "").strip())
            if sp is None and pt.get("main"):
                sp = _spare_for(spares, comp, ax)
            if sp is None and pt.get("grease"):
                sp = _spare_for(spares, "grease", ax)
                if sp is not None and _norm_pn(sp.get("unit")) != _norm_pn(unit):
                    q = _grease_kg(cfg, ax) if _norm_pn(sp.get("unit")) == "KG" else None
                    if q is None:
                        sp = None                        # 單位對不起來（桶 vs kg 又不知道給脂量）就不估
                    else:
                        qty, unit = q, sp.get("unit")
            if sp is not None and sp.get("price") is not None:
                price, src = sp["price"], "spare"
            items.append({"name": pt.get("name", ""), "pn": pt.get("pn", ""), "qty": qty, "unit": unit,
                          "price": price, "sub": None if price is None else round(qty * float(price), 2), "src": src})
    else:
        sp = _spare_for(spares, comp, ax)
        if sp is None:
            return None
        qty = 1.0
        if comp == "grease":
            qty = _grease_kg(cfg, ax) if _norm_pn(sp.get("unit")) == "KG" else None
        price = sp.get("price")
        items.append({"name": sp.get("name", ""), "pn": sp.get("part", ""), "qty": qty, "unit": sp.get("unit", ""),
                      "price": price, "sub": None if price is None or qty is None else round(qty * float(price), 2),
                      "src": "spare" if price is not None else ""})
    if not items:
        return None
    priced = [x for x in items if x["sub"] is not None]
    return {"total": round(sum(x["sub"] for x in priced), 2) if priced else None, "priced": len(priced),
            "unpriced": len(items) - len(priced),
            "items": items, "source": "pack" if w and w.get("parts") else "spare"}


def maint(cs, now: float | None = None, kind: str = "") -> dict:
    F = Fleet(cs, now)
    now = F.now
    d = datetime.fromtimestamp(now)
    monday = datetime(d.year, d.month, d.day) - timedelta(days=d.weekday())
    start = monday.timestamp()
    end = (monday + timedelta(days=42)).timestamp()
    allj = [job_view(cs, j) for j in cs.q("SELECT j.*, r.name AS rname, s.name AS sname FROM jobs j "
                                           "LEFT JOIN robots r ON r.rkey=j.rkey LEFT JOIN sites s ON s.site_id=j.site_id "
                                           "WHERE j.ts>=? AND j.ts<? ORDER BY j.ts", (start, end))]
    sp_rows = cs.q("SELECT * FROM spares ORDER BY id")
    for j in allj:
        if j["status"] != "done":
            j["cost"] = material_cost(cs.cfg, j, sp_rows)
    jobs = [j for j in allj if not kind or j["kind"] == kind]
    weeks = []
    for k in range(6):
        w0 = monday + timedelta(days=7 * k)
        weeks.append({"label": _("第 {n} 週", n=w0.isocalendar()[1]),
                      "range": f"{w0:%m/%d}–{(w0 + timedelta(days=6)):%m/%d}"})
    lanes = []
    for sid, s in F.sites.items():
        js = [j for j in jobs if j["site"] == sid]
        if not js and not any(r["site_id"] == sid for r in F.robots):
            continue
        lanes.append({"site": sid, "name": s["name"] or sid,
                      "jobs": [dict(j, x=round((j["ts"] - start) / 86400, 2)) for j in js]})
    t_week_end = start + 7 * 86400
    week = [j for j in allj if j["ts"] < t_week_end and j["status"] != "done"]
    m30 = [j for j in allj if now <= j["ts"] < now + 30 * 86400 and j["status"] != "done"]

    def cost_sum(js):
        cs_ = [j["cost"] for j in js if j.get("cost")]
        return {"total": round(sum(c["total"] or 0 for c in cs_), 2), "jobs": len(cs_),
                "unpriced": sum(c["unpriced"] for c in cs_), "no_estimate": sum(1 for j in js if not j.get("cost"))}
    interval = float(cs.cfg["life"]["grease_interval_h"])
    greases = []
    for a in F.axes:
        dd = a.get("decision") or {}
        fr = dd.get("grease_frac")
        if fr is None:
            continue
        greases.append({"rkey": a["robot"]["rkey"], "robot": a["robot"]["disp"], "axis": a["axis"], "label": a["label"],
                        "frac": _r(fr, 3), "hours": _r(fr * interval, 0), "due_ts": dd.get("grease_due_ts"),
                        "level": (dd.get("grease") or {}).get("level", 0),
                        "temp_factor": _r(dd.get("grease_temp_factor"), 2),
                        "interval_adj_h": _r(dd.get("grease_interval_adj_h"), 0)})
    greases.sort(key=lambda g: -(g["frac"] or 0))
    over = [g for g in greases if (g["frac"] or 0) >= 1]
    spares = spare_demand(cs, F.axes)
    packs = []
    from . import workpack as wp
    try:
        cat = wp.load_catalog(cs.cfg)
    except Exception:
        cat = {"packs": [], "path": "", "error": ""}
    for j in [j for j in allj if j["status"] != "done" and j["ts"] >= day_start(now)][:12]:
        pk = None
        if j["axis"]:
            comp = {"rep": drive_comp(cs.cfg, j["axis"]), "motor": "motor", "grease": "grease"}.get(j["kind"])
            if comp:
                try:
                    pk = wp.summary(wp.get(cs.cfg, j["axis"], comp))
                    if pk:
                        pk["url"] = f"/workorder?axis={j['axis']}&comp={comp}"
                except Exception:
                    pk = None
        if not pk:
            continue
        total = int(pk.get("n_parts") or 0) + 3
        done = len([v for v in (j["prep"] or {}).values() if v])
        packs.append(dict(j, pack=pk, prep_done=done, prep_total=total))
    return {"now": now, "demo": demo_flag(cs), "start": start, "end": end, "today_x": round((now - start) / 86400, 2),
            "weeks": weeks, "lanes": lanes, "kind": kind,
            "kinds": [{"k": k, "label": v} for k, v in CentralStore.JOB_KINDS.items()],
            "summary": {"week": len(week), "week_hours": _r(sum(float(j["hours"] or 0) for j in week), 1),
                        "m30": len(m30), "m30_kinds": dict(Counter(j["kind_label"] for j in m30)),
                        "grease_over": len(over), "grease_over_top": over[0] if over else None,
                        "spares_order": sum(1 for s in spares if s["act_key"] in ("urgent", "order")),
                        "spares_urgent": [s["name"] for s in spares if s["act_key"] == "urgent"],
                        "week_cost": cost_sum(week), "m30_cost": cost_sum(m30),
                        "stock_value": round(sum(float(s["stock"] or 0) * float(s["price"]) for s in spares
                                                 if s.get("price") is not None), 2),
                        "unpriced_spares": sum(1 for s in spares if s.get("price") is None)},
            "currency": (cs.cfg.get("maint") or {}).get("currency") or "TWD",
            "vendor": vendors_view(cs.cfg),
            "fx": fx_view(cs.cfg),
            "packs": packs, "grease": greases[:8], "grease_interval_h": interval, "spares": spares,
            "catalog": [{"id": p.get("id"), "title": p.get("title"), "comp": p.get("comp"), "axes": p.get("axes", [])}
                        for p in cat.get("packs", [])],
            "robots": [{"rkey": r["rkey"], "disp": r["disp"], "site": r["site_id"],
                        "axes": [a["axis"] for a in r["axes"]]} for r in F.robots]}


# ============================================================ 7 壽命統計
def life(cs, axis: int = 2, now: float | None = None) -> dict:
    from .life import b_life, weibull_fit
    from .central import cond_fail
    now = now or time.time()
    hpd = float(cs.cfg["life"].get("hours_per_day", 20))
    table = []
    sel = None
    fleet_rows = cs.q("SELECT rkey, hours FROM robots")
    total_h = sum(float(r["hours"] or 0) for r in fleet_rows)
    for ax in AXES_ALL:
        comp = drive_comp(cs.cfg, ax)
        lives, failed = cs.life_data(comp, ax, now)
        n_fail = sum(failed)
        if not lives:
            continue
        fit = weibull_fit(lives, failed) if n_fail >= 2 else None
        conf = _("可用") if n_fail >= 5 else (_("僅供參考") if n_fail >= 2 else _("不配適"))
        fails = [t for t, f in zip(lives, failed) if f]
        row = {"axis": ax, "label": ax_label(cs.cfg, ax), "comp": comp, "part": DRIVE_PARTS.get(ax, ""),
               "failures": n_fail, "n": len(lives), "beta": _r(fit["beta"], 2) if fit else None,
               "eta_h": _r(fit["eta_h"], 0) if fit else None, "b10_h": round(b_life(fit["beta"], fit["eta_h"])) if fit else None,
               "mean_h": round(statistics.mean(fails)) if fails else None, "conf": conf, "conf_key":
               "ok" if n_fail >= 5 else ("ref" if n_fail >= 2 else "none")}
        model = fit or life_fit_axis(cs, comp, None, now)
        nxt = 0.0
        if model:
            b, e = float(model["beta"]), float(model["eta_h"])
            for t, f in zip(lives, failed):
                if not f:
                    nxt += cond_fail(t, hpd * 365, b, e)
        row["next12"] = round(nxt, 1)
        table.append(row)
        if ax == axis:
            pts = weibull_points(lives, failed)
            sel = dict(row, points=[[round(t), round(p, 4)] for t, p in pts],
                       censored=[round(t) for t, f in zip(lives, failed) if not f])
    if sel is None and table:
        return life(cs, table[0]["axis"], now)
    comps = cs.q("SELECT removed_ts, failed, comp FROM components WHERE life_h IS NOT NULL AND comp IN ('reducer','gearbox')")
    years = Counter(datetime.fromtimestamp(c["removed_ts"]).year for c in comps if c["removed_ts"])
    hours = [float(r["hours"] or 0) for r in fleet_rows if r["hours"]]
    bins = [0, 10000, 20000, 30000, 40000, 1e12]
    hist = [{"label": (f"{int(bins[i] / 1000)}–{int(bins[i + 1] / 1000)}k" if bins[i + 1] < 1e11 else f"{int(bins[i] / 1000)}k+"),
             "n": sum(1 for h in hours if bins[i] <= h < bins[i + 1])} for i in range(len(bins) - 1)]
    with_b10 = [t for t in table if t["b10_h"] and t["conf_key"] == "ok"] or [t for t in table if t["b10_h"]]
    shortest = min(with_b10, key=lambda t: t["b10_h"]) if with_b10 else None
    first = min([c["removed_ts"] for c in comps if c["removed_ts"]] or [0])
    model_name = (cs.one("SELECT model FROM robots WHERE model!='' GROUP BY model ORDER BY COUNT(*) DESC LIMIT 1") or {}).get("model")
    return {"now": now, "demo": demo_flag(cs), "axis": sel["axis"] if sel else axis, "sel": sel, "table": table,
            "summary": {"records": len(comps), "since_year": datetime.fromtimestamp(first).year if first else None,
                        "robots": len(fleet_rows), "total_h": round(total_h), "avg_h": round(total_h / len(fleet_rows))
                        if fleet_rows else None, "shortest": shortest,
                        "next12": round(sum(t["next12"] for t in table), 1), "model": model_name or ""},
            "hist": hist, "years": [{"y": y, "n": years[y]} for y in sorted(years)],
            "hours_per_year": hpd * 365}


# ============================================================ 8 系統健康
def system(cs, now: float | None = None, role: str = "central", host_info: dict | None = None) -> dict:
    F = Fleet(cs, now)
    now = F.now
    edges = []
    layer_tot = [0, 0, 0, 0]
    for sid, s in F.sites.items():
        info = jload(s.get("info"), {}) or {}
        host = info.get("host") or {}
        wd = info.get("watchdog") or {}
        sup = info.get("supervisor") or {}
        rs = [r for r in F.robots if r["site_id"] == sid]
        off30 = (cs.one("SELECT COUNT(*) AS n FROM alarms WHERE rkey=? AND akey='edge:offline' AND ts>=?",
                        (f"{sid}/_edge", now - 30 * 86400)) or {}).get("n", 0)
        restarts = sum(int(c.get("restarts_1h") or 0) for c in sup.get("children", []))
        sv30 = (cs.one("SELECT COUNT(*) AS n FROM alarms WHERE rkey=? AND akey LIKE 'child:%' AND ts>=?",
                       (f"{sid}/_edge", now - 30 * 86400)) or {}).get("n", 0)
        layers = [restarts + sv30, int(wd.get("restarts_30d") or 0), int(wd.get("hw_stops_30d") or 0), off30]
        for i, v in enumerate(layers):
            layer_tot[i] += v
        disk = info.get("disk") or {}
        edges.append({"site": sid, "name": s["name"] or sid, "host": s.get("host") or host.get("host") or "",
                      "role": s.get("role"), "version": s.get("version"), **F.site_state(s),
                      "uptime_s": host.get("uptime_s"), "cpu": host.get("cpu_pct"), "mem": host.get("mem_pct"),
                      "disk_used": _r(100 - disk["free_pct"], 1) if disk.get("free_pct") is not None else None,
                      "disk_free_gb": disk.get("free_gb"), "ctl_on": sum(1 for r in rs if r["fresh"] and r.get("connected")),
                      "ctl": len(rs), "layers": layers, "wd_running": wd.get("running"), "wd_hw": wd.get("hw"),
                      "maintenance": wd.get("maintenance"), "children": sup.get("children", []),
                      "backup": _site_backup(rs), "clock_offset_s": info.get("clock_offset_s"),
                      "clock_bad": info.get("clock_offset_s") is not None and abs(float(info["clock_offset_s"])) >
                      float(cs.cfg["central"].get("clock_drift_s", 60)) > 0,
                      "downtime": [r["disp"] for r in rs if r.get("downtime")]})
    lat = defaultdict(list)
    for m in cs.q("SELECT ts, site_id, lag_s FROM edge_metrics WHERE ts>=? ORDER BY ts", (now - 86400,)):
        lat[m["site_id"]].append([round(m["ts"]), _r(m["lag_s"], 0)])
    lags = [e["lag_s"] for e in edges if e["lag_s"] is not None]
    all_lag = [v for s in lat.values() for _t, v in s if v is not None]
    audit = cs.q("SELECT * FROM audit ORDER BY id DESC LIMIT 40")
    sysev = cs.q("SELECT * FROM sys_events ORDER BY id DESC LIMIT 20")
    names = {sid: (s["name"] or sid) for sid, s in F.sites.items()}
    return {"now": now, "demo": demo_flag(cs), "role": role, "host": host_info or {},
            "summary": {"edges": len(edges), "edges_on": sum(1 for e in edges if e["key"] != "off"),
                        "clock_bad": [e["name"] for e in edges if e["clock_bad"]],
                        "late": [e["name"] for e in edges if e["key"] == "late"],
                        "off": [e["name"] for e in edges if e["key"] == "off"],
                        "ctl_on": sum(e["ctl_on"] for e in edges), "ctl": sum(e["ctl"] for e in edges),
                        "lag_med": _r(statistics.median(lags), 0) if lags else None,
                        "lag_max24": _r(max(all_lag), 0) if all_lag else None},
            "edges": edges,
            "layers": [{"n": 1, "name": _("服務自動重啟"), "what": _("程式結束或卡住 → Windows 服務／監督者重啟"),
                        "count": layer_tot[0]},
                       {"n": 2, "name": _("軟體看門狗"), "what": _("狀態檔停止更新、資料中斷 → 重啟服務"), "count": layer_tot[1]},
                       {"n": 3, "name": _("硬體看門狗"), "what": _("整台電腦當機 → 硬體計時器重開機"), "count": layer_tot[2]},
                       {"n": 4, "name": _("中央心跳"), "what": _("邊緣主機太久沒上傳 → 中央發系統警報"), "count": layer_tot[3]}],
            "latency": [{"site": sid, "name": names.get(sid, sid), "points": pts} for sid, pts in lat.items()],
            "audit": [{"ts": a["ts"], "who": a["who"] or "", "addr": a["addr"], "action": a["action"], "ok": bool(a["ok"]),
                       "detail": (a["detail"] or "")[:160]} for a in audit],
            "sys_events": sysev,
            "commands": [dict(c, payload=jload(c["payload"], {}), result=jload(c["result"], None))
                         for c in cs.commands(20)]}


def _site_backup(robots: list[dict]) -> dict:
    bs = [r["status"].get("backup") or {} for r in robots]
    last = [b.get("last_ts") for b in bs if b.get("last_ts")]
    errs = [b.get("error") for b in bs if b.get("error")]
    return {"enabled": sum(1 for b in bs if b.get("enabled")), "robots": len(robots), "last_ts": max(last) if last else None,
            "oldest_ts": min(last) if last else None, "count": sum(int(b.get("count") or 0) for b in bs), "errors": errs[:3]}


def robot_list(cs) -> list[dict]:
    F = Fleet(cs)
    return [{"rkey": r["rkey"], "disp": r["disp"], "site": r["site_id"], "lv": r["lv"],
             "axes": [a["axis"] for a in r["axes"]]} for r in F.robots]


VIEWS = {"overview": overview, "plant": plant, "robot": robot, "risk": risk, "alerts": alerts, "maint": maint,
         "life": life, "system": system}

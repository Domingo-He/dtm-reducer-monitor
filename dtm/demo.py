"""中央伺服器演示資料：6 個廠區、160 台 R-2000iC/165F（其中 10 台含 E1 行走軸）。

所有數字都是程式產生的「演示資料」（畫面右上角會標示），用來看中央畫面與操作流程；不是任何工廠的實際資料。
指令：python -m dtm -c config.central.example.toml demo        （建立演示資料庫並啟動中央伺服器）
      python -m dtm -c config.central.example.toml demo --build-only
演示模式下：邊緣主機心跳由程式模擬、測試通知不會真的發送；資料以「現在」為基準產生，隔天重建即可。
文字依建立時的語言產生（每種語言各一份演示資料庫，見 DemoStores）。
"""
from __future__ import annotations

import json
import math
import os
import random
import threading
import time
from datetime import datetime

from . import __version__
from .central import CentralStore, day_of, jdump
from .config import axis_name
from .i18n import N_, _, default_lang, get_lang, pack_json, templates, use

SITES = [("P1", N_("一廠"), 32, 2), ("P2", N_("二廠"), 28, 2), ("P3", N_("三廠"), 27, 3), ("P4", N_("四廠"), 25, 1),
         ("P5", N_("五廠"), 26, 2), ("P6", N_("六廠"), 22, 0)]
LINES = [N_("L1 焊接"), N_("L2 搬運"), N_("L3 堆疊"), N_("L4 上下料")]
# 演示用的人名與單位（翻譯表裡有各語言的寫法）
WHO = {"wang": N_("王志明"), "chen": N_("陳建宏"), "lin": N_("林家豪"), "dept": N_("設備課"), "night": N_("夜班值班"),
       "admin": N_("系統管理"), "group": N_("設備課群組")}


def who(k: str) -> str:
    return str(_(WHO[k]))


RULE_NAMES = [N_("設備課群組"), N_("值班簡訊"), N_("課長")]      # config.central.example.toml 的通知規則名稱
# 演示的收件人名單（虛構人物、範例號碼）：同一廠區有越南籍、菲律賓籍同仁，各自用自己的語言收通知
DEMO_PEOPLE = [
    {"id": "d1", "name": WHO["wang"], "phone": "0912-000-101", "email": "wang@example.com", "lang": "zh-TW",
     "groups": ["default", "設備課群組"], "min_level": 1, "enabled": True, "note": N_("設備課")},
    {"id": "d2", "name": WHO["lin"], "phone": "0912-000-102", "email": "", "lang": "",
     "groups": ["值班簡訊"], "min_level": 2, "enabled": True, "note": N_("夜班值班")},
    {"id": "d3", "name": "Nguyễn Văn Hùng", "phone": "0912-000-103", "email": "", "lang": "vi",
     "groups": ["值班簡訊"], "min_level": 2, "enabled": True, "note": N_("外籍技術員（越南）")},
    {"id": "d4", "name": "Maria Santos", "phone": "0912-000-104", "email": "maria.santos@example.com", "lang": "en",
     "groups": ["設備課群組", "值班簡訊"], "min_level": 2, "enabled": True, "note": N_("外籍技術員（菲律賓）")},
    {"id": "d5", "name": WHO["chen"], "phone": "", "email": "manager@example.com", "lang": "zh-TW",
     "groups": ["課長"], "min_level": 2, "enabled": True, "note": N_("課長")},
]
DEMO_PRICES = {"RED-J1": 320000, "RED-J2": 280000, "RED-J3": 210000, "GB-WRIST": 180000, "GB-E1": None,
               "MOT-J23": 95000, "A98L-0040-0174#16KG": 26000}   # 演示單價（新台幣，虛構）；GB-E1 故意不填，示範「未填單價」


def localize_cfg(cfg: dict) -> dict:
    """範例設定檔裡的通知規則名稱換成目前語言（演示畫面用；使用者自己寫的名稱不動）。"""
    for r in (cfg.get("notify") or {}).get("rules") or []:
        if r.get("name") in RULE_NAMES:
            r["name"] = str(_(r["name"]))
    return cfg
MODEL = "R-2000iC/165F"
HPD = 20.0
LIFE = {1: (2.8, 100000), 2: (2.6, 72000), 3: (2.4, 88000), 4: (2.0, 120000), 5: (2.0, 125000), 6: (2.0, 130000),
        7: (1.8, 48000)}          # 演示用的「真實」壽命分布（β, η 小時）
# 情境：(廠, 機器人編號, 軸, 目前 F 比值, 60 天前, z, 預估天數, 等級, 主要研判, 動作, 證據)
SCRIPT = [
    ("P3", 14, 2, 2.12, 1.02, 4.6, 0, 2, N_("減速機磨耗"), "drive2", "iron"),
    ("P5", 22, 7, 2.04, 1.10, 3.8, 0, 2, N_("齒輪箱磨耗"), "drive2", ""),
    ("P1", 7, 3, 1.62, 1.00, 3.4, 0, 2, N_("潤滑脂劣化"), "grease2", ""),
    ("P3", 5, 2, 1.71, 1.04, 2.9, 23, 1, N_("減速機磨耗"), "drive1", ""),
    ("P1", 16, 4, 1.58, 1.06, 2.5, 27, 1, N_("潤滑脂劣化"), "grease1", ""),
    ("P2", 19, 2, 1.52, 1.08, 2.1, 52, 1, N_("減速機磨耗"), "drive1", ""),
    ("P3", 21, 3, 1.21, 0.98, 2.4, 61, 1, N_("無明顯異常"), "watch", ""),
    ("P4", 3, 5, 1.50, 1.05, 2.0, 75, 1, N_("潤滑脂劣化"), "grease1", ""),
    ("P1", 11, 1, 1.55, 1.10, 2.2, 110, 1, N_("減速機磨耗"), "drive1", ""),
    ("P5", 8, 2, 1.53, 1.12, 2.0, 95, 1, N_("減速機磨耗"), "drive1", ""),
    ("P3", 9, 6, 1.51, 1.15, 2.0, 150, 1, N_("潤滑脂劣化"), "grease1", ""),
    ("P6", 11, 1, 1.18, 1.00, 2.1, 140, 1, N_("無明顯異常"), "watch", ""),
    ("P2", 3, 3, 1.41, 1.12, 1.6, 165, 0, N_("無明顯異常"), "watch", ""),
    ("P4", 17, 2, 1.44, 1.15, 1.7, 172, 0, N_("無明顯異常"), "watch", ""),
]
GREASE_OVER = [("P2", 24, 2), ("P1", 25, 6)]
BASELINE = [("P3", 27), ("P5", 26)]
OFFLINE = [("P4", 20), ("P2", 11)]
E1_ROBOTS = {"P1": [4, 18], "P2": [9, 21], "P3": [2, 14, 23], "P4": [12], "P5": [22, 6]}
REPLACED = ("P2", 12, 2)             # 48 天前換過 J2 減速機（趨勢圖上的事件線）
ACTIONS = {
    "drive2": ("drive", 2, N_("14 天內更換 {part}（兩項證據成立）")),
    "drive2s": ("drive", 2, N_("鐵粉取樣確認後決定是否更換 {part}（目前單一證據）")),
    "drive1": ("drive", 1, N_("預留 {part} 備品，排入下次停機")),
    "grease2": ("grease", 2, N_("立即換脂，換脂後觀察 7 天回落")),
    "grease1": ("grease", 1, N_("安排換脂並取樣鐵粉")),
    "watch": (None, 0, N_("持續觀察；確認溫度補償")),
}


def part_name(ax: int) -> str:
    """J2 減速機、J4 齒輪箱、E1 行走軸減速機（依目前語言）。"""
    if ax == 7:
        return str(_("E1 行走軸減速機"))
    return str(_("{ax} {comp}", ax=f"J{ax}", comp=_("齒輪箱") if ax >= 4 else _("減速機")))


def _wb(rng: random.Random, beta: float, eta: float) -> float:
    return eta * (-math.log(1 - rng.random())) ** (1 / beta)


def build_demo(cfg: dict, path: str, seed: int = 20261016, now: float | None = None) -> CentralStore:
    """建立（覆寫）演示資料庫。"""
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(path + ext)
        except OSError:
            pass
    rng = random.Random(seed)
    now = now or time.time()
    cs = CentralStore(path, cfg)
    lang = get_lang()
    script = {(s[0], s[1], s[2]): s for s in SCRIPT}
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    hour_now = datetime.fromtimestamp(now).hour
    hc = cfg["health"]
    cau, ale = float(hc["caution_ratio"]), float(hc["alert_ratio"])
    with cs.conn() as c:
        c.execute("INSERT OR REPLACE INTO meta (k,v) VALUES ('demo','1')")
        c.execute("INSERT OR REPLACE INTO meta (k,v) VALUES ('demo_built',?)", (str(now),))
        c.execute("INSERT OR REPLACE INTO meta (k,v) VALUES ('demo_lang',?)", (lang,))
        alarm_id = [0]

        def alarm(rkey, sid, ts, axis, kind, akey, level, msg, value=None, acked_by=None, acked_ts=None, cleared=None,
                  origin="edge"):
            alarm_id[0] += 1
            c.execute("INSERT INTO alarms (rkey,site_id,edge_id,origin,ts,axis,kind,akey,level,message,value,acked,"
                      "acked_by,acked_ts,cleared_ts,mtime,tmpl) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (rkey, sid, alarm_id[0] if origin == "edge" else None, origin, ts, axis, kind, akey, level, str(msg),
                       value, int(bool(acked_by)), acked_by, acked_ts, cleared, now, pack_json(msg)))
            return alarm_id[0]

        ev_id = [0]

        def event(rkey, ts, axis, kind, note, who):
            ev_id[0] += 1
            c.execute("INSERT INTO events (rkey,edge_id,ts,axis,kind,note,who) VALUES (?,?,?,?,?,?,?)",
                      (rkey, ev_id[0], ts, axis, kind, note, who))

        comp_id = [0]

        def component(rkey, axis, comp, install_ts, removed_ts=None, life_h=None, failed=0, reason=""):
            comp_id[0] += 1
            c.execute("INSERT INTO components (rkey,edge_id,axis,comp,install_ts,removed_ts,life_h,failed,reason) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (rkey, comp_id[0], axis, comp, install_ts, removed_ts, life_h, failed, reason))

        hours_pool = ([rng.uniform(5000, 10000) for _ in range(12)] + [rng.uniform(10000, 20000) for _ in range(28)]
                      + [rng.uniform(20000, 30000) for _ in range(54)] + [rng.uniform(30000, 40000) for _ in range(47)]
                      + [rng.uniform(40000, 47500) for _ in range(19)])
        rng.shuffle(hours_pool)
        hp = iter(hours_pool)
        for si, (sid, sname, n, _ne1) in enumerate(SITES):
            sname = str(_(sname))
            host = f"DTM-EDGE-{si + 1:02d}"
            late = sid == "P5"
            children = []
            for i in range(1, n + 1):
                rid = f"R{i:02d}"
                rkey = f"{sid}/{rid.lower()}"
                e1 = i in E1_ROBOTS.get(sid, [])
                axes = [1, 2, 3, 4, 5, 6] + ([7] if e1 else [])
                hours = next(hp)
                if (sid, i) == ("P3", 14):
                    hours = 31240.0
                baseline = (sid, i) in BASELINE
                if baseline:
                    hours = rng.uniform(300, 700)
                commission = now - hours / HPD * 86400
                offline = (sid, i) in OFFLINE
                line_id = LINES[(i - 1) * len(LINES) // n]
                impact = "高" if line_id == "L3 堆疊" else ("低" if line_id == "L4 上下料" else "中")
                line = str(_(line_id))
                last_ts = now - (rng.uniform(3, 15) if not offline else 6 * 3600) - (230 if late else 0)
                cyc = []
                if not offline:
                    for h in range(0, hour_now + 1):
                        t = midnight + h * 3600
                        k = rng.randint(7, 9) if h not in (7, 19) else rng.randint(4, 6)
                        if h == hour_now:
                            k = max(1, int(k * (now - t) / 3600))
                        cyc.append({"hour": int(t // 3600), "n": k})
                status = {"robot": rid, "id": rid, "source": "fanuc_http", "poll_hz": 25, "connected": not offline,
                          "last_ts": last_ts, "loop_ts": now - 2, "cycles": int(hours * 8 * 0.9), "samples": int(hours * 90000),
                          "version": __version__, "cycles_hourly": cyc,
                          "backup": {"enabled": True, "last_ts": midnight + 3 * 3600 if hour_now >= 3 else midnight - 21 * 3600,
                                     "count": 30, "dir": "D:\\DTM\\backups"},
                          "selfcheck": [{"name": str(_("資料庫完整性")), "level": "ok", "detail": "ok"}],
                          "db_mb": round(hours / 400, 1)}
                c.execute("INSERT INTO robots (rkey,site_id,id,name,line,model,impact,axes,port,version,connected,last_ts,"
                          "loop_ts,error,samples,cycles,hours,last_seen,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (rkey, sid, rid, rid, line, MODEL, impact, jdump(axes), 8100 + i, __version__, int(not offline),
                           last_ts, now - 2, "" if not offline else str(_("計畫停機（控制器關機）")), status["samples"],
                           status["cycles"], round(hours), now, jdump(status)))
                children.append({"id": rid, "name": rid, "port": 8100 + i, "alive": True, "state": "ok",
                                 "restarts_1h": 1 if (sid, i) == ("P5", 6) else 0, "gave_up": False,
                                 "connected": not offline, "last_ts": last_ts})
                for ax in axes:
                    _axis(c, rng, cfg, now, midnight, rkey, sid, rid, ax, hours, commission, script.get((sid, i, ax)),
                          baseline, (sid, i, ax) in GREASE_OVER, (sid, i, ax) == REPLACED, cau, ale, event, component,
                          alarm)
            info = {"host": {"host": host, "cpu_pct": round(rng.uniform(8, 26), 1), "mem_pct": round(rng.uniform(35, 62), 1),
                             "uptime_s": rng.uniform(5, 60) * 86400},
                    "disk": {"free_pct": round(rng.uniform(55, 82), 1), "free_gb": round(rng.uniform(180, 400)), "total_gb": 512},
                    "watchdog": {"running": True, "hw": "linux" if si % 2 else "modbus", "restarts_30d": 1 if sid == "P5" else 0,
                                 "hw_stops_30d": 0, "data_30d": 1 if sid == "P5" else 0, "maintenance": None},
                    "supervisor": {"children": children, "ts": now}, "role": "edge", "sent_ts": now - (230 if late else 8)}
            c.execute("INSERT INTO sites (site_id,name,country,role,version,host,last_seen,lag_s,info) VALUES (?,?,?,?,?,?,?,?,?)",
                      (sid, sname, "TW", "edge", __version__, host, now - (rng.uniform(4, 14) if not late else 40),
                       240.0 if late else rng.uniform(5, 14), jdump(info)))
            # 24 小時資料延遲
            for k in range(288):
                t = now - (287 - k) * 300
                lag = rng.uniform(4, 15)
                if sid == "P5" and abs(t - (midnight + 7 * 3600)) < 20 * 60:
                    lag = rng.uniform(180, 252)
                c.execute("INSERT INTO edge_metrics (ts,site_id,lag_s,cpu,mem,disk,n_robots,n_online) VALUES (?,?,?,?,?,?,?,?)",
                          (t, sid, lag, rng.uniform(8, 30), rng.uniform(35, 62), info["disk"]["free_pct"], n,
                           n - sum(1 for o in OFFLINE if o[0] == sid)))
        # 五廠邊緣主機延遲（系統警報，已確認）
        t5 = midnight + 7 * 3600 + 12 * 60
        if t5 > now:
            t5 = now - 2.5 * 3600
        alarm("P5/_edge", "P5", t5, None, "系統", "edge:late", 1,
              _("{site}邊緣主機資料上傳延遲 {m} 分鐘，資料先存本機不會遺失", site=_("五廠"), m=4), acked_by=who("admin"),
              acked_ts=t5 + 13 * 60, cleared=t5 + 20 * 60, origin="central")
        # 歷史已解除的警報（近 90 天）
        robots = [r[0] for r in c.execute("SELECT rkey FROM robots")]
        for k in range(36):
            rk = rng.choice(robots)
            ax = rng.choice([1, 2, 3, 4, 5, 6])
            t = now - rng.uniform(3, 90) * 86400
            kind, lv, msg = rng.choice([
                ("即時", 1, _("{name} 扭矩超過額定 {p}%（持續 {s} 秒）", name=axis_name(cfg, ax), p=80, s=3)),
                ("資料", 1, _("資料中斷 {s} 秒", s=60)),
                ("健康", 1, _("{name} F 比值 {r:.2f}（z {z:.1f}）", name=axis_name(cfg, ax), r=1.52, z=2.1))])
            alarm(rk, rk.split("/")[0], t, ax, kind, f"{kind}:{ax}", lv, msg, acked_by=who(rng.choice(["wang", "chen", "night"])),
                  acked_ts=t + rng.uniform(5, 40) * 60, cleared=t + rng.uniform(0.5, 30) * 3600)
        _jobs(c, now, midnight)
        _spares(c, now)
        _notify_audit(c, rng, now, midnight)
        # 動作碼 9001／9002 生產循環每日中位數（部分機器人）
        for rk in rng.sample(robots, 18) + ["P3/r14", "P1/r07"]:
            for ax in (2, 3):
                drift = 1.32 if (rk, ax) == ("P3/r14", 2) else (1.18 if (rk, ax) == ("P1/r07", 3) else 1.0)
                for code, base in ((9001, 42.0 + ax * 3), (9002, 31.0 + ax * 2)):
                    for d in range(150, -1, -1):
                        t = now - d * 86400
                        frac = (150 - d) / 150
                        season = 1 + 0.03 * math.cos((datetime.fromtimestamp(t).timetuple().tm_yday - 200) / 365 * 2 * math.pi)
                        rms = base * (1 + (drift - 1) * frac ** 2.2) * season * (1 + rng.gauss(0, 0.012))
                        c.execute("INSERT OR REPLACE INTO motion_daily (rkey,day,code,axis,n,ts,rms,mean_abs,peak,vib_r,vib_a,"
                                  "temp,dur) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (rk, day_of(t), code, ax, rng.randint(120, 190), t, rms, rms * 0.8, rms * 2.1,
                                   0.42 * (1 + (drift - 1) * frac ** 2) * (1 + rng.gauss(0, 0.03)), 0.2, 26 + 8 * (season - 1) * 30,
                                   3.0 if code == 9001 else 2.4))
        _validation_history(c, now, cau, ale, event)
    cs.compute_rules(now)
    return cs


def _validation_history(c, now: float, cau: float, ale: float, event):
    """現場驗證報告的演示：近一年更換前 120 天的健康等級（多數提早出現注意／警戒，少數漏報），
    再加幾次警戒後沒有保養、或只重設基準的情形。用自己的亂數，不影響其他演示資料。"""
    vr = random.Random(7151)
    put = "INSERT OR REPLACE INTO axis_daily (day,rkey,axis,level,ratio,motion_level,dec_level) VALUES (?,?,?,?,?,-1,0)"
    rows = c.execute("SELECT rkey, axis, removed_ts, failed FROM components WHERE removed_ts > ? AND removed_ts < ? "
                     "AND comp IN ('reducer','gearbox') ORDER BY rkey, axis, removed_ts",
                     (now - 400 * 86400, now - 30 * 86400)).fetchall()
    for rk, ax, rem, failed in rows:
        if c.execute("SELECT 1 FROM axis_daily WHERE rkey=? AND axis=? AND level>=2 AND day>=? AND day<?",
                     (rk, ax, day_of(rem - 90 * 86400), day_of(rem))).fetchone():
            continue                                    # 已經有劣化趨勢的（警戒後更換的情境）
        miss = bool(failed) and vr.random() < 0.12
        c_lead = vr.uniform(35, 95) if failed else vr.uniform(15, 45)
        a_lead = vr.uniform(6, 30) if failed and vr.random() < 0.8 else None
        for d in range(120, 0, -1):
            if miss:
                lv, r = 0, 1.05 + 0.15 * (1 - d / 120) + vr.gauss(0, 0.02)
            elif a_lead and d <= a_lead:
                lv, r = 2, ale + 0.25 * (1 - d / a_lead) + vr.gauss(0, 0.02)
            elif d <= c_lead:
                lv, r = 1, cau + (ale - cau) * 0.9 * (1 - d / c_lead)
            else:
                lv, r = 0, 1.0 + (cau - 1.0) * 0.8 * (1 - (d - c_lead) / (120 - c_lead)) + vr.gauss(0, 0.02)
            c.execute(put, (day_of(rem - d * 86400), rk, ax, lv, round(min(r, cau - 0.01) if lv == 0 else r, 4)))
    robots = [r[0] for r in c.execute("SELECT rkey FROM robots WHERE rkey NOT LIKE '%/_edge' ORDER BY rkey")]
    for back, dur, ax, reset in ((236, 5, 2, True), (171, 4, 3, False), (128, 3, 2, False)):
        rk = vr.choice(robots)
        t0 = now - back * 86400
        for d in range(-20, dur + 20):
            lv = 2 if 0 <= d < dur else 0
            c.execute(put, (day_of(t0 + d * 86400), rk, ax, lv, round(ale + 0.08 if lv else 1.08 + vr.gauss(0, 0.02), 4)))
        if reset:
            event(rk, t0 + (dur + 1) * 86400, ax, "baseline_reset", str(_("更換夾具，重設基準")), who("dept"))


def _axis(c, rng, cfg, now, midnight, rkey, sid, rid, ax, hours, commission, sc, baseline, grease_over, replaced,
          cau, ale, event, component, alarm):
    name = axis_name(cfg, ax)
    comp = "gearbox" if ax >= 4 else "reducer"
    beta, eta = LIFE[ax]
    # 元件歷史：依壽命分布逐次抽樣，運轉時數內壞掉的就換新（失效紀錄給 Weibull；未壞的是截尾資料）
    install = commission
    if not baseline and not sc and not replaced and not grease_over:
        used = 0.0
        while True:
            lf = _wb(rng, beta, eta)
            if used + lf >= hours - 500:
                break
            failed = int(rng.random() < 0.85)
            if not failed:
                lf *= rng.uniform(0.8, 0.95)          # 依狀態判定提前更換
            used += lf
            rem = commission + used / HPD * 86400
            if rem > now - 30 * 86400:
                break
            component(rkey, ax, comp, install, rem, round(lf), failed, str(_("磨耗（拆檢確認）") if failed else _("預防性更換")))
            event(rkey, rem, ax, "gearbox_replace" if comp == "gearbox" else "reducer_replace",
                  str(_("拆檢：齒面磨耗、鐵粉多") if failed else _("依狀態判定提前更換")), who("dept"))
            install = rem
    if replaced:
        rem = now - 48 * 86400
        lf = (rem - install) / 86400 * HPD
        component(rkey, ax, comp, install, rem, round(lf), 1, str(_("警戒後更換，拆檢確認磨耗")))
        event(rkey, rem, ax, "reducer_replace", str(_("警戒 2 週後更換；拆檢齒面剝離")), who("dept"))
        install = rem
    component(rkey, ax, comp, install)
    # 油脂：上次換脂
    gi = float(cfg["life"]["grease_interval_h"])
    if grease_over:
        g_age_h = gi * rng.uniform(1.01, 1.03)
    elif sc and sc[9].startswith("grease"):
        g_age_h = gi * rng.uniform(0.7, 0.95)
    else:
        g_age_h = gi * rng.uniform(0.05, 0.9)
    g_ts = max(install, now - g_age_h / HPD * 86400)
    if g_ts > commission + 86400:
        event(rkey, g_ts, ax, "grease_change", str(_("依時數例行換脂")), who(rng.choice(["chen", "lin", "wang"])))
    grease_frac = (now - g_ts) / 86400 * HPD / gi
    # 目前比值與 365 天趨勢（每天一點：比值、等級、溫度）
    if sc:
        r_now, r_60, z, eta_d, lv, primary, act, ev = sc[3], sc[4], sc[5], sc[6], sc[7], sc[8], sc[9], sc[10]
        c_at = min(cau, r_60 + (r_now - r_60) * 0.45) if lv >= 1 else cau     # z 也會觸發，所以比值門檻之前就可能變注意
        a_at = min(ale, r_60 + (r_now - r_60) * 0.9) if lv >= 2 else ale
    else:
        r_now = (rng.uniform(0.92, 1.32) if not replaced else rng.uniform(0.97, 1.04)) if not baseline else None
        r_60 = (r_now or 1.0) - rng.uniform(-0.03, 0.05)
        z = abs(rng.gauss(0.6, 0.5))
        eta_d, lv, primary, act, ev = None, (0 if not baseline else -1), N_("無明顯異常"), None, ""
        c_at, a_at = cau, ale
    series, daily = [], []
    span = 90.0 if not sc else 45.0 + (sc[1] * 37 + sc[2] * 11) % 110     # 每個情境劣化開始的時間錯開
    for d in range(365, -1, -1):
        t = now - d * 86400
        temp = 25 + 7 * math.cos((datetime.fromtimestamp(t).timetuple().tm_yday - 200) / 365 * 2 * math.pi) + rng.gauss(0, 1)
        if baseline:
            r = None if d > 12 else 1.0 + rng.gauss(0, 0.02)
        elif replaced and d > 48:
            x = min(1.0, max(0.0, (t - (now - 220 * 86400)) / (120 * 86400)))
            r = (1.0 + 1.1 * x ** 1.8) * (1 + rng.gauss(0, 0.015))
        elif sc and r_now - r_60 > 0.15:
            if d <= span:
                r = r_60 + (r_now - r_60) * (1 - d / span) ** 1.8
            else:
                r = 1.0 + (r_60 - 1.0) * (1 - (d - span) / (365 - span))
            r = r * (1 + rng.gauss(0, 0.018)) if d else r_now
        else:
            r = r_now - (r_now - r_60) * min(1.0, d / 60)
            r = r * (1 + rng.gauss(0, 0.018)) if d else r_now
        if r is None:
            lvd = -1
        elif baseline:
            lvd = -1
        elif sc and d == 0:
            lvd = lv
        else:
            lvd = 2 if r >= a_at else (1 if r >= c_at else 0)
        if r is not None and not (baseline and d > 12):
            series.append([round(t), round(r, 4), lvd, round(temp, 1)])
        if d <= 120:
            daily.append((day_of(t), r, lvd))
    if baseline:
        series = []
    # 判定摘要
    d_lv = {"grease": 0, "drive": 0, "motor": 0}
    actions = {"grease": _("正常"), "drive": _("正常"), "motor": _("正常")}
    nxt, notes = [], []
    part = part_name(ax)
    if act == "drive2" and ev != "iron":
        act = "drive2s"
    if act and ACTIONS[act][0]:
        k, l, txt = ACTIONS[act]
        d_lv[k] = l
        actions[k] = _(txt, part=part)
    if grease_over:
        d_lv["grease"] = max(d_lv["grease"], 1)
        actions["grease"] = _("已超過換脂週期，請安排換脂")
    if act == "drive2" and ev == "iron":
        nxt.append(_("鐵粉加做鐵譜（看大顆粒比例），振動做包絡頻譜，確認損傷部位；拆下後填寫拆檢結果，用來修正門檻"))
    if act == "drive2s":
        notes.append(_("尚無鐵粉資料：下次換脂或中途取樣送驗，才能確認是否為磨耗"))
    fam = {str(_("扭矩－磨耗型（Fc、漣波、累積）")): round(min(1.4, max(0.0, ((r_now or 1) - 1) / 1.0)), 2),
           str(_("扭矩－黏性（Fv）")): round(rng.uniform(0, 0.3) if not (act or "").startswith("grease") else 0.7, 2),
           str(_("扭矩－生產循環（每日中位數）")): round(min(1.2, max(0.0, ((r_now or 1) - 1) * 0.9)), 2),
           str(_("鐵粉（濃度、生成速率）")): 1.1 if ev == "iron" else None,
           str(_("油脂壽命")): round(min(1.2, grease_frac), 2)}
    age_h = (now - install) / 86400 * HPD
    b10 = eta * (-math.log(0.9)) ** (1 / beta)
    decision = {"primary": _(primary) if sc else (_("無明顯異常") if not grease_over else _("潤滑脂劣化")),
                "drive_label": _("齒輪箱") if comp == "gearbox" else _("減速機"), "drive_comp": comp,
                "scores": {"grease": round(min(1.0, grease_frac * 0.5 + (0.6 if (act or "").startswith("grease") else 0)), 2),
                           "drive": round(min(1.0, max(0.0, ((r_now or 1) - 1) * 0.9)), 2), "motor": round(rng.uniform(0, 0.1), 2)},
                **{k: {"level": d_lv[k], "action": actions[k]} for k in d_lv},
                "rul_days": eta_d if eta_d else None, "families": fam,
                "life": {"drive": {"age_h": round(age_h), "b10_h": round(b10), "install_ts": install},
                         "motor": {"age_h": round((now - commission) / 86400 * HPD), "b10_h": None, "install_ts": commission}},
                "grease_due_ts": now + max(0.0, (1 - grease_frac)) * gi / HPD * 86400, "grease_frac": round(grease_frac, 3),
                "iron_wt": 0.12 if ev == "iron" else None, "next": nxt, "notes": notes}
    hl = lv if sc else (0 if not baseline else -1)
    mets = {}
    if not baseline:
        F0 = 38.0 + ax * 4 + rng.uniform(-3, 3)
        wear = (r_now - 1)
        for m, k, base in (("F", 1.0, F0), ("Fc", 1.15, F0 * 0.62), ("Fv", 0.35 if not (act or "").startswith("grease") else 1.3,
                                                                     F0 * 0.21), ("ripple", 1.3, F0 * 0.09)):
            ratio = 1 + wear * k
            mets[m] = {"ratio": round(ratio, 3), "z": round(z * (k if m != "F" else 1), 2) if m != "F" else z,
                       "level": (2 if ratio >= ale else 1 if ratio >= cau else 0) if m != "Fv" or ratio > cau else 0,
                       "value": round(base * ratio, 3), "base": round(base, 3)}
    mo = {"level": -1 if baseline else (1 if (sc and sc[3] >= 1.5 and ax in (2, 3)) else 0),
          "ratio": round(1 + ((r_now or 1) - 1) * 0.55, 3) if not baseline else None, "days_to_alert": None, "codes": []}
    hint = "" if not baseline else _("建立基準中：{n}/{need} 次", n=12, need=30)
    reasons = []
    if sc and lv >= 1:
        reasons.append(_("F 比值 {r:.2f}（z {z:.1f}）", r=r_now, z=z) + (_("，持續上升") if r_now - r_60 > 0.3 else ""))
    side = templates({"decision": decision, "reasons": reasons, "hint": hint})
    c.execute("INSERT OR REPLACE INTO axis_state (rkey,axis,name,level,ratio,metrics,days_to_caution,days_to_alert,slope,"
              "motion,decision,series,reasons,hint,n_runs,need_runs,baseline_ready,temp,updated,tmpl) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (rkey, ax, name, hl, r_now, jdump(mets), None, eta_d if eta_d else None,
               round((r_now - r_60) / max(0.5, (r_now or 1)) * 50, 1) if r_now else None, jdump(mo), jdump(decision),
               jdump(series), jdump(reasons), hint, 12 if baseline else 30 + int(rng.uniform(100, 600)), 30,
               int(not baseline), series[-1][3] if series else None, now, jdump(side) if side else None))
    for dday, r, lvd in daily:
        c.execute("INSERT OR REPLACE INTO axis_daily (day,rkey,axis,level,ratio,motion_level,dec_level) VALUES (?,?,?,?,?,?,?)",
                  (dday, rkey, ax, lvd, None if r is None else round(r, 4), -1, 1 if grease_over and dday == day_of(now) else 0))
    # 作用中的警報
    if sc and lv >= 1:
        t0 = next((p[0] for p in series if p[2] >= lv), now - 3600)
        fz = _("{name} F 比值 {r:.2f}（z {z:.1f}）", name=name, r=r_now, z=z)
        if sc[1] == 14 and sc[0] == "P3":
            t0, msg, ack = now - 11 * 60, fz + _("＋鐵粉 {w} wt%，兩項證據成立", w=0.12), None
        elif sc[0] == "P5" and sc[1] == 22:
            t0, msg, ack = now - 45 * 60, fz + _("，單一證據，待鐵粉取樣"), None
        elif sc[0] == "P1" and sc[1] == 7:
            t0 = now - 11.5 * 3600
            msg, ack = fz + _("，注意後持續上升"), (who("night"), t0 + 17 * 60)
        else:
            msg = fz + _("，預估 {d:.0f} 天達警戒", d=eta_d)
            ack = (who(rng.choice(["dept", "wang", "chen"])), t0 + rng.uniform(10, 50) * 60)
        alarm(rkey, sid, t0, ax, "維護決策" if lv >= 2 else "健康", f"health:{ax}", lv, msg, r_now,
              acked_by=ack[0] if ack else None, acked_ts=ack[1] if ack else None)
    if grease_over:
        alarm(rkey, sid, now - 2 * 86400, ax, "維護決策", f"decision:{ax}:grease", 1,
              _("{name} 已超過換脂週期（{a:,.0f} h／{b:,.0f} h），請安排換脂", name=name, a=grease_frac * gi, b=gi),
              acked_by=who("dept"), acked_ts=now - 2 * 86400 + 1800)
    if sc and sc[0] == "P3" and sc[1] == 14:
        for k, (dd, wt) in enumerate(((210, 0.03), (72, 0.06), (6, 0.12))):
            c.execute("INSERT INTO iron (rkey,edge_id,ts,axis,wt_pct,sample) VALUES (?,?,?,?,?,?)",
                      (rkey, 900 + k, now - dd * 86400, ax, wt, "interim"))
        event(rkey, now - 6 * 86400, ax, "inspection", str(_("鐵粉取樣 {w} wt%（送驗）", w=0.12)), who("lin"))


def _jobs(c, now, midnight):
    def ts(days, hour=8):
        return midnight + days * 86400 + hour * 3600
    def t(msgid, ax="", **kw):
        return str(_(msgid, ax=ax, **kw))
    jobs = [
        ("P3", "P3/r14", 2, "rep", 2, 6, "planned", t("{part}更換", part=part_name(2)),
         t("備品已預留 · 工作包 {wp}", wp="R2000iC165F-J2-reducer")),
        ("P1", "P1/r07", 3, "grease", 5, 1.5, "planned", t("{ax} 換脂＋鐵粉取樣", "J3"), t("約 {h} 小時", h=1.5)),
        ("P5", "P5/r22", 7, "check", 9, 3, "tentative", t("E1 行走軸檢查"), t("齒條潤滑與減速機取樣；E1 備品交期 {w} 週", w=10)),
        ("P1", "P1/r16", 4, "grease", 6, 1.5, "planned", t("{ax} 換脂並取樣鐵粉", "J4"), ""),
        ("P3", "P3/r05", 2, "rep", 20, 6, "tentative", t("{part}更換", part=part_name(2)), t("等備品")),
        ("P4", "P4/r03", 5, "grease", 16, 1, "planned", t("{ax} 換脂", "J5"), ""),
        ("P2", "P2/r24", 2, "grease", 1, 1, "planned", t("{ax} 換脂（逾期）", "J2"), ""),
        ("P1", "P1/r25", 6, "grease", 3, 1, "planned", t("{ax} 換脂（逾期）", "J6"), ""),
        ("P3", "P3/r09", 6, "iron", 13, 0.5, "planned", t("{ax} 鐵粉取樣", "J6"), ""),
        ("P6", "P6/r11", 1, "check", 23, 1, "planned", t("{ax} 點檢與溫度補償確認", "J1"), ""),
        ("P2", "P2/r19", 2, "iron", 8, 0.5, "planned", t("{ax} 鐵粉取樣", "J2"), ""),
        ("P1", "P1/r03", 2, "grease", -3, 1, "done", t("{ax} 依時數換脂", "J2"), ""),
        ("P4", "P4/r09", 3, "grease", -1, 1, "done", t("{ax} 依時數換脂", "J3"), ""),
    ]
    for k in range(6):
        jobs.append(("P2", f"P2/r{k * 4 + 2:02d}", 3, "grease", 12 + k // 3, 1, "planned", t("{ax} 依時數例行換脂", "J3"),
                     t("{site} {n} 台分兩天", site=_("二廠"), n=6)))
    for k in range(5):
        jobs.append(("P%d" % (k + 2), f"P{k + 2}/r{k + 5:02d}", 2, "grease", 26 + k * 2, 1, "planned",
                     t("{ax} 依時數例行換脂", "J2"), ""))
    for sid, rk, ax, kind, d, h, st, title, note in jobs:
        c.execute("INSERT INTO jobs (site_id,rkey,axis,kind,ts,hours,status,title,note,workpack,prep,created_by,created_ts,"
                  "done_ts,done_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (sid, rk, ax, kind, ts(d), h, st, title, note,
                   "R2000iC165F-J2-reducer" if (kind == "rep" and ax == 2) else "",
                   jdump({"parts": True, "tools": True} if rk == "P3/r14" else {}), who("dept"), now - 5 * 86400,
                   ts(d, 15) if st == "done" else None, who("chen") if st == "done" else None))


def _spares(c, now):
    unit, sets, pail = str(_("件")), str(_("組")), str(_("桶"))
    rows = [("RED-J1", part_name(1), 1, 0, 1, 8, unit, "reducer", "1"),
            ("RED-J2", part_name(2), 2, 1, 1, 8, unit, "reducer", "2"),
            ("RED-J3", part_name(3), 1, 0, 1, 8, unit, "reducer", "3"),
            ("GB-WRIST", str(_("腕部齒輪箱 J4–J6")), 2, 0, 1, 6, sets, "gearbox", "4,5,6"),
            ("GB-E1", part_name(7), 0, 0, 1, 10, unit, "gearbox", "7"),
            ("MOT-J23", str(_("伺服馬達 J2／J3")), 1, 0, 1, 6, unit, "motor", "2,3"),
            ("A98L-0040-0174#16KG", str(_("潤滑脂（依手冊指定品牌）")), 3, 1, 2, 2, pail, "grease", "")]
    for part, name, stock, res, safety, lead, u, comp, axes in rows:
        c.execute("INSERT INTO spares (part,name,stock,reserved,safety,lead_weeks,unit,location,note,comp,axes,updated,price) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (part, name, stock, res, safety, lead, u,
                                                      str(_("總倉 {loc}", loc="B-12")), str(_("演示資料")), comp, axes, now,
                                                      DEMO_PRICES.get(part)))


def _notify_audit(c, rng, now, midnight):
    def t(h, m):
        x = midnight + h * 3600 + m * 60
        return x if x <= now else now - rng.uniform(1, 6) * 3600
    p5, p3 = str(_("五廠")), str(_("三廠"))
    grp, sent = who("group"), str(_("已寄出"))
    late = str(_("{site}邊緣主機資料上傳延遲 {m} 分鐘", site=p5, m=4))
    e1 = str(_("{name} F 比值 {r:.2f}（z {z:.1f}）", name="E1", r=2.04, z=3.8))
    j2 = str(_("{name} F 比值 {r:.2f}（z {z:.1f}）", name="J2", r=2.12, z=4.6))
    alert = str(_("{robot} {axis} {level}", robot="R22", axis="E1", level=_("警戒")))
    with use("vi"):
        alert_vi = str(_("{robot} {axis} {level}", robot="R22", axis="E1", level=_("警戒")))
    with use("en"):
        alert_en = str(_("{robot} {axis} {level}", robot="R22", axis="E1", level=_("警戒")))
    # 收件人姓名、語言、送達回報（演示）：(簡訊序號) → (姓名, 語言, 回報, 回報時間差)
    extra = {4: (who("night"), "", "delivered", 21), 5: (who("lin"), "", "delivered", 18),
             6: ("Nguyễn Văn Hùng", "vi", "delivered", 25), 7: ("Maria Santos", "en", "pending", 0)}
    logs = [(t(7, 12), "webhook", grp, 1, 1, "HTTP 200", late, p5, "", 1, "P5/_edge|edge:late"),
            (now - 45 * 60, "webhook", grp, 2, 1, "HTTP 200", e1, "R22", p5, 1, "P5/r22|health:7"),
            (now - 45 * 60, "email", "maint@example.com", 2, 1, sent, e1, "R22", p5, 1, "P5/r22|health:7"),
            (now - 15 * 60, "sms", "09xx-xxx-123", 2, 0, str(_("數據機無訊號，改用 http")), alert, "R22", p5, 2,
             "P5/r22|health:7"),
            (now - 15 * 60 + 8, "sms", "09xx-xxx-123", 2, 1, str(_("HTTP 200（改用 http）")), alert, "R22", p5, 2,
             "P5/r22|health:7"),
            (now - 15 * 60 + 9, "sms", "091…102", 2, 1, "HTTP 200", alert, "R22", p5, 2, "P5/r22|health:7"),
            (now - 15 * 60 + 9, "sms", "091…103", 2, 1, "HTTP 200", alert_vi, "R22", p5, 2, "P5/r22|health:7"),
            (now - 15 * 60 + 10, "sms", "091…104", 2, 1, "HTTP 200", alert_en, "R22", p5, 2, "P5/r22|health:7"),
            (now - 11 * 60, "webhook", grp, 2, 1, "HTTP 200", j2 + str(_("＋鐵粉 {w} wt%", w=0.12)), "R14", p3, 1,
             "P3/r14|health:2"),
            (now - 11 * 60, "email", "maint@example.com", 2, 1, sent, j2, "R14", p3, 1, "P3/r14|health:2"),
            (t(7, 30), "email", "maint@example.com", 0, 1, sent, str(_("每日摘要：警戒 {a}、注意 {c}", a=3, c=11)), "", "", 1,
             "summary")]
    for i, row in enumerate(logs):
        person, lang, dlr, dt = extra.get(i, ("", "", "", 0))
        if row[1] == "email" and "maint@" in row[2]:
            person = who("dept")
        c.execute("INSERT INTO notify_log (ts,channel,target,level,ok,detail,message,robot,site,stage,akey,rule,"
                  "person,lang,ref,dlr,dlr_ts,dlr_detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,'',?,?,?,?,?,?)",
                  (*row, person, lang, f"demo:{i}" if dlr else "", dlr, row[0] + dt if dt else None,
                   "DELIVRD" if dlr == "delivered" else ("" if dlr == "pending" else ("UNDELIV" if dlr else ""))))
    acts = [(now - 3 * 3600, who("wang"), "10.3.2.15", "login", 1, ""),
            (now - 3 * 3600 + 60, who("wang"), "10.3.2.15", "/api/c/job", 1, f"P3/r14 {part_name(2)}"),
            (now - 2 * 3600, who("chen"), "10.1.4.22", "/api/c/ack", 1, f"{_('一廠')}-R07 J3"),
            (now - 70 * 60, "unknown", "10.5.9.31", "login", 0, str(_("密碼錯誤"))),
            (now - 40 * 60, who("lin"), "10.3.2.40", "/api/c/iron", 1, f"{p3}-R14 J2 0.12 wt%"),
            (now - 20 * 60, who("dept"), "10.1.1.8", "/api/c/spare", 1, str(_("{part} 預留 {n}", part="RED-J2", n=1)))]
    for row in acts:
        c.execute("INSERT INTO audit (ts,who,addr,action,ok,detail) VALUES (?,?,?,?,?,?)", row)
    c.execute("INSERT INTO sys_events (ts,site_id,kind,level,detail) VALUES (?,?,?,?,?)",
              (now - 41 * 86400, "", "start", 0, str(_("中央伺服器啟動（演示）"))))


def demo_tick(cs, now: float | None = None):
    """演示模式：模擬各邊緣主機持續上傳（心跳、資料延遲），避免畫面顯示離線。"""
    now = now or time.time()
    rng = random.Random(int(now))
    with cs.conn() as c:
        for s in c.execute("SELECT site_id FROM sites").fetchall():
            sid = s[0]
            lag = rng.uniform(4, 15)
            c.execute("UPDATE sites SET last_seen=?, lag_s=? WHERE site_id=?", (now - rng.uniform(2, 12), lag, sid))
            c.execute("UPDATE robots SET last_ts=?, last_seen=? WHERE site_id=? AND connected=1",
                      (now - rng.uniform(2, 12), now, sid))
            n = c.execute("SELECT COUNT(*), SUM(connected) FROM robots WHERE site_id=?", (sid,)).fetchone()
            c.execute("INSERT INTO edge_metrics (ts,site_id,lag_s,cpu,mem,disk,n_robots,n_online) VALUES (?,?,?,?,?,?,?,?)",
                      (now, sid, lag, rng.uniform(8, 30), rng.uniform(35, 62), None, n[0], n[1] or 0))
        c.execute("DELETE FROM edge_metrics WHERE ts<?", (now - 3 * 86400,))


def demo_path(cfg: dict, lang: str | None = None) -> str:
    from .runtime import resolve
    base = resolve(cfg, cfg["central"].get("demo_db", "data/central_demo.sqlite3"))
    root, ext = os.path.splitext(base)
    return f"{root}.{lang or get_lang()}{ext or '.sqlite3'}"


def ensure_demo(cfg: dict, rebuild: bool = False, lang: str | None = None, newer_than: float = 0.0) -> str:
    """演示資料庫不存在、超過 1 天（資料以建立當下為基準）、語言不對或比 newer_than 舊，就重建。
    資料庫依目前語言（或 lang）各一份。"""
    lang = lang or get_lang()
    path = demo_path(cfg, lang)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    built, built_lang = 0.0, ""
    if os.path.exists(path) and not rebuild:
        try:
            st = CentralStore(path, cfg)
            built, built_lang = float(st.meta("demo_built", 0) or 0), st.meta("demo_lang", "") or ""
        except Exception:
            built = 0.0
    if rebuild or not built or built_lang != lang or time.time() - built > 86400 or built < newer_than:
        with use(lang):
            build_demo(cfg, path)
    return path


class DemoStores:
    """演示：每種語言各一份演示資料庫，依請求的語言（i18n.get_lang()）挑選；某個語言第一次被看時才建立（約 5 秒）。
    用起來跟 CentralStore 一樣（屬性與方法都轉給目前語言那一份）。"""

    _OWN = ("cfg", "stores", "lock", "attrs", "default")

    def __init__(self, cfg: dict, store: CentralStore, lang: str | None = None):
        object.__setattr__(self, "cfg", cfg)
        object.__setattr__(self, "default", lang or default_lang())
        object.__setattr__(self, "stores", {self.default: store})
        object.__setattr__(self, "lock", threading.Lock())
        object.__setattr__(self, "attrs", {})

    def store(self, lang: str | None = None) -> CentralStore:
        from .i18n import LANGS
        lang = lang or get_lang()
        if lang not in LANGS:
            lang = self.default
        st = self.stores.get(lang)
        if st is None:
            with self.lock:
                st = self.stores.get(lang)
                if st is None:
                    base = self.stores[self.default]
                    built = float(base.meta("demo_built", 0) or 0)
                    st = CentralStore(ensure_demo(self.cfg, lang=lang, newer_than=built - 60), self.cfg)
                    for k, v in self.attrs.items():
                        setattr(st, k, v)
                    self.stores[lang] = st
        return st

    def all(self) -> list[CentralStore]:
        return list(self.stores.values())

    def __getattr__(self, name):
        return getattr(self.store(), name)

    def __setattr__(self, name, value):
        if name in self._OWN:
            object.__setattr__(self, name, value)
            return
        self.attrs[name] = value
        for st in self.stores.values():
            setattr(st, name, value)


__all__ = ["build_demo", "demo_tick", "ensure_demo", "demo_path", "DemoStores", "localize_cfg", "json"]

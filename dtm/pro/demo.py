"""Pro 演示資料：兩台同型機器人（R01 處理組、R02 對照組），改前、改後各一期診斷，加上停止事件、加速規衝擊、每週全範圍掃描。"""
from __future__ import annotations

import os
import random
import shutil
import time

from ..i18n import _
from . import impacts, sim, sweep, views
from .analyze import Processor
from .store import ROLE_LABELS, Session, pro_dir

DAY = 86400.0


def build(cfg: dict, rebuild: bool = False, cycles_per_day: int = 30, days: float = 4.0, hz: float = 100.0,
          progress=print) -> list[str]:
    base = pro_dir(cfg)
    if os.listdir(base) and not rebuild:
        return [m["id"] for m in Session.list(cfg)]
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)
    now = time.time()
    day0 = now - now % DAY
    plan = [("R01", "before", "before", day0 - 21 * DAY, 1.00, 11),
            ("R02", "control_before", "before", day0 - 21 * DAY, 1.00, 21),
            ("R01", "after", "after", day0 - 10 * DAY, 1.04, 12),
            ("R02", "control_after", "before", day0 - 10 * DAY, 1.04, 22)]
    ids = []
    for robot, role, variant, t0, drift, seed in plan:
        sess = Session.create(cfg, robot, sim.demo_program(variant), role=role, group="demo",
                              label="", source="sim", t=t0,
                              change=_("13 行 FINE 改 CNT80；6、24、25 行用等待餘裕降速；P[7] 不再翻轉手腕") if role == "after" else "")
        P = Processor(cfg, sess)
        acc_rows, stops = [], []
        progress(_("產生 {robot} {role}（{n} 個循環）…", robot=robot, role=ROLE_LABELS[role], n=int(days * cycles_per_day)))
        sim.run(variant, days=days, cycles_per_day=cycles_per_day, hz=hz, seed=seed, t_start=t0, drift=drift,
                estop_p=0.02, sink=P.feed, acc_sink=acc_rows.extend, alarm_sink=stops.extend)
        P.flush()
        sess.save_meta(status="closed", closed=t0 + days * DAY)
        impacts.import_alarms(cfg, sess, "\n".join(sim.alarm_lines(stops)))
        impacts.import_accel(cfg, sess, acc_rows)
        ids.append(sess.id)
        sess.close()
    # 每週全範圍掃描：R01 的 J3 常用段摩擦逐週上升（磨耗與使用分布成正比），R02 不變
    progress(_("產生全範圍掃描…"))
    s = Session.open(cfg, ids[0])
    A = views.angle_map(cfg, s, 3)
    s.close()
    dens = {r["deg"]: r["d"] for r in A["bins"]} if A else {}
    mx = max(dens.values()) if dens else 1.0

    def wear_fn(amp):
        def f(ax, th):
            if ax != 3:
                return 0.0
            return amp * dens.get(float(int(th // 1.0)), 0.0) / mx
        return f
    rng = random.Random(5)
    for wk in range(9):
        t = day0 - (8 - wk) * 7 * DAY + 3600 * 7
        for robot, amp in (("R01", 0.2 + 0.47 * wk), ("R02", 0.3)):
            cols = {k: [] for k in sim.COLS}
            for ax in (2, 3):
                d = sim.sweep(ax, t, hz, rng, wear=wear_fn(amp) if robot == "R01" else wear_fn(amp))
                for k in cols:
                    cols[k].extend(d[k])
                t += 60
            sweep.import_cols(cfg, robot, cols, note=_("每週掃描"))
    return ids


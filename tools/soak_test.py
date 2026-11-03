"""長時間運轉測試（加速模擬）：不等真實時間，用模擬器把 N 天的資料以最快速度餵進完整的監測流程。

    python tools/soak_test.py --days 30                 # 預設每秒 5 筆（跟現場一樣），30 天約 1,300 萬筆
    python tools/soak_test.py --days 30 --hz 2 --out soak.json

走的是跟現場相同的程式路徑：讀取 → 即時警報 → 寫入 → 每分鐘統計 → 診斷切段與特徵 → 生產循環 → 每日中位數
→ 健康、溫度、維護判定 → 清除過期原始樣本 → 自動導入檢查報告；每個模擬日另外做一次每日備份。
時間用「模擬時鐘」（把 time.time 換成目前樣本的時間），所以 7 天保留、每小時清理、換日這些邏輯都會照常觸發。

每個模擬日記錄：實際花費秒數、記憶體（RSS）、資料庫與 WAL 大小、原始樣本筆數（一般樣本應該在第 7 天後持平；
診斷段與警報前後的保留片段會一直累積到 raw_keep_days 才清）、
生產循環與特徵筆數、警報數、記錄檔的數量與大小（記錄檔改成 200 KB 輪替，用來確認輪替正常）、備份大小。
限制：模擬器的資料型態固定，不能代表所有現場狀況；網路、控制器與 Windows 本身的長期行為要在現場驗證。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import resource
import shutil
import sys
import tempfile
import time as _time
from logging.handlers import RotatingFileHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REAL_TIME = _time.time


def rss_mb() -> float:
    try:
        with open("/proc/self/statm", encoding="ascii") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except (OSError, ValueError, AttributeError):
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def dir_stats(d: str, prefix: str = "") -> tuple[int, int]:
    if not os.path.isdir(d):
        return 0, 0
    fs = [os.path.join(d, f) for f in os.listdir(d) if f.startswith(prefix)]
    return len(fs), sum(os.path.getsize(f) for f in fs if os.path.isfile(f))


def run(days: float, hz: float | None, out: str | None, keep: bool = False) -> dict:
    from dtm import config as C
    tmp = tempfile.mkdtemp(prefix="dtm_soak_")
    cfg = C.load(os.path.join(ROOT, "config.sim.toml"))
    cfg["_path"] = os.path.join(tmp, "config.toml")
    cfg["storage"]["db"] = os.path.join(tmp, "dtm.sqlite3")
    cfg["storage"]["backup_dir"] = os.path.join(tmp, "backups")
    cfg["storage"]["backup_keep"] = 10
    cfg["web"]["users_db"] = os.path.join(tmp, "users.sqlite3")
    cfg["source"]["sim"]["diag_every_s"] = 6 * 3600            # 現場通常每班一次診斷
    cfg["source"]["sim"]["spike_every_s"] = 3600
    if hz:
        cfg["robot"]["poll_hz"] = hz
    logs = os.path.join(tmp, "logs")
    os.makedirs(logs)
    h = RotatingFileHandler(os.path.join(logs, "dtm.log"), maxBytes=200_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)

    clock = {"t": REAL_TIME() - days * 86400}
    _time.time = lambda: clock["t"]                             # 模擬時鐘（全部模組共用 time.time）
    try:
        from dtm import backup as B
        from dtm.service import Service
        from dtm.sources import SimSource
        from dtm.store import outside_keep
        svc = Service(cfg, notify=True)
        src = SimSource(cfg, realtime=False)
        t_start = clock["t"]
        t_end = t_start + days * 86400
        rows, day_idx, n = [], 0, 0
        w0 = REAL_TIME()
        w_day = w0
        rss0 = rss_mb()
        while clock["t"] < t_end:
            s = src.read()
            clock["t"] = s.ts
            svc.handle(s)
            n += 1
            if n % 50 == 0:
                svc.rt.check_conn(s.ts)
                svc.flush()
            if s.ts - t_start >= (day_idx + 1) * 86400:
                day_idx += 1
                svc.flush(force=True)
                try:
                    bk = B.backup_now(cfg, now=s.ts)
                    bk_mb = os.path.getsize(bk) / 1e6
                except Exception as e:                           # 備份失敗也要記錄
                    bk_mb = None
                    logging.getLogger("soak").warning("backup: %s", e)
                with svc.store.conn() as c:
                    cnt = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                           for t in ("samples", "agg_1min", "features", "cycles", "motion_daily", "alarms", "raw_keep")}
                    cond, p = outside_keep(c)                    # 一般原始樣本（不在保留片段內）：第 7 天後應該持平
                    cnt["samples_normal"] = c.execute(f"SELECT COUNT(*) FROM samples WHERE {cond}", p).fetchone()[0]
                db = cfg["storage"]["db"]
                nlog, slog = dir_stats(logs, "dtm.log")
                row = {"day": day_idx, "samples_in": n, "wall_s": round(REAL_TIME() - w_day, 1),
                       "rss_mb": round(rss_mb(), 1), "db_mb": round(os.path.getsize(db) / 1e6, 2),
                       "wal_mb": round(os.path.getsize(db + "-wal") / 1e6, 2) if os.path.exists(db + "-wal") else 0.0,
                       "backup_mb": round(bk_mb, 2) if bk_mb is not None else None,
                       "backups": len(B.list_backups(cfg)), "log_files": nlog, "log_kb": round(slog / 1024),
                       **cnt}
                rows.append(row)
                w_day = REAL_TIME()
                print(json.dumps(row, ensure_ascii=False), flush=True)
                if out:
                    with open(out, "w", encoding="utf-8") as f:
                        json.dump({"partial": True, "rows": rows}, f, ensure_ascii=False, indent=1)
        svc.flush(force=True)
    finally:
        _time.time = REAL_TIME
    res = {"days": days, "hz": cfg["robot"]["poll_hz"], "samples": n, "wall_s": round(REAL_TIME() - w0, 1),
           "rss_start_mb": round(rss0, 1), "rows": rows, "dir": tmp if keep else None}
    if rows:
        late = [r for r in rows if r["day"] >= 10]
        res["checks"] = {
            "samples_plateau": (max(r["samples_normal"] for r in late) <= 1.05 * min(r["samples_normal"] for r in late))
            if late else None,                                     # 不到 10 天無法判斷
            "rss_growth_mb": round(rows[-1]["rss_mb"] - rows[min(2, len(rows) - 1)]["rss_mb"], 1),
            "log_rotation_ok": max(r["log_files"] for r in rows) <= 6,
            "backups_pruned": rows[-1]["backups"] <= 10,
            "db_mb_per_day_after_7d": round((rows[-1]["db_mb"] - rows[min(7, len(rows) - 1)]["db_mb"]) /
                                            max(1, rows[-1]["day"] - rows[min(7, len(rows) - 1)]["day"]), 3),
        }
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    if not keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="DTM 長時間運轉測試（加速模擬）")
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--hz", type=float, default=None, help="取樣頻率（預設用 config.sim.toml 的 5 Hz）")
    ap.add_argument("--out", default=None, help="結果 JSON")
    ap.add_argument("--keep", action="store_true", help="保留暫存資料夾（資料庫、記錄檔）")
    a = ap.parse_args(argv)
    r = run(a.days, a.hz, a.out, a.keep)
    print(json.dumps({k: v for k, v in r.items() if k != "rows"}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

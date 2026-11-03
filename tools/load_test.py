"""中央伺服器負載測試：模擬 160 台機器人同時上傳，量上傳處理時間與 8 個畫面的 API 回應時間。

    python tools/load_test.py                         # 160 台、8 個廠區、每輪全部同時上傳、10 輪
    python tools/load_test.py --robots 160 --sites 8 --rounds 10 --mode standalone

模式：
- edge（預設）：每個廠區一台邊緣主機，一次上傳整廠的機器人（跟實際邊緣主機一樣）；各廠同時送。
- standalone：每台機器人自己上傳（最壞情況：160 個連線同時進來）。
中央在另一個程式裡執行（跟現場一樣），資料庫放暫存資料夾。第一輪附帶 3,000 筆歷史特徵（模擬補傳）。
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "load-test-token"


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else None


def robot_payload(sid: str, rid: str, rnd: int, now: float, rng: random.Random, backlog: int = 0) -> dict:
    axes = []
    for ax in range(1, 7):
        ratio = 1 + abs(rng.gauss(0.05, 0.08))
        series = [[round(now - (400 - i) * 21600), round(1 + 0.05 * rng.random(), 4), 0, 30.0] for i in range(400)]
        axes.append({"axis": ax, "name": f"J{ax}", "level": 1 if ratio > 1.5 else 0, "ratio_F": round(ratio, 3),
                     "metrics": {m: {"ratio": round(ratio * (0.9 + 0.2 * rng.random()), 3), "z": round(rng.gauss(0, 1), 2),
                                     "level": 0} for m in ("F", "Fc", "Fv", "ripple")},
                     "days_to_caution": None, "days_to_alert": rng.choice([None, 300.0, 120.0]),
                     "slope_pct_month": round(rng.gauss(0.3, 0.5), 2), "series": series, "reasons": [], "hint": "",
                     "n_runs": 200, "need_runs": 10, "baseline_ready": True, "temp": 31.0,
                     "motion": {"level": 0, "ratio": round(ratio, 3), "codes": []},
                     "decision": {"primary": "無明顯異常", "grease": {"level": 0, "action": "正常"},
                                  "drive": {"level": 0, "action": "正常"}, "motor": {"level": 0, "action": "正常"},
                                  "scores": {"grease": 0.1, "drive": 0.1, "motor": 0.05}, "families": {},
                                  "grease_frac": round(rng.random(), 3), "next": [], "notes": []}})
    n_feat = backlog or 6
    feats = [{"id": rnd * 100000 + i, "ts": now - (n_feat - i) * 3600, "axis": 1 + i % 6, "F": 10.0, "Fc": 6.0,
              "Fv": 0.1, "G": 0.0, "ripple": 0.5, "temp": 30.0, "valid": 1} for i in range(n_feat)]
    alarms = []
    if rng.random() < 0.05:
        alarms.append({"id": rnd * 1000 + 1, "ts": now, "axis": 3, "kind": "健康", "akey": f"health:3:{rnd}", "level": 1,
                       "message": "J3 注意（負載測試）", "value": 1.6, "acked": 0, "cleared_ts": None, "mtime": now})
    md = [{"day": time.strftime("%Y-%m-%d", time.localtime(now)), "code": 9000 + c, "axis": ax, "n": 50, "ts": now,
           "rms": 1.0, "mean_abs": 0.8, "peak": 2.0, "vib_r": None, "vib_a": None, "temp": 30.0, "dur": 20.0, "mtime": now}
          for c in (1, 2) for ax in range(1, 7)]
    status = {"robot": rid, "id": rid, "line": "L1", "model": "R-2000iC/165F", "version": "2.1.0", "ts": now,
              "loop_ts": now, "last_ts": now, "connected": True, "samples": 1000 * rnd, "cycles": 10 * rnd, "axes": axes,
              "alarms": [], "disk": {"free_pct": 60}, "hours": 25000 + rnd}
    return {"key": f"{sid}/{rid}", "meta": {"id": rid, "name": rid, "line": "L1", "impact": "中", "model": "R-2000iC/165F",
                                            "axes": [1, 2, 3, 4, 5, 6]},
            "status": status, "features": feats, "alarms": alarms, "events": [], "motion_daily": md, "iron": [], "vib": [],
            "cursors": {}}


def post(url: str, payload: dict) -> tuple[float, int, int]:
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    req = urllib.request.Request(url + "/api/sync", data=body, method="POST", headers={
        "Content-Type": "application/json", "Content-Encoding": "gzip", "Authorization": f"Bearer {TOKEN}"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as r:
        r.read()
        code = r.status
    return time.perf_counter() - t0, code, len(body)


def get(url: str, path: str) -> float:
    t0 = time.perf_counter()
    with urllib.request.urlopen(url + path, timeout=300) as r:
        r.read()
    return time.perf_counter() - t0


VIEWS = ["/api/c/info", "/api/c/overview", "/api/c/plant?site=S01", "/api/c/robot?rkey=S01%2FR001&days=180",
         "/api/c/risk", "/api/c/alerts?tab=active", "/api/c/maint", "/api/c/life?axis=2", "/api/c/system"]


def run(n_robots=160, n_sites=8, rounds=10, mode="edge", port=18390, keep=False) -> dict:
    tmp = tempfile.mkdtemp(prefix="dtm_load_")
    conf = os.path.join(tmp, "central.toml")
    with open(conf, "w", encoding="utf-8") as f:
        f.write(f'[system]\nrole = "central"\n[web]\nhost = "127.0.0.1"\nport = {port}\nusers_db = "users.sqlite3"\n'
                f'[central]\ndb = "central.sqlite3"\ntoken = "{TOKEN}"\n[notify]\nsummary_time = ""\n')
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONUTF8="1")
    proc = subprocess.Popen([sys.executable, "-m", "dtm", "-c", conf, "run", "--force"], cwd=tmp, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            get(url, "/api/c/info")
            break
        except Exception:
            time.sleep(0.3)
    rng = random.Random(1)
    sites = [f"S{i + 1:02d}" for i in range(n_sites)]
    robots = [(sites[i % n_sites], f"R{i + 1:03d}") for i in range(n_robots)]
    res = {"robots": n_robots, "sites": n_sites, "mode": mode, "rounds": []}
    try:
        for rnd in range(1, rounds + 1):
            now = time.time()
            if mode == "edge":
                jobs = []
                for s in sites:
                    rs = [robot_payload(s, rid, rnd, now, rng, 3000 if rnd == 1 else 0) for sid, rid in robots if sid == s]
                    jobs.append({"site": {"site_id": s, "site_name": s, "role": "edge", "sent_ts": now,
                                          "host": {"cpu_pct": 10, "mem_pct": 40}}, "robots": rs, "results": []})
            else:
                jobs = [{"site": {"site_id": sid, "site_name": sid, "role": "standalone", "sent_ts": now},
                         "robots": [robot_payload(sid, rid, rnd, now, rng, 3000 if rnd == 1 else 0)], "results": []}
                        for sid, rid in robots]
            times, sizes, errs = [], [], []
            lock = threading.Lock()

            def worker(p):
                try:
                    dt, code, sz = post(url, p)
                    with lock:
                        times.append(dt)
                        sizes.append(sz)
                except Exception as e:
                    with lock:
                        errs.append(str(e))
            t0 = time.perf_counter()
            ths = [threading.Thread(target=worker, args=(p,)) for p in jobs]
            for t in ths:
                t.start()
            view_t = {}
            for v in VIEWS[:3]:                                       # 上傳進行中同時開畫面
                try:
                    view_t[v] = round(get(url, v) * 1000)
                except Exception as e:
                    view_t[v] = str(e)
            for t in ths:
                t.join()
            wall = time.perf_counter() - t0
            row = {"round": rnd, "posts": len(jobs), "wall_s": round(wall, 2),
                   "post_p50_ms": round(pct(times, 50) * 1000) if times else None,
                   "post_p95_ms": round(pct(times, 95) * 1000) if times else None,
                   "post_max_ms": round(max(times) * 1000) if times else None,
                   "payload_kb_avg": round(statistics.fmean(sizes) / 1024) if sizes else None,
                   "errors": len(errs), "error_samples": errs[:3], "views_during_ms": view_t}
            res["rounds"].append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        after = {}
        for v in VIEWS:
            ts = [get(url, v) for _ in range(3)]
            after[v] = round(statistics.median(ts) * 1000)
        res["views_after_ms"] = after
        print(json.dumps({"views_after_ms": after}, ensure_ascii=False), flush=True)
        res["db_mb"] = round(os.path.getsize(os.path.join(tmp, "central.sqlite3")) / 1e6, 1)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not keep:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="DTM 中央負載測試")
    ap.add_argument("--robots", type=int, default=160)
    ap.add_argument("--sites", type=int, default=8)
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--mode", choices=["edge", "standalone"], default="edge")
    ap.add_argument("--port", type=int, default=18390)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    r = run(a.robots, a.sites, a.rounds, a.mode, a.port)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()

"""產生中央 8 個畫面的靜態演示（可放 GitHub Pages，不需要伺服器）。

    python tools/build_static_demo.py                 # 輸出到 site/
    python tools/build_static_demo.py --out site --robots 12

做法：在本機用演示資料（6 廠 160 台，程式產生）啟動一個暫時的中央伺服器，把畫面會呼叫的 API 結果存成 JSON 快照，
再把 central.html 複製成 index.html 並開啟「靜態模式」：畫面改讀快照，所有寫入（確認、登錄、排程…）一律停用並提示。
機器人詳情收錄每台的預設畫面，以及風險最高的幾台（--robots）的每一軸與 30／90／365 天。
多國語言：每種語言各產生一份演示資料與快照（繁中在 data/，其他在 data/<語言>/），畫面右上角可以切換。
放到 GitHub Pages：把輸出資料夾整個上傳（或用 .github/workflows/pages.yml 自動部署）。

    python tools/build_static_demo.py --langs zh-TW,en     # 只產生指定語言
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dtm import config as C  # noqa: E402
from dtm.auth import Auth  # noqa: E402
from dtm.demo import build_demo, localize_cfg  # noqa: E402
from dtm.fleetweb import serve_fleet  # noqa: E402
from dtm.i18n import SOURCE, use, visible_langs  # noqa: E402


def static_key(path: str) -> str:
    """跟 central.html 的 staticKey() 一致。"""
    p, _, q = path.partition("?")
    params = urllib.parse.parse_qs(q, keep_blank_values=True)
    keys = sorted(k for k, v in params.items() if v and v[0] != "")
    safe = lambda v: "".join(ch if ch.isascii() and (ch.isalnum() or ch in "_.-") else "_" for ch in v)  # noqa: E731
    name = p.replace("/api/c/", "", 1).replace("/", "_")
    return name + "".join(f"__{k}-{safe(params[k][0])}" for k in keys) + ".json"


def inject_script(root_lang: str, langs: list[str]) -> str:
    """加進 index.html 的設定：快照放在 data/（root_lang）與 data/<語言>/（其他語言）。"""
    return "<script>window.DTM_STATIC = " + json.dumps({"base": "data/", "lang": root_lang, "langs": langs}) + ";</script>"


def build(out: str, n_robots: int = 12, seed: int = 20261016, langs: list[str] | None = None) -> dict:
    langs = langs or list(visible_langs())
    root_lang = SOURCE if SOURCE in langs else langs[0]
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "data"))
    stats = {"files": 0, "bytes": 0, "langs": langs}
    for lang in langs:
        sub = "" if lang == root_lang else lang
        with use(lang):
            _build_lang(os.path.join(out, "data", sub), lang, n_robots, seed, stats)
    inject = inject_script(root_lang, langs)
    with open(os.path.join(ROOT, "dtm", "static", "central.html"), encoding="utf-8") as f:
        page = f.read()
    page = page.replace("</head>", inject + "\n</head>", 1)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    shutil.copytree(os.path.join(ROOT, "dtm", "static", "vendor"), os.path.join(out, "vendor"))   # 全頁截圖元件
    open(os.path.join(out, ".nojekyll"), "w").close()
    with open(os.path.join(out, "README.txt"), "w", encoding="utf-8") as f:
        f.write("DTM 中央畫面靜態演示（演示資料，程式產生；不含任何公司的實際資料）。\n"
                f"語言：{', '.join(langs)}\n產生時間：{time.strftime('%Y-%m-%d %H:%M')}\n")
    return stats


def _build_lang(data: str, lang: str, n_robots: int, seed: int, stats: dict):
    """一種語言：用這個語言建立演示資料庫、啟動暫時的中央伺服器，存下畫面會呼叫的 API 快照。"""
    tmp = tempfile.mkdtemp(prefix="dtm_static_")
    cfg = C.load(os.path.join(ROOT, "config.central.example.toml"))
    cfg["_path"] = os.path.join(tmp, "config.toml")
    cfg["web"]["users_db"] = os.path.join(tmp, "users.sqlite3")
    cfg["system"]["role"] = "central"
    localize_cfg(cfg)
    try:                                     # 備品價格的當地貨幣換算：先抓一次匯率（連不到網路就照原幣別顯示）
        from dtm import fx
        fx.get(cfg).refresh(wait=True)
    except Exception:
        pass
    cs = build_demo(cfg, os.path.join(tmp, "central.sqlite3"), seed=seed)
    httpd = serve_fleet(cfg, cs, role="central", auth=Auth(cfg), host="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    os.makedirs(data, exist_ok=True)

    def get(path: str):
        url = base + path + ("&" if "?" in path else "?") + "lang=" + lang
        with urllib.request.urlopen(url, timeout=120) as r:
            body = r.read()
        fn = os.path.join(data, static_key(path))
        with open(fn, "wb") as f:
            f.write(body)
        stats["files"] += 1
        stats["bytes"] += len(body)
        return json.loads(body)

    try:
        i18n = get("/api/c/i18n")
        assert i18n.get("lang") == lang, i18n.get("lang")
        i18n["langs"] = {k: v for k, v in (i18n.get("langs") or {}).items() if k in stats["langs"]}  # 只列有產生的語言
        os.remove(os.path.join(data, static_key("/api/c/i18n")))
        with open(os.path.join(data, "i18n.json"), "w", encoding="utf-8") as f:
            json.dump(i18n, f, ensure_ascii=False)
        info = get("/api/c/info")
        for p in ("/api/c/overview", "/api/c/risk", "/api/c/system", "/api/c/about", "/api/c/teardown/options",
                  "/api/c/plant"):
            get(p)
        robots = get("/api/c/robots")
        for s in info.get("sites", []):
            get("/api/c/plant?site=" + urllib.parse.quote(s["site"]))
        for tab in ("active", "unacked", "today", "all"):
            get("/api/c/alerts?tab=" + tab)
        mt = get("/api/c/maint")
        for k in mt.get("kinds", []):
            get("/api/c/maint?kind=" + k["k"])
        for ax in range(1, 8):
            get(f"/api/c/life?axis={ax}")
        with urllib.request.urlopen(base + "/api/c/validation.html?lang=" + lang, timeout=120) as r:   # 現場驗證報告
            body = r.read()
        with open(os.path.join(data, "validation.html"), "wb") as f:
            f.write(body)
        stats["files"] += 1
        stats["bytes"] += len(body)
        rows = robots if isinstance(robots, list) else robots.get("robots", [])
        keys = [r["rkey"] for r in rows]
        for rk in keys:
            get(f"/api/c/robot?rkey={urllib.parse.quote(rk, safe='')}&days=180")
        risk = json.loads(open(os.path.join(data, static_key("/api/c/risk")), encoding="utf-8").read())
        top = []
        for it in (risk.get("rows") or []):
            rk = it.get("rkey")
            if rk and rk not in top:
                top.append(rk)
        for rk in (top + keys)[:n_robots]:
            d = json.loads(open(os.path.join(data, static_key(f"/api/c/robot?rkey={urllib.parse.quote(rk, safe='')}&days=180")),
                                encoding="utf-8").read())
            q = urllib.parse.quote(rk, safe="")
            for a in d.get("axes", []):
                get(f"/api/c/robot?rkey={q}&axis={a['axis']}&days=180")
            for days in (30, 90, 365):
                get(f"/api/c/robot?rkey={q}&days={days}")
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)
    stats["robots"] = len(keys)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "site"))
    ap.add_argument("--robots", type=int, default=12, help="收錄每一軸與各天數的機器人台數（風險最高的優先）")
    ap.add_argument("--langs", default="", help="要產生的語言，逗號分隔（預設：翻譯完成的全部語言）")
    a = ap.parse_args(argv)
    st = build(a.out, a.robots, langs=[x.strip() for x in a.langs.split(",") if x.strip()] or None)
    print(f"已輸出 {a.out}：{', '.join(st['langs'])}，{st['files']} 個快照、{st['bytes'] / 1e6:.1f} MB、{st['robots']} 台機器人")


if __name__ == "__main__":
    main()

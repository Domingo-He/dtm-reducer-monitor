"""中央演示裡的「即時監測」與「程式診斷」（2.1）。

中央演示的 160 台是產生的長期資料，沒有在跑的程式。為了在同一個視窗裡試用即時監測與程式診斷，
演示另外啟動一台模擬機器人（單機模式、免登入），接到演示資料裡的第一台機器人。
第一次要先產生 180 天的模擬歷史與程式診斷資料（約 1–2 分鐘），之後直接啟動。
資料放在 data/demo_live，跟正式資料分開；演示結束時一併關閉。
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request

from . import runtime as R
from .i18n import _

log = logging.getLogger("dtm.demolive")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _toml_str(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def child_config(base: str, name: str, port: int, users_db: str, catalog: str) -> str:
    """模擬機器人的設定檔（以 config.sim.toml 為底，路徑都改成演示資料夾）。"""
    src = os.path.join(ROOT, "config.sim.toml")
    with open(src, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r'(?m)^name = .*$', "name = " + _toml_str(name) + '\nid = "R01"', text, count=1)   # 程式診斷演示資料的 R01
    text = re.sub(r'(?m)^db = .*$', "db = " + _toml_str(os.path.join(base, "demo_live.sqlite3")) +
                  "\nbackup_dir = " + _toml_str(os.path.join(base, "backups")), text, count=1)
    text = re.sub(r'(?m)^catalog = .*$', "catalog = " + _toml_str(catalog), text, count=1)
    text = re.sub(r'(?m)^port = .*$', f"port = {port}\nusers_db = " + _toml_str(users_db), text, count=1)
    text += ("\n[system]\nrun_dir = " + _toml_str(os.path.join(base, "run")) + "\nlog_dir = " +
             _toml_str(os.path.join(base, "logs")) + "\n\n[pro]\ndata_dir = " + _toml_str(os.path.join(base, "pro_demo")) + "\n")
    path = os.path.join(base, "config.toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class DemoLive:
    def __init__(self, cfg: dict, central_db: str):
        self.cfg = cfg
        self.base = R.resolve(cfg, os.path.join("data", "demo_live"))
        os.makedirs(self.base, exist_ok=True)
        self.proc: subprocess.Popen | None = None
        self.stopped = False
        rkey, name = self._pick(central_db)
        self.rkey, self.name = rkey, name
        self.port = _free_port()
        self.token = secrets.token_urlsafe(24)
        self.state = {"ready": False, "step": _("準備中")}
        cat = cfg["workpack"].get("catalog") or os.path.join(ROOT, "workpacks.r2000ic_165f.toml")
        self.conf = child_config(self.base, name, self.port, R.resolve(cfg, cfg["web"]["users_db"]), cat)
        cfg["_demo_live"] = {rkey: self.port}
        cfg["_demo_live_token"] = self.token
        cfg["_demo_live_state"] = self.state

    @staticmethod
    def _pick(db: str) -> tuple[str, str]:
        """演示資料裡的第一台機器人（一廠的第一台）。"""
        c = sqlite3.connect(db)
        try:
            r = c.execute("SELECT r.rkey, r.name, s.name FROM robots r LEFT JOIN sites s ON s.site_id=r.site_id "
                          "ORDER BY r.site_id, r.rkey LIMIT 1").fetchone()
        finally:
            c.close()
        if not r:
            return "P1/R01", "R01"
        name = r[1] if not r[2] or str(r[1]).startswith(str(r[2])) else f"{r[2]}-{r[1]}"
        return r[0], name

    def _env(self) -> dict:
        port = self.cfg["web"]["port"]
        return dict(os.environ, DTM_INTERNAL_TOKEN=self.token, DTM_DEMO="1", PYTHONUTF8="1",
                    DTM_PARENT_URL=("https" if self.cfg["web"].get("tls_cert") else "http") + f"://127.0.0.1:{port}/api/c/i18n")

    def _run(self, *args, timeout: float = 900):
        subprocess.run([sys.executable, "-m", "dtm", "-c", self.conf, *args], cwd=ROOT, env=self._env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=True)

    def _prepare(self):
        try:
            if not os.path.exists(os.path.join(self.base, "demo_live.sqlite3")):
                self.state["step"] = _("產生 180 天模擬歷史（第一次約 1 分鐘）")
                self._run("simulate-history", "--days", "180")
            pro = os.path.join(self.base, "pro_demo")
            if not os.path.isdir(pro) or not os.listdir(pro):
                self.state["step"] = _("產生程式診斷演示資料（第一次約 1 分鐘）")
                self._run("pro", "demo", "--build-only")
            if self.stopped:
                return
            self.state["step"] = _("啟動模擬機器人")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.proc = subprocess.Popen([sys.executable, "-m", "dtm", "-c", self.conf, "run", "--service", "--force"],
                                         cwd=ROOT, env=self._env(), stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, creationflags=flags)
            for _i in range(120):
                if self.stopped or self.proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/c/info", timeout=2):
                        self.state.update(ready=True, step="")
                        log.info(_("演示的即時監測已啟動：%s（連接埠 %s）"), self.name, self.port)
                        return
                except OSError:
                    time.sleep(0.5)
            self.state["step"] = _("模擬機器人沒有啟動，請看 {p}", p=os.path.join(self.base, "logs"))
        except Exception as e:                          # 演示的附加功能，失敗不影響中央演示
            log.warning(_("演示的即時監測無法啟動：%s"), e)
            self.state["step"] = _("無法啟動：{e}", e=e)

    def start(self):
        threading.Thread(target=self._prepare, name="demo-live", daemon=True).start()
        return self

    def stop(self):
        self.stopped = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

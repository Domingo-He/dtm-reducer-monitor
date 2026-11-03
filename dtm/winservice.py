"""一鍵安裝成系統服務：python -m dtm -c config.toml service install

Windows：用 WinSW（開源、MIT 授權，tools/winsw/ 內附）把 DTM 包成 Windows 服務：
  - 開機自動啟動、不需要有人登入；沒有黑色視窗可以誤點或誤關；
  - 程式結束就自動重啟（10 秒、30 秒、60 秒後重試，1 小時後重新計算）；
  - 記錄檔自動分割，不會無限長大。
  另裝一個「DTM-Watchdog」服務跑程式看門狗。要用系統管理員身分執行（在 cmd 按右鍵「以系統管理員身分執行」）。
Linux：寫入 systemd 單元檔（/etc/systemd/system/dtm.service、dtm-watchdog.service），Restart=always。
加 --dry-run 只產生設定檔、不安裝。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape

from .i18n import _
from . import runtime as R

WINSW_EXE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "winsw",
                         "WinSW.NET461.exe")


def is_admin() -> bool:
    if os.name == "nt":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0 if hasattr(os, "geteuid") else False


def service_names(cfg: dict) -> tuple[str, str]:
    name = cfg["watchdog"].get("service_name", "DTM")
    return name, f"{name}-Watchdog"


def winsw_xml(sid: str, title: str, desc: str, python: str, args: list[str], workdir: str, logdir: str,
              depend: str = "") -> str:
    arg = " ".join(f'"{a}"' if " " in a else a for a in args)
    dep = f"\n  <depend>{escape(depend)}</depend>" if depend else ""
    return f"""<service>
  <id>{escape(sid)}</id>
  <name>{escape(title)}</name>
  <description>{escape(desc)}</description>
  <executable>{escape(python)}</executable>
  <arguments>{escape(arg)}</arguments>
  <workingdirectory>{escape(workdir)}</workingdirectory>
  <startmode>Automatic</startmode>
  <delayedAutoStart>true</delayedAutoStart>{dep}
  <onfailure action="restart" delay="10 sec"/>
  <onfailure action="restart" delay="30 sec"/>
  <onfailure action="restart" delay="60 sec"/>
  <resetfailure>1 hour</resetfailure>
  <stoptimeout>20 sec</stoptimeout>
  <stopparentprocessfirst>true</stopparentprocessfirst>
  <env name="PYTHONUTF8" value="1"/>
  <env name="PYTHONIOENCODING" value="utf-8"/>
  <logpath>{escape(logdir)}</logpath>
  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>5</keepFiles>
  </log>
</service>
"""


def systemd_unit(desc: str, python: str, args: list[str], workdir: str, user: str = "", after: str = "") -> str:
    cmd = " ".join([python] + [f'"{a}"' if " " in a else a for a in args])
    return f"""[Unit]
Description={desc}
After=network-online.target{(' ' + after) if after else ''}
Wants=network-online.target
StartLimitIntervalSec=3600
StartLimitBurst=20

[Service]
Type=simple
WorkingDirectory={workdir}
ExecStart={cmd}
Restart=always
RestartSec=10
Environment=PYTHONUTF8=1
{('User=' + user) if user else ''}

[Install]
WantedBy=multi-user.target
"""


def build_files(cfg: dict, with_watchdog: bool = True) -> dict[str, str]:
    """回傳 {檔名: 內容}；Windows 是 WinSW 的 XML，其他系統是 systemd 單元檔。"""
    name, wname = service_names(cfg)
    conf = os.path.abspath(cfg["_path"]) if cfg.get("_path") else os.path.abspath("config.toml")
    work = R.base_dir(cfg)
    py = sys.executable or "python"
    logdir = R.log_dir(cfg)
    run_args = ["-m", "dtm", "-c", conf, "run", "--service"]
    wd_args = ["-m", "dtm", "-c", conf, "watchdog"]
    title = _("DTM 減速機監控（{role}）", role={"edge": _("邊緣主機"), "central": _("中央伺服器")}.get(
        cfg["system"].get("role"), cfg["robot"]["name"]))
    files = {}
    if os.name == "nt":
        files[f"{name}.xml"] = winsw_xml(name, title, _("讀取機器人干擾扭矩、判斷減速機健康、發送通知"), py, run_args,
                                         work, logdir)
        if with_watchdog:
            files[f"{wname}.xml"] = winsw_xml(wname, _("DTM 看門狗"), _("監看 DTM 是否真的在運作，卡住時重啟並通知"),
                                              py, wd_args, work, logdir)
    else:
        files[f"{name.lower()}.service"] = systemd_unit(title, py, run_args, work)
        if with_watchdog:
            files[f"{wname.lower()}.service"] = systemd_unit(_("DTM 看門狗"), py, wd_args, work,
                                                            after=f"{name.lower()}.service")
    return files


def install(cfg: dict, with_watchdog: bool = True, dry_run: bool = False, no_error_dialog: bool = False,
            out=print) -> int:
    files = build_files(cfg, with_watchdog)
    name, wname = service_names(cfg)
    if os.name == "nt":
        sdir = os.path.join(R.base_dir(cfg), "service")
        os.makedirs(sdir, exist_ok=True)
        for fn, text in files.items():
            with open(os.path.join(sdir, fn), "w", encoding="utf-8") as f:
                f.write(text)
        out(_("已產生服務設定：{d}", d=sdir))
        if dry_run:
            return 0
        if not os.path.exists(WINSW_EXE):
            out(_("找不到 {exe}。請到 https://github.com/winsw/winsw/releases 下載 WinSW.NET461.exe 放到 tools\\winsw\\",
                  exe=WINSW_EXE))
            return 2
        if not is_admin():
            out(_("要用系統管理員身分執行：開始 → 輸入 cmd → 按右鍵「以系統管理員身分執行」，再執行一次這個指令"))
            return 3
        for sid in [name] + ([wname] if with_watchdog else []):
            exe = os.path.join(sdir, f"{sid}.exe")
            shutil.copyfile(WINSW_EXE, exe)
            for verb in ("install", "start"):
                r = subprocess.run([exe, verb], capture_output=True, text=True)
                out(f"{sid} {verb}: {(r.stdout or r.stderr).strip()[:200]}")
        if no_error_dialog:
            r = subprocess.run(["reg", "add", r"HKLM\SOFTWARE\Microsoft\Windows\Windows Error Reporting", "/v",
                                "DontShowUI", "/t", "REG_DWORD", "/d", "1", "/f"], capture_output=True, text=True)
            out(_("關閉「程式已停止運作」對話框：{r}", r=(r.stdout or r.stderr).strip()[:120]))
        out(_("完成。之後開機會自動啟動；在「服務」（services.msc）可以看到 {n}", n=name))
        return 0
    target = "/etc/systemd/system" if not dry_run else os.path.join(R.base_dir(cfg), "service")
    os.makedirs(target, exist_ok=True)
    if not dry_run and not is_admin():
        out(_("要用 root 執行：sudo python3 -m dtm -c config.toml service install"))
        return 3
    for fn, text in files.items():
        with open(os.path.join(target, fn), "w", encoding="utf-8") as f:
            f.write(text)
    out(_("已寫入：{d}", d=", ".join(os.path.join(target, f) for f in files)))
    if dry_run:
        return 0
    subprocess.run(["systemctl", "daemon-reload"])
    for fn in files:
        subprocess.run(["systemctl", "enable", "--now", fn])
    return 0


def control(cfg: dict, verb: str, out=print) -> int:
    name, wname = service_names(cfg)
    if os.name == "nt":
        sdir = os.path.join(R.base_dir(cfg), "service")
        rc = 0
        for sid in (name, wname):
            exe = os.path.join(sdir, f"{sid}.exe")
            if not os.path.exists(exe):
                continue
            seq = {"uninstall": ["stop", "uninstall"]}.get(verb, [verb])
            for v in seq:
                r = subprocess.run([exe, v], capture_output=True, text=True)
                out(f"{sid} {v}: {(r.stdout or r.stderr).strip()[:200]}")
                rc = rc or r.returncode
        return rc
    units = [f"{name.lower()}.service", f"{wname.lower()}.service"]
    if verb == "uninstall":
        for u in units:
            subprocess.run(["systemctl", "disable", "--now", u])
            try:
                os.remove(os.path.join("/etc/systemd/system", u))
            except OSError:
                pass
        subprocess.run(["systemctl", "daemon-reload"])
        return 0
    r = subprocess.run(["systemctl", verb, *units], capture_output=True, text=True)
    out((r.stdout or r.stderr).strip())
    return r.returncode

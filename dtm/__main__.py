"""命令列入口：python -m dtm <指令> -c config.toml"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime

from . import __version__
from .config import COMP_LABELS, EVENT_LABELS, RESET_KINDS, axis_cfg, axis_name, enabled_axes, load
from .i18n import LANGS, _, join, norm, set_default
from .convert import calibrate_k
from .report import build_report, export_features
from .service import Service, simulate_history
from .web import serve


def _setup_log(level=logging.INFO, file: str | None = "dtm.log", console: bool = True):
    handlers = [logging.StreamHandler(sys.stdout)] if console else []
    if file:
        from logging.handlers import RotatingFileHandler   # 5 MB 一個檔，保留 5 個，不會無限長大
        handlers.append(RotatingFileHandler(file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"))
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


def cmd_run(cfg, a):
    role = cfg["system"].get("role", "standalone")
    if role == "edge" and not getattr(a, "robot", None):
        from .fleet import run_edge
        return run_edge(cfg, a)
    if role == "central":
        from .central import run_central
        return run_central(cfg, a)
    return run_robot(cfg, a)


def _watch_parent(url: str):
    """演示用的模擬機器人：中央演示關掉（連不到）30 秒後自己結束，不會留在背景。"""
    import ssl
    import urllib.request
    ctx = ssl._create_unverified_context() if url.startswith("https") else None   # 只連本機的演示程式

    def loop():
        miss = 0
        while True:
            time.sleep(10)
            try:
                with urllib.request.urlopen(url, timeout=5, context=ctx):
                    miss = 0
            except OSError:
                miss += 1
                if miss >= 3:
                    os._exit(0)
    threading.Thread(target=loop, name="parent-watch", daemon=True).start()


def cmd_demo(cfg, a):
    """中央演示：演示資料庫（6 廠 160 台）＋中央伺服器。J4–J6 視為齒輪箱。"""
    from .demo import ensure_demo
    cfg["system"]["role"] = "central"
    for ax in (4, 5, 6):
        cfg.setdefault("axis", {}).setdefault(str(ax), {}).setdefault("reducer_type", "gearbox")
    if a.port:
        cfg["web"]["port"] = a.port
    elif not cfg.get("_path") or not os.path.exists(cfg["_path"]):
        cfg["web"]["port"] = 8090
    cfg["_demo"] = True
    if not cfg["workpack"].get("catalog"):
        cand = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workpacks.r2000ic_165f.toml")
        if os.path.exists(cand):
            cfg["workpack"]["catalog"] = cand
    n = cfg["notify"]                                 # 演示畫面上的通道（演示模式不會真的發送）
    n["sms"] = dict(n["sms"], enabled=True, to=["0912000000"], provider="modem", fallback=["http"])
    n["webhook_url"] = n.get("webhook_url") or "https://chat.example.com/hooks/maint"
    n.update(email_enabled=True, smtp_host=n.get("smtp_host") or "mail.example.com",
             mail_to=n.get("mail_to") or ["maint@example.com"])
    n["sms"]["report"] = dict(n["sms"].get("report") or {}, enabled=True)
    if not n.get("people"):                           # 演示收件人：只留這份設定檔真的有的規則
        from .demo import DEMO_PEOPLE
        names = {"default"} | {str(r.get("name")) for r in n.get("rules") or []}
        n["people"] = [dict(p, groups=[g for g in p["groups"] if g in names] or ["default"]) for p in DEMO_PEOPLE]
    print(_("產生演示資料中（約 5 秒）…"))
    path = ensure_demo(cfg, a.rebuild)
    cfg["central"]["db"] = path
    # 演示的帳號與系統設定另外存，不會影響正式使用的 users.sqlite3
    cfg["web"]["users_db"] = os.path.join(os.path.dirname(os.path.abspath(path)), "demo_users.sqlite3")
    print(_("演示資料庫：{p}", p=path))
    if a.build_only:
        return
    live = None
    if not getattr(a, "no_live", False):
        from .demolive import DemoLive
        live = DemoLive(cfg, path).start()
        print(_("即時監測與程式診斷：{name}（第一次啟動約 1–2 分鐘後可用）", name=live.name))
    print(_("中央演示啟動：http://localhost:{port}（按 Ctrl+C 結束）", port=cfg["web"]["port"]))
    from .central import run_central
    a.force = True
    try:
        return run_central(cfg, a)
    finally:
        if live:
            live.stop()


def startup_checks(cfg, a, store_path: str):
    """崩潰紀錄、關閉快速編輯模式、未正常結束偵測、自我檢查；自我檢查有錯誤就不啟動（--force 可略過）。"""
    from . import runtime as R
    R.install_crash_handlers(cfg)
    if not getattr(a, "service", False):
        R.disable_quickedit()
    marker = R.RunMarker(store_path)
    unclean = marker.start()
    checks = R.self_check(cfg, unclean, db=store_path)
    for c in checks:
        (logging.info if c["level"] == "ok" else logging.warning if c["level"] == "warn" else logging.error)(
            _("自我檢查｜%s：%s"), c["name"], c["detail"])
    errors = [c for c in checks if c["level"] == "error"]
    if errors and not getattr(a, "force", False):
        marker.stop()
        path = R.crash_log(cfg, _("自我檢查失敗，沒有啟動"), "\n".join(f"{c['name']}: {c['detail']}" for c in errors))
        logging.critical(_("自我檢查失敗，沒有啟動（紀錄：%s）。修正後再啟動，或加 --force 強制啟動"), path)
        raise SystemExit(4)
    return marker, checks, unclean


def run_robot(cfg, a):
    from . import runtime as R
    marker, checks, unclean = startup_checks(cfg, a, cfg["storage"]["db"])
    svc = Service(cfg)
    svc.selfcheck = checks
    svc.store.add_sys_event("start", 1 if unclean else 0,
                            f"DTM {__version__} pid {os.getpid()}" + (_("（上次未正常結束）") if unclean else ""))
    for c in checks:
        if c["level"] != "ok":
            svc.store.add_sys_event("selfcheck", 2 if c["level"] == "error" else 1, f"{c['name']}: {c['detail']}")
    acq = svc.start()
    host, port = cfg["web"]["host"], int(cfg["web"]["port"])
    httpd = serve(svc, host, port)
    sw = R.StatusWriter(svc)
    sw.start(svc._stop)
    from .sync import run_robot_side
    side = threading.Thread(target=run_robot_side, args=(svc, svc._stop), name="dtm-side", daemon=True)
    side.start()
    R.watch_threads(cfg, {_("擷取"): acq, _("狀態檔"): sw.thread, _("中央同步"): side}, svc._stop,
                    allow_exit=lambda: svc.finished)
    scheme = "https" if cfg["web"].get("tls_cert") else "http"
    logging.info(_("DTM %s 啟動：來源=%s，儀表板 %s://%s:%s"), __version__, cfg["robot"]["source"], scheme,
                 "localhost" if host == "0.0.0.0" else host, port)
    if not svc.auth.enabled and host not in ("127.0.0.1", "localhost", "::1"):
        logging.warning(_("未設定寫入密碼：同網段任何人都能登錄事件、確認警報。請在設定檔 [web] 加上 write_password"))

    def stop():
        # 在另一個執行緒執行（serve_forever 在主執行緒；shutdown() 必須由其他執行緒呼叫，否則會互鎖）
        logging.info(_("停止中…"))
        svc.stop()
        httpd.shutdown()

    R.on_stop_signals(stop)                          # Ctrl+C、停止服務、Windows 的 Ctrl+Break
    try:
        httpd.serve_forever()
    finally:
        svc.stop()
        time.sleep(0.5)
        svc.flush(force=True)
        try:
            sw.write_once()
            svc.store.add_sys_event("stop", 0, _("正常結束"))
        except Exception:
            pass
        marker.stop()


def cmd_simulate_history(cfg, a):
    svc = simulate_history(cfg, days=a.days)
    for ax in svc.axes:
        h = svc.health[ax]
        print(f"{axis_cfg(cfg, ax)['name']}: {h['level_name']}  {join(h['reasons'])}")
    print(_("已寫入 {db}", db=cfg["storage"]["db"]))


def cmd_runs(cfg, a):
    from .service import Service as S
    svc = S(cfg, notify=False)
    for ax in ([a.axis] if a.axis else enabled_axes(cfg)):
        rows = svc.store.features(ax, valid_only=False)[-a.last:]
        print(_("--- {name}（原始值）", name=axis_cfg(cfg, ax)["name"]))
        print(f"{'ID':>6}  {_('時間'):16}  {'F':>9} {'Fc':>9} {'Fv':>8} {'G':>9} {_('漣波'):>7} {_('溫度'):>5} {_('有效')}")
        for r in rows:
            f = lambda v, n=2: "—" if v is None else f"{v:.{n}f}"  # noqa: E731
            print(f"{r['id']:>6}  {datetime.fromtimestamp(r['ts']):%Y-%m-%d %H:%M}  {f(r['F']):>9} {f(r['Fc']):>9} "
                  f"{f(r['Fv'], 3):>8} {f(r['G']):>9} {f(r['ripple']):>7} {f(r['temp'], 1):>5} "
                  f"{_('是') if r['valid'] else _('否')}")


def cmd_calibrate(cfg, a):
    svc = Service(cfg, notify=False)
    r0, r1 = svc.store.feature(a.without), svc.store.feature(a.with_)
    if not r0 or not r1:
        sys.exit(_("找不到指定的診斷 ID，請先用 runs 指令查詢"))
    if r0["axis"] != a.axis or r1["axis"] != a.axis:
        sys.exit(_("診斷 ID 的軸號與 --axis 不符"))
    res = calibrate_k(r0["G"], r1["G"], a.mass, a.dist)
    print(_("ΔG = {dg:.3f}（原始值），對應重力矩 {nm:.1f} Nm", dg=res["dG"], nm=res["dtau_nm"]))
    print(_("k = {k:.5f} Nm/原始值", k=res["k"]))
    noise = max(r0["ripple"] or 0, r1["ripple"] or 0)
    if abs(res["dG"]) < 3 * noise:
        print(_("注意：ΔG 小於漣波的 3 倍，校正誤差大，建議加大質量或力臂後重做"))
    if a.save:
        svc.store.set_calib(a.axis, res["k"], a.zero, f"m={a.mass}kg d={a.dist}m runs {a.without}/{a.with_}")
        svc.store.add_event(time.time(), a.axis, "calibration", f"k={res['k']:.5f}")
        svc.store.set_valid(a.with_, False)   # 加重那次不是正常狀態，排除在健康判定之外
        print(_("已寫入資料庫，重新啟動 run 後生效；加重那次診斷已排除在健康判定之外"))
    else:
        print(_("若要套用：加上 --save，或在設定檔 [axis.{axis}] 填 k_nm_per_raw = {k:.5f}", axis=a.axis, k=res["k"]))


def _ts(text: str | None) -> float:
    if not text:
        return time.time()
    for f in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, f).timestamp()
        except ValueError:
            pass
    raise SystemExit(_("時間格式錯誤：{text}（用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM）", text=text))


def _who(a) -> str:
    import getpass
    if getattr(a, "who", ""):
        return a.who
    try:
        return getpass.getuser() + "@cli"
    except Exception:
        return "cli"


def cmd_event(cfg, a):
    svc = Service(cfg, notify=False)
    eid = svc.register_event(_ts(a.time), a.axis, a.kind, a.note, a.failed, a.teardown, a.hours, who=_who(a))
    print(_("已登錄 #{id}：{label}", id=eid, label=EVENT_LABELS[a.kind])
          + (_("（之後重新建立基準）") if a.kind in RESET_KINDS else ""))


def cmd_events(cfg, a):
    svc = Service(cfg, notify=False)
    rows = svc.store.events(a.axis, include_undone=True)[-a.last:]
    for e in rows:
        ax = axis_name(cfg, e["axis"]) if e["axis"] else _("全軸")
        mark = _("〔已撤銷〕") if e.get("undone_ts") else ""
        print(f"#{e['id']:<5} {datetime.fromtimestamp(e['ts']):%Y-%m-%d %H:%M}  {ax:<4} "
              f"{EVENT_LABELS.get(e['kind'], e['kind'])}{mark}  {e.get('note') or ''}")
    if not rows:
        print(_("沒有事件紀錄"))


def cmd_undo_event(cfg, a):
    svc = Service(cfg, notify=False)
    try:
        ev = svc.undo_event(a.id, _who(a), a.note)
    except ValueError as e:
        raise SystemExit(str(e))
    print(_("已撤銷 #{id}：{label}；元件壽命、基準與維護決策已回到登錄前", id=ev["id"],
            label=EVENT_LABELS.get(ev["kind"], ev["kind"])))


def cmd_backup(cfg, a):
    from . import backup as B
    if a.list:
        where = "mirror" if a.mirror else "local"
        rows = B.list_backups(cfg, where)
        for b in rows:
            print(f"{datetime.fromtimestamp(b['ts']):%Y-%m-%d %H:%M}  {b['size'] / 1e6:7.1f} MB  {b['path']}")
        d = B.mirror_dir(cfg) if a.mirror else B.backup_dir(cfg)
        print(_("共 {n} 份，資料夾：{dir}", n=len(rows), dir=d or _("（未啟用）")))
        return
    try:
        path = B.backup_now(cfg, keep_raw=a.raw)
    except Exception as e:
        raise SystemExit(_("備份失敗：{e}", e=e))
    print(_("已備份：{path}（{mb:.1f} MB）", path=path, mb=os.path.getsize(path) / 1e6))
    if B.mirror_dir(cfg):
        try:
            r = B.mirror_sync(cfg)
            print(_("已複製到網路資料夾並比對 SHA-256：{n} 份（{dir}）", n=len(r["copied"]), dir=B.mirror_dir(cfg)))
        except Exception as e:
            print(_("複製到網路資料夾失敗：{e}（本機備份已完成；DTM 執行中會自動補複製）", e=e))


def _dashboard_running(cfg) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(cfg["web"]["port"])), timeout=1):
            return True
    except OSError:
        return False


def _ask(prompt: str, default: str = "") -> str:
    try:
        v = input(prompt + (f" [{default}]" if default else "") + "：").strip()
    except EOFError:
        v = ""
    return v or default


def _restore_wizard(cfg, a):
    """沒有指定 --source 時，一步一步問。"""
    from . import restore as RS
    src = RS.sources(cfg)
    opts = [("local", _("本機備份（{n} 份）", n=len(src["local"]))),
            ("mirror", _("網路資料夾（{n} 份）", n=len(src["mirror"])) + (f" ⚠ {src['mirror_error']}" if src["mirror_error"] else "")),
            ("snapshot", _("還原前自動備份（{n} 份）", n=len(src["snapshot"]))),
            ("central", _("從中央重建") + ("" if src["central"] else _("（未設定 [central] url）"))),
            ("file", _("指定檔案（備份 zip、.sqlite3、中央匯出 .json.gz）"))]
    print(_("== 還原精靈 =="))
    for i, (_k, t) in enumerate(opts, 1):
        print(f"  {i}. {t}")
    a.source = opts[int(_ask(_("來源"), "1")) - 1][0]
    if a.source in ("local", "mirror", "snapshot"):
        rows = src[a.source][:15]
        if not rows:
            raise SystemExit(_("找不到任何備份"))
        for i, b in enumerate(rows, 1):
            print(f"  {i}. {datetime.fromtimestamp(b['ts']):%Y-%m-%d %H:%M}  {b['name']}")
        a.name = rows[int(_ask(_("時間點"), "1")) - 1]["name"]
    elif a.source == "file":
        a.file = _ask(_("檔案路徑"))
    ax = _ask(_("範圍：整台請直接按 Enter，單軸請輸入軸號（例如 2）"), "")
    a.axis = int(ax) if ax else None
    keys = list(RS.GROUPS)
    for i, g in enumerate(keys, 1):
        print(f"  {i}. {RS.GROUP_LABELS[g]}")
    pick = _ask(_("資料類型（逗號分隔）"), "1,2,3,4")
    a.types = ",".join(keys[int(x) - 1] for x in pick.replace("，", ",").split(",") if x.strip())
    a.since = _ask(_("時間區間開始 YYYY-MM-DD（不限請按 Enter）"), "") or None
    a.until = _ask(_("時間區間結束 YYYY-MM-DD（不限請按 Enter）"), "") or None
    print("  1. " + _("補回遺失（不刪、不改現有資料）"))
    print("  2. " + _("回到時間點（範圍內改成跟備份一樣；備份之後的資料會刪除）"))
    a.mode = "overwrite" if _ask(_("模式"), "1") == "2" else "merge"


def cmd_restore(cfg, a):
    from . import backup as B
    from . import restore as RS
    if a.list:
        src = RS.sources(cfg)
        for k, title in (("local", _("本機備份")), ("mirror", _("網路資料夾")), ("snapshot", _("還原前自動備份"))):
            print(f"[{title}]" + (f" ⚠ {src['mirror_error']}" if k == "mirror" and src["mirror_error"] else ""))
            for b in src[k][:20]:
                print(f"  {datetime.fromtimestamp(b['ts']):%Y-%m-%d %H:%M}  {b['size'] / 1e6:7.1f} MB  {b['name']}")
        print(_("[從中央重建] {s}", s=src["central_url"] or _("未設定 [central] url")))
        return
    if a.mode == "full":                                  # 整個資料庫檔換掉（資料庫壞掉、打不開時用）
        src = a.file or (B.list_backups(cfg)[0]["path"] if B.list_backups(cfg) else None)
        if not src:
            raise SystemExit(_("找不到任何備份；請用 --file 指定檔案"))
        if _dashboard_running(cfg):
            raise SystemExit(_("DTM 還在執行（連接埠 {port} 有回應）。請先關閉 DTM 的黑色視窗再還原", port=cfg["web"]["port"]))
        print(_("要用這份備份整個取代目前的資料庫：{src}", src=src))
        print(_("目前的資料庫會先另存一份，萬一還原錯了還能換回來。"))
        if not a.yes and _ask(_("確定還原請輸入 y")).lower() != "y":
            raise SystemExit(_("已取消"))
        try:
            saved = B.restore(cfg, src)
        except Exception as e:
            raise SystemExit(_("還原失敗：{e}", e=e))
        print(_("已還原。原本的資料庫另存為：{path}", path=saved or "—"))
        return
    if not a.source:
        if a.file:
            a.source = "file"
        elif a.yes:
            a.source = "local"
        else:
            _restore_wizard(cfg, a)
    try:
        scope = RS.parse_scope({"axis": a.axis, "groups": a.types, "since": a.since, "until": a.until})
        opened = RS.open_source(cfg, a.source, name=a.name or "", path=a.file or "")
    except Exception as e:
        raise SystemExit(_("無法開啟來源：{e}", e=e))
    try:
        pv = RS.preview(cfg, opened, scope, a.mode)
        print(_("來源：{src}（{t}）", src=pv["source"], t=datetime.fromtimestamp(pv["source_ts"]).strftime("%Y-%m-%d %H:%M")
                if pv["source_ts"] else "—"))
        print(_("範圍：{s}｜模式：{m}", s=pv["scope"], m=pv["mode_label"]))
        print(_("  資料              備份      目前    會補回    會刪除"))
        for r in pv["tables"]:
            print(f"  {r['label']:<12}{r['backup']:>10}{r['current']:>10}{r['add']:>10}{r['remove']:>10}")
        for w in pv["warnings"]:
            print("  ⚠ " + w)
        if a.preview:
            return
        if not pv["add"] and not pv["remove"]:
            print(_("沒有需要還原的資料。"))
            return
        if _dashboard_running(cfg):
            raise SystemExit(_("DTM 執行中：請改用儀表板「資料還原」（管理員、這台電腦本機），完成後會自動重算判定；"
                               "或先關閉 DTM 再執行這個指令。"))
        confirm = ""
        if not a.yes:
            if _ask(_("確定執行請輸入 y")).lower() != "y":
                raise SystemExit(_("已取消"))
        if a.mode == "overwrite":
            confirm = a.confirm or ("" if a.yes else _ask(_("回到時間點會刪除資料。請再輸入「{p}」確認", p=pv["confirm_phrase"])))
        try:
            res = RS.run(cfg, opened, scope, a.mode, who=a.who or os.environ.get("USERNAME") or os.environ.get("USER") or "",
                         confirm=confirm)
        except PermissionError as e:
            raise SystemExit(str(e))
        print(_("完成：補回 {a} 筆、刪除 {r} 筆（{s} 秒）", a=res["added"], r=res["removed"], s=res["seconds"]))
        print(_("還原前的資料庫另存為：{p}", p=res["pre_backup"]))
        print(_("下次啟動 DTM 會自動重算各軸判定。"))
    finally:
        opened.close()


def cmd_export_analysis(cfg, a):
    """分析資料匯出：機器人資料庫（單機、或邊緣主機加 --robot），中央用 --rkey。"""
    from . import export as EX
    if cfg["system"].get("role") == "central" or a.rkey:
        from .central import CentralStore
        from . import runtime as R
        if not a.rkey:
            raise SystemExit(_("中央請用 --rkey 指定機器人，例如 --rkey P3/R14"))
        cs = CentralStore(R.resolve(cfg, cfg["central"].get("db", "central.sqlite3")), cfg, local=True)
        out = a.out or os.path.join(EX.exports_dir(cfg), EX.default_name(a.rkey, a.since, a.until))
        try:
            res = EX.export_central(cs, a.rkey, out, a.since, a.until, a.anonymize)
        except ValueError as e:
            raise SystemExit(str(e))
    else:
        if cfg["system"].get("role") == "edge" and not a.robot:
            raise SystemExit(_("邊緣主機請加 --robot 指定機器人，例如：python -m dtm -c config.toml --robot R01 export-analysis"))
        out = a.out or os.path.join(EX.exports_dir(cfg), EX.default_name(cfg["robot"].get("name") or "robot",
                                                                         a.since, a.until))
        res = EX.export_robot_db(cfg, out, a.since, a.until, a.anonymize)
    print(_("已匯出：{path}（{mb:.1f} MB）", path=os.path.abspath(res["path"]), mb=res["size"] / 1e6))
    for k, v in res["counts"].items():
        print(f"  {k}: {v}")
    if res["photos"]:
        print(f"  photos/: {res['photos']}")
    print(_("不含一般原始取樣、帳號、金鑰、電話與 Email；{x}", x=_("名稱已匿名化") if a.anonymize else _("要匿名化請加 --anonymize")))


def cmd_replay(cfg, a):
    from . import replay as RP
    from .store import Store
    try:
        over = RP.parse_sets(a.set or [])
        if a.alt:
            import tomllib
            with open(a.alt, "rb") as f:
                over = RP.with_overrides(tomllib.load(f), over) if over else tomllib.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(str(e))
    if not over:
        raise SystemExit(_("請用 --set 指定要試算的設定，例如 --set health.caution_ratio=1.3"))
    res = RP.run(Store(cfg["storage"]["db"]), cfg, over, until=time.time(), since=time.time() - a.days * 86400,
                 axes=[a.axis] if a.axis else None, decision=a.decision, step_days=a.step_days)
    print(RP.text(res))


def cmd_commissioning(cfg, a):
    from . import commissioning as CM
    out = a.out or os.path.join(CM.reports_dir(cfg), f"commissioning_{datetime.now():%Y%m%d_%H%M}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(CM.build(cfg, a.days))
    print(_("已產生導入檢查報告：{p}", p=os.path.abspath(out)))


def cmd_downtime(cfg, a):
    """計畫停機：DTM 執行中也可以設定（約 10 秒內生效）。"""
    from . import downtime as DT
    from .store import Store
    st = Store(cfg["storage"]["db"])
    who = _who(a)
    if a.cancel:
        had = DT.clear(cfg)
        if had:
            st.add_sys_event("downtime", 0, _("取消計畫停機") + f"｜{who}")
        print(_("已取消計畫停機") if had else _("目前沒有計畫停機"))
        return
    if not a.until:
        d = DT.get(cfg)
        print(DT.label(d) if d else _("目前沒有計畫停機"))
        return
    try:
        d = DT.set_(cfg, a.until, a.note, who)
    except ValueError as e:
        raise SystemExit(str(e))
    st.add_sys_event("downtime", 0, DT.label(d) + f"｜{who}")
    print(DT.label(d))


def cmd_teardown(cfg, a):
    from . import teardown as TD
    svc = Service(cfg, notify=False)
    photos = []
    for f in a.photo or []:
        import base64
        with open(f, "rb") as fh:
            photos.append(TD.save_photo(cfg, base64.b64encode(fh.read()).decode(), f"ev{a.id}"))
    form = {"location": [x for x in (a.location or "").split(",") if x], "modes": [x for x in (a.mode or "").split(",") if x],
            "grease": a.grease or "", "note": a.note, "photos": photos, "no_fault": a.no_fault}
    try:
        td = svc.set_teardown(a.id, form, _who(a))
    except ValueError as e:
        raise SystemExit(str(e))
    print(_("已登錄事件 #{id} 的拆檢結果：{t}", id=a.id, t=TD.text(td)))


def cmd_central_export(cfg, a):
    """在中央電腦匯出某台機器人的資料，帶到機器人電腦用 restore --file 重建。"""
    import gzip
    from .central import CentralStore
    from . import runtime as R
    db = R.resolve(cfg, cfg["central"].get("db", "central.sqlite3"))
    if not os.path.exists(db):
        raise SystemExit(_("找不到中央資料庫：{path}", path=db))
    cs = CentralStore(db, cfg, local=True)
    if not a.robot:
        for r in cs.q("SELECT rkey, name FROM robots ORDER BY rkey"):
            print(f"  {r['rkey']}  {r['name'] or ''}")
        raise SystemExit(_("請用 --robot 指定機器人代號（上面的清單）"))
    try:
        exp = cs.export_robot(a.robot)
    except ValueError as e:
        raise SystemExit(str(e))
    out = a.out or (a.robot.replace("/", "_") + "_central.json.gz")
    with gzip.open(out, "wt", encoding="utf-8") as f:
        json.dump(exp, f, ensure_ascii=False)
    n = sum(len(v) for v in exp.values() if isinstance(v, list))
    print(_("已匯出 {n} 筆：{path}", n=n, path=os.path.abspath(out)))
    print(_("到機器人電腦執行：python -m dtm restore --file {name}", name=os.path.basename(out)))


def cmd_check(a) -> int:
    from .check import ERROR, HINT, WARN, run_check
    print(_("檢查設定檔：{path}", path=os.path.abspath(a.config)))
    rep = run_check(a.config, a.connect, a.seconds)
    for line in rep.lines():
        print("  " + line)
    e, w, h = rep.count(ERROR), rep.count(WARN), rep.count(HINT)
    print(_("結果：錯誤 {e}、警告 {w}、提示 {h}", e=e, w=w, h=h))
    if e:
        print(_("請先修正【錯誤】再啟動。改完存檔後再跑一次 check。"))
    elif not a.connect:
        print(_("設定檔沒有錯誤。要測試跟機器人的連線，請加 --connect"))
    return 1 if e else 0


def cmd_temp_coef(cfg, a):
    """從自己的資料估計溫度係數，建議要不要更新設定檔。"""
    import statistics
    from .analysis import estimate_temp_coef
    svc = Service(cfg, notify=False)
    hc = cfg["health"]
    good = []
    for ax in ([a.axis] if a.axis else svc.axes):
        rows = svc.store.features(ax, since=svc.store.last_reset(ax) if not a.all else 0)
        est = estimate_temp_coef(rows)
        print(_("--- {name}", name=axis_cfg(cfg, ax)["name"]))
        for m, key in (("Fv", "temp_coef_fv"), ("Fc", "temp_coef_fc")):
            e = est[m]
            now = float(hc[key])
            if e["k"] is None:
                print("  " + _("{m}：無法估計，{why}（目前設定 {now:.3f}）", m=m, why=e["why"], now=now))
                continue
            print("  " + _("{m}：每 °C {k:+.3f}（±{se:.3f}），{n} 次診斷、溫差 {r:.1f}°C；目前設定 {now:.3f}",
                           m=m, k=e["k"], se=e["se"], n=e["n"], r=e["range"], now=now)
                  + ("" if e["ok"] else _("　← {why}", why=e["why"])))
            if e["ok"] and m == "Fv":
                good.append(e["k"])
    if not good:
        print(_("目前沒有可靠的估計。通常需要累積一個季節（溫差 8°C 以上）的診斷資料，而且要有溫度訊號。"))
        return
    k = statistics.median(good)
    now = float(hc["temp_coef_fv"])
    if abs(k - now) <= max(0.005, 0.2 * now):
        print(_("估計值 {k:.3f} 與目前設定 {now:.3f} 接近，不需要修改。", k=k, now=now))
    else:
        print(_("建議把設定檔 [health] 改成：temp_coef_fv = {k:.3f}（目前 {now:.3f}）。改完重新啟動 DTM。", k=k, now=now))


def cmd_scenarios(cfg, a):
    from .scenarios import run_all
    only = [x.strip() for x in a.only.split(",")] if a.only else None
    print(_("情境模擬：每個情境約 30 秒…"))
    run_all(cfg, a.days, only)


def cmd_iron(cfg, a):
    svc = Service(cfg, notify=False)
    svc.add_iron(_ts(a.time), a.axis, a.wt, a.sample, a.note)
    d = svc.decisions[a.axis]
    print(_("已登錄 J{axis} 鐵粉 {wt} wt%。{drive}：{da}；潤滑脂：{ga}", axis=a.axis, wt=a.wt, drive=d["drive_label"],
            da=d["drive"]["action"], ga=d["grease"]["action"]))


def cmd_vib_import(cfg, a):
    """CSV 欄位：ts 或 time、axis、direction（radial/axial）、rms、env、mesh、motor1x、kurt、temp（可缺）。"""
    import csv
    svc = Service(cfg, notify=False)
    n, axes = 0, set()
    with open(a.file, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            ts = float(row["ts"]) if row.get("ts") else _ts(row.get("time"))
            vals = {k: float(row[k]) for k in ("rms", "env", "mesh", "motor1x", "kurt") if row.get(k) not in (None, "")}
            temp = float(row["temp"]) if row.get("temp") else None
            svc.add_vib(ts, int(row["axis"]), row.get("direction", "radial"), vals, temp, a.source, refresh=False)
            axes.add(int(row["axis"]))
            n += 1
    for ax in axes & set(svc.axes):
        svc.refresh_decision(ax)
    print(_("匯入 {n} 筆振動資料", n=n))


def cmd_install(cfg, a):
    svc = Service(cfg, notify=False)
    cur = svc.store.current_component(a.axis, a.comp)
    if cur and not a.force:
        raise SystemExit(_("J{axis} 已有使用中的{comp}紀錄（{date}）；更換請用 event 指令，覆蓋請加 --force",
                           axis=a.axis, comp=COMP_LABELS[a.comp],
                           date=f"{datetime.fromtimestamp(cur['install_ts']):%Y-%m-%d}"))
    svc.store.install(a.axis, a.comp, _ts(a.date), cfg["robot"]["name"], a.note)
    print(_("已登錄 J{axis} {comp} 安裝日 {date}", axis=a.axis, comp=COMP_LABELS[a.comp], date=a.date))


def cmd_history(cfg, a):
    """CSV 欄位：comp（reducer/gearbox/motor）、life_h、failed（1 失效／0 預防）、robot、note。"""
    import csv
    svc = Service(cfg, notify=False)
    n = 0
    with open(a.file, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            failed = str(row.get("failed", "1")).strip().lower() in ("1", "true", "yes", "y", "是")
            svc.store.add_history(row["comp"], float(row["life_h"]), failed,
                                  row.get("robot", ""), None, "", row.get("note", ""))
            n += 1
    print(_("匯入 {n} 筆壽命紀錄", n=n))


def cmd_hours(cfg, a):
    svc = Service(cfg, notify=False)
    svc.store.add_hours(_ts(a.time), a.hours)
    print(_("已登錄時數表 {h} h", h=a.hours))


def cmd_notify_test(cfg, a):
    from .notify import Notifier
    from .store import Store
    nt = Notifier(cfg, Store(cfg["storage"]["db"]))
    for st in nt.status():
        print("  " + _("{ch} {state}  最低等級 {lv}  收件人 {to}", ch=f"{st['channel']:<8}",
                       state=_("啟用") if st["enabled"] else _("停用"), lv=st["min_level"], to=st["targets"]))
    for r in nt.test(a.channel, a.text):
        print(f"{r['channel']:<8} {r.get('target', '-'):<14} {_('成功') if r['ok'] else _('失敗')}  {r['detail']}")


def cmd_decide(cfg, a):
    svc = Service(cfg, notify=False)
    for ax in ([a.axis] if a.axis else svc.axes):
        d = svc.refresh_decision(ax, raise_alarms=False)
        print(f"=== {d['name']}  " + _("研判：{primary}", primary=d["primary"]))
        print(f"  {COMP_LABELS['grease']}: {d['grease']['action']}")
        print(f"  {d['drive_label']}: {d['drive']['action']}")
        print(f"  {COMP_LABELS['motor']}: {d['motor']['action']}")
        for k, v in d["families"].items():
            print(f"    {k:<24} {'—' if v is None else f'{v:.2f}'}")
        for x in d["next"]:
            print(f"  → {x}")
        for x in d["notes"]:
            print(f"  ({x})")


def cmd_workpack(cfg, a):
    from . import workpack as wp
    cat = wp.load_catalog(cfg)
    if cat["error"] or not cat["path"]:
        sys.exit(cat["error"] or _("設定檔沒有指定作業包目錄：[workpack] catalog"))
    if a.list or not a.axis:
        print(_("作業包目錄：{path}（{model}）", path=cat["path"], model=cat["meta"].get("robot_model", "")))
        for p in cat["packs"]:
            print(f"  {p.get('id', ''):<28} {p.get('title', ''):<16} "
                  + _("元件 {comp}  軸 {axes}", comp=p.get("comp"), axes=p.get("axes") or _("全部")))
        return
    comp_ = a.comp or wp.comp_for(cfg, a.axis, "drive")
    w = wp.get(cfg, a.axis, comp_)
    if not w:
        sys.exit(_("J{axis} {comp} 尚未建立作業包", axis=a.axis, comp=comp_))
    if a.html:
        svc = Service(cfg, notify=False)
        d = svc.refresh_decision(a.axis, raise_alarms=False) if a.axis in svc.axes else None
        key = {"grease": "grease", "motor": "motor"}.get(comp_, "drive")
        with open(a.html, "w", encoding="utf-8") as f:
            f.write(wp.render_workorder(cfg, w, d, key))
        print(_("工單：{url}", url=a.html))
        return
    print("=== " + w["label"] + _("｜") + _("人力 {crew} 人｜停機 {hours}（參考）", crew=w["crew"], hours=wp.hours_text(w)))
    print(_("備品："))
    for p in w["parts"]:
        flag = "  " + _("缺 {n:g}", n=p["short"]) if p["short"] else ""
        chk = "" if p.get("verified") else "  [" + _("待確認") + "]"
        print(f"  ☐ {p['name']:<20} {p.get('pn', ''):<22} ×{p['qty']:g} {p.get('unit', '')}{flag}{chk}")
        if p.get("grease_note"):
            print(f"      {p['grease_note']}")
    print(_("工具：") + join([t["name"] for t in w["tools"]], "、"))
    print(_("安全："))
    for i, x in enumerate(w["safety"], 1):
        print(f"  {i}. {x}")
    print(_("步驟："))
    for i, x in enumerate(w["steps"], 1):
        print(f"  {i:>2}. {x.get('name')} " + _("（{crew} 人，{h:g} h）", crew=x.get("crew", ""), h=float(x.get("hours", 0))))
    if w["sop_url"]:
        print(f"SOP: {w['sop_url']}")


def cmd_watchdog(cfg, a):
    from .watchdog import run_watchdog
    _setup_log(file=os.path.join(_log_dir(cfg), "watchdog.log"))
    run_watchdog(cfg, a)


def cmd_maintenance(cfg, a):
    from . import watchdog as W
    if a.action == "on":
        m = W.set_maintenance(cfg, a.minutes, a.reason, _who(a))
        print(_("維護模式開啟到 {t}：看門狗不會重啟服務，硬體看門狗暫停。完成後執行 maintenance off",
                t=datetime.fromtimestamp(m["until"]).strftime("%Y-%m-%d %H:%M")))
    elif a.action == "off":
        W.clear_maintenance(cfg)
        print(_("維護模式已結束"))
    else:
        m = W.maintenance(cfg)
        print(_("維護模式：到 {t}（{r}）", t=datetime.fromtimestamp(m["until"]).strftime("%Y-%m-%d %H:%M"),
                r=m.get("reason") or "—") if m else _("目前不是維護模式"))
        s = W.watchdog_summary(cfg)
        print(_("看門狗：{st}｜30 天內自動重啟 {r} 次、硬體重開機 {h} 次、資料中斷 {d} 次",
                st=_("執行中") if s["running"] else _("沒有在執行"), r=s["restarts_30d"], h=s["hw_stops_30d"],
                d=s["data_30d"]))


def cmd_service(cfg, a):
    from . import winservice as WS
    if a.action == "install":
        raise SystemExit(WS.install(cfg, not a.no_watchdog, a.dry_run, a.no_error_dialog))
    raise SystemExit(WS.control(cfg, a.action))


def cmd_selfcheck(cfg, a):
    from . import runtime as R
    for c in R.self_check(cfg, force_db=True):
        tag = {"ok": _("【正常】"), "warn": _("【警告】"), "error": _("【錯誤】")}[c["level"]]
        print(f"{tag} {c['name']}：{c['detail']}")


def cmd_user(cfg, a):
    import getpass
    from .auth import ROLE_NAMES, ROLES, UserStore
    from .runtime import resolve
    us = UserStore(resolve(cfg, cfg["web"].get("users_db", "users.sqlite3")))

    def ask_pw():
        if a.password:
            return a.password
        p1 = getpass.getpass(_("密碼（至少 8 個字）："))
        if p1 != getpass.getpass(_("再輸入一次：")):
            raise SystemExit(_("兩次密碼不一樣"))
        return p1
    try:
        if a.action == "list":
            rows = us.list()
            for u in rows:
                last = datetime.fromtimestamp(u["last_login"]).strftime("%Y-%m-%d %H:%M") if u["last_login"] else "—"
                print(f"{u['username']:<16} {u['display'] or '':<10} {ROLE_NAMES.get(u['role'], u['role']):<6} "
                      f"{_('啟用') if u['active'] else _('停用')}  {_('最後登入')} {last}  {u.get('plants') or ''}")
            print(_("共 {n} 個帳號（{path}）", n=len(rows), path=us.path))
            return
        if not a.username:
            raise SystemExit(_("請指定帳號"))
        if a.action == "add":
            us.add(a.username, ask_pw(), a.role or "operator", a.display, a.plants)
            print(_("已建立帳號 {u}（{r}）。有帳號之後，登入改用帳號密碼，共用密碼不再使用。", u=a.username,
                    r=ROLE_NAMES.get(a.role or "operator")))
        elif a.action == "passwd":
            us.set_password(a.username, ask_pw())
            print(_("已更新 {u} 的密碼", u=a.username))
        elif a.action == "role":
            if a.role not in ROLES:
                raise SystemExit(_("請用 --role 指定：{opts}", opts=_("、").join(ROLES)))
            us.update(a.username, role=a.role)
            print(_("{u} 改為 {r}", u=a.username, r=ROLE_NAMES.get(a.role)))
        elif a.action in ("disable", "enable"):
            us.update(a.username, active=1 if a.action == "enable" else 0)
            print(_("{u} 已{s}", u=a.username, s=_("啟用") if a.action == "enable" else _("停用")))
    except ValueError as e:
        raise SystemExit(str(e))


def cmd_validation(cfg, a):
    """現場驗證報告：失效前有沒有先出現注意／警戒、提早幾天、漏報、誤報候選。"""
    from . import runtime as R
    from . import validation as VA
    role = cfg["system"].get("role", "standalone")
    if role in ("central", "edge"):
        from .central import CentralStore
        db = R.resolve(cfg, cfg["central"].get("db", "central.sqlite3")) if role == "central" else \
            os.path.join(R.resolve(cfg, cfg["fleet"].get("data_dir", "data")), "_fleet.sqlite3")
        if not os.path.exists(db):
            raise SystemExit(_("找不到資料庫：{path}", path=db))
        r = VA.for_central(CentralStore(db, cfg, local=True), a.days)
        scope = cfg["system"].get("display_name") or cfg["system"].get("site_name") or ""
    else:
        from .store import Store
        r = VA.for_store(cfg, Store(R.resolve(cfg, cfg["storage"]["db"])), a.days)
        scope = cfg["robot"]["name"]
    out = a.out or time.strftime("dtm_validation_%Y%m%d.") + ("json" if a.json else "html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(VA.to_json(r) if a.json else VA.build_html(r, scope))
    s = r["summary"]
    rate = "—" if s["rate"] is None else f"{s['rate'] * 100:.0f}%"
    print(_("失效 {n} 件（可以評估 {e}）：提早偵測 {d}、漏報 {m}；偵測率 {r}；誤報候選 {f} 次",
            n=s["failures"], e=s["evaluable"], d=s["detected"], m=s["missed"], r=rate, f=s["false_alarm"]))
    print(_("已產生：{p}", p=os.path.abspath(out)))


def cmd_tls(cfg, a):
    """HTTPS：產生自簽憑證並寫進設定檔，或關閉，或查看目前的憑證。"""
    from . import settings as S
    from . import tlscert
    from .runtime import resolve
    w = cfg.get("web") or {}
    try:
        if a.off:
            bak = S.disable_tls(cfg, "127.0.0.1")
            print(_("已關閉 HTTPS（設定檔備份：{b}）。重新啟動 DTM 後生效。", b=bak))
            return
        if a.self_signed:
            info = S.enable_tls(cfg, "127.0.0.1", a.name or [])
            print(_("已產生自簽憑證：{p}", p=info["cert"]))
            print(_("包含名稱：{n}", n=", ".join(info["names"] + info["ips"])))
            print(_("指紋 SHA-256：{f}", f=info["sha256"]))
            print(_("設定檔已改為 HTTPS（備份：{b}）。重新啟動 DTM 後用 https:// 開啟。", b=info["backup"]))
            print(_("瀏覽器第一次會顯示「不安全」：把 {p} 匯入各台電腦的「受信任的根憑證授權單位」後就不會再出現。",
                    p=info["cert"]))
            return
    except (PermissionError, ValueError, OSError) as e:
        raise SystemExit(str(e))
    if not w.get("tls_cert"):
        print(_("目前沒有使用 HTTPS。用 python -m dtm tls --self-signed 產生自簽憑證並啟用。"))
        return
    path = resolve(cfg, w["tls_cert"])
    if not os.path.exists(path):
        raise SystemExit(_("找不到憑證檔：{p}", p=path))
    d = tlscert.describe(path)
    exp = datetime.fromtimestamp(d["not_after"]).strftime("%Y-%m-%d") if d.get("not_after") else "?"
    print(_("憑證：{p}", p=path))
    print(_("主旨：{s}　到期：{d}", s=d.get("subject") or "?", d=exp))
    print(_("指紋 SHA-256：{f}", f=d["sha256"]))


def cmd_version(cfg, a):
    print(f"DTM {__version__}")


def _log_dir(cfg) -> str:
    from . import runtime as R
    return R.log_dir(cfg)


def cmd_report(cfg, a):
    svc = Service(cfg, notify=False)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(build_report(svc, a.days))
    print(_("報告：{path}", path=a.out))


def cmd_export(cfg, a):
    svc = Service(cfg, notify=False)
    export_features(svc, a.out, a.days)
    print(_("已匯出：{path}", path=a.out))


def _early_lang(argv) -> None:
    """先讀 -c 與 --lang，讓說明文字（-h）也用正確的語言。"""
    import os
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("-c", "--config", default="config.toml")
    pre.add_argument("--lang")
    k, _rest = pre.parse_known_args(argv)
    lang = norm(k.lang) or norm(os.environ.get("DTM_LANG"))
    if not lang and k.config and os.path.exists(k.config):
        try:
            lang = norm(load(k.config).get("ui", {}).get("lang"))
        except Exception:
            lang = None
    if lang:
        set_default(lang)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):      # 輸出導到檔案時（Windows 預設 cp950），遇到無法編碼的字不要整個中斷
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = sys.argv[1:] if argv is None else argv
    _early_lang(argv)
    p = argparse.ArgumentParser(prog="python -m dtm", description=_("Fanuc 減速機干擾扭矩監控"))
    p.add_argument("-c", "--config", default="config.toml", help=_("設定檔（TOML）"))
    p.add_argument("--lang", choices=list(LANGS), help=_("顯示語言，覆蓋設定檔 [ui] lang"))
    p.add_argument("--robot", help=_("邊緣主機設定檔裡的機器人代碼（多台時指定要操作哪一台）"))
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help=_("啟動擷取與儀表板（依 [system] role：單機、邊緣主機或中央伺服器）"))
    s.add_argument("--service", action="store_true", help=_("以服務執行（不碰命令列視窗）"))
    s.add_argument("--force", action="store_true", help=_("自我檢查有錯誤也強制啟動"))
    sub.add_parser("watchdog", help=_("程式看門狗：DTM 卡住時重啟並通知（建議用 service install 一起裝成服務）"))
    s = sub.add_parser("maintenance", help=_("維護模式：安裝更新或維修前開啟，看門狗暫停"))
    s.add_argument("action", choices=["on", "off", "status"])
    s.add_argument("--minutes", type=float, default=60)
    s.add_argument("--reason", default="")
    s.add_argument("--who", default="")
    s = sub.add_parser("service", help=_("安裝／移除系統服務（Windows 用 WinSW，Linux 用 systemd）"))
    s.add_argument("action", choices=["install", "uninstall", "start", "stop", "restart", "status"])
    s.add_argument("--no-watchdog", action="store_true", help=_("不安裝看門狗服務"))
    s.add_argument("--dry-run", action="store_true", help=_("只產生設定檔，不安裝"))
    s.add_argument("--no-error-dialog", action="store_true", help=_("同時關閉 Windows「程式已停止運作」對話框"))
    sub.add_parser("selfcheck", help=_("自我檢查：資料庫完整性、磁碟空間、設定檔"))
    s = sub.add_parser("user", help=_("個人帳號：add 新增、list 列出、passwd 改密碼、role 改角色、disable／enable"))
    s.add_argument("action", choices=["add", "list", "passwd", "role", "disable", "enable"])
    s.add_argument("username", nargs="?")
    s.add_argument("--role", choices=["viewer", "operator", "engineer", "admin"])
    s.add_argument("--display", default="", help=_("顯示名稱（操作紀錄上看到的名字）"))
    s.add_argument("--plants", default="", help=_("負責的廠區代碼，逗號分隔（目前只記錄，2.0 尚未依廠區限制可看的畫面）"))
    s.add_argument("--password", default="", help=_("不建議（會留在命令列紀錄）；不填會詢問"))
    sub.add_parser("version", help=_("顯示版本"))
    s = sub.add_parser("validation", help=_("現場驗證報告：失效前有沒有先警報、提早幾天、漏報、誤報候選（HTML，可列印）"))
    s.add_argument("--days", type=float, default=730, help=_("統計最近幾天（預設 730）"))
    s.add_argument("--out", default=None)
    s.add_argument("--json", action="store_true", help=_("輸出 JSON（給其他系統分析）"))
    s = sub.add_parser("tls", help=_("HTTPS：--self-signed 產生自簽憑證並啟用、--off 關閉、不加參數 = 查看目前的憑證"))
    s.add_argument("--self-signed", action="store_true", help=_("產生自簽憑證並寫進設定檔"))
    s.add_argument("--name", action="append", help=_("另外加進憑證的主機名稱或 IP（可重複）"))
    s.add_argument("--off", action="store_true", help=_("關閉 HTTPS（改回 http://）"))
    s = sub.add_parser("demo", help=_("中央伺服器演示：產生 6 廠 160 台的演示資料並啟動中央畫面"))
    s.add_argument("--port", type=int, default=0, help=_("網頁連接埠（預設用設定檔 [web] port）"))
    s.add_argument("--rebuild", action="store_true", help=_("重新產生演示資料"))
    s.add_argument("--build-only", action="store_true", help=_("只產生資料庫，不啟動"))
    s.add_argument("--no-live", action="store_true", help=_("不啟動模擬機器人（即時監測與程式診斷）"))
    s = sub.add_parser("simulate-history", help=_("用模擬器產生歷史診斷資料（測試用）"))
    s.add_argument("--days", type=int, default=180)
    s = sub.add_parser("runs", help=_("列出最近的診斷特徵與 ID"))
    s.add_argument("--axis", type=int)
    s.add_argument("--last", type=int, default=10)
    s = sub.add_parser("calibrate", help=_("加重法換算 Nm"))
    s.add_argument("--axis", type=int, required=True)
    s.add_argument("--mass", type=float, required=True, help=_("加上的質量 kg"))
    s.add_argument("--dist", type=float, required=True, help=_("質量重心到該軸的水平距離 m"))
    s.add_argument("--without", type=int, required=True, help=_("不加重的診斷 ID"))
    s.add_argument("--with", dest="with_", type=int, required=True, help=_("加重後的診斷 ID"))
    s.add_argument("--zero", type=float, default=0.0, help=_("零點偏移（原始值），預設 0"))
    s.add_argument("--save", action="store_true")
    s = sub.add_parser("event", help=_("登錄保養事件（換脂、更換減速機／齒輪箱／馬達會寫入元件壽命）"))
    s.add_argument("--axis", type=int)
    s.add_argument("--kind", required=True, choices=list(EVENT_LABELS))
    s.add_argument("--note", default="")
    s.add_argument("--failed", action="store_true", help=_("失效更換（不加為預防性更換）"))
    s.add_argument("--teardown", default="", help=_("拆檢結果"))
    s.add_argument("--hours", type=float, help=_("當時的控制器運轉時數"))
    s.add_argument("--time", help=_("YYYY-MM-DD HH:MM，預設現在"))
    s.add_argument("--who", default="", help=_("登錄人（預設為電腦登入帳號）"))
    s = sub.add_parser("events", help=_("列出保養事件與編號（撤銷時要用編號）"))
    s.add_argument("--axis", type=int)
    s.add_argument("--last", type=int, default=20)
    s = sub.add_parser("undo-event", help=_("撤銷登錄錯誤的事件，元件壽命與基準一併還原"))
    s.add_argument("--id", type=int, required=True, help=_("事件編號（用 events 指令查）"))
    s.add_argument("--note", default="", help=_("撤銷原因"))
    s.add_argument("--who", default="", help=_("操作人（預設為電腦登入帳號）"))
    s = sub.add_parser("check", help=_("檢查設定檔（拼錯、格式、數值範圍）；加 --connect 測試連線"))
    s.add_argument("--connect", action="store_true", help=_("實際連到機器人讀幾秒資料"))
    s.add_argument("--seconds", type=float, default=10.0, help=_("連線測試秒數"))
    s = sub.add_parser("temp-coef", help=_("從自己的資料估計溫度係數（需要溫度訊號、累積一個季節）"))
    s.add_argument("--axis", type=int)
    s.add_argument("--all", action="store_true", help=_("用全部歷史資料（預設只用最近一次重設之後）"))
    s = sub.add_parser("scenarios", help=_("模擬季節溫差、冷機、換夾具等情境，量測誤報與漏報"))
    s.add_argument("--days", type=int, default=365)
    s.add_argument("--only", default="", help=_("只跑指定情境，逗號分隔，例如 seasonal_nosensor,grease_nosensor"))
    s = sub.add_parser("backup", help=_("立刻備份資料庫（DTM 執行中也可以）；--list 列出現有備份"))
    s.add_argument("--list", action="store_true", help=_("只列出現有備份"))
    s.add_argument("--mirror", action="store_true", help=_("配合 --list：列出網路資料夾裡的備份"))
    s.add_argument("--raw", action="store_true", help=_("連原始樣本一起備份（檔案會大很多）"))
    s = sub.add_parser("restore", help=_("還原精靈：從備份補回資料，或讓某一軸回到備份當時（不加參數會一步一步問）"))
    s.add_argument("--list", action="store_true", help=_("列出可用的備份來源"))
    s.add_argument("--source", choices=["local", "mirror", "snapshot", "central", "file"],
                   help=_("local 本機備份、mirror 網路資料夾、snapshot 還原前自動備份、central 從中央重建、file 指定檔案"))
    s.add_argument("--name", default="", help=_("備份檔名（預設最新一份）"))
    s.add_argument("--file", help=_("備份 zip、.sqlite3，或中央匯出的 .json.gz"))
    s.add_argument("--axis", type=int, help=_("只還原這一軸（不指定 = 整台）"))
    s.add_argument("--types", default="trend,sensor,maint,alarm",
                   help=_("資料類型，逗號分隔：trend 趨勢、sensor 振動鐵粉、maint 維護紀錄、alarm 警報、system 系統紀錄、raw 原始取樣"))
    s.add_argument("--since", help=_("時間區間開始 YYYY-MM-DD"))
    s.add_argument("--until", help=_("時間區間結束 YYYY-MM-DD（含當天）"))
    s.add_argument("--mode", default="merge", choices=["merge", "overwrite", "full"],
                   help=_("merge 補回遺失（預設）；overwrite 回到時間點；full 整個資料庫檔換掉（資料庫壞掉時用，要先關 DTM）"))
    s.add_argument("--preview", action="store_true", help=_("只預覽差異，不改任何資料"))
    s.add_argument("--confirm", default="", help=_("回到時間點的確認文字（搭配 --yes 用）"))
    s.add_argument("--who", default="", help=_("操作人（預設為電腦登入帳號）"))
    s.add_argument("--yes", action="store_true", help=_("不再詢問"))
    s = sub.add_parser("export-analysis", help=_("分析資料匯出 zip（特徵、每日中位數、事件、警報、鐵粉、振動、元件、"
                                                 "時數、校正、系統事件、去敏設定檔、保留的原始片段）"))
    s.add_argument("--since", help=_("開始日期 YYYY-MM-DD"))
    s.add_argument("--until", help=_("結束日期 YYYY-MM-DD（含當天）"))
    s.add_argument("--anonymize", action="store_true", help=_("廠名、機器人、產線、操作人換成代號"))
    s.add_argument("--rkey", default="", help=_("（中央）機器人代號，例如 P3/R14"))
    s.add_argument("--out", default="", help=_("輸出檔名；預設放在 data/exports/"))
    s = sub.add_parser("replay", help=_("門檻回放試算：套另一組門檻與比重，算過去會報幾次、什麼時候報"))
    s.add_argument("--set", action="append", help=_("要試算的設定，可重複，例如 --set health.caution_ratio=1.3"))
    s.add_argument("--alt", default="", help=_("或用一個 TOML 檔（只寫要改的區段）"))
    s.add_argument("--days", type=float, default=365)
    s.add_argument("--axis", type=int)
    s.add_argument("--decision", action="store_true", help=_("連維護決策也回放（每天重算一次，比較慢）"))
    s.add_argument("--step-days", type=float, default=1.0, help=_("維護決策回放的間隔天數"))
    s = sub.add_parser("commissioning-report", help=_("導入檢查報告（HTML）：取樣、資料中斷、診斷切段、雜訊、動作代碼涵蓋率"))
    s.add_argument("--days", type=float, default=7, help=_("看最近幾天（原始取樣只保留 7 天）"))
    s.add_argument("--out", default="", help=_("輸出檔名；預設放在 data/reports/"))
    s = sub.add_parser("downtime", help=_("單台計畫停機（歲修、停線）：期間不發資料中斷與連線警報；不加參數 = 查看"))
    s.add_argument("--until", default="", help=_("結束時間，例如 \"2026-10-20 08:00\" 或 8h"))
    s.add_argument("--note", default="", help=_("原因"))
    s.add_argument("--cancel", action="store_true", help=_("取消計畫停機"))
    s.add_argument("--who", default="", help=_("操作人（預設為電腦登入帳號）"))
    s = sub.add_parser("teardown", help=_("登錄拆檢結果（更換減速機、齒輪箱、馬達的事件）"))
    s.add_argument("--id", type=int, required=True, help=_("事件編號（用 events 指令查）"))
    s.add_argument("--location", default="", help=_("損傷位置，逗號分隔：input_gear、crank_bearing、cycloid、pin、"
                                                    "main_bearing、seal、gearbox_gear、gearbox_bearing、motor_bearing、brake、other"))
    s.add_argument("--mode", default="", help=_("損傷型態，逗號分隔：pitting 點蝕、spalling 剝離、wear 磨耗、broken 斷齒、other"))
    s.add_argument("--grease", default="", help=_("油脂狀態：normal、dark、metal、emulsified、hardened、leak、other"))
    s.add_argument("--photo", action="append", help=_("照片檔（可重複）"))
    s.add_argument("--no-fault", action="store_true", help=_("檢查無異常（這筆更換改當預防更換）"))
    s.add_argument("--note", default="")
    s.add_argument("--who", default="", help=_("登錄人（預設為電腦登入帳號）"))
    s = sub.add_parser("central-export", help=_("（中央電腦）匯出某台機器人的資料，給機器人端 restore --file 重建"))
    s.add_argument("--robot", default="", help=_("機器人代號，例如 TW01/R01；不填會列出清單"))
    s.add_argument("--out", default="", help=_("輸出檔名（.json.gz）"))
    s = sub.add_parser("iron", help=_("登錄鐵粉濃度"))
    s.add_argument("--axis", type=int, required=True)
    s.add_argument("--wt", type=float, required=True, help=_("wt%%，例如 0.045"))
    s.add_argument("--sample", default="drain_mid", choices=["drain_mid", "interim", "change", "after"],
                   help=_("drain_mid 換脂排出（中段，預設；前段先丟棄）、interim 中途取樣、change 換脂排出（未分段）、after 換脂後殘留"))
    s.add_argument("--note", default="")
    s.add_argument("--time")
    s = sub.add_parser("vib-import", help=_("匯入振動特徵 CSV"))
    s.add_argument("file")
    s.add_argument("--source", default="csv")
    s = sub.add_parser("install", help=_("登錄目前使用中元件的安裝日（導入時一次性設定）"))
    s.add_argument("--axis", type=int, required=True)
    s.add_argument("--comp", required=True, choices=list(COMP_LABELS))
    s.add_argument("--date", required=True, help="YYYY-MM-DD")
    s.add_argument("--note", default="")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("history", help=_("匯入過去或其他機台的更換壽命紀錄 CSV（Weibull 擬合用）"))
    s.add_argument("file")
    s = sub.add_parser("hours", help=_("登錄控制器運轉時數表讀數"))
    s.add_argument("--hours", type=float, required=True)
    s.add_argument("--time")
    s = sub.add_parser("notify-test", help=_("發送測試通知（簡訊、Email、Webhook）"))
    s.add_argument("--channel", default="all", choices=["all", "sms", "email", "webhook"])
    s.add_argument("--text", default="")
    s = sub.add_parser("decide", help=_("列印各軸維護決策"))
    s.add_argument("--axis", type=int)
    s = sub.add_parser("workpack", help=_("列出備品與作業包，或輸出可列印的工單"))
    s.add_argument("--axis", type=int)
    s.add_argument("--comp", choices=["grease", "reducer", "gearbox", "motor"])
    s.add_argument("--html", help=_("輸出工單 HTML 檔名"))
    s.add_argument("--list", action="store_true", help=_("列出目錄中的所有作業包"))
    from .pro import cli as pro_cli
    pro_cli.add(sub)
    s = sub.add_parser("report", help=_("產生 HTML 分析報告"))
    s.add_argument("--out", default="report.html")
    s.add_argument("--days", type=float, default=180)
    s = sub.add_parser("export", help=_("匯出診斷特徵 CSV"))
    s.add_argument("--out", default="features.csv")
    s.add_argument("--days", type=float, default=3650)
    a = p.parse_args(argv)
    if a.cmd == "check":                 # 設定檔可能有錯，不能先 load
        if a.lang or os.environ.get("DTM_LANG"):
            set_default(norm(a.lang) or norm(os.environ.get("DTM_LANG")))
        raise SystemExit(cmd_check(a))
    if (a.cmd in ("demo", "version") or (a.cmd == "pro" and a.pro_cmd in ("demo", "sim", "parse", "instrument"))) \
            and not os.path.exists(a.config):
        cfg = load(None)                 # 演示、看版本不需要設定檔
    else:
        cfg = load(a.config)
    if a.robot:
        from .fleet import apply_robot
        cfg = apply_robot(cfg, a.robot)
    override = norm(a.lang) or norm(os.environ.get("DTM_LANG"))   # 優先順序：--lang > DTM_LANG > 設定檔 [ui] lang
    if override:
        set_default(override)
        cfg.setdefault("ui", {})["lang"] = override
        cfg["_lang_cli"] = True                   # 網頁「設定」改的預設語言不蓋過指令列
    if os.environ.get("DTM_DEMO") == "1":         # 中央演示啟動的模擬機器人（免登入試用）
        cfg["_demo"] = True
        if a.cmd == "run" and os.environ.get("DTM_PARENT_URL"):
            _watch_parent(os.environ["DTM_PARENT_URL"])
    if a.cmd == "run":
        logf = os.path.join(_log_dir(cfg), "dtm.log" if not a.robot else f"dtm_{a.robot}.log")
        _setup_log(file=logf, console=not a.service)
    elif a.cmd == "demo":
        _setup_log(file=None)
    elif a.cmd != "watchdog":
        _setup_log(file=None)
    {"run": cmd_run, "simulate-history": cmd_simulate_history, "runs": cmd_runs, "calibrate": cmd_calibrate,
     "event": cmd_event, "events": cmd_events, "undo-event": cmd_undo_event, "backup": cmd_backup, "restore": cmd_restore,
     "central-export": cmd_central_export, "export-analysis": cmd_export_analysis, "teardown": cmd_teardown,
     "downtime": cmd_downtime, "commissioning-report": cmd_commissioning,
     "replay": cmd_replay,
     "temp-coef": cmd_temp_coef, "scenarios": cmd_scenarios, "report": cmd_report, "export": cmd_export, "iron": cmd_iron,
     "vib-import": cmd_vib_import, "install": cmd_install, "history": cmd_history, "hours": cmd_hours,
     "decide": cmd_decide, "notify-test": cmd_notify_test, "workpack": cmd_workpack, "watchdog": cmd_watchdog,
     "maintenance": cmd_maintenance, "service": cmd_service, "selfcheck": cmd_selfcheck,
     "version": cmd_version, "tls": cmd_tls, "validation": cmd_validation, "user": cmd_user, "demo": cmd_demo, "pro": pro_cli.run}[a.cmd](cfg, a)


if __name__ == "__main__":
    main()

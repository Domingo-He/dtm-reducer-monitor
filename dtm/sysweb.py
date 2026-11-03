"""設定（右上角齒輪）的 API：/api/sys/…。單機儀表板（web.py）與中央／廠區（fleetweb.py）共用。

- 我的帳號：顯示名稱、變更密碼、登入中的裝置。
- 帳號管理（管理員）：新增、角色、停用／啟用、重設密碼（下次登入要改）、解除鎖定；至少保留一個管理員。
- 系統設定（管理員）：sysconf.py 的項目與通知收件人；修改紀錄與一鍵還原。
- 隱私與安全：操作紀錄（可下載 CSV）、資料存在哪裡、對外連線一覽、登入中的裝置。
- 系統資訊：版本、執行模式、主機、資料庫、磁碟、備份、自我檢查、設定檔檢查；立即備份；診斷包（遮蔽密碼與聯絡資料）。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import platform
import re
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable

from . import sysconf as SC
from .auth import ROLE_NAMES, ROLES, Auth, check_password, is_loopback, role_ok
from .i18n import LANGS, TDict, _, default_lang

log = logging.getLogger("dtm.sysweb")

ACTIONS = TDict({
    "login": "登入", "logout": "登出", "/api/setup": "建立第一個管理員", "/api/ack": "確認警報", "/api/c/ack": "確認警報",
    "/api/event": "登錄保養事件", "/api/c/event": "登錄保養事件", "/api/event/undo": "撤銷保養事件",
    "/api/iron": "登錄鐵粉", "/api/c/iron": "登錄鐵粉", "/api/vibration": "登錄振動", "/api/hours": "登錄運轉時數",
    "/api/downtime": "計畫停機", "/api/c/downtime": "計畫停機", "/api/teardown": "拆檢結果", "/api/c/teardown": "拆檢結果",
    "/api/teardown/photo": "拆檢照片", "/api/replay": "門檻回放", "/api/c/replay": "門檻回放",
    "/api/export/analysis": "分析匯出", "/api/c/export/analysis": "分析匯出", "/api/restore/preview": "還原預覽",
    "/api/restore/run": "執行還原", "/api/notify/test": "測試通知", "/api/c/notify/test": "測試通知",
    "/api/c/job": "保養工單", "/api/c/job/done": "工單完成", "/api/c/job/prep": "工單備料", "/api/c/spare": "備品",
    "/api/c/maintenance": "維護模式", "/api/maintenance": "維護模式", "settings:devices": "設備設定",
    "/api/sys/settings": "系統設定", "/api/sys/settings/reset": "系統設定恢復預設", "/api/sys/settings/revert": "系統設定還原",
    "/api/sys/users": "帳號管理", "/api/sys/password": "變更密碼", "/api/sys/account": "修改顯示名稱",
    "/api/sys/sessions/revoke": "登出裝置", "/api/sys/diag": "下載診斷包", "/api/sys/backup": "立即備份",
    "/api/pro/session": "新診斷期", "/api/pro/session/delete": "刪除診斷期", "/api/pro/session/end": "結束診斷期",
    "/api/pro/import": "匯入診斷資料", "rebuild": "從中央重建", "/api/sync": "邊緣上傳",
    "/api/sys/notify/people": "收件人名單", "/api/sys/tls": "HTTPS 設定", "/api/sys/wizard": "第一次設定精靈",
})


@dataclass
class Ctx:
    """一個請求的環境：哪台主機（cfg、帳號、資料庫）、誰（session）、從哪台電腦（addr）。"""
    cfg: dict
    auth: Any
    role: str                                   # standalone / edge / central
    addr: str
    token: str = ""
    session: dict | None = None
    audit_add: Callable | None = None           # (who, addr, action, ok, detail)
    audit_conn: Callable | None = None          # 回傳 sqlite 連線的 context manager（audit 資料表）
    db_path: str = ""
    extra_info: Callable | None = None
    backup_fn: Callable | None = None
    backup_cfg: dict | None = None              # 備份用的設定（中央、邊緣主機的資料庫不是 [storage] db）
    notifier: Any = None
    started: float = field(default_factory=time.time)

    @property
    def mode(self) -> str:
        return getattr(self.auth, "mode", "readonly")

    def is_admin(self) -> bool:
        if self.mode == "demo":
            return True
        return bool(self.session) and role_ok(self.session.get("role"), "admin")

    def logged(self) -> bool:
        return self.mode == "demo" or bool(self.session)


def _need_admin(ctx: Ctx):
    if not ctx.is_admin():
        raise PermissionError(_("這個畫面只有管理員可以看"))


def _need_login(ctx: Ctx):
    if not ctx.logged():
        raise PermissionError(_("請先登入"))


def _users(ctx: Ctx):
    a = ctx.auth
    if not isinstance(a, Auth) or not a.users_path:
        raise ValueError(_("這台主機沒有帳號資料庫（[web] users_db）"))
    return a._store()


def _fmt_user(u: dict, now: float, pol: dict) -> dict:
    out = {k: u.get(k) for k in ("username", "display", "role", "active", "created", "last_login", "plants",
                                 "must_change", "pw_changed", "locked_until", "created_by")}
    out["role_name"] = ROLE_NAMES.get(u.get("role"), u.get("role"))
    out["locked"] = bool(u.get("locked_until") and float(u["locked_until"]) > now)
    days = pol.get("pw_days") or 0
    out["pw_expired"] = bool(days and u.get("pw_changed") and now - float(u["pw_changed"]) > days * 86400)
    return out


def policy_text(pol: dict) -> str:
    parts = [_("至少 {n} 個字", n=pol["pw_min"])]
    if pol["pw_mix"]:
        parts.append(_("要有英文字母與數字"))
    return _("、").join(parts)


# ------------------------------------------------------------ GET
def get(ctx: Ctx, path: str, q: dict):
    """回傳 (內容, 狀態碼)；下載回傳 (bytes, content-type, headers)；不是 /api/sys/ 的路徑回傳 None。"""
    if not path.startswith("/api/sys/"):
        return None
    try:
        return _get(ctx, path, q)
    except PermissionError as e:
        return {"error": str(e), "auth": "forbidden"}, 403
    except ValueError as e:
        return {"error": str(e)}, 400


def _get(ctx: Ctx, path: str, q: dict):
    now = time.time()
    pol = SC.policy(ctx.cfg)
    if path == "/api/sys/me":
        s = ctx.session
        return {"mode": ctx.mode, "role": ctx.role, "admin": ctx.is_admin(),
                "session": {k: s.get(k) for k in ("who", "role", "username", "must_change", "created", "expires",
                                                  "idle_minutes")} | {"role_name": ROLE_NAMES.get(s.get("role"), "")}
                if s else None,
                "policy": {"text": policy_text(pol), "min": pol["pw_min"], "mix": pol["pw_mix"]},
                "accounts": ctx.mode == "accounts"}, 200
    if path == "/api/sys/settings":
        _need_login(ctx)
        v = SC.get(ctx.cfg).view()
        v["can_edit"] = ctx.is_admin()
        v["mode"] = ctx.mode
        if not ctx.is_admin():                  # 非管理員：看得到原則，看不到收件人
            v["notify"] = {"rules": [], "default": {}, "channels": v["notify"]["channels"], "hidden": True}
        return v, 200
    if path == "/api/sys/settings/history":
        _need_admin(ctx)
        return {"rows": SC.get(ctx.cfg).history(int(q.get("limit", 100)))}, 200
    if path == "/api/sys/users":
        _need_admin(ctx)
        if ctx.mode != "accounts":
            return {"rows": [], "mode": ctx.mode, "policy": policy_text(pol)}, 200
        us = _users(ctx)
        sess = us.sessions(now)
        rows = [_fmt_user(u, now, pol) | {"sessions": sum(1 for s in sess if s["username"] == u["username"])}
                for u in us.list()]
        return {"rows": rows, "mode": ctx.mode, "policy": policy_text(pol),
                "roles": [{"code": k, "name": ROLE_NAMES.get(k)} for k in ROLES]}, 200
    if path == "/api/sys/sessions":
        _need_login(ctx)
        a = ctx.auth
        if not isinstance(a, Auth) or a.users is None:
            return {"rows": [], "note": _("共用密碼或演示模式沒有個人登入紀錄")}, 200
        me = (ctx.session or {}).get("username")
        rows = a.users.sessions(now, None if ctx.is_admin() and q.get("all") == "1" else (me or ""))
        cur = (ctx.session or {}).get("sid")
        out = []
        for r in rows:
            out.append({"id": r["id"], "username": r["username"], "who": r["who"],
                        "role_name": ROLE_NAMES.get(r["role"], r["role"]), "created": r["created"], "active": r["active"],
                        "expires": r["expires"], "addr": r["addr"], "agent": _agent(r["agent"]), "current": r["id"] == cur})
        return {"rows": out, "all": ctx.is_admin() and q.get("all") == "1"}, 200
    if path in ("/api/sys/audit", "/api/sys/audit.csv"):
        _need_admin(ctx)
        rows = audit_rows(ctx, q)
        if path.endswith(".csv"):
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow([_("時間"), _("人員"), _("電腦"), _("動作"), _("結果"), _("內容")])
            for r in rows:
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])), r["who"], r["addr"],
                            r["label"], _("成功") if r["ok"] else _("失敗"), r["detail"]])
            name = time.strftime("dtm_audit_%Y%m%d_%H%M.csv")
            return ("﻿" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8", \
                {"Content-Disposition": f"attachment; filename={name}"}
        return {"rows": rows}, 200
    if path == "/api/sys/notify/people":
        _need_login(ctx)
        return people_view(ctx), 200
    if path == "/api/sys/vendor":
        from . import vendors
        SC.get(ctx.cfg).refresh()
        return vendors.view(ctx.cfg), 200
    if path == "/api/sys/tls":
        _need_admin(ctx)
        return tls_view(ctx), 200
    if path == "/api/sys/wizard":
        _need_admin(ctx)
        from . import wizard
        return wizard.view(ctx.cfg, ctx.addr), 200
    if path == "/api/sys/tls/cert.crt":            # 憑證本來就公開（瀏覽器連線時會收到），給各台電腦匯入信任用
        c = _tls_cert_path(ctx.cfg)
        if not c or not os.path.exists(c):
            raise ValueError(_("這台主機沒有 HTTPS 憑證"))
        with open(c, "rb") as f:
            return f.read(), "application/x-x509-ca-cert", {"Content-Disposition": "attachment; filename=dtm-cert.crt"}
    if path == "/api/sys/info":
        _need_login(ctx)
        return system_info(ctx), 200
    if path == "/api/sys/check":
        _need_admin(ctx)
        return config_check(ctx), 200
    if path == "/api/sys/privacy":
        _need_login(ctx)
        return privacy_view(ctx), 200
    if path == "/api/sys/download":
        _need_admin(ctx)
        from . import export as EX
        p = EX.take_download(q.get("t", ""))
        if not p:
            return {"error": _("下載連結已失效（10 分鐘內有效、只能下載一次），請重新產生")}, 404
        with open(p, "rb") as f:
            body = f.read()
        return body, "application/zip", {"Content-Disposition": f"attachment; filename={os.path.basename(p)}"}
    return {"error": "not found"}, 404


def _agent(ua: str) -> str:
    """瀏覽器字串 → 簡短說明（Chrome · Windows）。"""
    ua = ua or ""
    b = next((n for k, n in (("Edg/", "Edge"), ("Chrome/", "Chrome"), ("Firefox/", "Firefox"), ("Safari/", "Safari"))
              if k in ua), "")
    o = next((n for k, n in (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                             ("Mac OS", "macOS"), ("Linux", "Linux")) if k in ua), "")
    return " · ".join(x for x in (b, o) if x) or (ua[:40] if ua else "—")


def audit_rows(ctx: Ctx, q: dict) -> list[dict]:
    if not ctx.audit_conn:
        return []
    lim = max(1, min(5000, int(q.get("limit", 200))))
    where, args = [], []
    if q.get("who"):
        where.append("who LIKE ?")
        args.append(f"%{q['who']}%")
    if q.get("fail") == "1":
        where.append("ok = 0")
    if q.get("since"):
        where.append("ts >= ?")
        args.append(float(q["since"]))
    if q.get("q"):
        where.append("(action LIKE ? OR detail LIKE ? OR addr LIKE ?)")
        args += [f"%{q['q']}%"] * 3
    sql = "SELECT * FROM audit" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    with ctx.audit_conn() as c:
        rows = [dict(r) for r in c.execute(sql, (*args, lim))]
    for r in rows:
        act = r.get("action") or ""
        base = act.split(":", 1)[0] + ":" if act.startswith("cmd:") else act
        r["label"] = ACTIONS.get(act) or (_("中央指令：{k}", k=act[4:]) if base == "cmd:" else act)
    return rows


def _size(p: str) -> int | None:
    try:
        return os.path.getsize(p)
    except OSError:
        return None


def system_info(ctx: Ctx) -> dict:
    from . import __version__
    from . import runtime as R
    cfg = ctx.cfg
    info = {"version": __version__, "role": ctx.role, "mode": ctx.mode,
            "role_name": {"standalone": _("單機（一台機器人）"), "edge": _("邊緣主機（一個廠區多台）"),
                          "central": _("中央伺服器（多個廠區）")}.get(ctx.role, ctx.role),
            "python": platform.python_version(), "os": f"{platform.system()} {platform.release()}",
            "host": R.host_stats().get("host") or platform.node(), "pid": os.getpid(),
            "started": ctx.started, "uptime_s": time.time() - ctx.started, "config": cfg.get("_path") or "",
            "demo": bool(cfg.get("_demo")), "tls": bool((cfg.get("web") or {}).get("tls_cert")),
            "web_host": (cfg.get("web") or {}).get("host"), "web_port": (cfg.get("web") or {}).get("port")}
    dbs = []
    if ctx.db_path:
        dbs.append({"name": _("監測資料庫"), "path": ctx.db_path, "bytes": _size(ctx.db_path)})
    up = getattr(ctx.auth, "users_path", "")
    if up:
        dbs.append({"name": _("帳號與系統設定"), "path": up, "bytes": _size(up)})
    info["dbs"] = dbs
    info["disk"] = R.disk_info(ctx.db_path or R.base_dir(cfg))
    try:
        from . import backup as B
        bc = ctx.backup_cfg or cfg
        bdir = B.backup_dir(bc)
        bl = B.list_backups(bc) if bdir else []
        info["backup"] = {"dir": bdir or "", "mirror": B.mirror_dir(bc) or "", "last_ts": bl[0]["ts"] if bl else None,
                          "count": len(bl), "can_run": bool(ctx.backup_fn and bdir),
                          "hour": (bc.get("storage") or {}).get("backup_hour", 3)}
    except Exception as e:
        info["backup"] = {"error": str(e), "can_run": bool(ctx.backup_fn)}
    try:
        db = ctx.db_path or cfg["storage"]["db"]
        info["selfcheck"] = [c for c in R.self_check(cfg, db=db) if c.get("name")]
    except Exception as e:
        info["selfcheck"] = [{"name": _("自我檢查"), "level": "warn", "detail": str(e)}]
    if ctx.extra_info:
        try:
            info.update(ctx.extra_info() or {})
        except Exception as e:
            log.warning("extra_info: %s", e)
    return info


def config_check(ctx: Ctx) -> dict:
    path = ctx.cfg.get("_path") or ""
    if not path or not os.path.exists(path):
        return {"path": "", "items": [], "note": _("這次啟動沒有使用設定檔（演示或測試），沒有可檢查的設定檔")}
    from .check import ERROR, HINT, WARN, run_check
    rep = run_check(path, connect=False, out=lambda *a, **k: None)
    order = {ERROR: 0, WARN: 1, HINT: 2}
    items = [{"level": lv, "where": w, "msg": m} for lv, w, m in sorted(rep.items, key=lambda i: order.get(i[0], 3))]
    return {"path": path, "items": items, "errors": rep.count(ERROR), "warnings": rep.count(WARN)}


def _host_of(url: str) -> str:
    m = re.match(r"^[a-z]+://(?:[^@/]*@)?([^/:?#]+)", str(url or ""), re.I)
    return m.group(1) if m else ""


def privacy_view(ctx: Ctx) -> dict:
    """這台主機存了哪些資料、會連到哪裡（只列出設定檔裡實際設定的）。"""
    cfg = ctx.cfg
    out = []
    src = (cfg.get("robot") or {}).get("source") or ""
    sc = (cfg.get("source") or {}).get(src) or {}
    if src in ("fanuc_http", "modbus") and sc.get("host"):
        out.append({"what": _("機器人控制器"), "to": sc["host"], "why": _("讀取干擾扭矩（只讀，不寫入控制器）")})
    for r in cfg.get("robots") or []:
        if r.get("host"):
            out.append({"what": _("機器人控制器"), "to": f"{r.get('name') or r.get('id')} · {r['host']}",
                        "why": _("讀取干擾扭矩（只讀，不寫入控制器）")})
    sen = cfg.get("sensors") or {}
    if sen.get("enabled") and sen.get("host"):
        out.append({"what": _("振動與溫度感測器"), "to": sen["host"], "why": _("讀取振動與溫度特徵值")})
    n = cfg.get("notify") or {}
    if n.get("email_enabled") and n.get("smtp_host"):
        out.append({"what": _("郵件伺服器"), "to": n["smtp_host"], "why": _("寄送警報 Email")})
    if n.get("webhook_url"):
        out.append({"what": "Webhook", "to": _host_of(n["webhook_url"]), "why": _("傳送警報到聊天軟體")})
    sms = n.get("sms") or {}
    if sms.get("enabled"):
        if (sms.get("http") or {}).get("url"):
            out.append({"what": _("簡訊閘道"), "to": _host_of(sms["http"]["url"]), "why": _("發送警報簡訊")})
        if sms.get("provider") == "modem":
            out.append({"what": _("4G 簡訊數據機"), "to": (sms.get("modem") or {}).get("port", ""),
                        "why": _("發送警報簡訊（本機序列埠）")})
    cen = cfg.get("central") or {}
    if cen.get("url") and ctx.role != "central":
        out.append({"what": _("中央伺服器"), "to": _host_of(cen["url"]), "why": _("上傳健康判定、警報與保養紀錄")})
    from . import fx
    if fx.outbound(cfg) and cfg.get("system", {}).get("role") in ("central", "edge"):
        out.append({"what": _("匯率"), "to": _("、").join(fx.outbound(cfg)),
                    "why": _("下載匯率表，把備品價格換算成觀看者的當地貨幣（不送出任何資料；設定 → 一般可關閉）")})
    stored = [
        {"what": _("干擾扭矩、溫度、振動與診斷特徵"), "where": _("監測資料庫"), "keep": _("原始樣本保存 {d} 天，其餘永久",
                                                                         d=(cfg.get("storage") or {}).get("raw_days", "—"))},
        {"what": _("保養事件、鐵粉、拆檢紀錄與照片"), "where": _("監測資料庫、data 資料夾"), "keep": _("永久")},
        {"what": _("帳號（密碼只存雜湊值）、登入中的裝置、系統設定"), "where": _("帳號與系統設定資料庫"), "keep": _("登入到期即刪除")},
        {"what": _("操作紀錄：誰、從哪台電腦、做了什麼"), "where": _("監測資料庫"), "keep": _("永久（可下載 CSV）")},
    ]
    return {"outbound": out, "stored": stored,
            "statement": [_("DTM 不會把任何資料傳給作者或第三方，也沒有使用統計、自動更新或廣告。"),
                          _("對外連線只有上面列出、你自己在設定檔裡設定的對象。"),
                          _("所有資料都存在這台電腦；備份與網路資料夾的位置由你決定。"),
                          _("匯出分析資料時可以匿名化：廠名、機器人、產線與操作人換成代號。")]}


# ------------------------------------------------------------ POST（呼叫前已確認登入與角色：auth.PERMS）
def post(ctx: Ctx, path: str, b: dict, who: str):
    if not path.startswith("/api/sys/"):
        return None
    try:
        return _post(ctx, path, b or {}, who)
    except PermissionError as e:
        return {"error": str(e)}, 403
    except ValueError as e:
        return {"error": str(e)}, 400


def _post(ctx: Ctx, path: str, b: dict, who: str):
    a = ctx.auth
    sess = ctx.session or {}
    me = sess.get("username") or ""
    if path == "/api/sys/ping":
        if ctx.token and hasattr(a, "touch"):
            a.touch(ctx.token)
        return {"ok": True}, 200
    if path == "/api/sys/password":
        if ctx.mode != "accounts" or not me:
            raise ValueError(_("目前不是個人帳號模式，沒有自己的密碼可以改"))
        new = str(b.get("new") or "")
        if new != str(b.get("confirm", new)):
            raise ValueError(_("兩次輸入的新密碼不一樣"))
        n = a.change_password(me, str(b.get("old") or ""), new, ctx.token)
        return {"ok": True, "ended": n}, 200
    if path == "/api/sys/account":
        if ctx.mode != "accounts" or not me:
            raise ValueError(_("目前不是個人帳號模式"))
        disp = " ".join(str(b.get("display") or "").split())[:30]
        if not disp:
            raise ValueError(_("顯示名稱不能空白"))
        _users(ctx).update(me, display=disp)
        return {"ok": True, "display": disp}, 200
    if path == "/api/sys/sessions/revoke":
        if not isinstance(a, Auth) or a.users is None:
            raise ValueError(_("共用密碼或演示模式沒有個人登入紀錄"))
        if b.get("others"):
            if not me:
                raise ValueError(_("目前不是個人帳號模式"))
            return {"ok": True, "ended": a.users.end_user_sessions(me, keep_token=ctx.token)}, 200
        sid = int(b.get("id") or 0)
        n = a.users.end_session(sid=sid, username=None if ctx.is_admin() else me)
        if not n:
            raise ValueError(_("找不到這個登入（可能已經登出）"))
        return {"ok": True, "ended": n}, 200
    if path == "/api/sys/settings":
        _need_admin(ctx)
        ch = b.get("changes") or {}
        if not isinstance(ch, dict) or not ch:
            raise ValueError(_("沒有要儲存的項目"))
        done = SC.get(ctx.cfg).set(ch, who or "demo", ctx.addr, client=ctx.addr)
        return {"ok": True, "changed": done}, 200
    if path == "/api/sys/settings/reset":
        _need_admin(ctx)
        return {"ok": True, "reset": SC.get(ctx.cfg).reset(str(b.get("key") or ""), who or "demo", ctx.addr)}, 200
    if path == "/api/sys/settings/revert":
        _need_admin(ctx)
        k = SC.get(ctx.cfg).revert(int(b.get("id") or 0), who or "demo", ctx.addr, client=ctx.addr)
        return {"ok": True, "key": k}, 200
    if path == "/api/sys/users":
        _need_admin(ctx)
        return users_action(ctx, b, who), 200
    if path == "/api/sys/notify/people":
        _need_admin(ctx)
        return people_action(ctx, b, who), 200
    if path == "/api/sys/tls":
        _need_admin(ctx)
        return tls_action(ctx, b), 200
    if path == "/api/sys/wizard":
        _need_admin(ctx)
        from . import wizard
        if ctx.role != "standalone":
            raise ValueError(_("第一次設定精靈只用在單機；邊緣主機的機器人清單請改設定檔 [[robots]]"))
        act = str(b.get("action") or "")
        if act == "test":
            return wizard.test(ctx.cfg, ctx.addr, b), 200
        if act == "save":
            return wizard.save(ctx.cfg, ctx.addr, b), 200
        raise ValueError(_("不認得的動作"))
    if path == "/api/sys/backup":
        _need_admin(ctx)
        if not ctx.backup_fn:
            raise ValueError(_("這台主機沒有可以備份的資料庫"))
        p = ctx.backup_fn()
        return {"ok": True, "file": os.path.basename(p), "dir": os.path.dirname(p), "bytes": _size(p)}, 200
    if path == "/api/sys/diag":
        _need_admin(ctx)
        from . import export as EX
        out = os.path.join(EX.exports_dir(ctx.cfg), time.strftime("dtm_diagnostics_%Y%m%d_%H%M%S.zip"))
        res = diag_bundle(ctx, out)
        res["url"] = "/api/sys/download?t=" + EX.issue_download(out)
        res["name"] = os.path.basename(out)
        return res, 200
    return {"error": "not found"}, 404


def users_action(ctx: Ctx, b: dict, who: str) -> dict:
    a = ctx.auth
    if ctx.mode == "demo":
        raise ValueError(_("演示模式沒有帳號；正式使用時由管理員在這裡新增"))
    us = _users(ctx)
    pol = SC.policy(ctx.cfg)
    act = str(b.get("action") or "")
    name = str(b.get("username") or "").strip()
    me = (ctx.session or {}).get("username") or ""
    if not name:
        raise ValueError(_("請指定帳號"))
    if act == "add":
        pw = str(b.get("password") or "")
        us.add(name, pw, str(b.get("role") or "operator"), " ".join(str(b.get("display") or "").split())[:30],
               str(b.get("plants") or ""), must_change=pol["change_first"], pol=pol, created_by=me or who)
        a._n_users, a._n_ts = None, 0.0
        return {"ok": True, "must_change": pol["change_first"]}
    u = us.get(name)
    if not u:
        raise ValueError(_("找不到帳號 {u}", u=name))
    if act == "update":
        kw = {}
        if "display" in b:
            kw["display"] = " ".join(str(b["display"] or "").split())[:30] or name
        if "role" in b:
            if name == me and b["role"] != "admin":
                raise ValueError(_("不能降低自己的權限（請另一位管理員操作）"))
            a.guard_admin(name, role=str(b["role"]))
            kw["role"] = str(b["role"])
        if "plants" in b:
            kw["plants"] = str(b["plants"] or "")
        us.update(name, **kw)
        return {"ok": True}
    if act in ("disable", "enable"):
        if act == "disable":
            if name == me:
                raise ValueError(_("不能停用自己的帳號"))
            a.guard_admin(name, active=False)
        us.update(name, active=0 if act == "disable" else 1, locked_until=0, fails=0)
        ended = us.end_user_sessions(name) if act == "disable" else 0
        a._n_users, a._n_ts = None, 0.0
        return {"ok": True, "ended": ended}
    if act == "reset":
        pw = str(b.get("password") or "")
        err = check_password(pw, pol, name)
        if err:
            raise ValueError(err)
        us.set_password(name, pw, must_change=pol["change_first"] and name != me, pol=pol)
        ended = us.end_user_sessions(name, keep_token=ctx.token if name == me else "")
        return {"ok": True, "ended": ended, "must_change": pol["change_first"] and name != me}
    if act == "unlock":
        us.update(name, locked_until=0, fails=0)
        return {"ok": True}
    if act == "logout":
        return {"ok": True, "ended": us.end_user_sessions(name, keep_token=ctx.token if name == me else "")}
    raise ValueError(_("未知的動作：{a}", a=act))


# ------------------------------------------------------------ 收件人名單
def _mask_addr(v: str) -> str:
    v = str(v or "")
    if not v:
        return ""
    if "@" in v:
        a, _sep, d = v.partition("@")
        return (a[:2] + "…" if len(a) > 2 else a) + "@" + d
    d = re.sub(r"\D", "", v)
    return (v[:4] + "…" + d[-3:]) if len(d) > 6 else v


def _rule_desc(r: dict) -> str:
    from .notify import CHANNEL_LABELS
    lv = _("警戒") if int(r.get("min_level", 1) or 1) >= 2 else _("注意以上")
    parts = [_("第 {n} 階段", n=int(r.get("stage", 1) or 1)), lv,
             _("、").join(CHANNEL_LABELS.get(c, c) for c in (r.get("channels") or [])) or "—"]
    if r.get("sites") and "*" not in r["sites"]:
        parts.append(_("、").join(str(x) for x in r["sites"]))
    if r.get("duty"):
        parts.append(_("另外發給值班人員"))
    if r.get("enabled") is False:
        parts.append(_("規則已停用"))
    return " · ".join(parts)


def people_view(ctx: Ctx) -> dict:
    sc = SC.get(ctx.cfg)
    sc.refresh()
    n = ctx.cfg.get("notify") or {}
    admin = ctx.is_admin()
    groups = [{"key": "default", "label": _("預設收件人"), "desc": _("沒有設定規則、或規則沒有指定收件人時用")}]
    groups += [{"key": str(r.get("name")), "label": _(str(r.get("name"))), "desc": _rule_desc(r)}
               for r in (n.get("rules") or []) if r.get("name")]           # 範例設定檔的規則名稱有翻譯
    demo = bool(ctx.cfg.get("_demo"))
    dl = default_lang()
    langs = [{"code": "", "name": _("系統預設語言（{l}）", l=LANGS.get(dl, dl))}] + [{"code": k, "name": v} for k, v in LANGS.items()]
    people = []
    for p in n.get("people") or []:
        q = dict(p)
        if demo:                                # 演示的虛構人名與備註有翻譯
            q["name"], q["note"] = _(p.get("name") or ""), _(p.get("note") or "") if p.get("note") else ""
        if not admin:
            q["phone"], q["email"], q["note"] = _mask_addr(p.get("phone")), _mask_addr(p.get("email")), ""
        q["lang_name"] = LANGS.get(p.get("lang") or "", "") or _("系統預設語言（{l}）", l=LANGS.get(dl, dl))
        people.append(q)
    sms = n.get("sms") or {}
    return {"people": people, "groups": groups, "langs": langs, "can_edit": admin, "web": SC.PEOPLE_KEY in sc.values,
            "channels": {"sms": bool(sms.get("enabled")), "email": bool(n.get("email_enabled")) and bool(n.get("smtp_host"))},
            "report": bool((sms.get("report") or {}).get("enabled")), "max": SC.MAX_PEOPLE}


# ------------------------------------------------------------ HTTPS（2.1）
def _tls_file_value(cfg: dict) -> str:
    """設定檔裡現在寫的 [web] tls_cert（可能剛改、還沒重新啟動）。"""
    p = cfg.get("_path") or ""
    if p and os.path.exists(p):
        try:
            import tomllib
            with open(p, "rb") as f:
                return str((tomllib.load(f).get("web") or {}).get("tls_cert") or "")
        except (OSError, ValueError):
            pass
    return str((cfg.get("web") or {}).get("tls_cert") or "")


def _tls_cert_path(cfg: dict) -> str:
    from .runtime import resolve
    v = _tls_file_value(cfg) or str((cfg.get("web") or {}).get("tls_cert") or "")
    return resolve(cfg, v) if v else ""


def tls_view(ctx: Ctx) -> dict:
    from . import settings as ST
    from . import tlscert
    cfg = ctx.cfg
    running = str((cfg.get("web") or {}).get("tls_cert") or "")
    configured = _tls_file_value(cfg)
    cert = None
    path = _tls_cert_path(cfg)
    if path and os.path.exists(path):
        try:
            d = tlscert.describe(path)
            cert = {"subject": d.get("subject"), "not_after": d.get("not_after"), "sha256": d.get("sha256"),
                    "days_left": int((d["not_after"] - time.time()) // 86400) if d.get("not_after") else None,
                    "self_signed": os.path.basename(path) == "dtm-cert.pem", "path": path}
        except (OSError, ValueError, IndexError) as e:
            cert = {"error": str(e), "path": path}
    ok, why = ST.config_editable(cfg, ctx.addr)
    w = cfg.get("web") or {}
    return {"running": bool(running), "configured": bool(configured), "restart": running != configured,
            "cert": cert, "can_edit": ok, "why": why, "port": w.get("port"),
            "local_only": str(w.get("host") or "") in ("127.0.0.1", "::1", "localhost"),
            "role": ctx.role, "names": tlscert.local_names() if ok else None}


def tls_action(ctx: Ctx, b: dict) -> dict:
    from . import settings as ST
    act = str(b.get("action") or "")
    if act == "self_signed":
        extra = [x for x in re.split(r"[\s,，、]+", str(b.get("names") or "")) if x][:10]
        info = ST.enable_tls(ctx.cfg, ctx.addr, extra)
        return {"ok": True, "sha256": info["sha256"], "names": info["names"], "ips": info["ips"], "restart": True}
    if act == "off":
        ST.disable_tls(ctx.cfg, ctx.addr)
        return {"ok": True, "restart": True}
    raise ValueError(_("不認得的動作"))


def people_action(ctx: Ctx, b: dict, who: str) -> dict:
    sc = SC.get(ctx.cfg)
    sc.refresh(force=True)
    cur = [dict(p) for p in (ctx.cfg.get("notify") or {}).get("people") or []]
    act = str(b.get("action") or "")
    pid = str(b.get("id") or (b.get("person") or {}).get("id") or "")
    if act == "test":
        p = next((x for x in cur if x.get("id") == pid), None)
        if not p:
            raise ValueError(_("找不到這位收件人"))
        if ctx.cfg.get("_demo"):
            raise ValueError(_("演示資料模式不會真的發送通知"))
        if not ctx.notifier:
            raise ValueError(_("通知未啟用"))
        return {"ok": True, "results": ctx.notifier.test_person(p, str(b.get("text") or "")[:200])}
    if act == "save":
        p = dict(b.get("person") or {})
        if pid and any(x.get("id") == pid for x in cur):
            cur = [dict(p, id=pid) if x.get("id") == pid else x for x in cur]
        else:
            p["id"] = SC._pid({str(x.get("id")) for x in cur})
            pid = p["id"]
            cur.append(p)
    elif act == "delete":
        if not any(x.get("id") == pid for x in cur):
            raise ValueError(_("找不到這位收件人"))
        cur = [x for x in cur if x.get("id") != pid]
    elif act == "enable":
        if not any(x.get("id") == pid for x in cur):
            raise ValueError(_("找不到這位收件人"))
        cur = [dict(x, enabled=bool(b.get("enabled"))) if x.get("id") == pid else x for x in cur]
    else:
        raise ValueError(_("未知的動作：{a}", a=act))
    changed = sc.set({SC.PEOPLE_KEY: cur}, who or "demo", ctx.addr, note=act, client=ctx.addr)
    return {"ok": True, "id": pid, "changed": bool(changed)}


SECRET_FIELDS = re.compile(r"(password|passwd|^old$|^new$|^confirm$|token|secret|data)$", re.I)


def audit_detail(path: str, b) -> str:
    """寫入操作紀錄的內容：密碼、金鑰、照片內容一律不記。"""
    if path == "/api/sys/settings" and isinstance(b, dict):
        keys = list((b.get("changes") or {}).keys())
        return _("修改：{k}", k=_("、").join(keys))[:300]
    if path == "/api/sys/password":
        return ""
    if path == "/api/sys/wizard" and isinstance(b, dict):
        return " ".join(str(b.get(k) or "") for k in ("action", "source", "host") if b.get(k))[:200]
    if path == "/api/sys/notify/people" and isinstance(b, dict):      # 不記電話與 Email
        p = b.get("person") or {}
        return " ".join(x for x in (str(b.get("action") or ""), str(p.get("name") or b.get("id") or "")) if x)[:200]

    def strip(o):
        if isinstance(o, dict):
            return {k: ("***" if SECRET_FIELDS.search(str(k)) else strip(v)) for k, v in o.items()}
        if isinstance(o, list):
            return [strip(v) for v in o[:20]]
        return o
    try:
        return json.dumps(strip(b), ensure_ascii=False)[:300]
    except (TypeError, ValueError):
        return ""


# ------------------------------------------------------------ 診斷包
MASK_KEYS = re.compile(r"^\s*([A-Za-z0-9_\-]*(password|passwd|token|secret|api_key|apikey|key|pass|pwd|webhook_url|url)"
                       r"[A-Za-z0-9_\-]*)\s*=\s*(.+)$", re.I)
EMAIL_ANY = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_ANY = re.compile(r"(?<![\d.])\+?\d[\d \-]{7,15}\d(?![\d.])")


def mask_text(text: str) -> str:
    """設定檔與記錄檔：密碼、金鑰、網址、Email、電話一律遮蔽。"""
    out = []
    for line in text.splitlines():
        m = MASK_KEYS.match(line)
        if m and not line.lstrip().startswith("#"):
            v = m.group(3).strip()
            if v not in ('""', "''", "[]", "{}") and not v.startswith('"env:'):
                line = f"{m.group(1)} = \"***\""
        line = EMAIL_ANY.sub("***@***", line)
        line = PHONE_ANY.sub("***", line)
        out.append(line)
    return "\n".join(out)


def diag_bundle(ctx: Ctx, out: str) -> dict:
    """給維護人員或回報問題用：版本、設定檔（遮蔽）、檢查結果、自我檢查、狀態檔、最近的記錄檔。不含資料庫與帳號。"""
    from . import runtime as R
    cfg = ctx.cfg
    files = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        def put(name, data):
            z.writestr(name, data)
            files.append(name)
        info = system_info(ctx)
        put("system.json", json.dumps(info, ensure_ascii=False, indent=1, default=str))
        put("check.json", json.dumps(config_check(ctx), ensure_ascii=False, indent=1))
        sc = SC.get(cfg)
        put("settings.json", json.dumps({k: ("***" if k == SC.NOTIFY_KEY else v) for k, v in sc.values.items()},
                                        ensure_ascii=False, indent=1))
        p = cfg.get("_path")
        if p and os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                put("config.toml", mask_text(f.read()))
        for d, pat in ((R.run_dir(cfg), r"\.json$"), (R.log_dir(cfg), r"\.(log|txt)(\.\d+)?$")):
            if not os.path.isdir(d):
                continue
            names = sorted((n for n in os.listdir(d) if re.search(pat, n)),
                           key=lambda n: os.path.getmtime(os.path.join(d, n)), reverse=True)[:12]
            for n in names:
                fp = os.path.join(d, n)
                try:
                    with open(fp, "rb") as f:
                        f.seek(max(0, os.path.getsize(fp) - 2_000_000))
                        put(os.path.basename(d) + "/" + n, mask_text(f.read().decode("utf-8", "replace")))
                except OSError:
                    continue
        put("README.txt", _("DTM 診斷包：給維護人員或回報問題用。\n"
                            "內容：版本與主機資訊、設定檔（密碼、金鑰、網址、Email、電話已遮蔽）、設定檔檢查、自我檢查、狀態檔、最近的記錄檔。\n"
                            "不含：監測資料庫、帳號與密碼、照片。\n"
                            "記錄檔可能有 IP 位址與操作人姓名，寄出前可以先打開檢查。\n"))
    return {"ok": True, "files": files, "bytes": os.path.getsize(out)}


def headers_secure() -> dict:
    """所有回應都加的安全標頭：不讓別的網站嵌入、不猜檔案類型、不把網址傳給外站。"""
    return {"X-Frame-Options": "SAMEORIGIN", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
            "Permissions-Policy": "camera=(self), microphone=(), geolocation=()"}


def real_client(handler, internal_token: str = "") -> str:
    """經由 DTM 畫面轉送（同一台主機的廠區畫面 → 機器人程式）的請求，用轉送時附上的原始電腦 IP。"""
    import hmac
    addr = handler.client_address[0]
    fwd = handler.headers.get("X-DTM-Client", "")
    tok = internal_token or os.environ.get("DTM_INTERNAL_TOKEN", "")
    if fwd and tok and is_loopback(addr) and hmac.compare_digest(handler.headers.get("X-DTM-Internal", ""), tok):
        return fwd
    return addr


__all__ = ["Ctx", "get", "post", "headers_secure", "real_client", "mask_text", "policy_text", "ACTIONS", "audit_detail"]

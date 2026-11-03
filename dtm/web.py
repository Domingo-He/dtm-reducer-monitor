"""內建網頁伺服器（只用標準函式庫）：儀表板與 JSON API。"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import sysconf as SC
from . import sysweb as SW
from .auth import ROLE_NAMES, Auth, WriteAuth, is_loopback, need_role
from .analysis import HIGH_METRICS, LEVEL_NAMES, METRIC_LABELS, comp, epochs
from .config import COMP_LABELS, EVENT_LABELS, RESET_KINDS, VIB_METRICS, axis_cfg, axis_name
from .i18n import FONT_STACK, HTML_LANG, _, default_lang, get_lang, norm, render, ui_messages, use, visible_langs
from .model import decode_seg
from .report import build_report, export_features

log = logging.getLogger("dtm.web")
STATIC = os.path.join(os.path.dirname(__file__), "static")


def _num(v, nd=3):
    return None if v is None else round(float(v), nd)


VENDOR = {"/vendor/html2canvas.min.js": ("vendor/html2canvas.min.js", "application/javascript; charset=utf-8")}
PUBLIC_GET = {"/", "/index.html", "/live", "/pro", "/pro.html", "/central.html", "/api/i18n", "/favicon.ico", "/vendor/html2canvas.min.js",
              "/api/about", "/api/c/info", "/api/c/i18n", "/api/c/about", "/api/sys/me", "/api/sys/tls/cert.crt"}
PAGES = {"/": "central.html", "/central.html": "central.html", "/index.html": "index.html", "/live": "index.html",
         "/pro": "pro.html", "/pro.html": "pro.html"}


def make_handler(svc, auth: WriteAuth | None = None):
    cfg = svc.cfg
    auth = auth or Auth(cfg)
    svc.auth = auth
    started = time.time()
    SC.get(cfg).start_watch()                          # 網頁上改的系統設定（通知收件人等）10 秒內套用

    def backup_now() -> str:
        from . import backup as B
        return B.backup_now(cfg)

    class H(BaseHTTPRequestHandler):
        timeout = 120          # 連上卻不送資料的連線（含 HTTPS 交握）最多佔用 2 分鐘，不會卡住伺服器

        def log_message(self, fmt, *args):  # 靜音
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in SW.headers_secure().items():
                self.send_header(k, v)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _addr(self) -> str:
            return SW.real_client(self)

        def _token(self) -> str:
            return self.headers.get("X-DTM-Token", "")

        def _ctx(self, sess=None) -> SW.Ctx:
            return SW.Ctx(cfg=cfg, auth=auth, role="standalone", addr=self._addr(), token=self._token(),
                          session=sess if sess is not None else self._session(), audit_add=svc.store.add_audit,
                          audit_conn=svc.store.conn, db_path=os.path.abspath(cfg["storage"]["db"]),
                          backup_fn=backup_now, backup_cfg=cfg, notifier=getattr(svc, "notifier", None), started=started)

        def _sms_report(self, q, method):
            """簡訊商的送達回呼（不需要登入、不受允許連線清單限制；用 [notify.sms.report] key 把關）。"""
            from .notify import handle_report, report_params
            body = b""
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(min(n, 1_000_000)) if n else b""
            code, txt = handle_report(cfg, svc.store, report_params(q, body, self.headers.get("Content-Type", "")))
            return self._send(code, txt.encode(), "text/plain; charset=utf-8")

        def _blocked(self) -> bool:
            """[web] allow_ips：不在清單裡的電腦一律拒絕（本機一律可以）。"""
            addr = self._addr()
            if SC.ip_allowed(addr, SC.policy(cfg)["allow_ips"]):
                return False
            self._send(403, (_("這台電腦（{ip}）不在允許連線的清單裡，請洽系統管理員。", ip=addr)).encode(),
                       "text/plain; charset=utf-8")
            return True

        def _json(self, obj, code=200):
            self._send(code, json.dumps(clean(obj), ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 60_000_000:
                raise ValueError(_("檔案太大"))
            return json.loads(self.rfile.read(n) or b"{}")

        # -------------------------------------------------------- GET
        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/api/sms/report":
                return self._sms_report(q, "GET")
            if self._blocked():
                return
            with use(norm(q.get("lang")) or default_lang()):
                return self._get(u, q)

        def _session(self):
            fn = getattr(auth, "session", None)
            return fn(self.headers.get("X-DTM-Token", "")) if fn else None

        def _get(self, u, q):
            try:
                if getattr(auth, "require_login", False) and u.path not in PUBLIC_GET and not self._session():
                    return self._json({"error": _("需要登入才能查看"), "auth": "required"}, 401)
                if u.path in VENDOR:                   # 第三方元件（全頁截圖用，MIT 授權，見 static/vendor）
                    with open(os.path.join(STATIC, VENDOR[u.path][0]), "rb") as f:
                        return self._send(200, f.read(), VENDOR[u.path][1])
                if u.path in PAGES:                    # / = DTM 主畫面（左側選單）；即時監測與程式診斷在裡面開
                    with open(os.path.join(STATIC, PAGES[u.path]), "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if u.path.startswith("/api/sys/"):
                    r = SW.get(self._ctx(), u.path, q)
                    if len(r) == 3:
                        return self._send(200, r[0], r[1], r[2])
                    return self._json(*r)
                if u.path == "/api/c/info":
                    return self._json(shell_info(svc, auth, self._session(), self.server.server_address[0],
                                                 self._addr(), started))
                if u.path == "/api/c/about":
                    from .about import info
                    return self._json(info())
                if u.path == "/api/c/robots":
                    return self._json([{"rkey": "local", "disp": cfg["robot"]["name"], "lv": max(
                        [h.get("level", -1) for h in svc.health.values()] or [-1])}])
                if u.path.startswith("/api/pro/"):
                    from .pro import web as PW
                    r = PW.get(cfg, u.path, q)
                    if r is None:
                        return self._json({"error": "not found"}, 404)
                    if isinstance(r, tuple):
                        return self._send(200, r[0], r[1], r[2])
                    return self._json(r)
                if u.path in ("/api/i18n", "/api/c/i18n"):
                    lang = get_lang()
                    return self._json({"lang": lang, "default": default_lang(), "langs": visible_langs((lang, default_lang())),
                                       "html_lang": HTML_LANG[lang], "font": FONT_STACK[lang],
                                       "messages": ui_messages(lang)})
                if u.path == "/api/status":
                    st = status(svc)
                    st.update(security_info(auth, self.server.server_address[0], self._addr(), cfg))
                    return self._json(st)
                if u.path == "/api/settings/devices":
                    from . import settings as SET
                    return self._json(SET.view(cfg, svc.sensors.status() if getattr(svc, "sensors", None) else None,
                                               self._addr()))
                if u.path == "/api/restore/sources":
                    from . import restore as RS
                    return self._json(RS.web_sources(cfg, self._addr()))
                if u.path == "/api/audit":
                    return self._json(svc.store.audit(int(q.get("limit", 50))))
                if u.path == "/api/live":
                    return self._json(live(svc, float(q.get("seconds", 120))))
                if u.path == "/api/health":
                    return self._json(health(svc))
                if u.path == "/api/trend":
                    return self._json(trend(svc, int(q.get("axis", svc.axes[0])), q.get("metric", "F"),
                                            float(q.get("days", 180))))
                if u.path == "/api/alarms":
                    rows = svc.store.alarms(int(q.get("limit", 100)), q.get("active") == "1")
                    for r in rows:
                        r["level_name"] = LEVEL_NAMES.get(r["level"])
                        r["kind_label"] = _(r["kind"]) if r.get("kind") else ""
                        r["message"] = render(r.pop("tmpl", None), r["message"])
                    return self._json(rows)
                if u.path == "/api/events":
                    from . import teardown as TD
                    rows = svc.store.events(include_undone=True)
                    for r in rows:
                        r["label"] = EVENT_LABELS.get(r["kind"], r["kind"])
                        td = (r.get("detail") or {}).get("teardown")
                        r["teardown_text"] = TD.text(td) if td else ""
                        r["teardown_photos"] = (td or {}).get("photos") or []
                        r["can_teardown"] = r["kind"] in TD.REPLACE_KINDS and not r.get("undone_ts")
                        r.pop("detail", None)
                    return self._json(rows)
                if u.path == "/api/motion":
                    return self._json(motion_view(svc))
                if u.path == "/api/motion/trend":
                    return self._json(motion_trend(svc, int(q.get("axis", svc.axes[0])), int(q.get("code", 0)),
                                                   float(q.get("days", 90))))
                if u.path == "/api/decision":
                    return self._json([decision_view(svc, current_decision(svc, ax)) for ax in svc.axes])
                if u.path in ("/api/workpack", "/workorder"):
                    from . import workpack as wp
                    ax = int(q.get("axis", svc.axes[0]))
                    key = q.get("key", "")
                    comp_ = q.get("comp") or wp.comp_for(cfg, ax, key or "drive")
                    key = key or {"grease": "grease", "motor": "motor"}.get(comp_, "drive")
                    w = wp.get(cfg, ax, comp_)
                    if u.path == "/api/workpack":
                        cat = wp.load_catalog(cfg)
                        return self._json({"axis": ax, "comp": comp_, "key": key, "pack": w, "error": cat["error"],
                                           "catalog": cat["path"], "print_url": f"/workorder?axis={ax}&comp={comp_}"})
                    if not w:
                        return self._send(404, ("<p>" + _("{name} {comp} 尚未建立作業包。", name=axis_name(cfg, ax),
                                                            comp=COMP_LABELS.get(comp_, comp_)) + "</p>").encode(),
                                          "text/html; charset=utf-8")
                    d = current_decision(svc, ax) if ax in svc.axes else None
                    return self._send(200, wp.render_workorder(cfg, w, d, key).encode(), "text/html; charset=utf-8")
                if u.path == "/api/workpacks":
                    from . import workpack as wp
                    cat = wp.load_catalog(cfg)
                    return self._json({"catalog": cat["path"], "error": cat["error"], "meta": cat["meta"],
                                       "packs": [{"id": p.get("id"), "title": p.get("title"), "comp": p.get("comp"),
                                                  "axes": p.get("axes", [])} for p in cat["packs"]]})
                if u.path == "/api/components":
                    rows = svc.store.components()
                    for r in rows:                     # 程式產生的固定文字（失效、預防／例行…）依請求語言顯示；使用者打的字不在翻譯表裡，照原樣
                        for k in ("reason", "note", "robot"):
                            if r.get(k):
                                r[k] = _(r[k])
                    return self._json(rows)
                if u.path == "/api/notify":
                    nt = svc.notifier
                    from .fleetviews import dlr_state
                    to = float(((cfg["notify"].get("sms") or {}).get("report") or {}).get("timeout_min", 30) or 30) * 60
                    rows = [dict(r, dlr=dlr_state(r, time.time(), to)) for r in svc.store.notify_log(30)]
                    return self._json({"channels": nt.status() if nt else [], "log": rows})
                if u.path == "/api/report/commissioning":
                    from . import commissioning as CM
                    body = CM.build(cfg, float(q.get("days", 7)),
                                    sensors_status=svc.sensors.status() if getattr(svc, "sensors", None) else None).encode()
                    return self._send(200, body, "text/html; charset=utf-8")
                if u.path in ("/api/report/validation", "/api/report/validation.json"):     # 現場驗證報告
                    from . import validation as VA
                    r = VA.for_store(cfg, svc.store, max(30.0, min(3650.0, float(q.get("days", 730)))))
                    if u.path.endswith(".json"):
                        return self._json(r)
                    return self._send(200, VA.build_html(r, cfg["robot"]["name"]).encode("utf-8"), "text/html; charset=utf-8")
                if u.path == "/api/report":
                    body = build_report(svc, float(q.get("days", 180))).encode()
                    return self._send(200, body, "text/html; charset=utf-8")
                if u.path == "/api/export/download":
                    from . import export as EX
                    p = EX.take_download(q.get("t", ""))
                    if not p:
                        return self._json({"error": _("下載連結已失效（10 分鐘內有效、只能下載一次），請重新匯出")}, 404)
                    with open(p, "rb") as f:
                        body = f.read()
                    return self._send(200, body, "application/zip",
                                      {"Content-Disposition": f"attachment; filename={os.path.basename(p)}"})
                if u.path == "/api/about":
                    from .about import info
                    return self._json(info())
                if u.path == "/api/downtime":
                    return self._json({"downtime": svc.downtime() if hasattr(svc, "downtime") else None})
                if u.path == "/api/teardown/options":
                    from . import teardown as TD
                    return self._json(TD.options())
                if u.path == "/api/photo":
                    from . import teardown as TD
                    p = TD.photo_path(cfg, q.get("name", ""))
                    if not p:
                        return self._json({"error": "not found"}, 404)
                    with open(p, "rb") as f:
                        body = f.read()
                    ext = os.path.splitext(p)[1].lower()
                    return self._send(200, body, {".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg"))
                if u.path == "/api/export.csv":
                    fd, path = tempfile.mkstemp(suffix=".csv")
                    os.close(fd)
                    export_features(svc, path)
                    with open(path, "rb") as f:
                        body = f.read()
                    os.remove(path)
                    return self._send(200, body, "text/csv; charset=utf-8",
                                      {"Content-Disposition": "attachment; filename=features.csv"})
                return self._json({"error": "not found"}, 404)
            except Exception as e:
                log.exception("GET %s", u.path)
                return self._json({"error": str(e)}, 500)

        # -------------------------------------------------------- POST
        def do_POST(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/api/sms/report":
                return self._sms_report(q, "POST")
            if self._blocked():
                return
            with use(norm(q.get("lang")) or default_lang()):
                return self._post(u)

        def _post(self, u):
            try:
                b = self._body()
                addr = self._addr()
                raw_addr = self.client_address[0]
                if u.path == "/api/internal/cmd":
                    # 邊緣主機的監督者轉來的中央指令：只接受本機、而且要帶啟動時給的內部金鑰
                    import hmac
                    tok = os.environ.get("DTM_INTERNAL_TOKEN", "")
                    if not tok or raw_addr not in ("127.0.0.1", "::1") or not hmac.compare_digest(
                            self.headers.get("X-DTM-Internal", ""), tok):
                        return self._json({"ok": False, "detail": "forbidden"}, 403)
                    from .sync import execute_command
                    res = execute_command(svc, b)
                    svc.store.add_audit(str(b.get("who") or "central"), addr, f"cmd:{b.get('kind')}", bool(res.get("ok")),
                                        json.dumps(b.get("payload") or {}, ensure_ascii=False)[:300])
                    return self._json(res)
                if u.path == "/api/login":
                    who = " ".join(str(b.get("who", "") or b.get("username", "")).split())[:30]
                    if isinstance(auth, Auth):
                        token, err, code = auth.login(addr, who, str(b.get("password", "")),
                                                      username=str(b.get("username", "") or ""),
                                                      agent=self.headers.get("User-Agent", ""))
                    else:
                        token, err, code = auth.login(addr, who, str(b.get("password", "")))
                    sess = auth.session(token) if token and hasattr(auth, "session") else None
                    svc.store.add_audit((sess or {}).get("who") or who, addr, "login", bool(token), err)
                    if not token:
                        return self._json({"error": err}, code)
                    return self._json({"ok": True, "token": token, "who": (sess or {}).get("who", who),
                                       "role": (sess or {}).get("role"), "auth": auth.enabled,
                                       "role_name": ROLE_NAMES.get((sess or {}).get("role"), ""),
                                       "must_change": bool((sess or {}).get("must_change")),
                                       "hours": round(auth.ttl / 3600, 1)})
                if u.path == "/api/logout":
                    old = self._session()
                    auth.logout(self._token())
                    if old:
                        svc.store.add_audit(old.get("who", ""), addr, "logout", True, str(b.get("reason") or ""))
                    return self._json({"ok": True})
                if u.path == "/api/setup":
                    return self._json(*first_admin(auth, addr, b, svc.store.add_audit))
                if auth.mode == "readonly":
                    svc.store.add_audit("", addr, u.path, False, _("唯讀模式"))
                    return self._json({"error": _("目前是唯讀模式：請在這台電腦上開啟儀表板，建立第一個管理員帳號"),
                                       "auth": "setup"}, 403)
                sess = self._session()
                if sess is None and auth.enabled:
                    svc.store.add_audit("", addr, u.path, False, _("未登入"))
                    return self._json({"error": _("需要登入才能修改資料"), "auth": "required"}, 401)
                if hasattr(auth, "can") and not auth.can(sess, u.path):
                    need = need_role(u.path)
                    svc.store.add_audit((sess or {}).get("who", ""), addr, u.path, False, _("權限不足"))
                    return self._json({"error": _("權限不足：這個動作需要「{role}」以上", role=ROLE_NAMES.get(need, need)),
                                       "auth": "forbidden"}, 403)
                who = (sess or {}).get("who") or ""
                if u.path == "/api/sys/ping":
                    auth.touch(self._token())
                    return self._json({"ok": True})
                auth.touch(self._token())                  # 有寫入 = 有在操作（閒置自動登出）
                if u.path.startswith("/api/sys/"):
                    obj, code = SW.post(self._ctx(sess), u.path, b, who)
                else:
                    try:
                        obj, code = self._write(u.path, b, who)
                    except Exception as e:
                        svc.store.add_audit(who, addr, u.path, False, f"{e}")
                        raise
                if code != 404:
                    detail = SW.audit_detail(u.path, b)
                    svc.store.add_audit(who, addr, u.path, code == 200, detail if code == 200 else str(obj.get("error", "")))
                return self._json(obj, code)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                log.exception("POST %s", u.path)
                return self._json({"error": str(e)}, 400)

        def _write(self, path: str, b, who: str) -> tuple:
            """所有會改資料的 API；回傳 (內容, HTTP 狀態碼)。呼叫前已確認權限。"""
            ok = ({"ok": True}, 200)
            if path.startswith("/api/pro/"):
                from .pro import web as PW
                return PW.post(cfg, path, b, who)
            if path == "/api/settings/devices":
                from . import settings as SET
                before = SET.device_flags(cfg)
                try:
                    dev, bak = SET.save(cfg, b, self._addr())
                except PermissionError as e:
                    return {"error": str(e)}, 403
                if getattr(svc, "sensors", None):
                    svc.sensors.refresh_flags()
                svc.store.add_audit(who, self._addr(), "settings:devices", True,
                                    _("{chg}（舊檔備份：{bak}）", chg=SET.summary(before, dev), bak=os.path.basename(bak)))
                return {"ok": True, "devices": dev, "backup": bak}, 200
            if path == "/api/export/analysis":
                from . import export as EX
                out = os.path.join(EX.exports_dir(cfg), EX.default_name(cfg["robot"].get("name") or "robot",
                                                                         b.get("since"), b.get("until")))
                res = EX.export_robot_db(cfg, out, b.get("since"), b.get("until"), bool(b.get("anonymize")))
                res["url"] = "/api/export/download?t=" + EX.issue_download(out)
                res["name"] = os.path.basename(out)
                res.pop("path", None)
                return res, 200
            if path == "/api/replay":
                from . import replay as RP
                over = b.get("sets") if isinstance(b.get("sets"), dict) else RP.parse_sets(b.get("set") or [])
                if not over:
                    return {"error": _("請指定要試算的設定")}, 400
                days = min(1095.0, float(b.get("days") or 365))
                res = RP.run(svc.store, cfg, over, since=time.time() - days * 86400,
                             decision=bool(b.get("decision")))
                return {"ok": True, "rows": RP.summary(res), "text": RP.text(res)}, 200
            if path == "/api/downtime":
                if b.get("cancel"):
                    return {"ok": True, "cancelled": svc.cancel_downtime(who)}, 200
                return {"ok": True, "downtime": svc.set_downtime(b.get("until"), str(b.get("note", ""))[:200], who)}, 200
            if path == "/api/teardown/photo":
                from . import teardown as TD
                return {"ok": True, "file": TD.save_photo(cfg, str(b.get("data") or ""), f"ev{int(b.get('event_id') or 0)}")}, 200
            if path == "/api/teardown":
                td = svc.set_teardown(int(b["event_id"]), b, who)
                from . import teardown as TD
                return {"ok": True, "teardown": td, "text": TD.text(td)}, 200
            if path == "/api/restore/preview":
                from . import restore as RS
                scope = RS.parse_scope(b.get("scope") or {})
                src = str(b.get("source") or "local")
                if src not in ("local", "mirror", "snapshot", "central"):
                    return {"error": _("網頁只能選備份資料夾、網路資料夾、還原前自動備份或中央；其他檔案請放進備份資料夾，"
                                       "或用指令 python -m dtm restore --file")}, 400
                opened = RS.open_source(cfg, src, name=str(b.get("name") or ""), keep=True)
                return RS.preview(cfg, opened, scope, str(b.get("mode") or "merge")), 200
            if path == "/api/restore/run":
                from . import restore as RS
                ok_, why = RS.editable(cfg, self._addr())
                if not ok_:
                    return {"error": why}, 403
                opened = RS.cached(str(b.get("preview_id") or ""))
                try:
                    res = RS.run(cfg, opened, opened.scope, opened.mode, who=who, svc=svc, confirm=str(b.get("confirm") or ""))
                except PermissionError as e:
                    return {"error": str(e)}, 400
                RS.cached(opened.id, pop=True).close()
                res["pre_backup"] = os.path.basename(res["pre_backup"])
                return res, 200
            if path == "/api/notify/test":
                if not svc.notifier:
                    return {"error": _("通知未啟用")}, 400
                return svc.notifier.test(b.get("channel", "all"), str(b.get("text", ""))[:200]), 200
            if path == "/api/ack":
                svc.store.ack_alarm(int(b["id"]), who)
                return ok
            if path == "/api/event":
                kind = b.get("kind", "other")
                if kind not in EVENT_LABELS:
                    return {"error": _("未知事件類別")}, 400
                axis = int(b["axis"]) if b.get("axis") not in (None, "", "all") else None
                ts = float(b.get("ts") or time.time())
                hrs = float(b["hours"]) if b.get("hours") not in (None, "") else None
                code = int(b["code"]) if str(b.get("code") or "").strip().isdigit() else None
                eid = svc.register_event(ts, axis, kind, str(b.get("note", ""))[:500], bool(b.get("failed")),
                                         str(b.get("teardown", ""))[:1000], hrs, who=who, code=code)
                return {"ok": True, "id": eid}, 200
            if path == "/api/event/undo":
                ev = svc.undo_event(int(b["id"]), who, str(b.get("note", ""))[:300])
                return {"ok": True, "id": ev["id"]}, 200
            if path == "/api/iron":
                wt = float(b["wt_pct"])
                if not 0 <= wt < 20:
                    return {"error": _("鐵粉濃度請以 wt% 輸入（例如 0.045）")}, 400
                svc.add_iron(float(b.get("ts") or time.time()), int(b["axis"]), wt,
                             b.get("sample") or cfg["iron"].get("default_sample", "drain_mid"), str(b.get("note", ""))[:300])
                return ok
            if path == "/api/vibration":
                items = b if isinstance(b, list) else [b]
                for it in items:
                    vals = {k: float(it[k]) for k in VIB_METRICS if it.get(k) not in (None, "")}
                    if not vals:
                        return {"error": _("至少要有一個振動數值")}, 400
                    svc.add_vib(float(it.get("ts") or time.time()), int(it["axis"]), it.get("direction", "radial"),
                                vals, it.get("temp"), str(it.get("source", "api")), refresh=False)
                for ax in {int(it["axis"]) for it in items}:
                    if ax in svc.axes:
                        svc.refresh_decision(ax)
                return {"ok": True, "n": len(items)}, 200
            if path == "/api/hours":
                svc.store.add_hours(float(b.get("ts") or time.time()), float(b["hours"]))
                for ax in svc.axes:
                    svc.refresh_decision(ax)
                return ok
            return {"error": "not found"}, 404

    return H


# ------------------------------------------------------------ API 內容
def clean(o):
    """JSON 不接受 Infinity／NaN，一律轉成 null。"""
    if isinstance(o, float):
        return o if o == o and abs(o) != float("inf") else None
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def decision_view(svc, d: dict) -> dict:
    from . import workpack as wp
    v = dict(d)
    ax = d["axis"]
    use = bool(wp.load_catalog(svc.cfg)["path"])
    v["packs"] = {k: {"comp": wp.comp_for(svc.cfg, ax, k),
                      "pack": wp.summary(wp.get(svc.cfg, ax, wp.comp_for(svc.cfg, ax, k))) if use else None}
                  for k in ("grease", "drive", "motor")} if use else {}
    v["iron"] = {k: val for k, val in d["iron"].items() if k != "points"}
    v["families"] = [{"name": k, "sev": val} for k, val in d["families"].items()]
    for key in ("drive", "motor"):
        lf = d[key]["life"]
        m = lf.get("model") or {}
        v[key] = dict(d[key], life={"age_h": lf.get("age_h"), "b10_h": lf.get("b10_h"),
                                     "p_fail_lead": lf.get("p_fail_lead"), "lead_days": lf.get("lead_days"),
                                     "beta": m.get("beta"), "eta_h": m.get("eta_h"), "source": m.get("source"),
                                     "install_ts": lf.get("install_ts")})
    return v


def status(svc) -> dict:
    st = dict(svc.status)
    now = time.time()
    st["age_s"] = None if st["last_ts"] is None else round(now - st["last_ts"], 1)
    st["source"] = _(st["source"]) if st.get("source") else ""
    st["robot"] = svc.cfg["robot"]["name"]
    st["export_anonymize"] = bool((svc.cfg.get("privacy") or {}).get("export_anonymize", True))
    st["server_time"] = now
    st["downtime"] = svc.downtime() if hasattr(svc, "downtime") else None
    seg = svc.seg.state
    st["diag"] = seg
    if seg.get("code"):
        info = decode_seg(seg["code"])
        st["diag"]["label"] = (f"{axis_name(svc.cfg, info[0])} {info[1]}% " + (_("正轉") if info[2] == 1 else _("反轉"))) if info else ""
    axes = []
    with svc.lock:
        for ax in svc.axes:
            last = svc.live[ax][-1][1] if svc.live[ax] else None
            v = svc.conv.value(ax, last)
            nm = svc.conv.nm(ax, last)
            h = svc.health.get(ax, {})
            axes.append({"axis": ax, "name": axis_cfg(svc.cfg, ax)["name"], "unit": svc.conv.unit(ax),
                         "calibrated": svc.conv.calibrated(ax), "rated": svc.conv.ax[ax]["rated"],
                         "value": _num(v, 1), "pct": _num(svc.conv.pct_rated(ax, nm), 1),
                         "level": h.get("level", -1),
                         "level_name": LEVEL_NAMES.get(h.get("level", -1)) if h else _("無資料"),
                         "ratio_F": _num((h.get("metrics", {}).get("F") or {}).get("ratio"), 3),
                         "days_to_alert": _num(h.get("days_to_alert"), 1),
                         "n_runs": h.get("n_runs", 0), "need_runs": h.get("need_runs"),
                         "motion_level": (svc.motion_health.get(ax) or {}).get("level"),
                         "motion_ratio": _num((svc.motion_health.get(ax) or {}).get("ratio"), 3)})
    st["axes"] = axes
    st["motion_code"] = svc.motion.state["code"] or None
    st["sensors"] = svc.sensors.status() if getattr(svc, "sensors", None) else None
    st["active_alarms"] = len(svc.store.active_alarms())
    st["backup"] = dict(svc.backup.info) if getattr(svc, "backup", None) else None
    st["system"] = system_brief(svc)
    return st


def system_brief(svc) -> dict:
    """儀表板上方的系統狀態列：只列需要注意的項目（自我檢查、磁碟、看門狗、維護模式、感測器、中央同步）。"""
    from . import runtime as R
    cfg = svc.cfg
    items = []
    for c in getattr(svc, "selfcheck", []) or []:
        if c.get("level") in ("warn", "error"):
            items.append({"level": 2 if c["level"] == "error" else 1, "text": _("自我檢查｜{n}：{d}", n=c["name"], d=c["detail"])})
    di = R.disk_info(cfg["storage"]["db"])
    sc = cfg["system"]
    if di.get("free_pct") is not None and di["free_pct"] < float(sc["disk_alarm_pct"]):
        items.append({"level": 2 if di["free_pct"] < float(sc["disk_critical_pct"]) else 1,
                      "text": _("磁碟剩 {p}%（{gb} GB）", p=di["free_pct"], gb=di["free_gb"])})
    wd = {}
    try:
        from .watchdog import watchdog_summary
        wd = watchdog_summary(cfg)
    except Exception:
        wd = {}
    if wd.get("maintenance"):
        m = wd["maintenance"]
        items.append({"level": 1, "text": _("維護模式中（到 {t}）：看門狗暫停", t=time.strftime("%H:%M", time.localtime(m.get("until", 0))))})
    if wd.get("gave_up"):
        items.append({"level": 2, "text": _("看門狗多次重啟仍無法恢復，已停止自動重啟，請人工處理")})
    sn = svc.sensors.status() if getattr(svc, "sensors", None) else None
    if sn and sn.get("enabled") and sn.get("error"):
        items.append({"level": 1, "text": _("感測器：{e}", e=str(sn["error"])[:80])})
    sync = None
    if (cfg.get("central") or {}).get("url"):
        sync = R.read_json(os.path.join(R.run_dir(cfg), "central_sync.json"), {}) or {}
        ok = sync.get("last_ok")
        if not ok or time.time() - float(ok) > float(cfg["notify"].get("edge_fallback_s", 300)):
            items.append({"level": 1, "text": _("連不上中央（{e}）：資料留在本機，恢復後補傳；期間警戒由這台自己通知",
                                                e=(sync.get("last_error") or _("尚未成功"))[:80])})
    return {"items": items, "watchdog": {k: wd.get(k) for k in ("running", "hw", "maintenance", "gave_up")} if wd else None,
            "sync": sync, "disk": di}


def live(svc, seconds: float) -> dict:
    snap = svc.live_snapshot(seconds)
    return {"axes": {str(ax): [[round(t, 2), _num(v, 2)] for t, v in pts] for ax, pts in snap["axes"].items()},
            "seg": [[round(m[0], 2), m[1]] for m in snap["meta"]],
            "units": {str(ax): svc.conv.unit(ax) for ax in svc.axes},
            "rated": {str(ax): svc.conv.ax[ax]["rated"] for ax in svc.axes}}


def motion_view(svc) -> dict:
    from . import motion as M
    now = time.time()
    out = []
    for ax in svc.axes:
        m = svc.motion_health.get(ax) if get_lang() == default_lang() else M.evaluate_axis(svc.store, svc.cfg, ax, now)
        if not m:
            continue
        m = dict(m)
        m["codes"] = [{k: v for k, v in c.items() if k != "series"} for c in m.get("codes", [])]
        m["level_name"] = M.LEVEL_NAMES.get(m.get("level", -1))
        for c in m["codes"]:
            c["level_name"] = M.LEVEL_NAMES.get(c.get("level", -1))
        out.append(m)
    t0 = now - 24 * 3600
    hours = svc.store.cycle_count(t0, now)
    st = svc.motion.state
    return {"axes": out, "active_code": st["code"] or None, "metric": svc.cfg["motion"].get("metric", "rms"),
            "metric_label": M.MOTION_METRICS.get(svc.cfg["motion"].get("metric", "rms")),
            "hourly": hours, "cycles_total": svc.status.get("cycles", 0),
            "codes": sorted({c for c, _a in svc.store.cycle_codes()}),
            "caution_ratio": svc.cfg["motion"]["caution_ratio"], "alert_ratio": svc.cfg["motion"]["alert_ratio"]}


def motion_trend(svc, ax: int, code: int, days: float) -> dict:
    from . import motion as M
    now = time.time()
    r = M.evaluate_code(svc.store, svc.cfg, ax, code, now)
    since = now - days * 86400
    conv = svc.conv
    pts = [{"ts": p["ts"], "v": _num(conv.feature(ax, "F", p["v"]), 4), "n": p["n"], "level": p["level"],
            "ratio": _num(p.get("ratio"), 3), "vib": _num(p.get("vib_r"), 4), "temp": p.get("temp")}
           for p in r["series"] if p["ts"] >= since]
    lines = []
    if r["baseline"]:
        mc = svc.cfg["motion"]
        for lab, k, kind in ((_("基準"), 1.0, "base"), (_("注意"), float(mc["caution_ratio"]), "caution"),
                             (_("警戒"), float(mc["alert_ratio"]), "alert")):
            lines.append({"label": lab, "v": _num(conv.feature(ax, "F", r["baseline"] * k), 4), "kind": kind,
                          "x0": max(r["reset_ts"], since)})
    events = [{"ts": e["ts"], "label": EVENT_LABELS.get(e["kind"], e["kind"]), "note": e["note"]}
              for e in svc.store.events(ax) if e["ts"] >= since]
    return {"axis": ax, "code": code, "name": r["name"], "label": M.MOTION_METRICS.get(r["metric"], r["metric"]),
            "unit": conv.unit(ax), "points": pts, "lines": lines, "events": events, "level": r["level"],
            "level_name": M.LEVEL_NAMES.get(r["level"]), "days": r["days"], "need_days": r["need_days"]}


def current_decision(svc, ax: int) -> dict:
    """系統語言直接用快取；其他語言即時重算（文字依請求語言產生，不影響快取與警報）。"""
    if get_lang() == default_lang():
        return svc.decisions.get(ax) or svc.refresh_decision(ax, raise_alarms=False)
    from .cross import decide
    return decide(svc, ax)


def current_health(svc, ax: int) -> dict:
    if get_lang() == default_lang():
        return svc.health.get(ax) or svc.refresh_health(ax)
    from .analysis import evaluate_axis, load_rows
    res = evaluate_axis(load_rows(svc.store, svc.cfg, ax, since=svc.store.last_reset(ax)), svc.cfg)
    res["axis"], res["name"] = ax, axis_cfg(svc.cfg, ax)["name"]
    return res


def health(svc) -> list[dict]:
    out = []
    for ax in svc.axes:
        h = dict(current_health(svc, ax))
        h.pop("series", None)
        conv = svc.conv
        mets = {}
        for m, d in (h.get("metrics") or {}).items():
            mets[m] = {"label": METRIC_LABELS.get(m, m), "value": _num(conv.feature(ax, m, d.get("value")), 3),
                       "base": _num(conv.feature(ax, m, d.get("base")), 3), "ratio": _num(d.get("ratio"), 3),
                       "z": _num(d.get("z"), 2), "level": d.get("level", 0), "dev_frac": _num(d.get("dev_frac"), 3)}
        h["metrics"] = mets
        h["unit"] = conv.unit(ax)
        for k in ("baseline", "baseline_std", "tcoef"):
            h.pop(k, None)
        out.append(h)
    return out


def trend_vib_iron(svc, ax: int, metric: str, days: float) -> dict:
    from .cross import iron_analysis, sev_abs, sev_ratio
    from .life import Hours
    since = time.time() - days * 86400
    cfg = svc.cfg
    resets = sorted(e["ts"] for e in svc.store.events(ax) if e["kind"] in RESET_KINDS)
    events = [{"ts": e["ts"], "label": EVENT_LABELS.get(e["kind"], e["kind"]), "note": e["note"]}
              for e in svc.store.events(ax) if e["ts"] >= since]
    pts, lines, unit, label = [], [], "", metric
    lvl = lambda sv: -1 if sv is None else (2 if sv >= 1 else 1 if sv >= 0.5 else 0)  # noqa: E731
    if metric.startswith("vib_"):
        _pfx, d, m = metric.split("_", 2)
        vc = cfg["vibration"]
        rows = [r for r in svc.store.vib(ax) if r["direction"] == d]
        n = int(vc["baseline_n"])
        bounds = [0.0, *resets, float("inf")]
        for a, b in zip(bounds[:-1], bounds[1:]):
            seg = [r for r in rows if a <= r["ts"] < b and r[m] is not None]
            if not seg:
                continue
            base = sum(r[m] for r in seg[:n]) / len(seg[:n]) if len(seg) >= n else None
            for i, r in enumerate(seg):
                if r["ts"] < since:
                    continue
                if m == "kurt":
                    sv = sev_abs(r[m], float(vc["kurt_caution"]), float(vc["kurt_alert"]), 3.0)
                else:
                    sv = sev_ratio(r[m] / base, float(vc["caution_ratio"]), float(vc["alert_ratio"])) if base and i >= n else None
                pts.append({"ts": r["ts"], "v": _num(r[m], 4), "level": lvl(sv), "temp": r["temp"]})
            if base and m != "kurt":
                x0, x1 = max(seg[0]["ts"], since), seg[-1]["ts"]
                for lab, k, kind in ((_("基準"), 1.0, "base"), (_("注意"), float(vc["caution_ratio"]), "caution"),
                                     (_("警戒"), float(vc["alert_ratio"]), "alert")):
                    lines.append({"label": lab, "v": _num(base * k, 4), "kind": kind, "x0": x0, "x1": x1})
        if m == "kurt":
            lines = [{"label": _("注意"), "v": float(vc["kurt_caution"]), "kind": "caution"},
                     {"label": _("警戒"), "v": float(vc["kurt_alert"]), "kind": "alert"}]
        unit = _("峭度") if m == "kurt" else ("gE" if m == "env" else "g")
        label = _("振動－{dir}{metric}", dir=_("徑向") if d == "radial" else _("軸向"), metric=VIB_METRICS[m])
    else:
        ia = iron_analysis(svc.store, cfg, Hours(svc.store, cfg), ax, time.time(), None)
        key = "wt" if metric == "iron_wt" else "W"
        for p in ia["points"]:
            if p["ts"] < since or p[key] is None:
                continue
            if key == "wt" and ia["caution_wt"]:
                sv = sev_abs(p["wt"], ia["caution_wt"], ia["alert_wt"], ia["caution_wt"] * 0.5)
            elif key == "W" and ia.get("W_base"):
                sv = sev_ratio(p["W"] / ia["W_base"], float(cfg["iron"]["w_caution_ratio"]), float(cfg["iron"]["w_alert_ratio"]))
            else:
                sv = None
            pts.append({"ts": p["ts"], "v": _num(p[key], 4), "level": lvl(sv), "temp": None,
                        "tag": {"interim": _("中途"), "change": _("換脂排出"), "drain_mid": _("換脂排出中段"),
                                "after": _("換脂後")}.get(p["sample"], "")})
        if key == "wt" and ia["caution_wt"]:
            lines = [{"label": _("注意"), "v": ia["caution_wt"], "kind": "caution"},
                     {"label": _("警戒"), "v": ia["alert_wt"], "kind": "alert"}]
        if key == "W" and ia.get("W_base"):
            lines = [{"label": _("基準"), "v": _num(ia["W_base"], 4), "kind": "base"},
                     {"label": _("注意"), "v": _num(ia["W_base"] * float(cfg["iron"]["w_caution_ratio"]), 4), "kind": "caution"},
                     {"label": _("警戒"), "v": _num(ia["W_base"] * float(cfg["iron"]["w_alert_ratio"]), 4), "kind": "alert"}]
        unit, label = ("wt%", _("鐵粉濃度")) if key == "wt" else ("g/1000h", _("鐵粉生成速率 W"))
    return {"axis": ax, "metric": metric, "label": label, "unit": unit, "points": pts, "lines": lines,
            "events": events}


def trend(svc, ax: int, metric: str, days: float) -> dict:
    if metric.startswith("vib_") or metric.startswith("iron_"):
        return trend_vib_iron(svc, ax, metric, days)
    from .analysis import load_rows
    since = time.time() - days * 86400
    rows = load_rows(svc.store, svc.cfg, ax, since=since)
    resets = [e["ts"] for e in svc.store.events(ax) if e["kind"] in RESET_KINDS]
    conv = svc.conv
    hc = svc.cfg["health"]
    pts, lines = [], []
    for ep in epochs(rows, resets, svc.cfg):
        base = ep["base"]
        for r in ep["rows"]:
            v = comp(r, metric, base) if metric in HIGH_METRICS else r.get(metric)
            pts.append({"ts": r["ts"], "v": _num(conv.feature(ax, metric, v), 4),
                        "level": ep["levels"].get(r["id"], -1), "temp": r["temp"]})
        if base and metric in HIGH_METRICS:
            bm = base["stats"][metric][0]
            x0, x1 = ep["start"], ep["end"]
            for label, k, kind in ((_("基準"), 1.0, "base"), (_("注意"), float(hc["caution_ratio"]), "caution"),
                                   (_("警戒"), float(hc["alert_ratio"]), "alert")):
                lines.append({"label": label, "v": _num(conv.feature(ax, metric, bm * k), 4), "kind": kind,
                              "x0": x0, "x1": x1})
    events = [{"ts": e["ts"], "label": EVENT_LABELS.get(e["kind"], e["kind"]), "note": e["note"]}
              for e in svc.store.events(ax) if e["ts"] >= since]
    return {"axis": ax, "metric": metric, "label": METRIC_LABELS.get(metric, metric),
            "unit": conv.unit(ax) + (_("/%速度") if metric == "Fv" else ""), "points": pts, "lines": lines,
            "events": events, "reset_ts": svc.store.last_reset(ax)}


def _has_features(store) -> bool:
    try:
        with store.conn() as c:
            return c.execute("SELECT 1 FROM features LIMIT 1").fetchone() is not None
    except Exception:
        return True


def shell_info(svc, auth, sess, bind_host: str, client: str, started: float) -> dict:
    """DTM 主畫面（左側選單）用的基本資訊：單機模式只有一台機器人，選單是即時監測、程式診斷、設定。"""
    from . import __version__
    cfg = svc.cfg
    act = svc.store.alarms(500, True)
    pol = SC.policy(cfg)
    lv = [h.get("level", -1) for h in svc.health.values()] or [-1]
    return {"role": "standalone", "version": __version__, "now": time.time(), "demo": bool(cfg.get("_demo")),
            "title": cfg["system"].get("display_name") or cfg["robot"]["name"], "site_id": cfg["system"].get("site_id", ""),
            "require_login": pol["require_login"], **security_info(auth, bind_host, client, cfg),
            "session": {"who": sess["who"], "role": sess["role"], "role_name": ROLE_NAMES.get(sess["role"], ""),
                        "username": sess.get("username", ""), "must_change": bool(sess.get("must_change")),
                        "idle_minutes": sess.get("idle_minutes", 0)} if sess else None,
            "alarms": {"alert": sum(1 for a in act if a["level"] >= 2), "caution": sum(1 for a in act if a["level"] == 1),
                       "unacked": sum(1 for a in act if not a.get("acked"))},
            "sites": [], "robots": 1, "refresh_s": max(10, int(cfg.get("ui", {}).get("refresh_s") or 20)),
            # 第一次設定精靈：還在用模擬器、或從來沒有連上機器人（只有本機看得到這個提示）
            "setup_hint": bool(cfg.get("_path")) and not cfg.get("_demo") and is_loopback(client) and (
                cfg["robot"].get("source") == "sim" or not (svc.status.get("samples") or _has_features(svc.store))),
            "setup_pending": bool(cfg.get("_setup_pending")),
            "robot": {"rkey": "local", "name": cfg["robot"]["name"], "model": cfg["robot"].get("model", ""),
                      "lv": max(lv), "connected": bool(svc.status.get("connected"))},
            "export_anonymize": bool((cfg.get("privacy") or {}).get("export_anonymize", True)),
            "uptime_s": time.time() - started, "sync": None, "central_url": cfg.get("central", {}).get("url", ""),
            "edge_host": ""}


def security_info(auth, bind_host: str, client: str, cfg: dict) -> dict:
    """給畫面顯示的安全狀態：唯讀、首次設定、跨網段未加密的提醒。"""
    tls = bool((cfg.get("web") or {}).get("tls_cert"))
    mode = getattr(auth, "mode", "password" if auth.enabled else "readonly")
    return {"write_auth": auth.enabled, "auth_mode": mode,
            "local_only": bind_host in ("127.0.0.1", "::1", "localhost"),
            "setup_needed": bool(getattr(auth, "setup_needed", False)) and mode != "accounts",
            "setup_local": is_loopback(client), "tls": tls,
            "tls_warn": (not tls) and bind_host not in ("127.0.0.1", "::1", "localhost")}


def first_admin(auth, addr: str, b: dict, audit) -> tuple[dict, int]:
    """首次設定：只接受本機連線、只在還沒有任何帳號時，建立第一個管理員並直接登入。"""
    if not is_loopback(addr):
        audit("", addr, "/api/setup", False, _("非本機連線"))
        return {"error": _("第一個管理員帳號只能在這台電腦本機建立（http://127.0.0.1）")}, 403
    if not isinstance(auth, Auth) or not auth.setup_needed:
        return {"error": _("已經有帳號了，請用管理員登入後再新增")}, 400
    username = str(b.get("username", "")).strip()
    display = " ".join(str(b.get("display", "")).split())[:30]
    try:
        auth.create_first_admin(username, str(b.get("password", "")), display)
    except ValueError as e:
        return {"error": str(e)}, 400
    token, err, code = auth.login(addr, username, str(b.get("password", "")), username=username)
    audit(display or username, addr, "/api/setup", bool(token), _("建立第一個管理員 {u}", u=username))
    if not token:
        return {"error": err}, code
    sess = auth.session(token)
    return {"ok": True, "token": token, "who": sess["who"], "role": sess["role"],
            "role_name": ROLE_NAMES.get(sess["role"], ""), "hours": round(auth.ttl / 3600, 1)}, 200


def tls_wrap(httpd, cfg: dict):
    """設定了 [web] tls_cert／tls_key 就改用 HTTPS（憑證向資訊單位申請，或用公司的內部 CA 簽發）。"""
    w = cfg.get("web", {})
    if not w.get("tls_cert"):
        return httpd
    import ssl
    from .runtime import resolve
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(resolve(cfg, w["tls_cert"]), resolve(cfg, w.get("tls_key") or None) if w.get("tls_key") else None)
    # 交握延到各連線自己的執行緒（第一次讀取時）才做：慢速或惡意的連線不會擋住其他人連線
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    return httpd


class Server(ThreadingHTTPServer):
    """連線佇列放大（預設只有 5）：中央同時收很多台上傳、或多人同時開畫面時，不會被作業系統直接拒絕連線。"""
    request_queue_size = 256
    daemon_threads = True


def serve(svc, host: str, port: int, auth: WriteAuth | None = None) -> ThreadingHTTPServer:
    httpd = Server((host, port), make_handler(svc, auth))
    httpd.daemon_threads = True
    return tls_wrap(httpd, svc.cfg)

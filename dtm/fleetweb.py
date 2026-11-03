"""中央／廠區儀表板的網頁伺服器（只用標準函式庫）。

- 中央伺服器（role = "central"）：/api/sync 接收各邊緣主機上傳；/ 是全廠 8 個畫面。
- 邊緣主機（role = "edge"）：/ 是本廠的同一套畫面（資料來自本機廠區資料庫，斷網也能看）。
- 2.1：每台機器人的即時監測與程式診斷在同一個視窗裡開（/r/<機器人>/… 由這裡轉送到那台機器人的程式；
  同一台主機共用登入狀態）。中央伺服器轉送到邊緣主機時只能檢視（登入狀態不跨主機）。
寫入（確認警報、登錄事件、排程、備品…）一律要登入，權限依角色（見 auth.py）。
在中央登錄的事件會排入指令，下次邊緣同步時在那台機器人上執行；在邊緣主機登錄則立即執行。
"""
from __future__ import annotations

import csv
import gzip
import html
import http.client
import io
import json
import logging
import os
import re
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import fleetviews as V
from . import runtime as R
from . import sysconf as SC
from . import sysweb as SW
from .auth import ROLE_NAMES, Auth, need_role
from .central import check_token, jload
from .config import EVENT_LABELS
from .i18n import FONT_STACK, HTML_LANG, _, default_lang, get_lang, norm, ui_messages, use, visible_langs
from .web import VENDOR, Server, clean, first_admin, security_info, tls_wrap

log = logging.getLogger("dtm.fleetweb")
STATIC = os.path.join(os.path.dirname(__file__), "static")
PUBLIC_GET = {"/", "/index.html", "/central.html", "/favicon.ico", "/api/c/info", "/api/c/i18n", "/api/sys/me", "/api/sys/tls/cert.crt",
              "/vendor/html2canvas.min.js"}
PROXY_RE = re.compile(r"^/r/([^/]+)(/.*)?$")
MAX_SYNC_BYTES = 200 * 1024 * 1024


def make_fleet_handler(cfg: dict, cs, role: str = "central", notifier=None, supervisor=None, edge_store=None,
                       sync=None, auth: Auth | None = None):
    auth = auth or Auth(cfg)
    started = time.time()
    SC.get(cfg).start_watch()
    internal = getattr(supervisor, "token", "") or cfg.get("_demo_live_token", "")

    def backup_now() -> str:
        from . import backup as B
        return B.backup_now(dict(cfg, storage=dict(cfg["storage"], db=cs.path)))

    def proxy_target(rkey: str):
        """這台機器人的即時監測在哪裡：(主機, 連接埠, 同一台主機？, 名稱)；找不到回傳 None。"""
        demo = cfg.get("_demo_live") or {}
        if rkey in demo:
            return "127.0.0.1", int(demo[rkey]), True, rkey
        rid = rkey.split("/", 1)[1] if "/" in rkey else rkey
        if role == "edge" and supervisor is not None:
            for ch in supervisor.children:
                if ch.id == rid:
                    return "127.0.0.1", int(ch.port), True, ch.cfg["robot"]["name"]
            return None
        if role == "central":
            r = cs.one("SELECT r.site_id, r.port, r.name, s.info FROM robots r LEFT JOIN sites s ON s.site_id=r.site_id "
                       "WHERE r.rkey=?", (rkey,))
            host = (jload(r["info"], {}) or {}).get("edge_host") if r else ""
            if r and host and r["port"]:
                return str(host), int(r["port"]), False, r["name"]
        return None

    def dispatch(rkey: str, kind: str, payload: dict, who: str) -> dict:
        """事件、鐵粉、確認… 送到那台機器人：中央排入指令；邊緣主機立即透過機器人程式的內部 API 執行。"""
        if role == "central":
            return cs.command(rkey, kind, payload, who)
        if sync is None:
            raise ValueError(_("這台主機無法把指令送到機器人"))
        rid = rkey.split("/", 1)[1] if "/" in rkey else rkey
        res = sync.run_command({"id": None, "robot_id": rid, "kind": kind, "payload": payload, "who": who})
        if not res.get("ok"):
            raise ValueError(res.get("detail") or _("執行失敗"))
        return res

    def host_info() -> dict:
        from . import __version__
        from .watchdog import watchdog_summary
        info = {"role": role, "version": __version__, "started": started, "uptime_s": time.time() - started,
                "pid": os.getpid(), "disk": R.disk_info(cs.path), "watchdog": watchdog_summary(cfg),
                "db_mb": round(os.path.getsize(cs.path) / 1e6, 1) if os.path.exists(cs.path) else None,
                "host": R.host_stats()}
        if supervisor is not None:
            info["supervisor"] = R.read_json(supervisor.status_path, {}) or {}
        if sync is not None:
            info["sync"] = sync.state.st
            info["central_url"] = cfg["central"].get("url", "")
        return info

    class H(BaseHTTPRequestHandler):
        timeout = 120          # 連上卻不送資料的連線（含 HTTPS 交握）最多佔用 2 分鐘，不會卡住伺服器

        def log_message(self, fmt, *args):
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
            return SW.Ctx(cfg=cfg, auth=auth, role=role, addr=self._addr(), token=self._token(),
                          session=sess if sess is not None else self._session(), audit_add=cs.add_audit,
                          audit_conn=cs.conn, db_path=os.path.abspath(cs.path), extra_info=host_info,
                          backup_fn=backup_now, backup_cfg=dict(cfg, storage=dict(cfg["storage"], db=cs.path)),
                          notifier=notifier, started=started)

        def _sms_report(self, q, method):
            """簡訊商的送達回呼（不需要登入、不受允許連線清單限制；用 [notify.sms.report] key 把關）。"""
            from .notify import handle_report, report_params
            body = b""
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(min(n, 1_000_000)) if n else b""
            code, txt = handle_report(cfg, cs, report_params(q, body, self.headers.get("Content-Type", "")))
            return self._send(code, txt.encode(), "text/plain; charset=utf-8")

        def _blocked(self) -> bool:
            addr = self._addr()
            if SC.ip_allowed(addr, SC.policy(cfg)["allow_ips"]) or self.path.startswith("/api/sync"):
                return False                       # 邊緣上傳用金鑰把關，不受限
            self._send(403, (_("這台電腦（{ip}）不在允許連線的清單裡，請洽系統管理員。", ip=addr)).encode(),
                       "text/plain; charset=utf-8")
            return True

        def _proxy(self, method: str, u, body: bytes = b""):
            """/r/<機器人>/… → 那台機器人的程式（即時監測、程式診斷）。"""
            m = PROXY_RE.match(u.path)
            rkey = unquote(m.group(1))
            rest = m.group(2) or "/"
            if rest == "/":
                rest = "/live"
            page = not rest.startswith("/api/") and method == "GET"
            if auth.require_login and not self._session() and rest not in ("/live", "/pro", "/index.html", "/pro.html",
                                                                            "/api/i18n", "/api/about"):
                return self._json({"error": _("需要登入才能查看"), "auth": "required"}, 401)
            tgt = proxy_target(rkey)
            if not tgt:
                return self._proxy_err(page, _("這台機器人沒有即時監測（只有接在這台主機上的機器人才有）"), 404)
            host, port, local, name = tgt
            st = cfg.get("_demo_live_state")
            if st is not None and rkey in (cfg.get("_demo_live") or {}) and not st.get("ready"):
                return self._proxy_err(page, _("演示的即時監測準備中：{s}。請稍候再重新整理。", s=st.get("step") or ""), 503)
            hdr = {"Content-Type": self.headers.get("Content-Type", "application/json"),
                   "User-Agent": self.headers.get("User-Agent", ""), "X-DTM-Proxy": "1"}
            if local:                               # 同一台主機：帶登入狀態與原始電腦 IP
                hdr.update({"X-DTM-Token": self._token(), "X-DTM-Client": self._addr(), "X-DTM-Internal": internal})
            path = rest + ("?" + u.query if u.query else "")

            def fetch(tls: bool):
                if tls:     # 對方開了 HTTPS（自簽憑證）：仍經過加密，但不驗憑證（內部轉送用，憑證由對方主機自己管理）
                    import ssl
                    conn = http.client.HTTPSConnection(host, port, timeout=8, context=ssl._create_unverified_context())
                else:
                    conn = http.client.HTTPConnection(host, port, timeout=15 if local else 8)
                try:
                    conn.request(method, path, body=body or None, headers=hdr)
                    r = conn.getresponse()
                    return r, r.read()
                finally:
                    conn.close()
            try:
                try:
                    r, data = fetch(False)
                except (http.client.HTTPException, ConnectionResetError):
                    if local:
                        raise
                    r, data = fetch(True)               # 遠端主機改用 HTTPS 後，用 http:// 連會被斷線
            except (OSError, http.client.HTTPException) as e:
                return self._proxy_err(page, _("連不到 {name} 的監測程式（{host}:{port}）：{e}", name=name, host=host,
                                               port=port, e=e or type(e).__name__), 502)
            extra = {k: v for k, v in r.getheaders() if k.lower() in ("content-disposition",)}
            return self._send(r.status, data, r.getheader("Content-Type", "application/octet-stream"), extra)

        def _proxy_err(self, page: bool, msg: str, code: int):
            if not page:
                return self._json({"error": msg}, code)
            body = (f'<!doctype html><html><head><meta charset="utf-8"><style>body{{margin:0;background:#0F161C;'
                    f'color:#C9D3DB;font:14px system-ui,sans-serif;display:flex;align-items:center;justify-content:center;'
                    f'height:100vh}}div{{max-width:560px;padding:24px;border:1px solid #2A3540;border-radius:12px;'
                    f'background:#151E25;line-height:1.7}}</style></head><body><div>{html.escape(msg)}</div></body></html>')
            return self._send(code, body.encode(), "text/html; charset=utf-8")

        def _json(self, obj, code=200):
            self._send(code, json.dumps(clean(obj), ensure_ascii=False, default=str).encode(),
                       "application/json; charset=utf-8")

        def _raw(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_SYNC_BYTES:
                raise ValueError(_("上傳資料太大"))
            raw = self.rfile.read(n) if n else b""
            if self.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            return raw

        def _session(self):
            return auth.session(self.headers.get("X-DTM-Token", ""))

        # -------------------------------------------------------- GET
        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/api/sms/report":
                return self._sms_report(q, "GET")
            if self._blocked():
                return
            if PROXY_RE.match(u.path):
                return self._proxy("GET", u)
            with use(norm(q.get("lang")) or default_lang()):
                try:
                    return self._get(u, q)
                except ValueError as e:
                    return self._json({"error": str(e)}, 404)
                except Exception as e:
                    log.exception("GET %s", u.path)
                    return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def _get(self, u, q):
            p = u.path
            if auth.require_login and p not in PUBLIC_GET and not self._session():
                return self._json({"error": _("需要登入才能查看"), "auth": "required"}, 401)
            if p in VENDOR:
                with open(os.path.join(STATIC, VENDOR[p][0]), "rb") as f:
                    return self._send(200, f.read(), VENDOR[p][1])
            if p in ("/", "/index.html", "/central.html"):
                with open(os.path.join(STATIC, "central.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if p.startswith("/api/sys/"):
                r = SW.get(self._ctx(), p, q)
                if len(r) == 3:
                    return self._send(200, r[0], r[1], r[2])
                return self._json(*r)
            if p == "/api/c/i18n":
                lang = get_lang()
                return self._json({"lang": lang, "default": default_lang(), "langs": visible_langs((lang, default_lang())),
                                   "html_lang": HTML_LANG[lang], "font": FONT_STACK[lang], "messages": ui_messages(lang)})
            if p == "/api/c/info":
                from . import __version__
                sess = self._session()
                act = cs.q("SELECT level, acked FROM alarms WHERE cleared_ts IS NULL")
                sites = cs.q("SELECT site_id, name, last_seen FROM sites ORDER BY site_id")
                timeout = float(cfg["central"].get("edge_timeout_s", 300))
                return self._json({
                    "role": role, "version": __version__, "now": time.time(), "demo": V.demo_flag(cs),
                    "title": cfg["system"].get("display_name") or cfg["system"].get("site_name")
                    or (_("中央監控") if role == "central" else _("廠區監控")),
                    "display_name": cfg["system"].get("display_name") or "",
                    "site_id": cfg["system"].get("site_id", ""), "require_login": auth.require_login,
                    **security_info(auth, self.server.server_address[0], self._addr(), cfg),
                    "session": {"who": sess["who"], "role": sess["role"], "role_name": ROLE_NAMES.get(sess["role"], ""),
                                "username": sess.get("username", ""), "must_change": bool(sess.get("must_change")),
                                "idle_minutes": sess.get("idle_minutes", 0)} if sess else None,
                    "export_anonymize": bool((cfg.get("privacy") or {}).get("export_anonymize", True)),
                    "live": {"mode": "local" if role == "edge" else ("remote" if role == "central" else ""),
                             "demo": sorted((cfg.get("_demo_live") or {}).keys())},
                    "alarms": {"alert": sum(1 for a in act if a["level"] >= 2), "caution": sum(1 for a in act if a["level"] == 1),
                               "unacked": sum(1 for a in act if not a["acked"])},
                    "sites": [{"site": s["site_id"], "name": s["name"] or s["site_id"],
                               "online": time.time() - float(s["last_seen"] or 0) < timeout} for s in sites],
                    "robots": (cs.one("SELECT COUNT(*) AS n FROM robots") or {}).get("n", 0),
                    "refresh_s": max(10, int(cfg.get("ui", {}).get("refresh_s") or 0)) if (cfg.get("ui") or {}).get("refresh_s")
                    else (60 if role == "central" else 20),
                    "sync": sync.state.st if sync is not None else None,
                    "central_url": cfg["central"].get("url", "") if role == "edge" else "",
                    "edge_host": cfg["web"].get("public_host", "")})
            if p == "/api/c/robots":
                return self._json(V.robot_list(cs))
            if p == "/api/c/overview":
                return self._json(V.overview(cs))
            if p == "/api/c/plant":
                return self._json(V.plant(cs, q.get("site")))
            if p == "/api/c/robot":
                return self._json(V.robot(cs, q.get("rkey", ""), int(q["axis"]) if q.get("axis") else None,
                                          int(q.get("days", 180))))
            if p == "/api/c/risk":
                return self._json(V.risk(cs, horizon=int(q.get("horizon", 180))))
            if p == "/api/c/alerts":
                d = V.alerts(cs, q.get("tab", "active"), q.get("site", ""), q.get("level", ""),
                             int(q.get("limit", 200)), notifier)
                d["duty"] = [{"name": x.get("name", ""), "source": x.get("source", "")} for x in (d.get("duty") or [])]
                return self._json(d)
            if p == "/api/c/maint":
                return self._json(V.maint(cs, kind=q.get("kind", "")))
            if p == "/api/c/life":
                return self._json(V.life(cs, int(q.get("axis", 2))))
            if p in ("/api/c/validation", "/api/c/validation.html"):        # 現場驗證報告
                from . import validation as VA
                r = VA.for_central(cs, max(30.0, min(3650.0, float(q.get("days", 730)))))
                if p.endswith(".html"):
                    scope = cfg["system"].get("display_name") or cfg["system"].get("site_name") or ""
                    return self._send(200, VA.build_html(r, scope).encode("utf-8"), "text/html; charset=utf-8")
                return self._json(r)
            if p == "/api/c/system":
                d = V.system(cs, role=role, host_info=host_info())
                return self._json(d)
            if p == "/api/c/about":
                from .about import info
                return self._json(info())
            if p == "/api/c/teardown/options":
                from . import teardown as TD
                return self._json(TD.options())
            if p == "/api/c/export/download":
                from . import export as EX
                fp = EX.take_download(q.get("t", ""))
                if not fp:
                    return self._json({"error": _("下載連結已失效（10 分鐘內有效、只能下載一次），請重新匯出")}, 404)
                with open(fp, "rb") as f:
                    body = f.read()
                return self._send(200, body, "application/zip",
                                  {"Content-Disposition": f"attachment; filename={os.path.basename(fp)}"})
            if p == "/api/c/export.csv":
                body, name = export_csv(cs, q.get("view", "plant"), q)
                return self._send(200, body, "text/csv; charset=utf-8",
                                  {"Content-Disposition": f"attachment; filename={name}"})
            if p == "/workorder":
                from . import workpack as wp
                ax = int(q.get("axis", 3))
                comp = q.get("comp") or "reducer"
                w = wp.get(cfg, ax, comp)
                if not w:
                    return self._send(404, ("<p>" + _("尚未建立作業包。") + "</p>").encode(), "text/html; charset=utf-8")
                c2 = dict(cfg, robot=dict(cfg["robot"]))
                dec = None
                if q.get("rkey"):
                    r = cs.one("SELECT r.name, s.name AS sname FROM robots r LEFT JOIN sites s ON s.site_id=r.site_id "
                               "WHERE r.rkey=?", (q["rkey"],))
                    if r:
                        c2["robot"]["name"] = r["name"] if str(r["name"]).startswith(r["sname"] or "\0") else \
                            f"{r['sname']}-{r['name']}"
                    a = cs.one("SELECT name, decision FROM axis_state WHERE rkey=? AND axis=?", (q["rkey"], ax))
                    if a:
                        dec = dict(jload(a["decision"], {}) or {}, name=a["name"])
                key = {"grease": "grease", "motor": "motor"}.get(comp, "drive")
                return self._send(200, wp.render_workorder(c2, w, dec, key).encode(), "text/html; charset=utf-8")
            return self._json({"error": "not found"}, 404)

        # -------------------------------------------------------- POST
        def do_POST(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/api/sms/report":
                return self._sms_report(q, "POST")
            if self._blocked():
                return
            if PROXY_RE.match(u.path):
                n = int(self.headers.get("Content-Length") or 0)
                if n > 60_000_000:
                    return self._json({"error": _("檔案太大")}, 413)
                return self._proxy("POST", u, self.rfile.read(n) if n else b"")
            with use(norm(q.get("lang")) or default_lang()):
                try:
                    return self._post(u)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:
                    log.exception("POST %s", u.path)
                    return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def _post(self, u):
            p = u.path
            addr = self._addr()
            if p == "/api/sync":
                return self._sync(addr)
            b = json.loads(self._raw() or b"{}")
            if p == "/api/login":
                who = " ".join(str(b.get("who", "") or b.get("username", "")).split())[:30]
                token, err, code = auth.login(addr, who, str(b.get("password", "")), username=str(b.get("username", "") or ""),
                                              agent=self.headers.get("User-Agent", ""))
                sess = auth.session(token) if token else None
                cs.add_audit((sess or {}).get("who") or who, addr, "login", bool(token), err)
                if not token:
                    return self._json({"error": err}, code)
                return self._json({"ok": True, "token": token, "who": sess["who"], "role": sess["role"],
                                   "role_name": ROLE_NAMES.get(sess["role"], ""), "must_change": bool(sess.get("must_change")),
                                   "hours": round(auth.ttl / 3600, 1)})
            if p == "/api/logout":
                old = self._session()
                auth.logout(self._token())
                if old:
                    cs.add_audit(old.get("who", ""), addr, "logout", True, str(b.get("reason") or ""))
                return self._json({"ok": True})
            if p == "/api/setup":
                return self._json(*first_admin(auth, addr, b, cs.add_audit))
            if auth.mode == "readonly":
                cs.add_audit("", addr, p, False, _("唯讀模式"))
                return self._json({"error": _("目前是唯讀模式：請在這台電腦上開啟畫面，建立第一個管理員帳號"),
                                   "auth": "setup"}, 403)
            sess = self._session()
            if sess is None and auth.enabled:
                cs.add_audit("", addr, p, False, _("未登入"))
                return self._json({"error": _("需要登入才能修改資料"), "auth": "required"}, 401)
            if not auth.can(sess, p):
                need = need_role(p)
                cs.add_audit((sess or {}).get("who", ""), addr, p, False, _("權限不足"))
                return self._json({"error": _("權限不足：這個動作需要「{role}」以上", role=ROLE_NAMES.get(need, need)),
                                   "auth": "forbidden"}, 403)
            who = (sess or {}).get("who") or ""
            if p == "/api/sys/ping":
                auth.touch(self._token())
                return self._json({"ok": True})
            auth.touch(self._token())
            if p.startswith("/api/sys/"):
                obj, code = SW.post(self._ctx(sess), p, b, who)
            else:
                try:
                    obj, code = self._write(p, b, who)
                except Exception as e:
                    cs.add_audit(who, addr, p, False, str(e))
                    raise
            if code != 404:
                cs.add_audit(who, addr, p, code == 200, SW.audit_detail(p, b) if code == 200
                             else str(obj.get("error", "")))
            return self._json(obj, code)

        def _sync(self, addr: str):
            if role != "central":
                return self._json({"ok": False, "error": _("這台不是中央伺服器")}, 404)
            raw = self._raw()
            payload = json.loads(raw or b"{}")
            sid = str((payload.get("site") or {}).get("site_id") or "")
            tok = self.headers.get("Authorization", "")
            tok = tok[7:] if tok.lower().startswith("bearer ") else ""
            if not sid or not check_token(cfg, sid, tok):
                cs.add_audit(sid, addr, "/api/sync", False, _("金鑰錯誤"))
                return self._json({"ok": False, "error": _("金鑰錯誤或未設定（[central] token／tokens）")}, 403)
            if payload.get("rebuild"):                      # 機器人端「從中央重建」：只給自己廠區的機器人
                rkey = str(payload["rebuild"].get("robot") or "")
                if not rkey.startswith(sid + "/"):
                    cs.add_audit(sid, addr, "rebuild", False, rkey)
                    return self._json({"ok": False, "error": _("只能取回自己廠區的機器人資料")}, 403)
                try:
                    exp = cs.export_robot(rkey)
                except ValueError as e:
                    return self._json({"ok": False, "error": str(e)}, 404)
                cs.add_audit(sid, addr, "rebuild", True, rkey)
                return self._json({"ok": True, "rebuild": exp})
            res = cs.ingest(payload, notify=True)
            return self._json(res)

        def _write(self, p: str, b: dict, who: str) -> tuple:
            ok = {"ok": True}
            if p == "/api/c/ack":
                a = cs.ack(int(b["id"]), who, dispatch if role == "edge" else None)
                return {"ok": True, "alarm": a}, 200
            if p == "/api/c/event":
                kind = b.get("kind", "other")
                if kind not in EVENT_LABELS:
                    return {"error": _("未知事件類別")}, 400
                pl = {"axis": b.get("axis"), "kind": kind, "note": str(b.get("note", ""))[:500],
                      "failed": bool(b.get("failed")), "teardown": str(b.get("teardown", ""))[:1000],
                      "hours": b.get("hours"), "ts": float(b.get("ts") or time.time()), "code": b.get("code")}
                return dispatch(str(b["rkey"]), "event", pl, who), 200
            if p == "/api/c/replay":
                import shutil
                import tempfile
                from . import replay as RP
                from .restore import build_from_export
                from .store import Store
                over = b.get("sets") if isinstance(b.get("sets"), dict) else RP.parse_sets(b.get("set") or [])
                if not over:
                    return {"error": _("請指定要試算的設定")}, 400
                days = min(1095.0, float(b.get("days") or 365))
                tmp = tempfile.mkdtemp(prefix="dtm_replay_")
                try:
                    db = build_from_export(cs.export_robot(str(b["rkey"])), os.path.join(tmp, "r.sqlite3"))
                    r = cs.one("SELECT axes FROM robots WHERE rkey=?", (str(b["rkey"]),)) or {}
                    rcfg = RP.with_overrides(cfg, {"axes": {"enabled": jload(r.get("axes"), []) or [2, 3]}})
                    res = RP.run(Store(db), rcfg, over, since=time.time() - days * 86400)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
                return {"ok": True, "rows": RP.summary(res), "note": _("用中央保存的資料與中央的設定試算，數字是近似值；"
                                                                      "精確值請在那台機器人上執行 replay 指令")}, 200
            if p == "/api/c/downtime":
                pl = {"cancel": True} if b.get("cancel") else {"until": b.get("until"), "note": str(b.get("note", ""))[:200]}
                if not b.get("cancel"):
                    from . import downtime as DT
                    DT.parse_until(b.get("until"))           # 先檢查格式
                return dispatch(str(b["rkey"]), "downtime", pl, who), 200
            if p == "/api/c/teardown":
                from . import teardown as TD
                form = {k: b.get(k) for k in ("location", "modes", "grease", "note", "no_fault")}
                TD.normalize(cfg, form, who)                  # 先在中央檢查一次（照片只能在機器人儀表板上傳）
                return dispatch(str(b["rkey"]), "teardown", {"edge_id": int(b["edge_id"]), "form": form}, who), 200
            if p == "/api/c/export/analysis":
                from . import export as EX
                rkey = str(b["rkey"])
                out = os.path.join(EX.exports_dir(cfg), EX.default_name(rkey, b.get("since"), b.get("until")))
                res = EX.export_central(cs, rkey, out, b.get("since"), b.get("until"), bool(b.get("anonymize")))
                res["url"] = "/api/c/export/download?t=" + EX.issue_download(out)
                res["name"] = os.path.basename(out)
                res.pop("path", None)
                return res, 200
            if p == "/api/c/undo":
                return dispatch(str(b["rkey"]), "undo", {"edge_id": int(b["edge_id"]), "note": str(b.get("note", ""))[:300]},
                                who), 200
            if p == "/api/c/iron":
                wt = float(b["wt_pct"])
                if not 0 <= wt < 20:
                    return {"error": _("鐵粉濃度請以 wt% 輸入（例如 0.045）")}, 400
                return dispatch(str(b["rkey"]), "iron", {"axis": int(b["axis"]), "wt_pct": wt,
                                                         "sample": b.get("sample") or cfg["iron"].get("default_sample", "drain_mid"),
                                                         "note": str(b.get("note", ""))[:300],
                                                         "ts": float(b.get("ts") or time.time())}, who), 200
            if p == "/api/c/job":
                j = dict(b)
                if j.get("date") and not j.get("ts"):
                    j["ts"] = datetime.strptime(str(j["date"])[:10], "%Y-%m-%d").timestamp() + 8 * 3600
                if not j.get("id") and (not j.get("ts") or not j.get("kind")):
                    return {"error": _("請填日期與保養類型")}, 400
                return {"ok": True, "id": cs.save_job(j, who)}, 200
            if p == "/api/c/job/done":
                return {"ok": True, **cs.job_done(int(b["id"]), who, bool(b.get("failed")), str(b.get("note", ""))[:500],
                                                  float(b["hours"]) if b.get("hours") not in (None, "") else None,
                                                  dispatch=dispatch if role == "edge" else None)}, 200
            if p == "/api/c/job/prep":
                cs.save_job({"id": int(b["id"]), "prep": b.get("prep") or {}}, who)
                return ok, 200
            if p == "/api/c/spare":
                return {"ok": True, "id": cs.save_spare(b)}, 200
            if p == "/api/c/notify/test":
                if V.demo_flag(cs):
                    return {"error": _("演示資料模式不會真的發送通知")}, 400
                if not notifier:
                    return {"error": _("通知未啟用")}, 400
                return {"ok": True, "results": notifier.test(b.get("channel", "all"), str(b.get("text", ""))[:200])}, 200
            if p == "/api/c/maintenance":
                from .watchdog import clear_maintenance, set_maintenance
                if b.get("on"):
                    return {"ok": True, "maintenance": set_maintenance(cfg, float(b.get("minutes") or 60),
                                                                       str(b.get("reason", ""))[:200], who)}, 200
                clear_maintenance(cfg)
                return ok, 200
            return {"error": "not found"}, 404

    return H


# ------------------------------------------------------------ 匯出
def export_csv(cs, view: str, q: dict) -> tuple[bytes, str]:
    buf = io.StringIO()
    w = csv.writer(buf)
    stamp = datetime.now().strftime("%Y%m%d")
    if view == "plant":
        d = V.plant(cs, q.get("site"))
        w.writerow([_("機器人"), _("產線")] + d.get("axis_labels", []) + [_("運轉時數"), _("上次換脂")])
        for r in d.get("rows", []):
            w.writerow([r["disp"], r["line"]] + [("" if c.get("lv") is None else (c.get("ratio") if c.get("ratio") is not None
                                                                                else V.LV.get(c["lv"]))) for c in r["cells"]]
                       + [r["hours"] or "", datetime.fromtimestamp(r["grease_ts"]).strftime("%Y-%m-%d") if r["grease_ts"] else ""])
        name = f"plant_{d.get('site') or ''}_{stamp}.csv"
    elif view == "risk":
        d = V.risk(cs)
        w.writerow(["#", _("機器人"), _("軸"), _("狀態"), _("F 比值"), "z", _("預估達警戒（天）"), _("範圍（天）"), _("影響度"),
                    _("建議動作")])
        for r in d["rows"]:
            w.writerow([r["rank"], r["robot"], r["label"], r["lv_name"], r["ratio"], r["z"], r["eta"],
                        f"{r['lo']}–{r['hi']}" if r["lo"] is not None else "", r["impact"], r["action"]])
        name = f"risk_{stamp}.csv"
    elif view == "maint":
        d = V.maint(cs)
        w.writerow([_("日期"), _("廠區"), _("機器人"), _("軸"), _("類型"), _("工時"), _("狀態"), _("內容")])
        for lane in d["lanes"]:
            for j in lane["jobs"]:
                w.writerow([datetime.fromtimestamp(j["ts"]).strftime("%Y-%m-%d"), lane["name"], j["robot"], j["axis_label"],
                            j["kind_label"], j["hours"] or "", j["status"], j["title"]])
        name = f"maintenance_{stamp}.csv"
    elif view == "life":
        d = V.life(cs, int(q.get("axis", 2)))
        w.writerow([_("軸"), _("更換"), _("樣本"), "β", "η (h)", "B10 (h)", _("平均間隔 (h)"), _("可信度"), _("未來 12 個月預估")])
        for t in d["table"]:
            w.writerow([t["label"], t["failures"], t["n"], t["beta"], t["eta_h"], t["b10_h"], t["mean_h"], t["conf"], t["next12"]])
        name = f"life_{stamp}.csv"
    else:
        d = V.alerts(cs, q.get("tab", "all"))
        w.writerow([_("時間"), _("等級"), _("來源"), _("軸"), _("內容"), _("確認"), _("解除")])
        for a in d["rows"]:
            w.writerow([datetime.fromtimestamp(a["ts"]).strftime("%Y-%m-%d %H:%M"), V.LV.get(a["level"]), a["src"],
                        a["axis_label"], a["message"], a["acked_by"] or "",
                        datetime.fromtimestamp(a["cleared_ts"]).strftime("%Y-%m-%d %H:%M") if a["cleared_ts"] else ""])
        name = f"alarms_{stamp}.csv"
    return ("﻿" + buf.getvalue()).encode("utf-8"), name


def serve_fleet(cfg: dict, cs, role: str = "central", notifier=None, supervisor=None, edge_store=None, sync=None,
                auth: Auth | None = None, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    httpd = Server((host or cfg["web"].get("host", "0.0.0.0"), int(port if port is not None else cfg["web"]["port"])),
                                make_fleet_handler(cfg, cs, role, notifier, supervisor, edge_store, sync, auth))
    httpd.daemon_threads = True
    return tls_wrap(httpd, cfg)

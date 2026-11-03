"""Pro 網頁 API（/api/pro/…）與演示伺服器。讀取不需權限；寫入由主程式的權限檢查把關（工程師以上，刪除限管理員）。"""
from __future__ import annotations

import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import runtime as R
from ..i18n import FONT_STACK, HTML_LANG, _, default_lang, get_lang, norm, ui_messages, use, visible_langs
from . import accel, impacts, lsparse, report, slack, suggest, sweep, views
from .store import ROLE_LABELS, ROLES, Session, local_meta

log = logging.getLogger("dtm.pro.web")
STATIC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
MAX_TEXT = 50_000_000


def _robot(cfg: dict) -> str:
    return R.robot_id(cfg)


def session_list(cfg: dict, all_: bool = False) -> list[dict]:
    rid = _robot(cfg)
    out = []
    for m in map(local_meta, Session.list(cfg)):
        if not all_ and not cfg.get("_pro_demo") and m.get("robot") != rid:
            continue
        d = {k: m.get(k) for k in ("id", "robot", "role", "label", "group", "source", "status", "created", "closed",
                                   "t0", "t1", "n_cycles", "counts", "note", "change", "cycles_per_day", "hours_per_day",
                                   "axes", "torque", "torque_unit")}
        d["program"] = m.get("program", {})
        d["role_label"] = ROLE_LABELS.get(m.get("role"), m.get("role"))
        out.append(d)
    return out


def _open(cfg: dict, q: dict) -> Session:
    sid = q.get("id") or ""
    try:
        return Session.open(cfg, sid)
    except KeyError:
        raise ValueError(_("找不到診斷期 {id}", id=sid))


def program_view(sess: Session) -> dict:
    prog = sess.program()
    eff = lsparse.effective_terms(prog)
    return {"name": prog.name, "comment": prog.comment, "version": prog.version, "warnings": prog.warnings,
            "main_path": prog.main_path,
            "lines": [{"no": ln.no, "text": ln.text, "kind": ln.kind, "motion": ln.is_motion, "eff": eff.get(ln.no)}
                      for ln in prog.lines]}


def get(cfg: dict, path: str, q: dict):
    """回傳 dict（JSON）或 (bytes, content-type, 額外標頭)。"""
    p = path[len("/api/pro/"):]
    if p == "sessions":
        return {"sessions": session_list(cfg, q.get("all") == "1"), "robot": _robot(cfg), "roles": list(ROLES),
                "role_labels": {k: ROLE_LABELS[k] for k in ROLES}, "demo": bool(cfg.get("_pro_demo")),
                "line_reg": cfg["pro"]["line_reg"], "sample_hz": cfg["pro"]["sample_hz"]}
    if p == "sweeps":
        return {"items": [{"robot": r, "ax": a, "n": n} for r, a, n in sweep.robots_axes(cfg)]}
    if p == "sweep":
        return sweep.analyze(cfg, q.get("robot") or _robot(cfg), int(q.get("ax") or 3), q.get("sid") or None)
    if p in ("compare", "compare/report"):
        ids = {k: [x for x in (q.get(k) or "").split(",") if x] for k in ("before", "after", "ctrl_before", "ctrl_after")}
        res, ss = report.compare_sessions(cfg, ids)
        try:
            if p == "compare":
                return res
            return report.compare_report(cfg, res, ss).encode(), "text/html; charset=utf-8", {}
        finally:
            for lst in ss.values():
                for s in lst:
                    s.close()
    sess = _open(cfg, q)
    try:
        if p == "summary":
            return views.cached(sess, "summary", lambda: views.summary(cfg, sess))
        if p == "program":
            return program_view(sess)
        if p == "timeline":
            ax = int(q.get("ax") or (sess.meta.get("axes") or [1])[0])
            return views.cached(sess, f"timeline{ax}", lambda: views.timeline(cfg, sess, ax))
        if p == "angle":
            ax = int(q.get("ax") or (sess.meta.get("axes") or [1])[0])
            return views.cached(sess, f"angle{ax}", lambda: views.angle_map(cfg, sess, ax))
        if p == "slack":
            return views.cached(sess, "slack", lambda: slack.analyze(cfg, sess))
        if p == "suggest":
            return views.cached(sess, "suggest", lambda: suggest.build(cfg, sess))
        if p == "accel":
            return views.cached(sess, "accel", lambda: accel.analyze(cfg, sess))
        if p == "impacts":
            return impacts.summary(sess)
        if p == "report":
            return report.session_report(cfg, sess).encode(), "text/html; charset=utf-8", {}
        if p == "instrument":
            txt = lsparse.instrument(sess.program_text(), int(cfg["pro"]["line_reg"]))
            name = re.sub(r"[^A-Za-z0-9_]", "", sess.program().name) or "PROGRAM"
            return txt.encode("utf-8"), "text/plain; charset=utf-8", {"Content-Disposition": f"attachment; filename={name}.LS"}
        if p == "program.ls":
            name = re.sub(r"[^A-Za-z0-9_]", "", sess.program().name) or "PROGRAM"
            return sess.program_text().encode("utf-8"), "text/plain; charset=utf-8", {"Content-Disposition": f"attachment; filename={name}.LS"}
    finally:
        sess.close()
    return None


def post(cfg: dict, path: str, b: dict, who: str):
    """寫入；回傳 (內容, 狀態碼)。"""
    p = path[len("/api/pro/"):]
    if p == "parse":
        txt = str(b.get("text") or "")
        if len(txt) > MAX_TEXT:
            return {"error": _("檔案太大")}, 400
        prog = lsparse.parse(txt)
        ml = prog.motion_lines()
        return {"name": prog.name, "version": prog.version, "lines": len(prog.lines), "motion": len(ml),
                "joint_points": sum(1 for ln in ml if not ln.point_reg and prog.positions.get(ln.point_no or -1)
                                    and prog.positions[ln.point_no].joint_vec()),
                "warnings": prog.warnings}, 200
    if p == "session":
        txt = str(b.get("program") or "")
        if not txt.strip() or "/MN" not in txt:
            return {"error": _("請上傳程式的文字版（.LS）")}, 400
        role = b.get("role") or "diag"
        if role not in ROLES:
            return {"error": _("類別不正確")}, 400
        robot = str(b.get("robot") or _robot(cfg))[:40]
        s = Session.create(cfg, robot, txt, role=role, label=str(b.get("label") or "")[:80],
                           group=str(b.get("group") or "")[:40], source=str(b.get("source") or "listen")[:10],
                           note=str(b.get("note") or "")[:500], change=str(b.get("change") or "")[:500], created_by=who)
        sid = s.id
        s.close()
        return {"ok": True, "id": sid}, 200
    sess = _open(cfg, b)
    try:
        if p == "session/update":
            kw = {}
            for k in ("label", "group", "note", "change"):
                if k in b:
                    kw[k] = str(b[k] or "")[:500]
            if b.get("role") in ROLES:
                kw["role"] = b["role"]
            for k in ("cycles_per_day", "hours_per_day"):
                if k in b and b[k] not in (None, ""):
                    v = float(b[k])
                    if not 0 <= v <= (100000 if k == "cycles_per_day" else 24):
                        return {"error": _("數值超出範圍")}, 400
                    kw[k] = v
            sess.save_meta(**kw)
            return {"ok": True}, 200
        if p == "session/close":
            import time
            sess.save_meta(status="closed", closed=time.time())
            return {"ok": True}, 200
        if p == "session/reopen":
            sess.save_meta(status="open", closed=None)
            return {"ok": True}, 200
        if p == "session/delete":
            sess.delete()
            return {"ok": True}, 200
        if p == "alarms":
            txt = str(b.get("text") or "")
            if len(txt) > MAX_TEXT:
                return {"error": _("檔案太大")}, 400
            return dict(impacts.import_alarms(cfg, sess, txt), ok=True), 200
        if p == "accel":
            txt = str(b.get("text") or "")
            if len(txt) > MAX_TEXT:
                return {"error": _("檔案太大")}, 400
            return dict(impacts.import_accel(cfg, sess, txt), ok=True), 200
    finally:
        sess.close()
    return {"error": "not found"}, 404


# ------------------------------------------------------------ 演示伺服器（python -m dtm pro demo）
def serve_demo(cfg: dict, host: str, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            from ..web import clean
            self._send(code, json.dumps(clean(obj), ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            with use(norm(q.get("lang")) or default_lang()):
                try:
                    if u.path in ("/", "/pro", "/pro.html", "/index.html"):
                        with open(os.path.join(STATIC, "pro.html"), "rb") as f:
                            return self._send(200, f.read(), "text/html; charset=utf-8")
                    if u.path == "/api/i18n":
                        lang = get_lang()
                        return self._json({"lang": lang, "default": default_lang(), "langs": visible_langs((lang, default_lang())),
                                           "html_lang": HTML_LANG[lang], "font": FONT_STACK[lang], "messages": ui_messages(lang)})
                    if u.path == "/api/about":
                        from ..about import info
                        return self._json(info())
                    if u.path.startswith("/api/pro/"):
                        r = get(cfg, u.path, q)
                        if r is None:
                            return self._json({"error": "not found"}, 404)
                        if isinstance(r, tuple):
                            return self._send(200, r[0], r[1], r[2])
                        return self._json(r)
                    return self._json({"error": "not found"}, 404)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except Exception as e:
                    log.exception("GET %s", u.path)
                    return self._json({"error": str(e)}, 500)

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            b = json.loads(self.rfile.read(n) or b"{}")
            with use(default_lang()):
                if u.path == "/api/pro/parse":
                    obj, code = post(cfg, u.path, b, "")
                    return self._json(obj, code)
                return self._json({"error": _("演示模式不能修改資料")}, 403)

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    return srv

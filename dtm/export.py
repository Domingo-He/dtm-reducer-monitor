"""分析資料匯出（2.1）：選機器人與日期範圍，產生一個 zip 交給分析人員或用來改進門檻。

內容：診斷特徵、每日動作中位數、保養事件、警報、鐵粉、振動、元件壽命（含拆檢）、運轉時數、校正、系統事件、
去敏的設定檔（JSON）、保留的原始片段（診斷段、警報前後），以及拆檢照片。
不含：一般原始取樣、帳號資料庫、金鑰與密碼、電話與 Email、Webhook 網址；控制器與網路位址一律遮蔽。
匿名化（選用）：廠名、機器人名稱、產線、操作人改成代號（對照表不放進 zip）。
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import time
import zipfile
from datetime import datetime

from .i18n import _
from . import runtime as R

TABLES = [  # (資料表, 檔名, 時間欄位)
    ("features", "features.csv", "ts"),
    ("motion_daily", "motion_daily.csv", "ts"),
    ("events", "events.csv", "ts"),
    ("alarms", "alarms.csv", "ts"),
    ("iron", "iron.csv", "ts"),
    ("vib", "vibration.csv", "ts"),
    ("components", "components.csv", None),
    ("hours", "hours.csv", "ts"),
    ("calib", "calibration.csv", "ts"),
    ("sys_events", "system_events.csv", "ts"),
]
SECRET_KEY = re.compile(r"(password|passwd|token|secret|bearer|authorization|api_?key|credential|basic_user)", re.I)
DROP_KEYS = {"to", "mail_to", "mail_from", "smtp_user", "smtp_host", "duty_file", "webhook_url", "webhook_headers",
             "webhook_body", "url", "params", "users_db", "tls_cert", "tls_key", "ca_file", "proxy", "public_url",
             "port_name"}
PHONE = re.compile(r"^\+?\d[\d\- ]{7,}$")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
IPV4 = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")
NAME_KEYS = {"name", "site_name", "site_id", "id", "line", "robot", "rname", "sname"}
HOST_KEYS = {"host", "public_host", "child_host"}
MASK = "***"


def _ts(v, end: bool = False) -> float | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = datetime.strptime(str(v)[:10], "%Y-%m-%d").timestamp()
    return t + 86400 if end else t


class Anon:
    """把真實名稱換成代號；同一個名稱永遠換成同一個代號。"""

    def __init__(self, enabled: bool, names: dict[str, str] | None = None):
        self.on = enabled
        self.map: dict[str, str] = dict(names or {})
        self.people: dict[str, str] = {}

    def person(self, who: str) -> str:
        if not self.on or not who:
            return who
        if who not in self.people:
            self.people[who] = _("操作人{n}", n=len(self.people) + 1)
        return self.people[who]

    def text(self, s):
        if not isinstance(s, str):
            return s
        s = IPV4.sub("x.x.x.x", EMAIL.sub(MASK, s))
        if self.on:
            for real, code in sorted(self.map.items(), key=lambda kv: -len(kv[0])):
                if real:
                    s = s.replace(real, code)
        return s


def names_of(cfg: dict) -> dict[str, str]:
    """要匿名化的名稱 → 代號。"""
    out = {}
    sc = cfg.get("system") or {}
    for k, code in (("site_name", "SITE-A"), ("site_id", "SITE-A")):
        if sc.get(k):
            out[str(sc[k])] = code
    rb = cfg.get("robot") or {}
    for k in ("name", "id"):
        if rb.get(k):
            out[str(rb[k])] = "ROBOT-01"
    if rb.get("line"):
        out[str(rb["line"])] = "LINE-1"
    for i, r in enumerate(cfg.get("robots") or [], 1):
        for k in ("name", "id"):
            if isinstance(r, dict) and r.get(k):
                out.setdefault(str(r[k]), f"ROBOT-{i:02d}")
        if isinstance(r, dict) and r.get("line"):
            out.setdefault(str(r["line"]), f"LINE-{i}")
    return out


def sanitize(obj, anon: Anon, key: str = ""):
    """設定檔去敏：密碼、金鑰、電話、Email、通知網址一律遮蔽；位址遮蔽；匿名化時名稱換代號。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).startswith("_"):
                continue                                   # 程式內部欄位（檔案路徑等）
            if SECRET_KEY.search(str(k)) or k in DROP_KEYS:
                out[k] = MASK if v not in ("", None, [], {}) else v
            elif k in HOST_KEYS:
                out[k] = "x.x.x.x" if v else v
            elif anon.on and k in NAME_KEYS and isinstance(v, str):
                out[k] = anon.text(v)
            else:
                out[k] = sanitize(v, anon, k)
        return out
    if isinstance(obj, list):
        return [sanitize(v, anon, key) for v in obj]
    if isinstance(obj, str):
        if PHONE.match(obj.strip()) or EMAIL.search(obj):
            return MASK
        if obj.startswith(("\\\\", "//")) or (obj.startswith("/") and obj.count("/") >= 3) \
                or re.match(r"^[A-Za-z]:[\\/]", obj):
            return os.path.basename(obj.replace("\\", "/").rstrip("/"))   # 完整路徑可能帶出電腦帳號、伺服器名稱
        return anon.text(obj)
    return obj


def _csv(rows: list[dict], cols: list[str] | None = None) -> bytes:
    buf = io.StringIO()
    cols = cols or (list(rows[0].keys()) if rows else [])
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return ("﻿" + buf.getvalue()).encode("utf-8")          # BOM：Excel 直接開不會亂碼


def _clean_row(t: str, r: dict, anon: Anon) -> dict:
    r = dict(r)
    for k in ("who", "acked_by", "undo_who", "done_by"):
        if k in r and r[k]:
            r[k] = anon.person(str(r[k]))
    for k in ("note", "message", "detail", "reason", "undo_note", "teardown", "robot", "source"):
        if k in r and isinstance(r[k], str):
            r[k] = anon.text(r[k])
    if t == "components" and r.get("teardown"):
        from . import teardown as TD
        td = TD.parse(r["teardown"])
        if td:
            td.pop("who", None)
            r["teardown_text"] = TD.text(td)
            r["teardown"] = json.dumps(td, ensure_ascii=False)
    if t == "events" and r.get("detail"):
        try:                                               # 事件明細只留拆檢摘要（元件編號對分析沒用）
            det = json.loads(r["detail"])
            td = det.get("teardown")
            r["detail"] = json.dumps({"teardown": {k: v for k, v in td.items() if k != "who"}} if td else {},
                                     ensure_ascii=False)
        except (TypeError, ValueError):
            r["detail"] = ""
    return r


def export_robot_db(cfg: dict, out_path: str, since=None, until=None, anonymize: bool = False,
                    now: float | None = None) -> dict:
    """單機或邊緣管理的一台機器人：從它自己的資料庫匯出。回傳摘要（檔案、筆數）。"""
    from . import __version__
    from .config import EVENT_LABELS
    from . import teardown as TD
    now = now or time.time()
    t0, t1 = _ts(since), _ts(until, end=True)
    anon = Anon(anonymize, names_of(cfg))
    db = os.path.abspath(cfg["storage"]["db"])
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    counts: dict[str, int] = {}
    photos: set[str] = set()
    tmp = out_path + ".part"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for t, fname, tcol in TABLES:
                if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
                    continue
                q, args = f"SELECT * FROM {t} WHERE 1=1", []
                if tcol and t0 is not None:
                    q, args = q + f" AND {tcol}>=?", args + [t0]
                if tcol and t1 is not None:
                    q, args = q + f" AND {tcol}<?", args + [t1]
                rows = [_clean_row(t, dict(r), anon) for r in c.execute(q + " ORDER BY rowid", args)]
                if t == "events":
                    for r in rows:
                        r["kind_label"] = EVENT_LABELS.get(r["kind"], r["kind"])
                if t == "components":
                    for r in c.execute("SELECT teardown FROM components WHERE teardown IS NOT NULL"):
                        td = TD.parse(r[0]) or {}
                        photos.update(p for p in td.get("photos") or [] if TD.photo_path(cfg, p))
                cols = list(rows[0].keys()) if rows else [d[1] for d in c.execute(f"PRAGMA table_info({t})")]
                z.writestr(fname, _csv(rows, cols))
                counts[fname] = len(rows)
            # 保留的原始片段（診斷段、警報前後）
            keeps = [dict(r) for r in c.execute(
                "SELECT * FROM raw_keep WHERE t1>=? AND t0<=? ORDER BY t0", (t0 or 0, t1 or now + 1e9))] \
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='raw_keep'").fetchone() else []
            raw_rows = []
            for k in keeps:
                for r in c.execute("SELECT * FROM samples WHERE ts>=? AND ts<=? ORDER BY ts", (k["t0"], k["t1"])):
                    raw_rows.append(dict(r, keep_kind=k["kind"], keep_id=k["id"]))
            z.writestr("raw_segments.csv", _csv(raw_rows, None if raw_rows else
                                                ["keep_id", "keep_kind", "ts", "seg", "run", "ovr", "temp",
                                                 "d1", "d2", "d3", "d4", "d5", "d6", "d7"]))
            z.writestr("raw_segment_index.csv", _csv([{k: v for k, v in r.items() if k != "ref"} for r in keeps],
                                                     ["id", "kind", "t0", "t1", "axis", "ts"]))
            counts["raw_segments.csv"] = len(raw_rows)
            for p in sorted(photos):
                z.write(TD.photo_path(cfg, p), f"photos/{p}")
            conf = sanitize(cfg, anon)
            z.writestr("config_sanitized.json", json.dumps(conf, ensure_ascii=False, indent=2, default=str))
            rname = anon.text(str(cfg["robot"].get("name", "")))
            z.writestr("README.txt", _manifest(rname, t0, t1, now, anonymize, counts, len(photos), __version__))
        os.replace(tmp, out_path)
    finally:
        c.close()
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"path": out_path, "counts": counts, "photos": len(photos), "anonymized": anonymize,
            "size": os.path.getsize(out_path)}


def export_central(cs, rkey: str, out_path: str, since=None, until=None, anonymize: bool = False,
                   now: float | None = None) -> dict:
    """中央（或邊緣的廠區資料庫）：用中央保存的資料匯出某台機器人（沒有時數、校正、系統事件與原始片段）。"""
    from . import __version__
    now = now or time.time()
    t0, t1 = _ts(since), _ts(until, end=True)
    exp = cs.export_robot(rkey, now)
    r = cs.one("SELECT r.name, r.line, s.name AS sname FROM robots r LEFT JOIN sites s ON s.site_id=r.site_id "
               "WHERE r.rkey=?", (rkey,)) or {}
    names = {}
    if anonymize:
        for k, code in (("name", "ROBOT-01"), ("line", "LINE-1"), ("sname", "SITE-A")):
            if r.get(k):
                names[str(r[k])] = code
        for part, code in zip(rkey.split("/"), ("SITE-A", "ROBOT-01")):
            names.setdefault(part, code)
    anon = Anon(anonymize, names)
    counts = {}
    tmp = out_path + ".part"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for t, fname, tcol in TABLES:
                if t not in exp:
                    continue
                rows = [_clean_row(t, dict(x), anon) for x in exp[t]
                        if not tcol or ((t0 is None or (x.get(tcol) or 0) >= t0) and (t1 is None or (x.get(tcol) or 0) < t1))]
                z.writestr(fname, _csv(rows))
                counts[fname] = len(rows)
            z.writestr("README.txt", _manifest(anon.text(r.get("name") or rkey), t0, t1, now, anonymize, counts, 0,
                                               __version__, central=True))
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"path": out_path, "counts": counts, "photos": 0, "anonymized": anonymize, "size": os.path.getsize(out_path)}


def _manifest(robot, t0, t1, now, anonymize, counts, n_photos, version, central: bool = False) -> str:
    f = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d") if t else "…"  # noqa: E731
    lines = [_("DTM 分析資料匯出"), "",
             _("機器人：{r}", r=robot), _("期間：{a} ～ {b}", a=f(t0), b=f(t1 - 1 if t1 else None)),
             _("產生時間：{t}（DTM {v}）", t=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M"), v=version),
             _("匿名化：{x}", x=_("是（廠名、機器人、產線、操作人已換成代號）") if anonymize else _("否")),
             _("來源：{x}", x=_("中央保存的資料") if central else _("機器人資料庫")), "", _("檔案與筆數：")]
    lines += [f"  {k}: {v}" for k, v in counts.items()]
    if n_photos:
        lines.append(f"  photos/: {n_photos}")
    lines += ["", _("不包含：一般原始取樣、帳號、金鑰與密碼、電話與 Email、Webhook 網址；控制器與網路位址已遮蔽。"),
              _("時間欄位 ts 是 Unix 秒（UTC）；CSV 是 UTF-8（含 BOM），Excel 可直接開啟。")]
    return "\n".join(lines) + "\n"


def exports_dir(cfg: dict) -> str:
    d = R.resolve(cfg, os.path.join("data", "exports"))
    os.makedirs(d, exist_ok=True)
    return d


def default_name(label: str, since=None, until=None, now: float | None = None) -> str:
    stamp = datetime.fromtimestamp(now or time.time()).strftime("%Y%m%d_%H%M")
    rng = f"_{str(since)[:10]}_{str(until)[:10]}" if since or until else ""
    return f"dtm_analysis_{R.slug(label)}{rng.replace('None', '')}_{stamp}.zip"


# ------------------------------------------------------------ 網頁下載：一次性連結
_TOKENS: dict[str, tuple[str, float]] = {}


def issue_download(path: str, ttl: float = 600) -> str:
    import secrets
    now = time.time()
    for k, (_p, exp) in list(_TOKENS.items()):
        if exp < now:
            _TOKENS.pop(k, None)
    tok = secrets.token_urlsafe(18)
    _TOKENS[tok] = (path, now + ttl)
    return tok


def take_download(tok: str) -> str | None:
    p, exp = _TOKENS.pop(tok or "", (None, 0.0))
    return p if p and exp >= time.time() and os.path.isfile(p) else None

"""還原精靈（2.1）：從備份把資料補回來，或讓某一軸回到備份當時的狀態。

來源
- 本機備份（backup_dir）、網路資料夾（backup_mirror_dir）、還原前自動備份（每次還原前自動存的那份）
- 指定檔案：備份 zip、.sqlite3，或中央匯出的重建檔（.json／.json.gz）
- 從中央重建：直接向中央伺服器要這台機器人上傳過的資料（診斷特徵、每日動作統計、事件、元件、鐵粉、振動、警報）

範圍：整台／指定軸；資料類型（趨勢與特徵、振動與鐵粉、維護紀錄與元件、警報與通知、系統與操作紀錄、原始取樣）；時間區間。
原始取樣一筆含全部軸，不做單軸還原；沒有軸欄位的資料（時數、通知、系統紀錄）只在「整台」範圍處理。

模式
- merge（補回遺失，預設）：只補備份裡有、目前沒有的資料，不刪、不改任何現有資料。
- overwrite（回到時間點）：範圍內的資料改成跟備份一模一樣（備份之後新增的會刪掉）。要二次確認。
  操作紀錄（audit）一律只補不刪。

流程：預覽差異（不改任何資料）→ 執行前自動備份現況 → 分段寫入（每段一個交易，不會長時間卡住監測）→ 重算該軸判定。
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

from . import backup as B
from .i18n import N_, TDict, _

log = logging.getLogger("dtm.restore")

# 資料表：時間欄位（{a} = 別名）與換算成秒的倍數、軸欄位、用來判斷「同一筆」的欄位
TABLES = {
    "features": ("{a}.ts", 1, "axis", ("ts", "axis")),
    "agg_1min": ("{a}.minute", 60, "axis", ("minute", "axis")),
    "cycles": ("{a}.ts", 1, "axis", ("ts", "code", "axis")),
    "motion_daily": ("COALESCE({a}.ts, CAST(strftime('%s', {a}.day) AS REAL))", 1, "axis", ("day", "code", "axis")),
    "vib": ("{a}.ts", 1, "axis", ("ts", "axis", "direction")),
    "iron": ("{a}.ts", 1, "axis", ("ts", "axis")),
    "components": ("{a}.install_ts", 1, "axis", ("comp", "axis", "install_ts")),
    "hours": ("{a}.ts", 1, None, ("ts",)),
    "calib": ("{a}.ts", 1, "axis", ("ts", "axis")),
    "events": ("{a}.ts", 1, "axis", ("ts", "axis", "kind")),
    "alarms": ("{a}.ts", 1, "axis", ("ts", "akey")),
    "notify_log": ("{a}.ts", 1, None, ("ts", "channel", "target")),
    "sys_events": ("{a}.ts", 1, None, ("ts", "kind")),
    "audit": ("{a}.ts", 1, None, ("ts", "action", "who")),
    "samples": ("{a}.ts", 1, None, ("ts",)),
    "raw_keep": ("{a}.t0", 1, None, ("kind", "t0", "t1")),
}
GROUPS = {
    "trend": ("features", "agg_1min", "cycles", "motion_daily"),
    "sensor": ("vib", "iron"),
    "maint": ("components", "hours", "calib", "events"),
    "alarm": ("alarms", "notify_log"),
    "system": ("sys_events", "audit"),
    "raw": ("samples", "raw_keep"),
}
GROUP_LABELS = TDict({"trend": "趨勢與診斷特徵", "sensor": "振動與鐵粉", "maint": "維護紀錄與元件壽命",
                      "alarm": "警報與通知紀錄", "system": "系統與操作紀錄", "raw": "原始取樣（只能整台）"})
TABLE_LABELS = TDict({
    "features": "診斷特徵", "agg_1min": "每分鐘統計", "cycles": "生產循環", "motion_daily": "每日動作統計",
    "vib": "振動", "iron": "鐵粉", "components": "元件壽命", "hours": "運轉時數", "calib": "校正",
    "events": "保養事件", "alarms": "警報", "notify_log": "通知紀錄", "sys_events": "系統紀錄",
    "audit": "操作紀錄", "samples": "原始取樣", "raw_keep": "原始片段保留標記"})
DEFAULT_GROUPS = ("trend", "sensor", "maint", "alarm")
APPEND_ONLY = {"audit"}                       # 回到時間點也不刪
PY_TABLES = ("components", "hours", "events")   # 有互相參照（事件明細記元件與時數的編號），逐筆處理
ORDER = ["components", "hours", "calib", "events", "features", "agg_1min", "cycles", "motion_daily", "vib", "iron",
         "alarms", "notify_log", "sys_events", "audit", "samples", "raw_keep"]
WINDOW_S = 30 * 86400                         # 大表分段：每 30 天一個交易
SNAP_RE = re.compile(r"^(?P<stem>.+)_before_restore_(?P<ts>\d{8}_\d{6})\.sqlite3$")
MODE_LABELS = TDict({"merge": "補回遺失", "overwrite": "回到時間點"})


@dataclass
class Scope:
    axis: int | None = None
    groups: tuple = DEFAULT_GROUPS
    since: float | None = None
    until: float | None = None
    tables: list = field(default_factory=list)

    def __post_init__(self):
        bad = [g for g in self.groups if g not in GROUPS]
        if bad:
            raise ValueError(_("未知的資料類型：{x}", x=", ".join(bad)))
        if not self.groups:
            raise ValueError(_("請至少選一種資料類型"))
        if self.axis is not None and "raw" in self.groups:
            raise ValueError(_("原始取樣一筆含全部軸，不能只還原單軸；請選「整台」或取消原始取樣"))
        if self.since and self.until and self.until <= self.since:
            raise ValueError(_("時間區間的結束要晚於開始"))
        ts = [t for g in self.groups for t in GROUPS[g]]
        if self.axis is not None:
            ts = [t for t in ts if TABLES[t][2]]
        self.tables = [t for t in ORDER if t in ts]

    def label(self, cfg=None) -> str:
        from .config import axis_name
        who = _("整台") if self.axis is None else (axis_name(cfg, self.axis) if cfg else f"J{self.axis}")
        rng = ""
        if self.since or self.until:
            f = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d") if t else "…"  # noqa: E731
            rng = f" {f(self.since)}～{f(self.until)}"
        return who + _("：") + _("、").join(GROUP_LABELS[g] for g in self.groups) + rng


def parse_scope(d: dict) -> Scope:
    ax = d.get("axis")
    ax = None if ax in (None, "", "all") else int(ax)

    def ts(v, end=False):
        if v in (None, ""):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        t = datetime.strptime(str(v)[:10], "%Y-%m-%d").timestamp()
        return t + 86400 if end else t                   # 結束日含當天
    groups = d.get("groups") or DEFAULT_GROUPS
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    return Scope(ax, tuple(groups), ts(d.get("since")), ts(d.get("until"), end=True))


# ============================================================ 來源
def snapshot_dirs(cfg) -> list[str]:
    out = [os.path.dirname(B.db_path(cfg))]
    d = B.backup_dir(cfg)
    if d and d not in out:
        out.insert(0, d)
    return out


def list_snapshots(cfg) -> list[dict]:
    stem = os.path.splitext(os.path.basename(B.db_path(cfg)))[0]
    out = []
    for d in snapshot_dirs(cfg):
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            m = SNAP_RE.match(f)
            if m and m["stem"] == stem:
                p = os.path.join(d, f)
                out.append({"path": p, "name": f, "ts": datetime.strptime(m["ts"], "%Y%m%d_%H%M%S").timestamp(),
                            "size": os.path.getsize(p), "where": "snapshot"})
    return sorted(out, key=lambda b: b["ts"], reverse=True)


def editable(cfg, client: str) -> tuple[bool, str]:
    """執行還原的條件（預覽不受限）：不是演示、從這台電腦本機操作。管理員權限由網頁層檢查。"""
    from .auth import is_loopback
    if cfg.get("_demo"):
        return False, _("演示模式不能還原資料")
    if cfg["system"].get("role") == "central":
        return False, _("中央伺服器的資料請用備份檔整份還原（restore --mode full）")
    if not is_loopback(client):
        return False, _("資料還原只能在這台電腦本機操作（http://127.0.0.1）；預覽可以在任何地方看")
    return True, ""


def web_sources(cfg, client: str) -> dict:
    """網頁用：不回傳完整路徑。"""
    from .config import axis_name, enabled_axes
    s = sources(cfg)
    ok, why = editable(cfg, client)
    strip = lambda rows: [{k: b[k] for k in ("name", "ts", "size")} for b in rows]  # noqa: E731
    return {"local": strip(s["local"]), "mirror": strip(s["mirror"]), "snapshot": strip(s["snapshot"]),
            "mirror_dir": s["mirror_dir"], "mirror_error": s["mirror_error"], "central": s["central"],
            "central_url": s["central_url"], "editable": ok, "why": why,
            "groups": [{"key": g, "label": GROUP_LABELS[g], "default": g in DEFAULT_GROUPS} for g in GROUPS],
            "axes": [{"axis": ax, "name": axis_name(cfg, ax)} for ax in enabled_axes(cfg)]}


def central_ok(cfg) -> bool:
    return bool((cfg.get("central") or {}).get("url")) and cfg["system"].get("role") != "central"


def sources(cfg) -> dict:
    """還原精靈的來源清單（給網頁與 CLI）。網路資料夾連不上時回傳錯誤訊息，不影響其他來源。"""
    out = {"local": B.list_backups(cfg), "snapshot": list_snapshots(cfg), "mirror": [], "mirror_dir": B.mirror_dir(cfg) or "",
           "mirror_error": "", "central": central_ok(cfg), "central_url": (cfg.get("central") or {}).get("url", "")}
    if out["mirror_dir"]:
        try:
            out["mirror"] = B.list_backups(cfg, "mirror")
        except OSError as e:
            out["mirror_error"] = str(e)
    return out


def _extract(src: str, tmp: str) -> str:
    """zip → 解出資料庫；.json(.gz) → 中央重建檔轉成資料庫；.sqlite3 → 直接用（唯讀複製一份）。"""
    if not os.path.exists(src):
        raise FileNotFoundError(_("找不到備份檔：{path}", path=src))
    if zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as z:
            names = [n for n in z.namelist() if n.endswith((".sqlite3", ".db", ".sqlite"))]
            if not names:
                raise ValueError(_("備份檔裡沒有資料庫"))
            z.extract(names[0], tmp)
            return os.path.join(tmp, names[0])
    if src.endswith((".json", ".json.gz")):
        op = gzip.open if src.endswith(".gz") else open
        with op(src, "rt", encoding="utf-8") as f:
            return build_from_export(json.load(f), os.path.join(tmp, "central.sqlite3"))
    dst = os.path.join(tmp, "copy.sqlite3")
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()
    return dst


def _check_db(path: str):
    c = sqlite3.connect(path)
    try:
        ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()
    if not ok or not {"features", "events"} <= tables:
        raise ValueError(_("這個檔案不是完整的 DTM 資料庫，不能用來還原"))


def fetch_central(cfg) -> dict:
    """向中央要這台機器人的重建資料（用同步金鑰；中央只給這個廠區自己的機器人）。"""
    from .sync import Client, robot_key, site_id
    if not central_ok(cfg):
        raise RuntimeError(_("沒有設定中央伺服器（[central] url）；也可以在中央電腦執行 "
                             "python -m dtm central-export --robot 機器人代號，把檔案帶過來用 --file 指定"))
    resp = Client(cfg).post({"site": {"site_id": site_id(cfg)}, "rebuild": {"robot": robot_key(cfg)}}, timeout=180)
    if not resp.get("ok"):
        raise RuntimeError(_("中央回應錯誤：{e}", e=resp.get("error")))
    return resp["rebuild"]


def build_from_export(exp: dict, path: str) -> str:
    """把中央匯出的資料寫成一個本機格式的資料庫（編號沿用機器人端原本的編號）。"""
    from .store import Store
    Store(path)
    c = sqlite3.connect(path)
    try:
        for t, rows in exp.items():
            if t not in TABLES or not isinstance(rows, list) or not rows:
                continue
            have = {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
            for r in rows:
                cols = [k for k in r if k in have]
                c.execute(f"INSERT OR IGNORE INTO {t} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          [r[k] for k in cols])
        c.execute("CREATE TABLE IF NOT EXISTS restore_meta (k TEXT PRIMARY KEY, v TEXT)")
        c.execute("INSERT OR REPLACE INTO restore_meta VALUES ('central', ?)",
                  (json.dumps({"robot": exp.get("robot"), "ts": exp.get("ts")}),))
        c.commit()
    finally:
        c.close()
    return path


class Opened:
    """解開後的來源（暫存資料庫）。preview 之後保留 15 分鐘給 run 使用，確保執行的跟預覽的是同一份資料。"""

    def __init__(self, path: str, tmp: str, label: str, ts: float | None, kind: str, note: str = ""):
        self.id = uuid.uuid4().hex[:12]
        self.path, self.tmp, self.label, self.ts, self.kind, self.note = path, tmp, label, ts, kind, note
        self.opened = time.time()
        self.scope: Scope | None = None              # 網頁：預覽時的範圍與模式，執行時沿用（確保跟預覽一致）
        self.mode = "merge"

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


_CACHE: dict[str, Opened] = {}
_LOCK = threading.Lock()
CACHE_S = 900


def _gc(now: float | None = None):
    now = now or time.time()
    with _LOCK:
        for k, o in list(_CACHE.items()):
            if now - o.opened > CACHE_S:
                o.close()
                _CACHE.pop(k, None)


def open_source(cfg, source: str, name: str = "", path: str = "", keep: bool = False) -> Opened:
    """source = local／mirror／snapshot／file／central。keep=True 放進快取（網頁的預覽 → 執行）。"""
    _gc()
    tmp = tempfile.mkdtemp(prefix="dtm_restore_")
    try:
        note = ""
        if source in ("local", "mirror", "snapshot"):
            rows = list_snapshots(cfg) if source == "snapshot" else B.list_backups(cfg, source)
            if not rows:
                raise FileNotFoundError(_("找不到任何備份"))
            pick = next((b for b in rows if b["name"] == name), None) if name else rows[0]
            if not pick:
                raise FileNotFoundError(_("找不到備份：{name}", name=name))
            db, label, ts = _extract(pick["path"], tmp), pick["name"], pick["ts"]
        elif source == "file":
            db, label = _extract(path, tmp), os.path.basename(path)
            ts = os.path.getmtime(path)
            if path.endswith((".json", ".json.gz")):
                note = central_note()
        elif source == "central":
            exp = fetch_central(cfg)
            db = build_from_export(exp, os.path.join(tmp, "central.sqlite3"))
            label, ts, note = _("中央伺服器 {url}", url=cfg["central"]["url"]), exp.get("ts") or time.time(), central_note()
        else:
            raise ValueError(_("未知的來源：{s}", s=source))
        _check_db(db)
        o = Opened(db, tmp, label, ts, source, note)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if keep:
        with _LOCK:
            _CACHE[o.id] = o
    return o


def cached(pid: str, pop: bool = False) -> Opened:
    _gc()
    with _LOCK:
        o = _CACHE.pop(pid or "", None) if pop else _CACHE.get(pid or "")
    if not o:
        raise ValueError(_("預覽已過期（超過 15 分鐘）或不存在，請重新按「預覽差異」"))
    return o


def central_note() -> str:
    return _("中央只保存診斷特徵、每日動作統計、事件、元件、鐵粉、振動與警報；沒有原始取樣、每分鐘統計、生產循環逐筆、"
             "校正與時數。從中央補回的保養事件不含元件變動明細，之後不能用「撤銷」自動還原。")


# ============================================================ 差異與寫入
def _cols(c, schema: str, t: str) -> list[str]:
    return [r[1] for r in c.execute(f"PRAGMA {schema}.table_info({t})")]


def _where(t: str, scope: Scope, a: str, lo: float | None = None, hi: float | None = None) -> tuple[str, list]:
    """範圍條件；時間直接比對欄位（每分鐘統計比對 minute），才用得到索引。"""
    texpr, scale, acol, _k = TABLES[t]
    texpr = texpr.format(a=a)
    w, args = ["1=1"], []
    if scope.axis is not None:
        w.append(f"{a}.{acol} = ?")
        args.append(scope.axis)
    for v, op in ((scope.since, ">="), (lo, ">="), (scope.until, "<"), (hi, "<")):
        if v is not None:
            w.append(f"{texpr} {op} ?")
            args.append(v / scale)
    return " AND ".join(w), args


def _missing(t: str, x: str, other: str) -> str:
    keys = TABLES[t][3]
    return (f"NOT EXISTS (SELECT 1 FROM {other}.{t} y WHERE " +
            " AND ".join(f"y.{k} IS {x}.{k}" for k in keys) + ")")


def _connect(cfg, src: str):
    from .store import Store
    Store(B.db_path(cfg))                      # 資料庫不見了也能還原：先建好空的資料表
    c = sqlite3.connect(B.db_path(cfg), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE ? AS b", (src,))
    return c


def _has(c, schema: str, t: str) -> bool:
    return bool(c.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (t,)).fetchone())


def _range(c, schema: str, t: str, scope: Scope) -> tuple:
    w, args = _where(t, scope, "x")
    texpr, scale = TABLES[t][0].format(a="x"), TABLES[t][1]
    r = c.execute(f"SELECT COUNT(*), MIN({texpr}), MAX({texpr}) FROM {schema}.{t} x WHERE {w}", args).fetchone()
    return r[0], (r[1] * scale if r[1] is not None else None), (r[2] * scale if r[2] is not None else None)


def phrase(cfg, scope: Scope) -> str:
    from .config import axis_name
    return _("回到時間點 {who}", who=_("整台") if scope.axis is None else axis_name(cfg, scope.axis))


def preview(cfg, opened: Opened, scope: Scope, mode: str = "merge") -> dict:
    """不改任何資料：每個資料表在範圍內的筆數、會補回幾筆、（回到時間點時）會刪除幾筆。"""
    if mode not in MODE_LABELS:
        raise ValueError(_("未知的模式：{m}", m=mode))
    rows, warn = [], []
    c = _connect(cfg, opened.path)
    try:
        for t in scope.tables:
            if not _has(c, "b", t):
                warn.append(_("備份裡沒有「{t}」（舊版備份），略過", t=TABLE_LABELS[t]))
                continue
            nb, b0, b1 = _range(c, "b", t, scope)
            nm, m0, m1 = _range(c, "main", t, scope)
            w, args = _where(t, scope, "x")
            add = c.execute(f"SELECT COUNT(*) FROM b.{t} x WHERE {w} AND {_missing(t, 'x', 'main')}", args).fetchone()[0]
            rem = 0
            if mode == "overwrite" and t not in APPEND_ONLY:
                rem = c.execute(f"SELECT COUNT(*) FROM main.{t} x WHERE {w} AND {_missing(t, 'x', 'b')}",
                                args).fetchone()[0]
            rows.append({"table": t, "label": TABLE_LABELS[t], "backup": nb, "current": nm, "add": add, "remove": rem,
                         "backup_range": [b0, b1], "current_range": [m0, m1]})
    finally:
        c.close()
    if "samples" in scope.tables and not any(r["table"] == "samples" and r["backup"] for r in rows):
        warn.append(_("這份備份不含原始取樣（每日備份預設不存原始取樣）"))
    if opened.note:
        warn.append(opened.note)
    if mode == "overwrite" and "audit" in scope.tables:
        warn.append(_("操作紀錄只補回、不刪除"))
    opened.scope, opened.mode = scope, mode
    out = {"preview_id": opened.id, "source": opened.label, "source_kind": opened.kind, "source_ts": opened.ts,
           "mode": mode, "mode_label": MODE_LABELS[mode], "scope": scope.label(cfg), "tables": rows, "warnings": warn,
           "add": sum(r["add"] for r in rows), "remove": sum(r["remove"] for r in rows)}
    if mode == "overwrite":
        out["confirm_phrase"] = phrase(cfg, scope)
    return out


def pre_backup(cfg, keep_raw: bool, now: float | None = None) -> str:
    """執行還原前把目前的資料庫整份另存（SQLite 線上備份，DTM 不用停）。"""
    src = B.db_path(cfg)
    d = snapshot_dirs(cfg)[0]
    os.makedirs(d, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]
    dst = os.path.join(d, f"{stem}_before_restore_{datetime.fromtimestamp(now or time.time()):%Y%m%d_%H%M%S}.sqlite3")
    n = 1
    while os.path.exists(dst):
        dst = dst.replace(".sqlite3", f"_{n}.sqlite3")
        n += 1
    B._copy_db(src, dst, keep_raw)
    keep = max(1, int(cfg["storage"].get("before_restore_keep", 5)))
    for s in list_snapshots(cfg)[keep:]:
        try:
            os.remove(s["path"])
        except OSError:
            pass
    return dst


def _windows(c, t: str, scope: Scope) -> list[tuple]:
    """大表依時間分段，每段一個交易（監測程式的寫入不會被卡住太久）。"""
    lo, hi = [], []
    for s in ("b", "main"):
        if _has(c, s, t):
            n, a, z = _range(c, s, t, scope)
            if n:
                lo.append(a)
                hi.append(z)
    if not lo:
        return []
    a, z = min(lo), max(hi) + 1
    out = []
    while a < z:
        out.append((a, min(a + WINDOW_S, z)))
        a += WINDOW_S
    return out


def _sql_table(c, t: str, scope: Scope, mode: str) -> tuple[int, int]:
    """回傳（補回筆數, 刪除筆數）。回到時間點：先刪掉備份沒有的，兩邊都有的也改回備份當時的內容。"""
    common = [k for k in _cols(c, "main", t) if k in set(_cols(c, "b", t))]
    added = removed = 0
    for lo, hi in _windows(c, t, scope):
        c.execute("BEGIN IMMEDIATE")
        try:
            w, args = _where(t, scope, "x", lo, hi)
            same = 0
            if mode == "overwrite" and t not in APPEND_ONLY:
                removed += c.execute(f"DELETE FROM main.{t} AS x WHERE {w} AND {_missing(t, 'x', 'b')}", args).rowcount
                same = c.execute(f"DELETE FROM main.{t} AS x WHERE {w}", args).rowcount
            miss = _missing(t, "x", "main")
            n = 0
            if "id" in common:
                n += c.execute(f"INSERT INTO main.{t} ({','.join(common)}) SELECT {','.join('x.' + k for k in common)} "
                               f"FROM b.{t} x WHERE {w} AND {miss} AND NOT EXISTS (SELECT 1 FROM main.{t} z WHERE z.id = x.id)",
                               args).rowcount
                rest = [k for k in common if k != "id"]
                n += c.execute(f"INSERT INTO main.{t} ({','.join(rest)}) SELECT {','.join('x.' + k for k in rest)} "
                               f"FROM b.{t} x WHERE {w} AND {miss}", args).rowcount
            else:
                n += c.execute(f"INSERT INTO main.{t} ({','.join(common)}) SELECT {','.join('x.' + k for k in common)} "
                               f"FROM b.{t} x WHERE {w} AND {miss}", args).rowcount
            added += n - same
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return added, removed


def _py_tables(c, tables: list[str], scope: Scope, mode: str) -> dict:
    """元件、時數、事件：逐筆處理，並把事件明細裡的元件／時數編號換成還原後的編號。"""
    res = {t: [0, 0] for t in tables}
    idmap: dict[str, dict] = {"components": {}, "hours": {}}

    def find(t, schema, row) -> int | None:
        keys = TABLES[t][3]
        r = c.execute(f"SELECT id FROM {schema}.{t} y WHERE " + " AND ".join(f"y.{k} IS ?" for k in keys),
                      [row[k] for k in keys]).fetchone()
        return r[0] if r else None

    def map_id(t, old) -> int | None:
        if old in idmap[t]:
            return idmap[t][old]
        if not _has(c, "b", t):
            return None
        r = c.execute(f"SELECT * FROM b.{t} WHERE id=?", (old,)).fetchone()
        new = find(t, "main", r) if r else None
        idmap[t][old] = new
        return new

    c.execute("BEGIN IMMEDIATE")
    try:
        for t in tables:
            if not _has(c, "b", t):
                continue
            common = [k for k in _cols(c, "main", t) if k in set(_cols(c, "b", t))]
            w, args = _where(t, scope, "x")
            if mode == "overwrite":
                res[t][1] = c.execute(f"DELETE FROM main.{t} AS x WHERE {w} AND {_missing(t, 'x', 'b')}", args).rowcount
                res[t][0] -= c.execute(f"DELETE FROM main.{t} AS x WHERE {w}", args).rowcount
            for r in c.execute(f"SELECT * FROM b.{t} x WHERE {w} ORDER BY x.id", args).fetchall():
                cur = find(t, "main", r)
                if cur is not None:
                    if t in idmap:
                        idmap[t][r["id"]] = cur
                    continue
                vals = {k: r[k] for k in common}
                if t == "events" and vals.get("detail"):
                    vals["detail"] = _remap_detail(vals["detail"], map_id)
                if c.execute(f"SELECT 1 FROM main.{t} WHERE id=?", (r["id"],)).fetchone():
                    vals.pop("id")
                cur = c.execute(f"INSERT INTO main.{t} ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})",
                                list(vals.values())).lastrowid
                if t in idmap:
                    idmap[t][r["id"]] = cur
                res[t][0] += 1
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return res


def _remap_detail(raw: str, map_id) -> str:
    try:
        det = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    lost = False
    inst = []
    for cid in det.get("installed", []) or []:
        n = map_id("components", cid)
        lost |= n is None
        if n is not None:
            inst.append(n)
    if "installed" in det:
        det["installed"] = inst
    closed = []
    for o in det.get("closed", []) or []:
        n = map_id("components", o.get("id"))
        lost |= n is None
        if n is not None:
            closed.append({**o, "id": n})
    if "closed" in det:
        det["closed"] = closed
    if det.get("hours_id"):
        n = map_id("hours", det["hours_id"])
        lost |= n is None
        det["hours_id"] = n
    if lost:
        det["restore_note"] = N_("部分元件或時數紀錄不在還原範圍，撤銷這筆事件時不會動到它們")
    return json.dumps(det, ensure_ascii=False)


def run(cfg, opened: Opened, scope: Scope, mode: str = "merge", who: str = "", svc=None, confirm: str = "",
        now: float | None = None) -> dict:
    """執行還原：先備份現況 → 分段寫入 → 重算判定。回傳每個資料表補回／刪除的筆數。"""
    if mode == "overwrite" and confirm.strip() != phrase(cfg, scope):
        raise PermissionError(_("回到時間點會刪除資料，請輸入「{p}」確認", p=phrase(cfg, scope)))
    t0 = time.time()
    snap = pre_backup(cfg, keep_raw="samples" in scope.tables, now=now)
    out = {"pre_backup": snap, "tables": [], "mode": mode, "scope": scope.label(cfg), "source": opened.label}
    c = _connect(cfg, opened.path)
    c.isolation_level = None
    try:
        py = [t for t in scope.tables if t in PY_TABLES]
        res = _py_tables(c, py, scope, mode) if py else {}
        for t in scope.tables:
            if t in PY_TABLES:
                a, r = res.get(t, (0, 0))
            elif not _has(c, "b", t):
                continue
            else:
                a, r = _sql_table(c, t, scope, mode)
            out["tables"].append({"table": t, "label": TABLE_LABELS[t], "added": a, "removed": r})
    finally:
        c.close()
    out["added"] = sum(x["added"] for x in out["tables"])
    out["removed"] = sum(x["removed"] for x in out["tables"])
    out["recomputed"] = recompute(cfg, scope, svc)
    out["seconds"] = round(time.time() - t0, 1)
    detail = _("{mode}｜{scope}｜來源 {src}｜補回 {a}、刪除 {r}｜還原前備份 {snap}", mode=MODE_LABELS[mode],
               scope=out["scope"], src=opened.label, a=out["added"], r=out["removed"], snap=os.path.basename(snap))
    store = svc.store if svc else None
    if store is None:
        from .store import Store
        store = Store(B.db_path(cfg))
    store.add_audit(who or "cli", "", "restore", True, detail)
    store.add_sys_event("restore", 0, detail)
    log.warning("restore: %s", detail)
    return out


def recompute(cfg, scope: Scope, svc=None) -> list[int]:
    """還原後重算：每日動作統計（有還原生產循環時）、警報狀態、校正，以及該軸（或全部軸）的健康與維護判定。
    沒有執行中的服務（CLI、DTM 已關閉）時只重算每日統計，其餘在下次啟動時自動重算。"""
    from . import motion as M
    from .store import Store
    store = svc.store if svc else Store(B.db_path(cfg))
    if hasattr(store, "forget_caches"):
        store.forget_caches()                         # 資料庫被還原改過：清理與動作代碼清單改回完整掃描
    if "cycles" in scope.tables:
        r = store.cycles(axis=scope.axis, since=scope.since or 0, until=scope.until)
        if r:
            M.refresh_daily(store, since=min(x["ts"] for x in r))
    if not svc:
        return []
    from .alarms import AlarmManager
    if "alarms" in scope.tables:
        fresh = AlarmManager(store).active
        with svc.alarms.lock:
            svc.alarms.active = fresh
    if "calib" in scope.tables:
        svc.conv.reload()
    if "cycles" in scope.tables:
        svc._known_codes = {cd for cd, _a in store.cycle_codes()}
    axes = [scope.axis] if scope.axis is not None else list(svc.axes)
    now = time.time()
    for ax in axes:
        if ax not in svc.axes:
            continue
        for key in ("grease", "drive", "motor"):
            svc._dec_hist.pop(f"dec:{ax}:{key}", None)
        svc.refresh_health(ax, now)
        svc.refresh_motion(ax, now, raise_alarms=False)
        if hasattr(svc, "refresh_thermal"):
            svc.refresh_thermal(ax, now, raise_alarms=False)
        svc.refresh_decision(ax, now, force=True)
    return axes

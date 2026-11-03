"""系統設定（2.1）：畫面右上角齒輪「設定」裡，可以直接在網頁上修改的項目。

- 只開放「改錯也不會中斷監測」的項目：顯示名稱、預設語言、畫面更新間隔、帳號與密碼原則、登入與閒置登出、
  允許連線的電腦、匯出預設匿名化、通知收件人與通知語言。
  機器人位址、資料來源、取樣、連接埠、HTTPS 憑證等，網頁上只顯示；要改請編輯設定檔
  （改錯會讓監測中斷，改完先執行 python -m dtm check，再重新啟動）。
- 存在 users.sqlite3（和帳號同一個檔，同一台主機的 DTM 程式共用：邊緣主機改一次，每台機器人的程式都會套用）。
  網頁上的值優先於設定檔；「恢復設定檔的值」會刪除網頁上的值。
- 每次修改都記錄舊值與新值、誰、從哪台電腦、什麼時候；可以從修改紀錄一鍵還原。
- 各程式每 10 秒檢查一次有沒有新的修改（不必重新啟動）。
"""
from __future__ import annotations

import copy
import ipaddress
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

from .i18n import LANGS, N_, _, norm

log = logging.getLogger("dtm.sysconf")

SECTIONS = {"general": N_("一般"), "password": N_("帳號與密碼"), "security": N_("隱私與安全"),
            "notify": N_("通知"), "service": N_("服務商")}
SERVICE_REGIONS = ("auto", "tw", "cn", "us", "jp", "kr", "vn")
FX_LABELS = {"auto": N_("依畫面語言換算，網路匯率自動更新"), "manual": N_("依畫面語言換算，用設定檔的手動匯率"),
             "off": N_("不換算，一律顯示原幣別")}
REGION_LABELS = {"auto": N_("自動（依畫面語言）"), "tw": N_("台灣"), "cn": N_("中國大陸"), "us": N_("美國"),
                 "jp": N_("日本"), "kr": N_("韓國"), "vn": N_("越南")}

# key, 區段, 型態, 設定檔位置, 預設值, 其他
FIELDS = [
    {"key": "ui.display_name", "sec": "general", "type": "str", "path": ("system", "display_name"), "default": "",
     "max": 40, "label": N_("系統顯示名稱"), "help": N_("左上角顯示的名稱；空白 = 用廠區名稱或機器人名稱")},
    {"key": "ui.default_lang", "sec": "general", "type": "lang", "path": ("ui", "lang"), "default": "zh-TW",
     "label": N_("預設語言"), "help": N_("通知、報告與第一次開啟畫面時用的語言；每個人仍可在右上角切換自己的畫面語言")},
    {"key": "ui.refresh_s", "sec": "general", "type": "int", "path": ("ui", "refresh_s"), "default": 0, "lo": 0, "hi": 600,
     "unit": N_("秒"), "label": N_("畫面自動更新間隔"),
     "help": N_("總覽、廠區、警報等畫面多久重新讀一次資料；0 = 自動（中央 60 秒、廠區 20 秒），最短 10 秒")},
    {"key": "maint.currency", "sec": "general", "type": "currency", "path": ("maint", "currency"), "default": "TWD",
     "label": N_("備品單價幣別"), "help": N_("ISO 4217 三碼，例如 TWD、CNY、USD、JPY、KRW、VND；保養材料費估算用")},
    {"key": "maint.fx", "sec": "general", "type": "choice", "path": ("maint", "fx"), "default": "auto",
     "options": ("auto", "manual", "off"), "labels": "fx", "label": N_("價格依畫面語言換算當地貨幣"),
     "help": N_("繁中 TWD、簡中 CNY、英文 USD、日文 JPY、韓文 KRW、越南文 VND；只影響顯示，單價仍用上面的幣別輸入。網路匯率只下載匯率表，不送出任何資料")},
    {"key": "password.min_length", "sec": "password", "type": "int", "path": ("web", "pw_min_length"), "default": 8,
     "lo": 8, "hi": 64, "unit": N_("個字"), "label": N_("密碼最少長度"), "help": N_("新設定或變更密碼時檢查；已經在用的密碼不受影響")},
    {"key": "password.require_mix", "sec": "password", "type": "bool", "path": ("web", "pw_require_mix"), "default": True,
     "label": N_("密碼要同時有英文字母與數字"), "help": N_("也不接受與帳號相同、或 12345678 這類常見密碼")},
    {"key": "password.max_age_days", "sec": "password", "type": "int", "path": ("web", "pw_max_age_days"), "default": 0,
     "lo": 0, "hi": 730, "unit": N_("天"), "label": N_("密碼有效天數"), "help": N_("超過天數登入時要先改密碼；0 = 不限")},
    {"key": "password.change_first", "sec": "password", "type": "bool", "path": ("web", "pw_change_first"),
     "default": True, "label": N_("管理員建立或重設的密碼，第一次登入要改"),
     "help": N_("避免管理員知道別人的密碼")},
    {"key": "security.require_login", "sec": "security", "type": "bool", "path": ("web", "require_login"), "default": False,
     "label": N_("看畫面也要登入"), "help": N_("關閉時，同一個網路的人不登入也能看畫面（不能修改）；開啟後連看都要帳號")},
    {"key": "security.session_hours", "sec": "security", "type": "float", "path": ("web", "session_hours"), "default": 12,
     "lo": 0.5, "hi": 72, "unit": N_("小時"), "label": N_("登入有效時間"), "help": N_("登入後超過這個時間要重新登入")},
    {"key": "security.idle_minutes", "sec": "security", "type": "int", "path": ("web", "idle_minutes"), "default": 30,
     "lo": 0, "hi": 720, "unit": N_("分鐘"), "label": N_("閒置自動登出"),
     "help": N_("操作員以上的帳號沒有操作超過這個時間就自動登出；檢視帳號（看板用）不受影響；0 = 不自動登出")},
    {"key": "security.max_fails", "sec": "security", "type": "int", "path": ("web", "max_fails"), "default": 5,
     "lo": 3, "hi": 20, "unit": N_("次"), "label": N_("密碼輸錯幾次就暫停登入"), "help": N_("同一台電腦或同一個帳號，10 分鐘內")},
    {"key": "security.lock_minutes", "sec": "security", "type": "int", "path": ("web", "lock_minutes"), "default": 10,
     "lo": 1, "hi": 240, "unit": N_("分鐘"), "label": N_("暫停登入多久")},
    {"key": "security.allow_ips", "sec": "security", "type": "iplist", "path": ("web", "allow_ips"), "default": [],
     "label": N_("允許連線的電腦"),
     "help": N_("一行一個 IP 或網段，例如 192.168.10.0/24；空白 = 不限制。這台電腦本機一律可以連")},
    {"key": "privacy.export_anonymize", "sec": "security", "type": "bool", "path": ("privacy", "export_anonymize"),
     "default": True, "label": N_("分析匯出預設匿名化"), "help": N_("廠名、機器人、產線、操作人換成代號；匯出時仍可取消勾選")},
    {"key": "service.region", "sec": "service", "type": "choice", "path": ("service", "region"), "default": "auto",
     "options": SERVICE_REGIONS, "label": N_("顯示哪個原廠服務據點"),
     "help": N_("自動 = 依觀看者的畫面語言；外籍同仁用其他語言看畫面時，仍要顯示本地據點請直接指定")},
    {"key": "service.contact_name", "sec": "service", "type": "str", "path": ("service", "contact_name"), "default": "",
     "max": 40, "label": N_("貴廠的服務窗口"), "help": N_("原廠或代理商指派給貴廠的業務、服務工程師姓名")},
    {"key": "service.contact_phone", "sec": "service", "type": "str", "path": ("service", "contact_phone"), "default": "",
     "max": 40, "label": N_("窗口電話")},
    {"key": "service.contact_email", "sec": "service", "type": "email", "path": ("service", "contact_email"), "default": "",
     "max": 80, "label": N_("窗口 Email")},
    {"key": "service.customer_no", "sec": "service", "type": "str", "path": ("service", "customer_no"), "default": "",
     "max": 40, "label": N_("客戶編號／合約編號"), "help": N_("報修時對方會問；也可以填保固或維護合約號碼")},
    {"key": "service.note", "sec": "service", "type": "str", "path": ("service", "note"), "default": "", "max": 120,
     "label": N_("備註"), "help": N_("例如：夜間緊急維修先打代理商、到廠時間約 4 小時")},
]
BY_KEY = {f["key"]: f for f in FIELDS}
NOTIFY_KEY = "notify.recipients"
PEOPLE_KEY = "notify.people"            # 收件人名單（姓名、手機、Email、語言、接收哪些通知）
LIST_KEYS = (NOTIFY_KEY, PEOPLE_KEY)
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# 網頁上只顯示、要改設定檔的項目（改錯會中斷監測或連線）
READONLY = [
    (N_("機器人"), [("robot", "name"), ("robot", "model"), ("robot", "source"), ("robot", "poll_hz")]),
    (N_("資料來源"), [("source", "fanuc_http", "host"), ("source", "modbus", "host"), ("source", "modbus", "port")]),
    (N_("網頁"), [("web", "host"), ("web", "port"), ("web", "tls_cert"), ("web", "public_host")]),
    (N_("資料保存"), [("storage", "db"), ("storage", "raw_days"), ("storage", "backup_dir"), ("storage", "mirror_dir")]),
    (N_("中央同步"), [("central", "url"), ("system", "site_id"), ("system", "site_name")]),
]
SECRET_HINT = re.compile(r"(password|token|secret|key|pass|pwd)", re.I)

_INST: dict[int, "SysConf"] = {}
_LOCK = threading.Lock()


def get(cfg: dict) -> "SysConf":
    """這份設定（cfg）對應的系統設定；同一個 cfg 只建一次。"""
    with _LOCK:
        sc = _INST.get(id(cfg))
        if sc is None or sc.cfg is not cfg:
            sc = SysConf(cfg)
            _INST[id(cfg)] = sc
    return sc


def policy(cfg: dict) -> dict:
    """目前的帳號與安全原則（設定檔為預設，網頁上改過的優先）。"""
    sc = get(cfg)
    sc.refresh()
    w = cfg.get("web") or {}

    def val(k):
        f = BY_KEY[k]
        v = w.get(f["path"][1], f["default"]) if f["path"][0] == "web" else f["default"]
        return f["default"] if v is None else v
    try:
        return {"session_hours": max(0.25, float(val("security.session_hours"))),
                "idle_minutes": max(0, int(val("security.idle_minutes"))),
                "max_fails": max(1, int(val("security.max_fails"))),
                "lock_minutes": max(1, int(val("security.lock_minutes"))),
                "require_login": bool(val("security.require_login")),
                "allow_ips": list(val("security.allow_ips") or []),
                "pw_min": max(8, int(val("password.min_length"))),
                "pw_mix": bool(val("password.require_mix")),
                "pw_days": max(0, int(val("password.max_age_days"))),
                "change_first": bool(val("password.change_first"))}
    except (TypeError, ValueError):
        return {"session_hours": 12.0, "idle_minutes": 30, "max_fails": 5, "lock_minutes": 10, "require_login": False,
                "allow_ips": [], "pw_min": 8, "pw_mix": True, "pw_days": 0, "change_first": True}


# ------------------------------------------------------------ 允許連線的電腦
def parse_ips(items) -> list[str]:
    """IP／網段清單 → 標準寫法；格式錯誤丟 ValueError。"""
    if isinstance(items, str):
        items = re.split(r"[\s,;，；、]+", items)
    out = []
    for it in items or []:
        s = str(it).strip()
        if not s:
            continue
        try:
            net = ipaddress.ip_network(s, strict=False)
        except ValueError:
            raise ValueError(_("「{v}」不是 IP 或網段（例如 192.168.10.25 或 192.168.10.0/24）", v=s))
        out.append(str(net) if "/" in s else str(net.network_address))
    if len(out) > 64:
        raise ValueError(_("最多 64 筆"))
    return list(dict.fromkeys(out))


def ip_allowed(addr: str, allow: list[str]) -> bool:
    if not allow:
        return True
    from .auth import is_loopback
    if is_loopback(addr):
        return True
    try:
        ip = ipaddress.ip_address(str(addr).split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    for s in allow:
        try:
            if ip in ipaddress.ip_network(s, strict=False):
                return True
        except ValueError:
            continue
    return False


# ------------------------------------------------------------ 通知收件人
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9 \-]{5,19}$")


def _list(v) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[\s,;，；、]+", v)
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def clean_recipients(v: dict, cfg: dict) -> dict:
    """{"default": {"email": [], "sms": []}, "rules": {規則名稱: {"enabled", "email", "sms", "lang"}}} 檢查格式。"""
    if not isinstance(v, dict):
        raise ValueError(_("通知收件人格式錯誤"))
    names = {str(r.get("name") or "") for r in (cfg.get("notify", {}).get("rules") or [])}

    def chk(d, where):
        out = {}
        if "email" in d:
            em = _list(d.get("email"))
            bad = [x for x in em if not EMAIL_RE.match(x)]
            if bad:
                raise ValueError(_("{w}：Email 格式不對：{v}", w=where, v=_("、").join(bad)))
            out["email"] = em[:50]
        if "sms" in d:
            ph = _list(d.get("sms"))
            bad = [x for x in ph if not PHONE_RE.match(x)]
            if bad:
                raise ValueError(_("{w}：手機號碼格式不對：{v}", w=where, v=_("、").join(bad)))
            out["sms"] = ph[:50]
        return out
    out = {"default": chk(v.get("default") or {}, _("預設收件人")), "rules": {}}
    for name, d in (v.get("rules") or {}).items():
        if name not in names:
            raise ValueError(_("找不到通知規則「{n}」（規則要先寫在設定檔 [[notify.rules]]）", n=name))
        r = chk(d or {}, name)
        if "enabled" in (d or {}):
            r["enabled"] = bool(d["enabled"])
        if "lang" in (d or {}):
            lg = d.get("lang") or ""
            if lg and not norm(lg):
                raise ValueError(_("{w}：不支援的語言 {v}", w=name, v=lg))
            r["lang"] = norm(lg) or ""
        out["rules"][name] = r
    return out


MAX_PEOPLE = 200


def people_groups(cfg: dict) -> list[str]:
    """收件人可以選的「接收哪些通知」：預設收件人＋設定檔的每一條規則。"""
    return ["default"] + [str(r.get("name") or "") for r in (cfg.get("notify", {}).get("rules") or []) if r.get("name")]


def _pid(used: set) -> str:
    import secrets
    while True:
        k = "p" + secrets.token_hex(3)
        if k not in used:
            return k


def clean_people(v, cfg: dict) -> list[dict]:
    """收件人名單檢查：姓名必填、手機或 Email 至少一個、格式、語言、至少接收一種通知、不能重複。"""
    if not isinstance(v, list):
        raise ValueError(_("收件人名單格式錯誤"))
    if len(v) > MAX_PEOPLE:
        raise ValueError(_("收件人最多 {n} 位", n=MAX_PEOPLE))
    groups = set(people_groups(cfg))
    used = {str(p.get("id")) for p in v if isinstance(p, dict) and p.get("id")}
    out, seen = [], {}
    for p in v:
        if not isinstance(p, dict):
            raise ValueError(_("收件人名單格式錯誤"))
        name = " ".join(str(p.get("name") or "").split())
        if not name:
            raise ValueError(_("收件人姓名不能空白"))
        if len(name) > 40:
            raise ValueError(_("{w}：姓名最多 {n} 個字", w=name[:40], n=40))
        phone = " ".join(str(p.get("phone") or "").split())
        email = str(p.get("email") or "").strip()
        if not phone and not email:
            raise ValueError(_("{w}：手機號碼或 Email 至少要填一個", w=name))
        if phone and not PHONE_RE.match(phone):
            raise ValueError(_("{w}：手機號碼格式不對：{v}", w=name, v=phone))
        if email and not EMAIL_RE.match(email):
            raise ValueError(_("{w}：Email 格式不對：{v}", w=name, v=email))
        lg = str(p.get("lang") or "")
        if lg and not norm(lg):
            raise ValueError(_("{w}：不支援的語言 {v}", w=name, v=lg))
        gs = [str(g) for g in (p.get("groups") or []) if str(g)]
        bad = [g for g in gs if g not in groups]
        if bad:
            raise ValueError(_("找不到通知規則「{n}」（規則要先寫在設定檔 [[notify.rules]]）", n=bad[0]))
        if not gs:
            raise ValueError(_("{w}：請至少選一種要接收的通知", w=name))
        for k in [x for x in (phone and "p:" + re.sub(r"\D", "", phone)[-9:], email and "e:" + email.lower()) if x]:
            if k in seen:
                raise ValueError(_("{v} 已經在 {w} 的資料裡", v=phone if k.startswith("p:") else email, w=seen[k]))
            seen[k] = name
        pid = str(p.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", pid):
            pid = _pid(used)
            used.add(pid)
        try:
            lv = 2 if int(p.get("min_level", 1)) >= 2 else 1
        except (TypeError, ValueError):
            lv = 1
        out.append({"id": pid, "name": name, "phone": phone, "email": email, "lang": norm(lg) or "",
                    "groups": list(dict.fromkeys(gs)), "min_level": lv, "enabled": bool(p.get("enabled", True)),
                    "note": " ".join(str(p.get("note") or "").split())[:80]})
    return out


def notify_view(cfg: dict, base: dict) -> dict:
    """給畫面的通知收件人（規則從設定檔來，收件人、語言、啟用可以在網頁改）。"""
    n = cfg.get("notify") or {}
    bn = base.get("notify") or {}
    rules = []
    for r, br in zip(n.get("rules") or [], bn.get("rules") or n.get("rules") or []):
        to = r.get("to") or {}
        rules.append({"name": r.get("name") or "", "enabled": r.get("enabled", True), "lang": r.get("lang") or "",
                      "channels": list(r.get("channels") or []), "levels": r.get("levels") or r.get("min_level"),
                      "duty": bool(r.get("duty")), "email": list(to.get("email") or []), "sms": list(to.get("sms") or []),
                      "file": {"email": list((br.get("to") or {}).get("email") or []),
                               "sms": list((br.get("to") or {}).get("sms") or [])}})
    ppl = [p for p in n.get("people") or [] if p.get("enabled", True)]
    for r in rules:
        r["people"] = sum(1 for p in ppl if r["name"] in (p.get("groups") or []))
    return {"rules": rules, "default": {"email": list(n.get("mail_to") or []),
                                        "sms": list((n.get("sms") or {}).get("to") or [])},
            "people": {"total": len(ppl), "default": sum(1 for p in ppl if "default" in (p.get("groups") or []))},
            "channels": {"email": bool(n.get("email_enabled")) and bool(n.get("smtp_host")),
                         "sms": bool((n.get("sms") or {}).get("enabled")), "webhook": bool(n.get("webhook_url"))}}


# ------------------------------------------------------------ 存取
class SysConf:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS sysconf (key TEXT PRIMARY KEY, value TEXT, ts REAL, who TEXT);
    CREATE TABLE IF NOT EXISTS sysconf_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, who TEXT, addr TEXT,
      key TEXT, old TEXT, new TEXT, note TEXT);"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.path = ""
        if cfg.get("web") and cfg["web"].get("users_db"):
            try:
                from .runtime import resolve
                self.path = resolve(cfg, cfg["web"]["users_db"])
            except Exception:
                self.path = ""
        self.base = self._snapshot()           # 設定檔原本的值（還原用）
        self.values: dict = {}
        self.rev = -1
        self.checked = 0.0
        self.lock = threading.RLock()
        self.watching = False
        self.hooks: list = []

    def _snapshot(self) -> dict:
        out = {}
        for f in FIELDS:
            out[f["key"]] = copy.deepcopy(self._cfg_get(f["path"], f["default"]))
        out["notify"] = copy.deepcopy({"rules": (self.cfg.get("notify") or {}).get("rules") or [],
                                       "mail_to": (self.cfg.get("notify") or {}).get("mail_to") or [],
                                       "sms_to": ((self.cfg.get("notify") or {}).get("sms") or {}).get("to") or [],
                                       "people": (self.cfg.get("notify") or {}).get("people") or []})
        return out

    def _cfg_get(self, path, default):
        d = self.cfg
        for p in path[:-1]:
            d = d.get(p) if isinstance(d, dict) else None
            if d is None:
                return default
        v = d.get(path[-1], default) if isinstance(d, dict) else default
        return default if v is None else v

    def _cfg_set(self, path, value):
        d = self.cfg
        for p in path[:-1]:
            d = d.setdefault(p, {})
        d[path[-1]] = value

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            c.executescript(self.SCHEMA)
            yield c
            c.commit()
        finally:
            c.close()

    # -------------------------------------------------------- 讀取與套用
    def refresh(self, force: bool = False) -> bool:
        """有新的修改就重新讀取並套用；最多每 5 秒查一次。回傳是否有變更。"""
        now = time.time()
        if not force and now - self.checked < 5:
            return False
        self.checked = now
        if not self.path or not os.path.exists(self.path):
            if self.values:
                with self.lock:
                    self.values = {}
                    self.apply()
                return True
            return False
        try:
            with self.conn() as c:
                rev = c.execute("SELECT COALESCE(MAX(id), 0) FROM sysconf_log").fetchone()[0]
                if rev == self.rev and not force:
                    return False
                rows = c.execute("SELECT key, value FROM sysconf").fetchall()
        except sqlite3.Error as e:
            log.warning(_("讀不到系統設定：%s"), e)
            return False
        vals = {}
        for r in rows:
            try:
                vals[r["key"]] = json.loads(r["value"])
            except (TypeError, ValueError):
                continue
        with self.lock:
            changed = vals != self.values or self.rev == -1
            self.values, self.rev = vals, rev
            if changed:
                self.apply()
        return changed

    def apply(self):
        """把網頁上的值套進 cfg（沒有就恢復設定檔的值）。"""
        for f in FIELDS:
            v = self.values.get(f["key"], self.base[f["key"]])
            self._cfg_set(f["path"], copy.deepcopy(v))
        n = self.cfg.setdefault("notify", {})
        bn = self.base["notify"]
        rec = self.values.get(NOTIFY_KEY) or {}
        d = rec.get("default") or {}
        n["mail_to"] = list(d["email"]) if "email" in d else list(bn["mail_to"])
        if "sms" in d or bn["sms_to"]:
            n.setdefault("sms", {})["to"] = list(d["sms"]) if "sms" in d else list(bn["sms_to"])
        rules = []
        for br in bn["rules"]:
            r = copy.deepcopy(br)
            o = (rec.get("rules") or {}).get(str(r.get("name") or ""))
            if o:
                to = dict(r.get("to") or {})
                for ch in ("email", "sms"):
                    if ch in o:
                        to[ch] = list(o[ch])
                r["to"] = to
                if "enabled" in o:
                    r["enabled"] = bool(o["enabled"])
                if "lang" in o:
                    r["lang"] = o["lang"] or None
            rules.append(r)
        if bn["rules"] or "rules" in n:
            n["rules"] = rules
        n["people"] = copy.deepcopy(self.values[PEOPLE_KEY] if PEOPLE_KEY in self.values else bn.get("people") or [])
        if "ui.default_lang" in self.values and not self.cfg.get("_lang_cli") and not os.environ.get("DTM_LANG"):
            from .i18n import set_default
            set_default(self.values["ui.default_lang"])
        for fn in list(self.hooks):
            try:
                fn(self)
            except Exception as e:
                log.warning("sysconf hook: %s", e)

    def start_watch(self, every: float = 10.0):
        """背景每 10 秒檢查一次（通知收件人等要在沒有人開畫面時也能更新）。"""
        if self.watching:
            return
        self.watching = True
        self.refresh(force=True)

        def loop():
            while True:
                time.sleep(every)
                try:
                    self.refresh()
                except Exception as e:
                    log.warning("sysconf watch: %s", e)
        threading.Thread(target=loop, name="sysconf", daemon=True).start()

    # -------------------------------------------------------- 給畫面
    def view(self) -> dict:
        self.refresh()
        fields = []
        for f in FIELDS:
            fields.append({"key": f["key"], "sec": f["sec"], "type": f["type"], "label": _(f["label"]),
                           "help": _(f["help"]) if f.get("help") else "", "unit": _(f["unit"]) if f.get("unit") else "",
                           "lo": f.get("lo"), "hi": f.get("hi"), "max": f.get("max"),
                           "options": [{"v": o, "label": _((FX_LABELS if f.get("labels") == "fx" else REGION_LABELS).get(o, o))}
                                       for o in f["options"]]
                           if f.get("options") else None,
                           "value": self._cfg_get(f["path"], f["default"]), "file": self.base[f["key"]],
                           "web": f["key"] in self.values})
        langs = [{"code": k, "name": v} for k, v in LANGS.items()]
        return {"fields": fields, "sections": {k: _(v) for k, v in SECTIONS.items()}, "langs": langs,
                "notify": notify_view(self.cfg, {"notify": {"rules": self.base["notify"]["rules"]}}),
                "notify_web": NOTIFY_KEY in self.values, "readonly": self.readonly_view(),
                "store": self.path, "rev": self.rev}

    def readonly_view(self) -> list[dict]:
        out = []
        for title, paths in READONLY:
            items = []
            for p in paths:
                v = self._cfg_get(p, None)
                if v in (None, "", []):
                    continue
                if SECRET_HINT.search(p[-1]) and p[-1] not in ("tls_cert",):
                    v = "••••••"
                items.append({"key": "[" + ".".join(p[:-1]) + "] " + p[-1], "value": v})
            if items:
                out.append({"title": _(title), "items": items})
        return out

    # -------------------------------------------------------- 修改
    def validate(self, key: str, v):
        if key == NOTIFY_KEY:
            return clean_recipients(v, {"notify": {"rules": self.base["notify"]["rules"]}})
        if key == PEOPLE_KEY:
            return clean_people(v, {"notify": {"rules": self.base["notify"]["rules"]}})
        f = BY_KEY.get(key)
        if not f:
            raise ValueError(_("這個項目不能在網頁上修改：{k}", k=key))
        t = f["type"]
        label = _(f["label"])
        if t == "bool":
            if not isinstance(v, bool):
                raise ValueError(_("{f}：只能是開或關", f=label))
            return v
        if t in ("int", "float"):
            try:
                x = float(v)
            except (TypeError, ValueError):
                raise ValueError(_("{f}：請輸入數字", f=label))
            if x != x or not f["lo"] <= x <= f["hi"]:
                raise ValueError(_("{f}：範圍 {lo}–{hi}", f=label, lo=f["lo"], hi=f["hi"]))
            return int(round(x)) if t == "int" else round(x, 2)
        if t == "str":
            s = " ".join(str(v or "").split())
            if len(s) > f.get("max", 100):
                raise ValueError(_("{f}：最多 {n} 個字", f=label, n=f.get("max", 100)))
            return s
        if t == "lang":
            lg = norm(v)
            if not lg:
                raise ValueError(_("{f}：不支援的語言 {v}", f=label, v=v))
            return lg
        if t == "iplist":
            return parse_ips(v)
        if t == "choice":
            if v not in f["options"]:
                raise ValueError(_("{f}：不支援的選項 {v}", f=label, v=v))
            return v
        if t == "email":
            s = str(v or "").strip()
            if s and not EMAIL_RE.match(s):
                raise ValueError(_("{f}：Email 格式不對：{v}", f=label, v=s))
            return s[:f.get("max", 80)]
        if t == "currency":
            s = str(v or "").strip().upper()
            if not CURRENCY_RE.match(s):
                raise ValueError(_("{f}：請填 3 個英文字母的幣別代碼，例如 TWD", f=label))
            return s
        raise ValueError(key)

    def set(self, changes: dict, who: str, addr: str, note: str = "", client: str = "") -> list[str]:
        """一次存多個項目（全部檢查通過才寫入）；回傳有變動的項目。"""
        if not self.path:
            raise ValueError(_("沒有設定 [web] users_db，無法儲存系統設定"))
        clean = {}
        for k, v in (changes or {}).items():
            clean[k] = self.validate(k, v)
        if "security.allow_ips" in clean and client and not ip_allowed(client, clean["security.allow_ips"]):
            raise ValueError(_("你現在用的電腦（{ip}）不在清單裡，存檔後你自己就連不進來了。請先把它加進清單", ip=client))
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        now = time.time()
        changed = []
        with self.lock, self.conn() as c:
            for k, v in clean.items():
                old = c.execute("SELECT value FROM sysconf WHERE key=?", (k,)).fetchone()
                if old and json.loads(old["value"]) == v:
                    continue
                if not old and k not in LIST_KEYS and v == self.base.get(k):
                    continue                          # 跟設定檔一樣，不必另存
                newj = json.dumps(v, ensure_ascii=False)
                c.execute("INSERT INTO sysconf (key, value, ts, who) VALUES (?,?,?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts, who=excluded.who",
                          (k, newj, now, who))
                # old = NULL 表示修改前用的是設定檔的值（還原 = 刪掉網頁上的值）
                c.execute("INSERT INTO sysconf_log (ts, who, addr, key, old, new, note) VALUES (?,?,?,?,?,?,?)",
                          (now, who, addr, k, old["value"] if old else None, newj, note))
                changed.append(k)
        if changed:
            self.refresh(force=True)
        return changed

    def reset(self, key: str, who: str, addr: str) -> bool:
        """刪除網頁上的值，恢復設定檔的值。"""
        if not self.path or not os.path.exists(self.path):
            return False
        if key not in LIST_KEYS and key not in BY_KEY:
            raise ValueError(_("這個項目不能在網頁上修改：{k}", k=key))
        with self.lock, self.conn() as c:
            old = c.execute("SELECT value FROM sysconf WHERE key=?", (key,)).fetchone()
            if not old:
                return False
            c.execute("DELETE FROM sysconf WHERE key=?", (key,))
            c.execute("INSERT INTO sysconf_log (ts, who, addr, key, old, new, note) VALUES (?,?,?,?,?,?,?)",
                      (time.time(), who, addr, key, old["value"], None, "reset"))
        self.refresh(force=True)
        return True

    def history(self, limit: int = 100) -> list[dict]:
        if not self.path or not os.path.exists(self.path):
            return []
        with self.conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM sysconf_log ORDER BY id DESC LIMIT ?", (int(limit),))]
        for r in rows:
            f = BY_KEY.get(r["key"])
            r["label"] = _(f["label"]) if f else {NOTIFY_KEY: _("通知收件人"), PEOPLE_KEY: _("收件人名單")}.get(r["key"], r["key"])
            r["old_file"] = r["old"] is None          # 修改前是設定檔的值
            r["new_file"] = r["new"] is None          # 恢復成設定檔的值
            for k in ("old", "new"):
                try:
                    r[k] = json.loads(r[k]) if r[k] is not None else (self.base.get(r["key"]) if f else None)
                except (TypeError, ValueError):
                    pass
        return rows

    def revert(self, log_id: int, who: str, addr: str, client: str = "") -> str:
        """把某一筆修改還原成修改前的值。"""
        with self.conn() as c:
            r = c.execute("SELECT * FROM sysconf_log WHERE id=?", (int(log_id),)).fetchone()
        if not r:
            raise ValueError(_("找不到這筆修改紀錄"))
        if r["old"] is None:
            self.reset(r["key"], who, addr)
        else:
            self.set({r["key"]: json.loads(r["old"])}, who, addr, note=f"revert #{log_id}", client=client)
        return r["key"]

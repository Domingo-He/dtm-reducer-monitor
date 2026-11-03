"""通知：手機簡訊（4G 簡訊數據機、通用 HTTP 簡訊閘道）、Email、Webhook。

- 簡訊：4G 數據機不需要網際網路，適合封閉的工控網路；通用 HTTP 閘道可接三竹、Twilio、中華電信等
  （設定範例見 config.example.toml）。主要供應商失敗時可改用備援。
- Webhook：可自訂標頭與內容模板，用來接 LINE、Teams、企業微信、Slack 或公司內部系統（見 README）。
- 每個通道有自己的最低等級：預設 Email／Webhook 注意以上、簡訊只發警戒（簡訊有成本）。
- 同一警報、同一通道在冷卻時間內不重發；簡訊另有每日上限。
- 密碼與 token 可寫成 "env:變數名稱"，從環境變數讀取，避免明碼放在設定檔。
- 所有發送結果寫入 notify_log，可在儀表板查看。
- 2.0：收件規則（依廠區、等級、階段）、值班表、逐級通報、夜間延後、每日摘要、簡訊備援供應商與模板。
- 2.1：收件人名單（[[notify.people]] 或畫面「警報與通知 → 收件人」）：每個人用自己的語言收通知
  （同一廠區有外籍同仁時，同一則警報分語言各發一次）；簡訊送達回報（手機已接收）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import smtplib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

from .config import axis_name
from .i18n import N_, TDict, _, get_lang, in_lang, norm, use

log = logging.getLogger("dtm.notify")
LEVEL_TXT = TDict({0: "解除", 1: "注意", 2: "警戒"})
SMS_MAX = 70   # 中文、日韓文、越南文（UCS-2）單則簡訊上限；純英文（GSM-7）可到 160


def diag_suffix(primary: str) -> str:
    """警報訊息尾端的「（研判：…）」；簡訊會把它拿掉以節省字數。"""
    return _("（研判：{primary}）", primary=primary)


def _diag_pattern() -> str:
    tpl = _("（研判：{primary}）")
    a, _sep, b = tpl.partition("{primary}")
    return re.escape(a) + ".*?" + re.escape(b)


def secret(v):
    """'env:NAME' → 環境變數 NAME 的值。"""
    if isinstance(v, str) and v.startswith("env:"):
        return os.environ.get(v[4:], "")
    return v


def e164(phone: str, cc: str = "886") -> str:
    p = re.sub(r"[^\d+]", "", phone)
    if p.startswith("+"):
        return p
    if p.startswith("0"):
        return f"+{cc}{p[1:]}"
    return f"+{p}"


class _Blank(dict):
    def __missing__(self, k):
        return ""


def mask(t: str) -> str:
    t = str(t)
    return t if len(t) <= 6 else t[:3] + "…" + t[-3:]


def pkey(addr: str, cc: str = "886") -> str:
    """比對同一個收件人用：Email 轉小寫；手機只留數字、去掉國碼與開頭的 0（0912… 與 +886912… 視為同一支）。"""
    a = str(addr or "").strip()
    if "@" in a:
        return a.lower()
    d = re.sub(r"\D", "", a)
    if a.startswith("+") and cc and d.startswith(cc):
        d = d[len(cc):]
    return d.lstrip("0")[-9:]


# ------------------------------------------------------------ 簡訊送達回報
REPORT_DELIVERED = r"^(0|DELIVRD|DELIVERED|delivered)$"
REPORT_PENDING = r"^(ENROUTE|ACCEPTD|ACCEPTED|accepted|queued|sent|SENT)$"


def _csv_fields(s: str) -> list[str]:
    out, cur, q = [], "", False
    for ch in s:
        if ch == '"':
            q = not q
            continue
        if ch == "," and not q:
            out.append(cur.strip())
            cur = ""
            continue
        cur += ch
    out.append(cur.strip())
    return out


def _ucs2(s: str) -> str:
    if s and re.fullmatch(r"(?:[0-9A-Fa-f]{4})+", s) and not s.isdigit():
        try:
            return bytes.fromhex(s).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return s
    return s


def parse_reports(buf: str) -> tuple[list[dict], list[int]]:
    """解析 4G 數據機文字模式的送達回報（3GPP TS 27.005）：
    存在數據機裡的 +CMGL: 序號,狀態,fo,mr,ra,tora,scts,dt,st；直接送出的 +CDS: fo,mr,ra,tora,scts,dt,st。
    回傳 ([{"mr", "phone", "st"}], 要刪除的序號)。一般收到的簡訊（第 3 欄是電話號碼）不處理、不刪除。"""
    reps, idx = [], []
    for line in buf.splitlines():
        line = line.strip()
        if line.startswith("+CMGL:"):
            f = _csv_fields(line[6:])
            if len(f) >= 9 and f[2].isdigit() and f[3].isdigit():
                reps.append({"mr": int(f[3]), "phone": _ucs2(f[4]), "st": int(f[-1]) if f[-1].isdigit() else -1})
                if f[0].isdigit():
                    idx.append(int(f[0]))
        elif line.startswith("+CDS:"):
            f = _csv_fields(line[5:])
            if len(f) >= 7 and f[1].isdigit():
                reps.append({"mr": int(f[1]), "phone": _ucs2(f[2]), "st": int(f[-1]) if f[-1].isdigit() else -1})
    return reps, idx


def report_status(st: int) -> str:
    """TP-Status：0–31 已送達、32–63 業者還在重送、64 以上送不到。"""
    if 0 <= st < 32:
        return "delivered"
    if 32 <= st < 64:
        return "pending"
    return "failed"


def modem_ref(mr: int, phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone))
    return f"modem:{int(mr)}:{digits[-6:]}"


def report_params(query: dict, body: bytes = b"", ctype: str = ""):
    """回呼內容：網址參數＋表單或 JSON 本文（JSON 陣列 = 批次回報，每筆都帶上網址參數）。"""
    out = dict(query or {})
    if body:
        if "json" in (ctype or "").lower() or body[:1] in (b"{", b"["):
            try:
                o = json.loads(body)
            except ValueError:
                o = None
            if isinstance(o, list):
                return [dict(out, **x) for x in o if isinstance(x, dict)]
            if isinstance(o, dict):
                out.update(o)
        else:
            out.update({k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "replace")).items()})
    return out


def handle_report(cfg: dict, store, params) -> tuple[int, str]:
    """通用 HTTP 簡訊閘道的送達回呼：{public_url}/api/sms/report?key=…&msgid=…&status=…
    沒有設定 [notify.sms.report] key 就不開放；key 不對一律拒絕。params 可以是 dict 或 dict 的 list（批次回報）。"""
    import hmac
    rc = ((cfg.get("notify") or {}).get("sms") or {}).get("report") or {}
    key = str(secret(rc.get("key") or ""))
    if not rc.get("enabled") or not key:
        return 404, "not found"
    items = params if isinstance(params, list) else [params or {}]
    given = str((items[0] if items else {}).get("key") or "")
    if not hmac.compare_digest(given, key):
        return 403, "forbidden"
    n = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        mid = str(it.get(rc.get("id_field") or "msgid") or "").strip()
        st = str(it.get(rc.get("status_field") or "status") or "").strip()
        if not mid:
            continue
        if re.search(rc.get("delivered") or REPORT_DELIVERED, st):
            status = "delivered"
        elif re.search(rc.get("pending") or REPORT_PENDING, st):
            status = "pending"
        else:
            status = "failed"
        if store is not None:
            n += store.update_dlr("http:" + mid, status, st[:60], time.time())
    if not n and not any(isinstance(it, dict) and it.get(rc.get("id_field") or "msgid") for it in items):
        return 400, "missing id"
    return 200, str(rc.get("reply") or "OK")


def short_text(level: int, robot: str, message: str, ts: float) -> str:
    """簡訊用短訊息：盡量塞進 70 字。"""
    stamp = time.strftime("%m/%d %H:%M", time.localtime(ts))
    head = f"[{LEVEL_TXT.get(level, level)}]{robot} "
    tail = f" {stamp}"
    limit = 160 if get_lang() == "en" else SMS_MAX
    room = limit - len(head) - len(tail)
    body = re.sub(_diag_pattern(), "", message)
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) > room:
        body = body[:max(1, room - 1)] + "…"
    return head + body + tail


class _Http:
    def __init__(self, proxy: str = "", timeout: float = 10):
        self.timeout = timeout
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
        self.opener = urllib.request.build_opener(*handlers)

    def request(self, method: str, url: str, data: bytes | None = None, headers: dict | None = None):
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")


# ------------------------------------------------------------ 通道
class Channel:
    name = ""

    def __init__(self, c: dict, http: _Http):
        self.c = c
        self.http = http
        self.enabled = bool(c.get("enabled", False))
        self.min_level = int(c.get("min_level", 1))

    def targets(self) -> list[str]:
        return list(self.c.get("to", []))

    def send(self, msg: dict, targets: list[str] | None = None) -> list[tuple[str, bool, str]]:
        raise NotImplementedError


class EmailChannel(Channel):
    name = "email"

    def targets(self):
        return list(self.c.get("mail_to", []))

    def send(self, msg, targets=None):
        n = self.c
        to = targets or self.targets()
        m = EmailMessage()
        m["Subject"] = msg["subject"]
        m["From"] = n.get("mail_from") or n.get("smtp_user")
        m["To"] = ", ".join(to)
        m.set_content(msg["text"])
        with smtplib.SMTP(n["smtp_host"], int(n["smtp_port"]), timeout=15) as s:
            if n.get("smtp_tls"):
                s.starttls()
            if n.get("smtp_user"):
                s.login(n["smtp_user"], secret(n.get("smtp_password", "")))
            s.send_message(m)
        return [(", ".join(to), True, _("已寄出"))]


def _fill(tpl, vals: dict):
    """把模板裡字串的 {text} 等欄位換掉；dict、list 逐層處理，數字與布林原樣保留。"""
    if isinstance(tpl, str):
        return tpl.format_map(_Blank(vals))
    if isinstance(tpl, dict):
        return {k: _fill(v, vals) for k, v in tpl.items()}
    if isinstance(tpl, list):
        return [_fill(v, vals) for v in tpl]
    return tpl


class WebhookChannel(Channel):
    """HTTP POST JSON。webhook_body 空白時送預設欄位；有模板時依模板組內容（例如直接呼叫 LINE Messaging API）。"""
    name = "webhook"

    def targets(self):
        return [self.c["webhook_url"]] if self.c.get("webhook_url") else []

    def payload(self, msg) -> dict:
        tpl = self.c.get("webhook_body") or {}
        if not tpl:
            return {"text": msg["text"], "level": msg["level"], "axis": msg["axis"], "kind": msg["kind"],
                    "ts": msg["ts"], "robot": msg["robot"], "site": msg.get("site", ""), "message": msg["message"]}
        vals = {"text": msg["text"], "subject": msg.get("subject", ""), "level": LEVEL_TXT.get(msg["level"], msg["level"]),
                "robot": msg["robot"], "site": msg.get("site", ""), "message": msg["message"],
                "axis": msg["axis"] if msg["axis"] is not None else "", "kind": msg.get("kind_label", msg["kind"]),
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(msg["ts"])), "short": msg.get("short", "")}
        return _fill(tpl, vals)

    def send(self, msg, targets=None):
        out = []
        headers = {"Content-Type": "application/json"}
        headers.update({k: secret(str(v)) for k, v in (self.c.get("webhook_headers") or {}).items()})
        body = json.dumps(self.payload(msg), ensure_ascii=False).encode()
        for url in targets or self.targets():
            code, txt = self.http.request("POST", secret(url), body, headers)
            out.append((mask(url), 200 <= code < 300, f"HTTP {code} {txt[:120]}"))
        return out


class SmsChannel(Channel):
    name = "sms"
    PROVIDERS = ("http", "modem")

    def __init__(self, c, http, serial_factory=None):
        super().__init__(c, http)
        self.provider = c.get("provider", "http")
        self.fallback = [p for p in c.get("fallback", []) if p in self.PROVIDERS and p != self.provider]
        self.serial_factory = serial_factory
        self.port_lock = threading.Lock()          # 發送與讀回報不能同時用序列埠
        self.cds_buf: list[dict] = []              # 發送時順便收到的 +CDS 送達回報

    @property
    def report_on(self) -> bool:
        return bool((self.c.get("report") or {}).get("enabled"))

    def uses_modem(self) -> bool:
        return self.provider == "modem" or "modem" in self.fallback

    def _msg_id(self, txt: str) -> str:
        """從簡訊閘道的回應抓訊息編號（送達回呼用來對應）。"""
        r = self.c.get("report") or {}
        pat = r.get("id_regex") or ""
        if pat:
            m = re.search(pat, txt)
            if m:
                return (m.group(1) if m.groups() else m.group(0)).strip()
        path = r.get("id_json") or ""
        if path:
            try:
                o = json.loads(txt)
            except ValueError:
                return ""
            for k in path.split("."):
                if isinstance(o, list) and k.isdigit() and int(k) < len(o):
                    o = o[int(k)]
                elif isinstance(o, dict):
                    o = o.get(k)
                else:
                    return ""
            return "" if o is None or isinstance(o, (dict, list)) else str(o)
        return ""

    def _send_with(self, provider: str, phones: list[str], text: str) -> list[tuple[str, bool, str]]:
        fn = {"http": self._generic, "modem": self._modem}.get(provider)
        if not fn:
            return [("-", False, _("未知簡訊供應商 {p}", p=provider))]
        try:
            if provider == "modem":
                return fn(phones, text)
            return [fn(p, text) for p in phones]
        except Exception as e:
            return [(mask(p), False, f"{provider}: {type(e).__name__}: {e}") for p in phones]

    def send(self, msg, targets=None):
        """主要供應商送不出去的門號，依序改用 fallback 的供應商（例如數據機故障改走 HTTP 閘道）。"""
        text = msg["short"]
        phones = list(targets or self.targets())
        res = self._send_with(self.provider, phones, text)
        for prov in self.fallback:
            failed = [i for i, r in enumerate(res) if not r[1]]
            if not failed:
                break
            if self.provider == "modem" and len(res) == 1 and len(phones) > 1:   # 數據機整個失敗只回一筆
                failed_phones = phones
            else:
                failed_phones = [phones[i] for i in failed if i < len(phones)]
            res2 = self._send_with(prov, failed_phones, text)
            ok = [r for r in res if r[1]]
            res = ok + [(r[0], r[1], f"{r[2]}（{_('改用 {p}', p=prov)}）", *r[3:]) for r in res2]
        return res

    def _generic(self, phone, text):
        """通用 HTTP 簡訊閘道（三竹、Twilio、中華電信等）：網址與參數中的 {phone}、{phone_e164}、{message} 會被替換；
        basic_user／basic_password 會組成 HTTP Basic 認證標頭（Twilio 用）。"""
        g = self.c.get("http", {})
        vals = {"phone": re.sub(r"\D", "", phone), "phone_e164": e164(phone, str(self.c.get("country_code", "886"))),
                "message": text}
        params = {k: secret(str(v)).format_map(_Blank(vals)) for k, v in g.get("params", {}).items()}
        headers = {k: secret(str(v)) for k, v in g.get("headers", {}).items()}
        if g.get("basic_user"):
            raw = f"{secret(g['basic_user'])}:{secret(g.get('basic_password', ''))}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        method = g.get("method", "POST").upper()
        url = secret(str(g.get("url", ""))).format_map(_Blank(vals))
        if not url:
            return (mask(phone), False, _("未設定簡訊閘道網址 [notify.sms.http] url"))
        if method == "GET":
            code, txt = self.http.request("GET", url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params),
                                          None, headers)
        elif g.get("json"):
            headers.setdefault("Content-Type", "application/json")
            code, txt = self.http.request("POST", url, json.dumps(params, ensure_ascii=False).encode(), headers)
        else:
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            code, txt = self.http.request("POST", url, urllib.parse.urlencode(params).encode(), headers)
        pat = g.get("success", "")
        ok = 200 <= code < 300 and (not pat or re.search(pat, txt) is not None)
        mid = self._msg_id(txt) if ok and self.report_on else ""
        return (mask(phone), ok, f"HTTP {code} {txt[:120]}", "http:" + mid if mid else "")

    def _serial(self):
        m = self.c.get("modem", {})
        if self.serial_factory is None:
            import serial  # pyserial；沒裝會丟 ImportError
            self.serial_factory = lambda: serial.Serial(m.get("port", "COM3"), int(m.get("baud", 115200)), timeout=2)
        return self.serial_factory()

    @staticmethod
    def _cmd(ser, c, wait="OK", timeout=5.0):
        ser.write((c + "\r").encode())
        buf, t0 = "", time.time()
        while time.time() - t0 < timeout:
            buf += ser.read(256).decode(errors="ignore")
            if wait in buf or "ERROR" in buf:
                break
        return buf

    def _modem(self, phones, text):
        """4G／GSM 簡訊數據機（AT 指令、UCS-2 文字模式）：不需要網際網路，適合封閉的工控網路。
        開啟送達回報時，每則簡訊要求業者回報（TP-SRR），回報由 poll_reports 讀回。"""
        m = self.c.get("modem", {})
        try:
            ser = self._serial()
        except ImportError:
            return [("-", False, _("需要安裝 pyserial：pip install pyserial"))]
        ucs2 = lambda s: s.encode("utf-16-be").hex().upper()  # noqa: E731
        out = []
        with self.port_lock:
            try:
                cmd = lambda c, wait="OK", timeout=5.0: self._cmd(ser, c, wait, timeout)  # noqa: E731
                setup = ["AT", "AT+CMGF=1", 'AT+CSCS="UCS2"', "AT+CSMP=49,167,0,8" if self.report_on else "AT+CSMP=17,167,0,8"]
                for c in setup:
                    if "ERROR" in cmd(c):
                        return [("-", False, _("數據機回應錯誤：{cmd}", cmd=c))]
                if self.report_on and m.get("cnmi", "AT+CNMI=2,1,0,2,0"):
                    cmd(m.get("cnmi", "AT+CNMI=2,1,0,2,0"))      # 不支援也照發，只是收不到回報
                body = ucs2(text[:SMS_MAX])
                for p in phones:
                    r = cmd(f'AT+CMGS="{ucs2(re.sub(r"[^0-9+]", "", p))}"', ">", 5)
                    if ">" not in r:
                        out.append((mask(p), False, _("數據機未進入輸入模式"), ""))
                        continue
                    ser.write(body.encode() + b"\x1a")
                    buf, t0 = "", time.time()
                    while time.time() - t0 < 30 and "OK" not in buf and "ERROR" not in buf:
                        buf += ser.read(256).decode(errors="ignore")
                    self.cds_buf += parse_reports(buf)[0]
                    mr = re.search(r"\+CMGS:\s*(\d+)", buf)
                    ok = "+CMGS" in buf and "ERROR" not in buf
                    ref = modem_ref(int(mr.group(1)), p) if ok and mr and self.report_on else ""
                    out.append((mask(p), ok, buf.strip()[-60:], ref))
            finally:
                ser.close()
        return out

    def poll_reports(self) -> list[dict]:
        """讀出數據機裡的送達回報（AT+CMGL），讀完刪掉回報（一般簡訊不動）。"""
        out, self.cds_buf = list(self.cds_buf), []
        if not self.uses_modem():
            return out
        m = self.c.get("modem", {})
        try:
            ser = self._serial()
        except ImportError:
            return out
        with self.port_lock:
            try:
                for c in ("AT", "AT+CMGF=1", 'AT+CSCS="GSM"', "AT+CSDH=1"):
                    self._cmd(ser, c)
                if m.get("report_mem"):
                    mem = re.sub(r"[^A-Z]", "", str(m["report_mem"]).upper())[:2]
                    if mem:
                        self._cmd(ser, f'AT+CPMS="{mem}"')
                reps, idx = parse_reports(self._cmd(ser, 'AT+CMGL="ALL"', "OK", 10))
                for i in idx:
                    self._cmd(ser, f"AT+CMGD={i}")
                out += reps
            finally:
                ser.close()
        return out


CHANNEL_LABELS = TDict({"sms": "簡訊", "email": "Email", "webhook": "Webhook"})


# ------------------------------------------------------------ 值班表、夜間時段
def _hm(s: str) -> int:
    h, m = str(s).strip().split(":")
    return int(h) * 60 + int(m)


def in_window(spec: str, now: float) -> bool:
    """spec 例如 "22:00-07:00"（可跨午夜）；空白 = 不在時段內。"""
    if not spec or "-" not in spec:
        return False
    a, b = spec.split("-", 1)
    t = time.localtime(now)
    cur = t.tm_hour * 60 + t.tm_min
    s, e = _hm(a), _hm(b)
    return s <= cur < e if s <= e else (cur >= s or cur < e)


def _days_ok(spec: str, wday: int) -> bool:
    """spec："*"、"1-5"（週一到週五）、"6,7"；wday 1 = 週一。"""
    spec = str(spec or "*").strip()
    if spec in ("*", ""):
        return True
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            if int(a) <= wday <= int(b):
                return True
        elif part.strip() and int(part) == wday:
            return True
    return False


def on_duty(cfg: dict, now: float | None = None) -> list[dict]:
    """目前當班的人：值班表 CSV（日期、起訖、姓名、電話、Email）優先，其次設定檔的固定班別。"""
    import csv
    now = now or time.time()
    n = cfg["notify"]
    out = []
    path = n.get("duty_file", "")
    if path:
        from .runtime import resolve
        path = resolve(cfg, path)
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                today = time.strftime("%Y-%m-%d", time.localtime(now))
                yday = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
                for r in csv.DictReader(f):
                    d = (r.get("date") or "").strip()
                    spec = f"{r.get('start', '00:00')}-{r.get('end', '24:00')}".replace("24:00", "23:59")
                    s, e = _hm(r.get("start") or "00:00"), _hm((r.get("end") or "23:59").replace("24:00", "23:59"))
                    overnight = e <= s
                    if d and d != today and not (overnight and d == yday):
                        continue
                    if not in_window(spec, now):
                        continue
                    if overnight and d == today and time.localtime(now).tm_hour * 60 + time.localtime(now).tm_min < e:
                        continue                                # 今天的夜班還沒開始（現在是昨天夜班的後半段）
                    if overnight and d == yday and time.localtime(now).tm_hour * 60 + time.localtime(now).tm_min >= s:
                        continue
                    out.append({"name": r.get("name", ""), "phones": [x for x in [(r.get("phone") or "").strip()] if x],
                                "email": [x for x in [(r.get("email") or "").strip()] if x], "source": "csv"})
        except OSError as e:
            log.warning(_("讀不到值班表 %s：%s"), path, e)
    if not out:
        wday = time.localtime(now).tm_wday + 1
        for s in n.get("duty", []) or []:
            spec = f"{s.get('start', '00:00')}-{s.get('end', '23:59')}"
            if _days_ok(s.get("days", "*"), wday) and in_window(spec, now):
                out.append({"name": s.get("name", ""), "phones": list(s.get("phones", [])),
                            "email": list(s.get("email", [])), "source": "config"})
    return out


# ------------------------------------------------------------ 通知中心
class Notifier:
    """通知中心。
    - 沒有設定 [[notify.rules]]：沿用舊方式，每個通道依自己的最低等級與收件人發送。
    - 有規則：依廠區／機器人、等級、階段（1 立即、2 逾時未確認、3 再逾時）決定發給誰、走哪個通道；
      可以指定「值班人員」（值班表）；夜間時段（quiet_hours）「注意」等級延到每日摘要。
    - 逐級通報：警戒在 stage2_after_s 秒內沒人確認 → 第 2 階段；stage3_after_s → 第 3 階段；確認或解除就停止。
    - gate：邊緣主機連得上中央時由中央統一發；連不上才由邊緣自己發「警戒」（備援）。"""

    def __init__(self, cfg: dict, store=None, http: _Http | None = None, serial_factory=None, gate=None,
                 summary_fn=None, is_acked=None, clock=time.time):
        n = cfg["notify"]
        self.cfg = cfg
        self.n = n
        self.robot = cfg["robot"]["name"]
        self.site = cfg["system"].get("site_name", "")
        self.store = store
        self.http = http or _Http(n.get("proxy", ""))
        self.cooldown = float(n["cooldown_s"])
        self.last: dict[tuple, float] = {}
        self.sms_day: tuple[str, int] = ("", 0)
        self.lock = threading.Lock()
        self.gate = gate
        self.summary_fn = summary_fn
        self.is_acked = is_acked
        self.clock = clock
        self.pending: dict[str, dict] = {}       # 逐級通報中的警報
        self.deferred: list[dict] = []           # 夜間延後、併入每日摘要的「注意」
        self.last_summary_day = ""
        self.last_poll = 0.0                     # 上次讀 4G 數據機送達回報的時間
        self.polling = False
        self.sync = False                        # 測試用：true = 同步發送（不開執行緒）
        legacy = dict(n, enabled=bool(n.get("email_enabled")) and bool(n.get("smtp_host")) and bool(n.get("mail_to")),
                      min_level=n.get("min_level", 1))
        self.channels: dict[str, Channel] = {
            "sms": SmsChannel(n.get("sms", {}), self.http, serial_factory),
            "email": EmailChannel(legacy, self.http),
            "webhook": WebhookChannel(dict(n, enabled=bool(n.get("webhook_url")), min_level=n.get("min_level", 1)),
                                      self.http),
        }

    @property
    def rules(self) -> list[dict]:
        """每次都從設定讀（網頁「設定 → 通知」改了收件人、語言、啟用，不必重新啟動）。"""
        return [r for r in (self.n.get("rules") or []) if r.get("enabled", True)]

    def _sync_targets(self):
        """預設收件人也可能在網頁上改過（Email 通道的設定是啟動時複製的一份）。"""
        em = self.channels.get("email")
        if em is not None:
            em.c["mail_to"] = list(self.n.get("mail_to") or [])
            has_to = bool(em.c["mail_to"]) or any(p.get("email") for p in self.people()) or any(
                (r.get("to") or {}).get("email") for r in self.rules)
            em.enabled = bool(self.n.get("email_enabled")) and bool(self.n.get("smtp_host")) and has_to

    # -------------------------------------------------------- 收件人名單（每個人自己的語言）
    @property
    def cc(self) -> str:
        return str((self.n.get("sms") or {}).get("country_code", "886"))

    def people(self, level: int | None = None) -> list[dict]:
        out = []
        for p in self.n.get("people") or []:
            if not p.get("enabled", True):
                continue
            if level is not None and level < int(p.get("min_level", 1) or 1):
                continue
            out.append(p)
        return out

    def _people_targets(self, channel: str, group: str, level: int | None) -> list[str]:
        key = {"sms": "phone", "email": "email"}.get(channel)
        if not key:
            return []
        return [p[key] for p in self.people(level) if p.get(key) and group in (p.get("groups") or [])]

    def person_of(self, addr: str) -> dict | None:
        k = pkey(addr, self.cc)
        if not k:
            return None
        for p in self.n.get("people") or []:
            for f in ("phone", "email"):
                if p.get(f) and pkey(p[f], self.cc) == k:
                    return p
        return None

    def _uniq(self, targets: list[str]) -> list[str]:
        seen, out = set(), []
        for t in targets:
            k = pkey(t, self.cc) or t
            if k not in seen:
                seen.add(k)
                out.append(t)
        return out

    def split_lang(self, name: str, targets: list[str] | None, lang: str | None) -> list[tuple[str | None, list[str] | None]]:
        """同一批收件人依每個人的語言分組（名單裡沒有的人用規則的語言或系統語言）。"""
        if name == "webhook":
            return [(lang, targets)]
        if not targets:                    # 通道的預設收件人：裡面有名單上設了語言的人才拆開
            ch = self.channels.get(name)
            dflt = ch.targets() if ch else []
            if targets is not None or not any((self.person_of(t) or {}).get("lang") for t in dflt):
                return [(lang, targets)]
            targets = list(dflt)
        groups: dict = {}
        for t in targets:
            p = self.person_of(t)
            lg = (p or {}).get("lang") or lang or None
            groups.setdefault(lg, []).append(t)
        return list(groups.items())

    def _names(self, targets) -> dict[str, str]:
        out = {}
        for t in targets or []:
            p = self.person_of(t)
            if p:
                out[mask(t)] = out[t] = p.get("name", "")
        return out

    # -------------------------------------------------------- 內容
    def sms_text(self, level: int, robot: str, message: str, ts: float, site: str = "") -> str:
        tpl = (self.n.get("sms") or {}).get("template", "")
        if not tpl:
            return short_text(level, robot, message, ts)
        limit = 160 if get_lang() == "en" else SMS_MAX
        body = re.sub(_diag_pattern(), "", message)
        body = re.sub(r"\s+", " ", body).strip()
        vals = {"level": LEVEL_TXT.get(level, level), "robot": robot, "site": site,
                "brand": (self.n.get("sms") or {}).get("brand", ""), "time": time.strftime("%m/%d %H:%M", time.localtime(ts))}
        head = tpl.replace("{message}", "\x00")
        txt = head.format_map(_Blank(vals))
        room = limit - len(txt) + 1
        if len(body) > room:
            body = body[:max(1, room - 1)] + "…"
        return txt.replace("\x00", body)[:limit]

    def compose(self, level: int, message: str, axis, kind: str, ts: float, extra: str = "", robot: str | None = None,
                site: str | None = None, stage: int = 1) -> dict:
        robot = robot or self.robot
        site = self.site if site is None else site
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        lv = LEVEL_TXT.get(level, str(level))
        who = f"{site} {robot}".strip() if site and site not in robot else robot
        text = _("【{level}】{robot}\n{message}\n軸：{axis}｜類別：{kind}\n時間：{time}", level=lv, robot=who,
                 message=message, axis=axis_name(self.cfg, axis) if axis else "—", kind=_(kind), time=stamp)
        if stage >= 2:
            text = _("【逾時未確認・第 {n} 階段通報】", n=stage) + "\n" + text
        text += extra                                   # 維護決策：備品與作業包摘要（簡訊不附，只留在 Email／Webhook）
        url = self.n.get("public_url", "")
        if url:
            text += _("\n儀表板：{url}", url=url)
        return {"level": level, "axis": axis, "kind": kind, "ts": ts, "robot": robot, "site": site, "message": message,
                "text": text, "subject": f"[{lv}] {who} {message[:60]}", "kind_label": _(kind), "url": url,
                "short": self.sms_text(level, who, (_("逾時未確認：") if stage >= 2 else "") + message, ts, site),
                "stage": stage, "lang": get_lang(),
                "_args": (level, message, axis, kind, ts, bool(extra), robot, site, stage)}

    def in_lang(self, msg: dict, lang: str | None) -> dict:
        """同一則通知換成規則指定的語言（[[notify.rules]] lang）；警報文字能換的就換，不能換的保留原文。"""
        lang = norm(lang)
        if not lang or lang == msg.get("lang") or "_args" not in msg:
            return msg
        cache = msg.setdefault("_tr", {})
        if lang not in cache:
            level, message, axis, kind, ts, has_extra, robot, site, stage = msg["_args"]
            with use(lang):
                extra = ""
                if has_extra:
                    try:
                        from .workpack import notify_text
                        extra = notify_text(self.cfg, msg.get("akey", ""), level)
                    except Exception:
                        extra = ""
                m = self.compose(level, in_lang(message, lang), axis, kind, ts, extra, robot, site, stage)
            m["akey"] = msg.get("akey", "")
            cache[lang] = m
        return cache[lang]

    def _sms_quota(self, n: int) -> bool:
        today = time.strftime("%Y-%m-%d")
        with self.lock:
            day, cnt = self.sms_day
            if day != today:
                day, cnt = today, 0
            cap = int(self.n.get("sms", {}).get("max_per_day", 30))
            if cnt + n > cap:
                return False
            self.sms_day = (day, cnt + n)
        return True

    # -------------------------------------------------------- 路由
    def _rule_matches(self, r: dict, level: int, robot: str, site: str, stage: int, cleared: bool) -> bool:
        if int(r.get("stage", 1)) != stage or level < int(r.get("min_level", 1)):
            return False
        if cleared and not r.get("notify_clear", self.n.get("notify_clear", False)):
            return False
        sites = [str(x) for x in r.get("sites", []) or []]
        if sites and "*" not in sites and site not in sites and self.cfg["system"].get("site_id", "") not in sites:
            names = (self.cfg.get("central") or {}).get("sites") or {}       # 中央：規則可以寫廠區代碼或名稱
            if not any(sid in sites for sid, v in names.items() if (v or {}).get("name") == site):
                return False
        robots = [str(x) for x in r.get("robots", []) or []]
        return not robots or robot in robots

    def route(self, level: int, robot: str, site: str, stage: int = 1, cleared: bool = False,
              now: float | None = None) -> list[tuple[str, list[str] | None, str, bool, str | None]]:
        """回傳 [(通道, 收件人（None = 通道預設）, 規則名稱, 是否延後到摘要, 語言（None = 系統語言）)]。"""
        now = now or self.clock()
        self._sync_targets()
        quiet = level == 1 and not cleared and in_window(self.n.get("quiet_hours", ""), now)
        out = []
        if not self.rules:
            if stage != 1:
                return []
            for name, ch in self.channels.items():
                if not (ch.enabled and level >= ch.min_level):
                    continue
                if name == "webhook":
                    if ch.targets():
                        out.append((name, None, "", quiet, None))
                    continue
                extra = self._people_targets(name, "default", level)
                if extra:
                    out.append((name, self._uniq(ch.targets() + extra), "", quiet, None))
                elif ch.targets():
                    out.append((name, None, "", quiet, None))      # None = 通道的預設收件人
            return out
        duty = None
        for r in self.rules:
            if not self._rule_matches(r, level, robot, site, stage, cleared):
                continue
            to = r.get("to") or {}
            for name in r.get("channels", []):
                ch = self.channels.get(name)
                if not ch or not ch.enabled:            # 通道要先在 [notify.xxx] 啟用、填好帳號
                    continue
                targets = list(to.get(name, []) or []) + self._people_targets(name, str(r.get("name") or ""), level)
                if r.get("duty"):
                    duty = duty if duty is not None else on_duty(self.cfg, now)
                    key = {"sms": "phones", "email": "email"}.get(name)
                    if key:
                        targets += [x for d in duty for x in d.get(key, [])]
                    if not targets and name == "sms":
                        targets = list(self.n.get("duty_default_phones", []) or [])
                if not targets:
                    extra = [] if name == "webhook" else self._people_targets(name, "default", level)
                    targets = (ch.targets() + extra) if extra else (None if ch.targets() else [])
                if targets == []:
                    continue
                out.append((name, self._uniq(targets) if targets else targets, r.get("name", ""),
                            quiet and r.get("quiet", True), r.get("lang") or None))
        return out

    # -------------------------------------------------------- 發送
    def send(self, level: int, message: str, axis, kind: str, ts: float, key: str, cleared: bool = False,
             robot: str | None = None, site: str | None = None):
        robot = robot or self.robot
        site = self.site if site is None else site
        g = self.gate() if self.gate else "all"
        if g == "none" or (g == "alert_only" and (level < 2 or cleared)):
            return
        extra = ""
        if not cleared:
            try:
                from .workpack import notify_text
                extra = notify_text(self.cfg, key, level)
            except Exception as e:                     # 作業包目錄有錯也不能擋住警報
                log.warning(_("作業包摘要失敗：%s"), e)
        msg = self.compose(0 if cleared else level, (_("已解除：") if cleared else "") + message, axis, kind, ts, extra,
                           robot, site)
        msg["akey"] = key
        ekey = f"{site}|{robot}|{key}"
        esc = self.n.get("escalation") or {}
        if cleared:
            self.pending.pop(ekey, None)
        elif level >= int(esc.get("min_level", 2)) and esc.get("enabled", True):
            cur = self.pending.get(ekey)
            if not cur or cur["level"] < level:
                self.pending[ekey] = {"ts": ts, "level": level, "message": message, "axis": axis, "kind": kind,
                                      "robot": robot, "site": site, "akey": key, "stages": [1]}
        self._dispatch(msg, level, key, cleared, stage=1, now=ts)

    def _dispatch(self, msg: dict, level: int, key: str, cleared: bool, stage: int, now: float):
        base = msg
        for name, targets_all, rule, defer, lang0 in self.route(level, base["robot"], base["site"], stage, cleared, now):
            for lang, targets in self.split_lang(name, targets_all, lang0):
                self._dispatch_one(base, name, targets, rule, defer, lang, level, key, cleared, stage, now)

    def _dispatch_one(self, base, name, targets, rule, defer, lang, level, key, cleared, stage, now):
        msg = self.in_lang(base, lang)
        ch = self.channels[name]
        if cleared and not self.n.get("notify_clear", False) and not rule:
            return
        if defer:
            self.deferred.append({"ts": now, "robot": msg["robot"], "site": msg["site"], "message": msg["message"],
                                  "level": level})
            self._log(now, name, "-", level, True, _("夜間時段，併入每日摘要"), msg["message"][:200], msg, stage, rule)
            return
        tkey = ",".join(targets) if targets else ""
        ck = (f"{msg['site']}|{msg['robot']}|{key}" + (":clear" if cleared else f":L{level}:S{stage}"), name, tkey)
        with self.lock:                             # 等級升高一定通知；同等級在冷卻內不重發
            if now - self.last.get(ck, -1e18) < self.cooldown:
                return
            self.last[ck] = now
        n_to = len(targets) if targets else len(ch.targets())
        if name == "sms" and not self._sms_quota(n_to):
            self._log(now, name, "-", level, False, _("已達每日簡訊上限，未發送"), msg["short"], msg, stage, rule)
            return
        if self.sync:
            self._deliver(ch, msg, targets=targets, stage=stage, rule=rule)
        else:
            threading.Thread(target=self._deliver, args=(ch, msg), kwargs={"targets": targets, "stage": stage,
                                                                       "rule": rule}, daemon=True).start()

    def _deliver(self, ch: Channel, msg: dict, retries: int = 2, targets=None, stage: int = 1, rule: str = "") -> list:
        results = []
        for attempt in range(retries + 1):
            try:
                results = [tuple(r) + ("",) * (4 - len(r)) for r in (ch.send(msg, targets) if targets else ch.send(msg))]
                if all(r[1] for r in results):
                    break
            except Exception as e:
                results = [("-", False, f"{type(e).__name__}: {e}", "")]
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
        names = self._names(targets or ch.targets())
        track = ch.name == "sms" and getattr(ch, "report_on", False)
        for target, ok, detail, ref in results:
            who = names.get(target) or _("、").join(x for x in (names.get(t.strip(), "") for t in str(target).split(",")) if x)
            self._log(msg["ts"], ch.name, target, msg["level"], ok, detail,
                      msg["short"] if ch.name == "sms" else msg["text"][:300], msg, stage, rule,
                      person=who, ref=ref, dlr="pending" if ok and ref and track else "")
            (log.info if ok else log.warning)(_("通知 %s → %s：%s %s"), ch.name, target, _("成功") if ok else _("失敗"), detail)
        return [r[:3] for r in results]

    def _log(self, ts, channel, target, level, ok, detail, message, msg=None, stage=1, rule="", person="", ref="", dlr=""):
        if self.store is not None:
            msg = msg or {}
            self.store.add_notify_log(ts, channel, target, level, ok, detail, message, msg.get("robot", ""),
                                      msg.get("site", ""), stage, msg.get("akey", ""), rule,
                                      lang=msg.get("lang", ""), person=person, ref=ref, dlr=dlr)

    # -------------------------------------------------------- 簡訊送達回報
    def apply_report(self, ref: str, status: str, detail: str = "", ts: float | None = None) -> int:
        if self.store is None or not ref or not hasattr(self.store, "update_dlr"):
            return 0
        return self.store.update_dlr(ref, status, detail, ts or self.clock())

    def poll_reports(self, now: float | None = None) -> int:
        """4G 數據機：讀回送達回報、對到發送紀錄。回傳更新幾筆。"""
        sms = self.channels.get("sms")
        if not (sms and getattr(sms, "report_on", False) and sms.uses_modem()):
            return 0
        n = 0
        for r in sms.poll_reports():
            n += self.apply_report(modem_ref(r["mr"], r["phone"]), report_status(r["st"]), f"TP-ST {r['st']}", now)
        return n

    def _poll_tick(self, now: float):
        sms = self.channels.get("sms")
        rc = (self.n.get("sms") or {}).get("report") or {}
        if not (sms and sms.enabled and rc.get("enabled") and sms.uses_modem() and self.store is not None):
            return
        if self.polling or now - self.last_poll < float(rc.get("poll_s", 60) or 60):
            return
        self.last_poll = now
        pend = getattr(self.store, "pending_dlr", None)
        if pend is None or not pend(now - 3 * 86400, "modem:"):
            return

        def run():
            self.polling = True
            try:
                self.poll_reports(now)
            except Exception as e:
                log.warning(_("讀取簡訊送達回報失敗：%s"), e)
            finally:
                self.polling = False
        if self.sync:
            run()
        else:
            threading.Thread(target=run, name="sms-report", daemon=True).start()

    # -------------------------------------------------------- 逐級通報與每日摘要（每幾秒呼叫一次）
    def tick(self, now: float | None = None):
        now = now or self.clock()
        esc = self.n.get("escalation") or {}
        if self.pending and esc.get("enabled", True):
            delays = {2: float(esc.get("stage2_after_s", 1800)), 3: float(esc.get("stage3_after_s", 7200))}
            for ekey, st in list(self.pending.items()):
                if self.is_acked and self.is_acked(st["akey"], st["robot"], st["site"]):
                    self.pending.pop(ekey, None)
                    continue
                for stage, delay in delays.items():
                    if stage in st["stages"] or now - st["ts"] < delay:
                        continue
                    st["stages"].append(stage)
                    msg = self.compose(st["level"], st["message"], st["axis"], st["kind"], now, "", st["robot"],
                                       st["site"], stage)
                    msg["akey"] = st["akey"]
                    g = self.gate() if self.gate else "all"
                    if g != "none":
                        self._dispatch(msg, st["level"], st["akey"], False, stage, now)
        self._summary_tick(now)
        self._poll_tick(now)

    def _summary_tick(self, now: float):
        at = self.n.get("summary_time", "")
        if not at:
            return
        t = time.localtime(now)
        today = time.strftime("%Y-%m-%d", t)
        if self.last_summary_day == today or t.tm_hour * 60 + t.tm_min < _hm(at):
            return
        self.last_summary_day = today
        g = self.gate() if self.gate else "all"
        if g == "none":
            self.deferred = []
            return
        text = self.summary_text(now)
        self.deferred = []
        if text:
            self.send_text(_("每日摘要"), text, self.n.get("summary_channels") or ["email", "webhook"], now)

    def summary_text(self, now: float | None = None) -> str:
        now = now or self.clock()
        parts = []
        if self.summary_fn:
            try:
                parts.append(self.summary_fn())
            except Exception as e:
                log.warning("summary: %s", e)
        if self.deferred:
            parts.append(_("夜間「注意」{n} 則：", n=len(self.deferred)) + "\n" + "\n".join(
                f"・{time.strftime('%H:%M', time.localtime(d['ts']))} {d['robot']} {d['message'][:60]}"
                for d in self.deferred[:15]))
        return "\n\n".join(p for p in parts if p)

    def send_text(self, title: str, text: str, channels: list[str], now: float | None = None) -> list:
        """直接發一段文字（每日摘要、系統通知），不經過冷卻與規則。
        簡訊只有明確列在 channels 時才發，而且只發第一行（當作每日心跳），並計入每日上限。"""
        now = now or self.clock()
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        limit = 160 if get_lang() == "en" else SMS_MAX
        msg = {"level": 1, "axis": None, "kind": "摘要", "kind_label": _("摘要"), "ts": now, "robot": self.robot, "site": self.site,
               "message": title, "text": f"【{title}】\n{text}", "subject": f"[DTM] {title}",
               "short": f"[DTM {title}] {first}"[:limit], "url": self.n.get("public_url", ""), "akey": "summary"}
        out = []
        for name in channels:
            ch = self.channels.get(name)
            if not (ch and ch.enabled and ch.targets()):
                continue
            if name == "sms" and not self._sms_quota(len(ch.targets())):
                continue
            out += self._deliver(ch, msg, retries=1)
        return out

    def escalations(self) -> list[dict]:
        return [{"key": k, **{x: v[x] for x in ("ts", "level", "message", "robot", "site", "akey", "stages")}}
                for k, v in self.pending.items()]

    # -------------------------------------------------------- 其他
    def preview(self, level: int = 2, message: str = "", axis=None, kind: str = "健康", robot: str | None = None,
                site: str | None = None) -> dict:
        msg = self.compose(level, message or _("J3 減速機：安排更換（排程停機）"), axis, kind, self.clock(), "", robot, site)
        return {"text": msg["text"], "sms": msg["short"], "subject": msg["subject"],
                "webhook": self.channels["webhook"].payload(msg)}

    def _test_msg(self, text: str, lang: str | None) -> dict:
        ts = time.time()
        if lang:
            with use(lang):
                msg = self.compose(1, text or _("測試通知：DTM 減速機扭力監控系統通知設定正常"), None, N_("測試"), ts)
        else:
            msg = self.compose(1, text or _("測試通知：DTM 減速機扭力監控系統通知設定正常"), None, N_("測試"), ts)
        msg["akey"] = "test"
        return msg

    def test(self, channel: str, text: str = "") -> list[dict]:
        """立即發一則測試訊息給預設收件人（不受等級、冷卻限制）；名單裡的人用自己的語言。回傳各收件人結果。"""
        self._sync_targets()

        def targets_of(name):
            ch = self.channels[name]
            if name == "webhook":
                return ch.targets()
            return self._uniq(ch.targets() + self._people_targets(name, "default", None))
        names = [k for k, c in self.channels.items() if c.enabled or c.targets()] if channel == "all" else [channel]
        if channel == "all":
            names = [k for k in names if targets_of(k)]
        out = []
        for name in names:
            ch = self.channels.get(name)
            if not ch:
                out.append({"channel": name, "ok": False, "detail": _("沒有這個通道")})
                continue
            tg = targets_of(name)
            if not tg:
                out.append({"channel": name, "ok": False, "detail": _("沒有收件人")})
                continue
            for lang, part in self.split_lang(name, tg, None):
                msg = self._test_msg(text, lang)
                for target, ok, detail in self._deliver(ch, msg, retries=0, targets=None if name == "webhook" else part):
                    out.append({"channel": name, "target": target, "ok": ok, "detail": detail, "lang": msg.get("lang")})
        return out

    def test_person(self, p: dict, text: str = "") -> list[dict]:
        """發一則測試訊息給名單裡的一個人（用他的語言；手機、Email 有填又有啟用的通道都發）。"""
        self._sync_targets()
        msg = self._test_msg(text, p.get("lang") or None)
        out = []
        for name, key in (("sms", "phone"), ("email", "email")):
            ch = self.channels.get(name)
            if not p.get(key):
                continue
            if not (ch and (ch.enabled or name == "email" and self.n.get("email_enabled") and self.n.get("smtp_host"))):
                out.append({"channel": name, "ok": False, "detail": _("通道未啟用（設定檔 [notify]）")})
                continue
            if name == "sms" and not self._sms_quota(1):
                out.append({"channel": name, "ok": False, "detail": _("已達每日簡訊上限，未發送")})
                continue
            for target, ok, detail in self._deliver(ch, msg, retries=0, targets=[p[key]]):
                out.append({"channel": name, "target": target, "ok": ok, "detail": detail, "lang": msg.get("lang")})
        return out

    def status(self) -> list[dict]:
        self._sync_targets()
        out = []
        for k, c in self.channels.items():
            tg = c.targets() if k == "webhook" else self._uniq(c.targets() + self._people_targets(k, "default", None))
            out.append({"channel": k, "label": CHANNEL_LABELS.get(k, k), "enabled": c.enabled, "min_level": c.min_level,
                        "targets": [mask(t) for t in tg], "provider": getattr(c, "provider", None),
                        "fallback": getattr(c, "fallback", None), "report": bool(getattr(c, "report_on", False))})
        return out

"""寫入權限與帳號：儀表板上會改資料的動作都要先登入，讀取畫面預設不需要登入。

兩種方式（有建立帳號就用帳號；沒有帳號時沿用 1.5 版的共用密碼）：
1. 個人帳號（2.0）：每人一組帳號密碼，依角色分權限。用指令建立：
     python -m dtm -c config.toml user add wang --role engineer --display 王小明
   角色由低到高：
     viewer   檢視（設定 web.require_login = true 時，連看畫面都要登入）
     operator 操作員：確認警報、登錄鐵粉、振動、時數
     engineer 工程師：登錄與撤銷保養事件、排程、備品、工作包
     admin    管理員：帳號、通知收件人、測試通知、維護模式
   帳號存在 users.sqlite3（同一台主機上的所有 DTM 程式共用）；密碼以 PBKDF2-SHA256 雜湊保存，不存明碼。
2. 共用密碼（舊方式）：config.toml [web] write_password，或環境變數 DTM_WRITE_PASSWORD。
   姓名由登入的人自己填，所以操作紀錄是「誰說自己是誰」，不是身分驗證。
2.1 起：沒有帳號也沒設密碼時「一律唯讀」。第一次使用時，在這台電腦本機（127.0.0.1）開啟儀表板，
畫面會請你建立第一個管理員帳號；從其他電腦連進來不能建立。系統不設任何預設密碼。
2.1 起：
- 登入狀態存在 users.sqlite3（只存 token 的 SHA-256），同一台主機的 DTM 程式共用：
  在 DTM 畫面登入一次，裡面的機器人即時監測、程式診斷都有效；管理員可以看到「登入中的裝置」並強制登出。
- 密碼原則、登入有效時間、閒置自動登出、輸錯鎖定，可以在「設定 → 帳號與密碼／隱私與安全」修改（見 sysconf.py）。
- 輸錯鎖定同時看「同一台電腦」與「同一個帳號」；停用帳號、重設密碼會立刻讓那個人的登入失效。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

from .i18n import TDict, _

MAX_FAILS = 5              # 同一台電腦在 FAIL_WINDOW 內輸錯幾次就暫停登入
FAIL_WINDOW = 600.0        # 秒
LOCK_S = 600.0             # 暫停多久（秒）
PBKDF2_ITERS = 200_000
ROLES = {"viewer": 0, "operator": 1, "engineer": 2, "admin": 3}
ROLE_NAMES = TDict({"viewer": "檢視", "operator": "操作員", "engineer": "工程師", "admin": "管理員"})

# 寫入 API → 最低角色（沒列到的寫入一律要 engineer）
PERMS = {
    "/api/ack": "operator", "/api/iron": "operator", "/api/vibration": "operator", "/api/hours": "operator",
    "/api/c/ack": "operator", "/api/c/job/prep": "operator",
    "/api/event": "engineer", "/api/event/undo": "engineer", "/api/c/event": "engineer", "/api/c/job": "engineer",
    "/api/c/job/done": "engineer", "/api/c/spare": "engineer", "/api/c/iron": "operator",
    "/api/notify/test": "admin", "/api/c/notify/test": "admin", "/api/notify/recipients": "admin",
    "/api/maintenance": "admin", "/api/c/maintenance": "admin", "/api/users": "admin",
    "/api/settings/devices": "admin", "/api/restore/preview": "admin", "/api/restore/run": "admin",
    "/api/export/analysis": "admin", "/api/c/export/analysis": "admin", "/api/report/commissioning": "engineer",
    "/api/downtime": "engineer", "/api/c/downtime": "engineer", "/api/teardown": "engineer", "/api/teardown/photo": "engineer",
    "/api/c/teardown": "engineer", "/api/replay": "engineer", "/api/c/replay": "engineer",
    "/api/pro/session/delete": "admin", "/api/pro/parse": "viewer",
    # 2.1 設定（齒輪）：帳號自己的部分 viewer 就能改；系統設定、帳號管理、診斷包只限管理員
    "/api/sys/password": "viewer", "/api/sys/account": "viewer", "/api/sys/ping": "viewer",
    "/api/sys/sessions/revoke": "viewer", "/api/sys/settings": "admin", "/api/sys/settings/reset": "admin",
    "/api/sys/settings/revert": "admin", "/api/sys/users": "admin", "/api/sys/diag": "admin", "/api/sys/backup": "admin",
    "/api/sys/notify/people": "admin", "/api/sys/tls": "admin", "/api/sys/wizard": "admin",
}
# 必須先改密碼的登入（管理員重設、密碼過期）只能做這些
MUST_CHANGE_OK = {"/api/sys/password", "/api/logout", "/api/sys/ping"}
COMMON_PW = {"password", "password1", "password123", "passw0rd", "12345678", "123456789", "1234567890", "87654321",
             "11111111", "00000000", "88888888", "66666666", "qwerty123", "qwertyui", "abc12345", "abcd1234", "admin123",
             "admin1234", "iloveyou", "1qaz2wsx", "a1234567", "aa123456", "fanuc123", "robot123", "dtm12345", "welcome1"}
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost")


def is_loopback(addr: str) -> bool:
    return str(addr or "") in LOOPBACK


def need_role(path: str) -> str:
    return PERMS.get(path, "engineer")


def role_ok(role: str | None, need: str) -> bool:
    return ROLES.get(role or "", -1) >= ROLES.get(need, 9)


def hash_pw(password: str, salt: bytes | None = None, iters: int = PBKDF2_ITERS) -> tuple[str, str, int]:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return dk.hex(), salt.hex(), iters


def verify_pw(password: str, pw_hash: str, salt_hex: str, iters: int) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
    return hmac.compare_digest(dk.hex(), pw_hash)




def token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def check_password(password: str, pol: dict | None = None, username: str = "") -> str:
    """依密碼原則檢查新密碼；可以用回傳空字串，不行回傳原因。"""
    pol = pol or {}
    n = max(8, int(pol.get("pw_min", 8)))
    p = str(password or "")
    if len(p) < n:
        return _("密碼至少 {n} 個字", n=n)
    if len(p) > 128:
        return _("密碼最多 128 個字")
    if pol.get("pw_mix", False):
        if not any(c.isalpha() for c in p) or not any(c.isdigit() for c in p):
            return _("密碼要同時有英文字母與數字")
        if p.lower() in COMMON_PW or len(set(p)) <= 2:
            return _("這個密碼太常見，容易被猜到，請換一個")
        if username and p.lower() == str(username).lower():
            return _("密碼不能和帳號相同")
    return ""


class UserStore:
    """個人帳號與登入狀態（同一台主機共用一個 users.sqlite3）。"""
    SCHEMA = """CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, display TEXT, role TEXT NOT NULL,
      pw_hash TEXT NOT NULL, salt TEXT NOT NULL, iters INTEGER NOT NULL, active INTEGER DEFAULT 1,
      created REAL, last_login REAL, plants TEXT DEFAULT '', must_change INTEGER DEFAULT 0, pw_changed REAL,
      fails INTEGER DEFAULT 0, fail_ts REAL DEFAULT 0, locked_until REAL DEFAULT 0, created_by TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS sessions (
      id INTEGER PRIMARY KEY AUTOINCREMENT, th TEXT UNIQUE NOT NULL, username TEXT, who TEXT, role TEXT,
      created REAL, active REAL, expires REAL, addr TEXT, agent TEXT, must_change INTEGER DEFAULT 0);"""
    NEW_COLS = {"must_change": "INTEGER DEFAULT 0", "pw_changed": "REAL", "fails": "INTEGER DEFAULT 0",
                "fail_ts": "REAL DEFAULT 0", "locked_until": "REAL DEFAULT 0", "created_by": "TEXT DEFAULT ''"}
    HIDDEN = ("pw_hash", "salt", "iters")

    def __init__(self, path: str):
        self.path = path
        if path:
            with self.conn() as c:
                c.executescript(self.SCHEMA)
                have = {r[1] for r in c.execute("PRAGMA table_info(users)")}
                for col, decl in self.NEW_COLS.items():
                    if col not in have:                     # 2.0 的帳號檔：補欄位
                        c.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def count(self) -> int:
        if not self.path or not os.path.exists(self.path):
            return 0
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]

    def admins(self, exclude: str = "") -> int:
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) FROM users WHERE active=1 AND role='admin' AND username<>?",
                             (exclude,)).fetchone()[0]

    def add(self, username: str, password: str, role: str = "operator", display: str = "", plants: str = "",
            must_change: bool = False, pol: dict | None = None, created_by: str = "") -> int:
        username = username.strip()
        if role not in ROLES:
            raise ValueError(_("角色只能是 {opts}", opts=_("、").join(ROLES)))
        if not username or len(password) < 8:
            raise ValueError(_("帳號不能空白，密碼至少 8 個字"))
        if len(username) > 30 or any(c.isspace() for c in username):
            raise ValueError(_("帳號最多 30 個字，不能有空白"))
        if pol is not None:
            err = check_password(password, pol, username)
            if err:
                raise ValueError(err)
        h, s, it = hash_pw(password)
        with self.conn() as c:
            if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise ValueError(_("帳號 {u} 已經存在", u=username))
            now = time.time()
            return c.execute("INSERT INTO users (username,display,role,pw_hash,salt,iters,active,created,plants,"
                             "must_change,pw_changed,created_by) VALUES (?,?,?,?,?,?,1,?,?,?,?,?)",
                             (username, display or username, role, h, s, it, now, plants, int(bool(must_change)), now,
                              created_by)).lastrowid

    def get(self, username: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
        return dict(r) if r else None

    def list(self) -> list[dict]:
        with self.conn() as c:
            return [{k: r[k] for k in r.keys() if k not in self.HIDDEN}
                    for r in c.execute("SELECT * FROM users ORDER BY username")]

    def set_password(self, username: str, password: str, must_change: bool = False, pol: dict | None = None):
        if len(password) < 8:
            raise ValueError(_("密碼至少 8 個字"))
        if pol is not None:
            err = check_password(password, pol, username)
            if err:
                raise ValueError(err)
        h, s, it = hash_pw(password)
        with self.conn() as c:
            if not c.execute("UPDATE users SET pw_hash=?, salt=?, iters=?, must_change=?, pw_changed=?, fails=0, "
                             "locked_until=0 WHERE username=?",
                             (h, s, it, int(bool(must_change)), time.time(), username)).rowcount:
                raise ValueError(_("找不到帳號 {u}", u=username))

    def update(self, username: str, **kw):
        cols = {k: v for k, v in kw.items() if k in ("display", "role", "active", "plants", "must_change", "locked_until",
                                                      "fails")}
        if "role" in cols and cols["role"] not in ROLES:
            raise ValueError(_("角色只能是 {opts}", opts=_("、").join(ROLES)))
        if not cols:
            return
        with self.conn() as c:
            q = ", ".join(f"{k}=?" for k in cols)
            if not c.execute(f"UPDATE users SET {q} WHERE username=?", (*cols.values(), username)).rowcount:
                raise ValueError(_("找不到帳號 {u}", u=username))

    def touch(self, username: str):
        with self.conn() as c:
            c.execute("UPDATE users SET last_login=?, fails=0, locked_until=0 WHERE username=?", (time.time(), username))

    def locked(self, username: str, now: float) -> float:
        """帳號暫停登入到什麼時候（0 = 沒有暫停）。"""
        with self.conn() as c:
            r = c.execute("SELECT locked_until FROM users WHERE username=?", (username,)).fetchone()
        return float(r[0] or 0) if r and float(r[0] or 0) > now else 0.0

    def fail(self, username: str, now: float, max_fails: int, lock_s: float) -> int:
        """記一次輸錯；回傳還可以再錯幾次（0 = 已暫停）。10 分鐘沒錯就重新計算。"""
        with self.conn() as c:
            r = c.execute("SELECT fails, fail_ts FROM users WHERE username=?", (username,)).fetchone()
            if not r:
                return max_fails
            n = (int(r[0] or 0) if now - float(r[1] or 0) < FAIL_WINDOW else 0) + 1
            lock = now + lock_s if n >= max_fails else 0
            c.execute("UPDATE users SET fails=?, fail_ts=?, locked_until=? WHERE username=?",
                      (0 if lock else n, now, lock, username))
        return 0 if lock else max_fails - n

    # -------------------------------------------------------- 登入狀態
    def add_session(self, token: str, username: str, who: str, role: str, now: float, ttl: float, addr: str = "",
                    agent: str = "", must_change: bool = False) -> int:
        with self.conn() as c:
            c.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            return c.execute("INSERT INTO sessions (th, username, who, role, created, active, expires, addr, agent, "
                             "must_change) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (token_hash(token), username, who, role, now, now, now + ttl, addr[:60], agent[:200],
                              int(bool(must_change)))).lastrowid

    def get_session(self, token: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT s.*, u.active AS u_active, u.role AS u_role, u.display AS u_display, "
                          "u.must_change AS u_must FROM sessions s LEFT JOIN users u ON u.username = s.username "
                          "WHERE s.th=?", (token_hash(token),)).fetchone()
        return dict(r) if r else None

    def touch_session(self, token: str, now: float):
        with self.conn() as c:
            c.execute("UPDATE sessions SET active=? WHERE th=?", (now, token_hash(token)))

    def end_session(self, token: str = "", sid: int | None = None, username: str | None = None) -> int:
        with self.conn() as c:
            if sid is not None:
                q, a = "DELETE FROM sessions WHERE id=?", [int(sid)]
                if username is not None:
                    q, a = q + " AND username=?", a + [username]
                return c.execute(q, a).rowcount
            return c.execute("DELETE FROM sessions WHERE th=?", (token_hash(token),)).rowcount

    def end_user_sessions(self, username: str, keep_token: str = "") -> int:
        with self.conn() as c:
            return c.execute("DELETE FROM sessions WHERE username=? AND th<>?",
                             (username, token_hash(keep_token) if keep_token else "")).rowcount

    def clear_must_change(self, token: str):
        with self.conn() as c:
            c.execute("UPDATE sessions SET must_change=0 WHERE th=?", (token_hash(token),))

    def sessions(self, now: float, username: str | None = None) -> list[dict]:
        with self.conn() as c:
            q = "SELECT id, username, who, role, created, active, expires, addr, agent, th FROM sessions WHERE expires>=?"
            a: list = [now]
            if username is not None:
                q, a = q + " AND username=?", a + [username]
            return [dict(r) for r in c.execute(q + " ORDER BY active DESC", a)]


class WriteAuth:
    """共用密碼（1.5 版的方式）；同時負責登入 token 與輸錯鎖定，個人帳號也共用這一套。"""

    def __init__(self, cfg: dict):
        w = cfg.get("web", {})
        from .notify import secret
        self.password = os.environ.get("DTM_WRITE_PASSWORD") or str(secret(w.get("write_password")) or "")
        self._ttl = max(0.25, float(w.get("session_hours", 12))) * 3600
        self.tokens: dict[str, tuple] = {}
        self.fails: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    @property
    def ttl(self) -> float:
        return self._ttl

    @property
    def max_fails(self) -> int:
        return MAX_FAILS

    @property
    def lock_s(self) -> float:
        return LOCK_S

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    @property
    def mode(self) -> str:
        return "password" if self.password else "readonly"

    def can(self, session: dict | None, path: str) -> bool:
        """共用密碼模式：登入就能寫入；沒有密碼：唯讀。"""
        return bool(self.password) and bool(session)

    def _locked(self, addr: str, now: float) -> str:
        lock_s, mx = self.lock_s, self.max_fails
        fl = [t for t in self.fails.get(addr, []) if now - t < FAIL_WINDOW + lock_s]
        self.fails[addr] = fl
        if fl and now - fl[-1] < lock_s and len([t for t in fl if fl[-1] - t < FAIL_WINDOW]) >= mx:
            wait = int((lock_s - (now - fl[-1])) // 60) + 1
            return _("密碼錯誤次數太多，請 {m} 分鐘後再試", m=wait)
        return ""

    def _fail(self, addr: str, now: float, msg: str = "") -> str:
        fl = self.fails.setdefault(addr, [])
        fl.append(now)
        left = self.max_fails - len([t for t in fl if now - t < FAIL_WINDOW])
        return (msg or _("密碼錯誤")) + (_("（再錯 {n} 次會暫停登入）", n=left) if 0 < left <= 2 else "")

    def issue(self, who: str, role: str, username: str, now: float, **kw) -> str:
        with self.lock:
            for k in [k for k, v in self.tokens.items() if v[-1] < now]:   # 順手清掉過期的
                del self.tokens[k]
            token = secrets.token_urlsafe(24)
            self.tokens[token] = (who, role, username, now + self.ttl)
        return token

    def login(self, addr: str, who: str, password: str, now: float | None = None) -> tuple[str | None, str, int]:
        """回傳 (token, 錯誤訊息, HTTP 狀態碼)。"""
        now = now or time.time()
        who = " ".join(str(who or "").split())[:30]
        with self.lock:
            err = self._locked(addr, now)
            if err:
                return None, err, 429
            if not self.enabled:
                return None, _("目前是唯讀模式：請在監測電腦本機建立第一個管理員帳號"), 403
            if not who:
                return None, _("請填姓名（操作紀錄會記下是誰登錄的）"), 400
            if not hmac.compare_digest(password.encode("utf-8"), self.password.encode("utf-8")):
                return None, self._fail(addr, now), 403
            self.fails.pop(addr, None)
        return self.issue(who, "admin", "", now, addr=addr), "", 200

    def session(self, token: str, now: float | None = None) -> dict | None:
        if not token:
            return None
        now = now or time.time()
        with self.lock:
            t = self.tokens.get(token)
            if not t or t[-1] < now:
                self.tokens.pop(token, None)
                return None
            return {"who": t[0], "role": t[1], "username": t[2], "must_change": False}

    def who(self, token: str, now: float | None = None) -> str | None:
        """有效的 token → 姓名；無效或過期 → None。"""
        s = self.session(token, now)
        return s["who"] if s else None

    def logout(self, token: str):
        with self.lock:
            self.tokens.pop(token, None)

    def touch(self, token: str, now: float | None = None):
        """使用者有操作（閒置自動登出用）；共用密碼模式不需要。"""


class Auth(WriteAuth):
    """2.0：有個人帳號就用帳號＋角色；沒有就退回共用密碼（WriteAuth）。
    2.1：登入狀態存在 users.sqlite3，同一台主機的 DTM 程式共用（單一登入）；原則來自系統設定。"""

    def __init__(self, cfg: dict, users: UserStore | None = None):
        super().__init__(cfg)
        from .runtime import resolve
        self.cfg = cfg
        path = resolve(cfg, cfg["web"].get("users_db", "users.sqlite3")) if cfg.get("web") else ""
        self.users_path = path
        self.users = users if users is not None else (UserStore(path) if path and os.path.exists(path) else None)
        self.demo = bool(cfg.get("_demo"))          # 演示模式：資料是產生的，不用登入就能試用寫入功能
        self._n_users = None
        self._n_ts = 0.0

    # -------------------------------------------------------- 原則（系統設定，網頁上改了 10 秒內生效）
    @property
    def pol(self) -> dict:
        from .sysconf import policy
        return policy(self.cfg)

    @property
    def require_login(self) -> bool:
        return self.pol["require_login"]

    @property
    def ttl(self) -> float:
        return self.pol["session_hours"] * 3600

    @property
    def max_fails(self) -> int:
        return self.pol["max_fails"]

    @property
    def lock_s(self) -> float:
        return self.pol["lock_minutes"] * 60.0

    @property
    def accounts(self) -> bool:
        now = time.time()
        if self.users is None:
            if self.users_path and os.path.exists(self.users_path):   # 另一個程式剛建立了帳號檔
                self.users = UserStore(self.users_path)
            else:
                return False
        if self._n_users is None or now - self._n_ts > 30:
            try:
                self._n_users, self._n_ts = self.users.count(), now
            except sqlite3.Error:
                self._n_users = 0
        return self._n_users > 0

    @property
    def enabled(self) -> bool:
        return self.accounts or bool(self.password)

    @property
    def mode(self) -> str:
        if self.accounts:
            return "accounts"
        if self.password:
            return "password"
        return "demo" if self.demo else "readonly"

    @property
    def setup_needed(self) -> bool:
        """還沒有任何個人帳號（共用密碼模式也可以在本機建立第一個管理員，改用帳號）。"""
        return not self.accounts and not self.demo

    def _store(self) -> UserStore:
        if self.users is None:
            if not self.users_path:
                raise ValueError(_("沒有設定 [web] users_db"))
            d = os.path.dirname(self.users_path)
            if d:
                os.makedirs(d, exist_ok=True)
            self.users = UserStore(self.users_path)
        return self.users

    def create_first_admin(self, username: str, password: str, display: str = "") -> int:
        """建立第一個管理員；已經有帳號就拒絕（之後的帳號由管理員用指令或畫面建立）。呼叫端要先確認是本機連線。"""
        if self.accounts:
            raise ValueError(_("已經有帳號了，請用管理員登入後再新增"))
        if not self.users_path:
            raise ValueError(_("沒有設定 [web] users_db"))
        uid = self._store().add(username, password, "admin", display, pol=self.pol)
        self._n_users, self._n_ts = None, 0.0
        return uid

    # -------------------------------------------------------- 登入與登入狀態
    def issue(self, who: str, role: str, username: str, now: float, addr: str = "", agent: str = "",
              must_change: bool = False, **kw) -> str:
        if self.users is None:
            return super().issue(who, role, username, now)
        token = secrets.token_urlsafe(32)
        self.users.add_session(token, username, who, role, now, self.ttl, addr, agent, must_change)
        return token

    def login(self, addr: str, who: str, password: str, now: float | None = None, username: str = "",
              agent: str = ""):
        if not self.accounts:
            return super().login(addr, who, password, now)
        now = now or time.time()
        username = (username or who or "").strip()
        pol = self.pol
        with self.lock:
            err = self._locked(addr, now)
            if err:
                return None, err, 429
            if not username:
                return None, _("請輸入帳號"), 403
            until = self.users.locked(username, now)
            if until:
                return None, _("密碼錯誤次數太多，請 {m} 分鐘後再試", m=int((until - now) // 60) + 1), 429
            u = self.users.get(username)
            if not u or not u["active"] or not verify_pw(password, u["pw_hash"], u["salt"], u["iters"]):
                msg = self._fail(addr, now, _("帳號或密碼錯誤"))
                if u and u["active"]:
                    left = self.users.fail(username, now, pol["max_fails"], pol["lock_minutes"] * 60.0)
                    if left == 0:
                        return None, _("密碼錯誤次數太多，請 {m} 分鐘後再試", m=pol["lock_minutes"]), 429
                return None, msg, 403
            self.fails.pop(addr, None)
        self.users.touch(username)
        must = bool(u.get("must_change"))
        if pol["pw_days"] and u.get("pw_changed") and now - float(u["pw_changed"]) > pol["pw_days"] * 86400:
            must = True                                   # 密碼過期：登入後要先改
        return self.issue(u["display"] or username, u["role"], username, now, addr=addr, agent=agent,
                          must_change=must), "", 200

    def session(self, token: str, now: float | None = None) -> dict | None:
        if not token:
            return None
        if self.users is None:
            return super().session(token, now)
        now = now or time.time()
        try:
            s = self.users.get_session(token)
        except sqlite3.Error:
            return None
        if not s:
            return super().session(token, now)          # 帳號檔建立前發出的（共用密碼）
        if s["expires"] < now:
            self.users.end_session(token)
            return None
        role = s["role"]
        if s["username"]:
            if not s["u_active"]:                       # 帳號已停用或刪除
                self.users.end_session(token)
                return None
            role = s["u_role"] or role                  # 角色改了立即生效
        idle = self.pol["idle_minutes"]
        if idle and role != "viewer" and now - float(s["active"] or s["created"]) > idle * 60:
            self.users.end_session(token)
            return None
        return {"who": s["u_display"] or s["who"] if s["username"] else s["who"], "role": role,
                "username": s["username"] or "", "sid": s["id"], "must_change": bool(s["must_change"]),
                "created": s["created"], "expires": s["expires"], "idle_minutes": idle if role != "viewer" else 0}

    def touch(self, token: str, now: float | None = None):
        if self.users is not None and token:
            try:
                self.users.touch_session(token, now or time.time())
            except sqlite3.Error:
                pass

    def logout(self, token: str):
        super().logout(token)
        if self.users is not None and token:
            self.users.end_session(token)

    def can(self, session: dict | None, path: str) -> bool:
        """這個登入狀態能不能做這個寫入。沒有帳號也沒密碼：唯讀，任何人都不能寫入。"""
        m = self.mode
        if m == "readonly":
            return False
        if m == "demo":
            return True
        if not session:
            return False
        if session.get("must_change") and path not in MUST_CHANGE_OK:
            return False
        return role_ok(session.get("role"), need_role(path))

    # -------------------------------------------------------- 帳號管理（設定 → 帳號與密碼）
    def change_password(self, username: str, old: str, new: str, token: str = "") -> int:
        """自己改密碼：要舊密碼；其他裝置的登入一併登出。回傳登出了幾個。"""
        if not username:
            raise ValueError(_("共用密碼模式沒有個人帳號，請改設定檔或建立個人帳號"))
        u = self._store().get(username)
        if not u or not verify_pw(old, u["pw_hash"], u["salt"], u["iters"]):
            raise PermissionError(_("目前的密碼不對"))
        if old == new:
            raise ValueError(_("新密碼不能和目前的一樣"))
        self.users.set_password(username, new, must_change=False, pol=self.pol)
        if token:
            self.users.clear_must_change(token)
        return self.users.end_user_sessions(username, keep_token=token)

    def guard_admin(self, username: str, role: str | None = None, active: bool | None = None):
        """至少要留一個啟用中的管理員。"""
        u = self._store().get(username)
        if not u or u["role"] != "admin" or not u["active"]:
            return
        if (role is not None and role != "admin") or active is False:
            if self.users.admins(exclude=username) == 0:
                raise ValueError(_("至少要保留一個啟用中的管理員"))

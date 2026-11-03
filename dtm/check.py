"""設定檔檢查與自我診斷：python -m dtm -c config.toml check [--connect]

不改任何東西，只列出問題：
  【錯誤】一定要修，否則跑不起來或資料不對（有錯誤時結束代碼為 1，start.bat 會停下來）
  【警告】可以跑，但可能不是你要的（拼錯的項目、門檻前後顛倒、沒設密碼…）
  【提示】提醒還沒填的選填項目
加 --connect 會實際連到機器人讀幾秒資料，檢查心跳、扭矩暫存器、倍率。
"""
from __future__ import annotations

import difflib
import os
import re
import time
import tomllib

from .config import DEFAULTS, E1, REDUCER_TYPES, axis_cfg, axis_name, enabled_axes, load
from .i18n import LANGS, TDict, _, norm

SOURCES = ("sim", "fanuc_http", "modbus", "csv")
# 這些區段的內容由使用者自訂，不檢查項目名稱
FREE = {("source", "modbus", "map"), ("notify", "sms", "http", "params"), ("notify", "sms", "http", "headers"),
        ("sensors", "map"), ("motion", "codes"), ("central", "tokens"), ("central", "sites")}
SYS_ROLES = ("standalone", "edge", "central")
ERROR, WARN, HINT, OK = "error", "warn", "hint", "ok"
TAGS = TDict({ERROR: "【錯誤】", WARN: "【警告】", HINT: "【提示】", OK: "【正常】"})


class Report:
    def __init__(self):
        self.items: list[tuple[str, str, str]] = []

    def add(self, level: str, where: str, msg: str):
        self.items.append((level, where, msg))

    def count(self, level: str) -> int:
        return sum(1 for i in self.items if i[0] == level)

    def lines(self) -> list[str]:
        order = {ERROR: 0, WARN: 1, HINT: 2, OK: 3}
        out = []
        for lv, where, msg in sorted(self.items, key=lambda i: order[i[0]]):
            out.append(f"{TAGS[lv]} {where + _('：') if where else ''}{msg}")
        return out


def _where(path: tuple) -> str:
    if len(path) == 1:
        return f"[{path[0]}]"
    return f"[{'.'.join(path[:-1])}] {path[-1]}"


def _type_name(v) -> str:
    if isinstance(v, bool):
        return _("true／false")
    if isinstance(v, (int, float)):
        return _("數字")
    if isinstance(v, str):
        return _("文字")
    if isinstance(v, list):
        return _("清單")
    if isinstance(v, dict):
        return _("區段")
    return type(v).__name__


def _type_ok(default, v) -> bool:
    if isinstance(default, bool):
        return isinstance(v, bool)
    if isinstance(default, (int, float)):
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if isinstance(default, str):
        return isinstance(v, str)
    if isinstance(default, list):
        return isinstance(v, list)
    return True


def _type_msg(default, v) -> str:
    if isinstance(default, bool):
        hint = _("請寫 true 或 false（小寫、不加引號）")
    elif isinstance(default, (int, float)):
        hint = _("請寫數字、不加引號，例如 {ex}", ex=default)
        if isinstance(v, str) and re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", v):
            hint = _("數字不要加引號：把 \"{v}\" 改成 {v}", v=v.strip())
    elif isinstance(default, str):
        hint = _("文字要用半形雙引號括起來，例如 \"{ex}\"", ex=default or "abc")
    elif isinstance(default, list):
        hint = _("請寫成清單，例如 [2, 3]")
    else:
        hint = ""
    return _("格式不對（目前是{got}）。{hint}", got=_type_name(v), hint=hint)


def _walk(raw: dict, ref: dict, path: tuple, rep: Report, bad: list | None = None):
    """比對預設值：不認識的項目 → 警告；型別不對 → 錯誤，並記下位置（bad），之後用預設值代替再檢查數值。"""
    bad = [] if bad is None else bad
    for k, v in raw.items():
        p = path + (k,)
        if p in FREE:
            continue
        if path == () and k == "axis":
            if not isinstance(v, dict):
                rep.add(ERROR, "[axis]", _("應該寫成 [axis.2]、[axis.3] 這樣的區段"))
                bad.append(p)
                continue
            for ax, sub in v.items():
                if ax not in {str(i) for i in range(1, 8)}:
                    rep.add(ERROR, f"[axis.{ax}]", _("軸號只能是 1 到 7（7 = 行走軸 E1）"))
                    bad.append(p + (ax,))
                elif not isinstance(sub, dict):
                    rep.add(ERROR, f"[axis.{ax}]", _("應該是一個區段"))
                    bad.append(p + (ax,))
                else:
                    _walk(sub, DEFAULTS["axis_default"], ("axis", ax), rep, bad)
            continue
        if k not in ref:
            near = difflib.get_close_matches(k, [str(x) for x in ref], n=1, cutoff=0.6)
            msg = _("不認識這個項目，程式會忽略它") + (_("。是不是要寫 {near}？", near=near[0]) if near else "")
            rep.add(WARN, _where(p), msg)
            continue
        d = ref[k]
        if isinstance(d, dict):
            if not isinstance(v, dict):
                rep.add(ERROR, _where(p), _("應該是一個區段 [{name}]", name=".".join(p)))
                bad.append(p)
            elif d:
                _walk(v, d, p, rep, bad)
        elif p == ("health", "common_mode"):
            if not (isinstance(v, bool) or v == "auto"):
                rep.add(ERROR, _where(p), _("只能是 \"auto\"、true 或 false"))
                bad.append(p)
        elif not _type_ok(d, v):
            rep.add(ERROR, _where(p), _type_msg(d, v))
            bad.append(p)
    return bad


def _toml_hint(msg: str, line: str) -> str:
    if re.search(r"[＝“”‘’　，：]", line):
        return _("這一行有全形符號（＝ “ ” ， 或全形空白），請改成半形")
    if "Illegal character" in msg and line.count('"') % 2:
        return _("雙引號沒有成對")
    if "after a statement" in msg:
        return _("一行只能寫一個設定；文字要用雙引號括起來，例如 host = \"192.168.0.10\"")
    if "Expected '='" in msg:
        return _("這一行少了等號 =，或項目名稱裡有空白")
    if "overwrite" in msg or "twice" in msg or "Cannot declare" in msg:
        return _("同一個項目或區段出現了兩次，請刪掉其中一個")
    if "Invalid value" in msg:
        return _("值的寫法不對：文字要加雙引號、true／false 要小寫、數字不加引號")
    return ""


def read_raw(path: str, rep: Report) -> dict | None:
    if not os.path.exists(path):
        rep.add(ERROR, path, _("找不到設定檔。請把 config.example.toml 複製一份、改名為 config.toml"))
        return None
    with open(path, "rb") as f:
        data = f.read()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        rep.add(ERROR, path, _("設定檔不是 UTF-8 編碼。用記事本開啟 → 另存新檔 → 下方「編碼」選 UTF-8 → 存檔"))
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        msg = str(e)
        m = re.search(r"line (\d+), column (\d+)", msg)
        if m:
            n = int(m.group(1))
            lines = text.splitlines()
            line = lines[n - 1] if 0 < n <= len(lines) else ""
            hint = _toml_hint(msg, line)
            rep.add(ERROR, _("第 {n} 行", n=n), _("格式錯誤：{line}", line=line.strip())
                    + (_("。{hint}", hint=hint) if hint else "") + f"（{msg}）")
        else:
            rep.add(ERROR, path, _("格式錯誤：{msg}", msg=msg))
        return None


def _check_web_security(cfg: dict, rep: Report):
    """2.1：沒有帳號也沒有寫入密碼時一律唯讀；開放給其他電腦但沒有 HTTPS 時提醒。"""
    from .auth import Auth
    from .runtime import resolve
    web = cfg["web"]
    local = web["host"] in ("127.0.0.1", "localhost", "::1")
    if web.get("tls_cert"):
        for k in ("tls_cert", "tls_key"):
            if web.get(k) and not os.path.exists(resolve(cfg, web[k])):
                rep.add(ERROR, f"[web] {k}", _("找不到檔案：{p}", p=resolve(cfg, web[k])))
    elif not local:
        rep.add(WARN, "[web] tls_cert", _("儀表板開放給其他電腦連線但沒有設定 HTTPS：登入密碼會以明碼在網路上傳送。"
                                          "建議向資訊單位申請憑證，或只在本機使用（host = \"127.0.0.1\"）"))
    if web.get("allow_ips"):
        from .sysconf import parse_ips
        try:
            parse_ips(web["allow_ips"])
        except ValueError as e:
            rep.add(ERROR, "[web] allow_ips", str(e))
    if int(web.get("idle_minutes", 30) or 0) and int(web.get("idle_minutes", 30)) < 5:
        rep.add(WARN, "[web] idle_minutes", _("閒置自動登出少於 5 分鐘，操作中容易被登出"))
    a = Auth(cfg)
    if a.mode == "readonly":
        rep.add(HINT, "[web]", _("還沒有任何帳號：儀表板目前是唯讀。請在這台電腦本機開啟儀表板建立第一個管理員，"
                                 "或執行 user add 建立帳號"))
    elif a.mode == "password":
        rep.add(HINT, "[web] write_password", _("目前用共用寫入密碼；建議改用個人帳號（操作紀錄才知道是誰）"))
    if web.get("write_password") and not str(web["write_password"]).startswith("env:"):
        rep.add(HINT, "[web] write_password", _("密碼寫在設定檔裡；建議改成 \"env:DTM_WRITE_PASSWORD\" 從環境變數讀取"))


def check_values(cfg: dict, rep: Report):
    """合併預設值之後，檢查數值範圍與前後關係。"""
    if cfg["system"].get("role") == "central":           # 中央不讀機器人：只檢查網頁、通知、上傳相關
        web = cfg["web"]
        if not 1 <= int(web["port"]) <= 65535:
            rep.add(ERROR, "[web] port", _("連接埠要在 1–65535"))
        _check_web_security(cfg, rep)
        _check_notify(cfg, rep)
        _check_fleet(cfg, rep)
        return
    r = cfg["robot"]
    if r["source"] not in SOURCES:
        rep.add(ERROR, "[robot] source", _("只能是 {opts}", opts=_("、").join(SOURCES)))
    if not 0.5 <= float(r["poll_hz"]) <= 50:
        rep.add(WARN, "[robot] poll_hz", _("讀取頻率 {v} Hz 不太合理，一般 4–10", v=r["poll_hz"]))
    if r["source"] == "sim":
        rep.add(HINT, "[robot] source", _("目前是模擬器（sim），不會連到真的機器人"))

    axes = cfg["axes"]["enabled"]
    if not isinstance(axes, list) or not axes or not all(isinstance(a, int) and 1 <= a <= 7 for a in axes):
        rep.add(ERROR, "[axes] enabled", _("要列出監控的軸，例如 [2, 3]；軸號 1 到 7（7 = 行走軸 E1）"))
        axes = []
    for ax in axes:
        a = axis_cfg(cfg, ax)
        w = f"[axis.{ax}]"
        if a["reducer_type"] not in REDUCER_TYPES:
            rep.add(ERROR, w + " reducer_type", _("只能是 rv、gearbox 或 rack（行走軸）"))
        if a["dir_sign"] not in (1, -1):
            rep.add(ERROR, w + " dir_sign", _("只能是 1 或 -1"))
        if float(a["k_nm_per_raw"]) <= 0:
            rep.add(HINT, w, _("還沒校正（k_nm_per_raw = 0），儀表板顯示原始值；健康判定用比值，不受影響"))
        elif float(a["rated_nm"]) <= 0:
            rep.add(HINT, w + " rated_nm", _("已校正但沒填額定扭矩，即時超限警報只能用原始值門檻"))
        if float(a["rt_caution_pct"]) >= float(a["rt_alert_pct"]):
            rep.add(WARN, w, _("rt_caution_pct 應該小於 rt_alert_pct"))
        if float(a["grease_mass_g"]) <= 0:
            rep.add(HINT, w + " grease_mass_g", _("沒填油脂量，鐵粉只能看濃度、無法換算生成速率 W"))

    reg = cfg["registers"]
    if r["source"] in ("fanuc_http",):
        start = int(reg["dt_start"])
        n_dt = 7 if E1 in axes else 6
        used = {start + i: axis_name(cfg, i + 1) for i in range(n_dt)}
        for key in ("seg", "run", "heartbeat", "override", "temp"):
            n = int(reg.get(key) or 0)
            if n and n in used:
                rep.add(ERROR, f"[registers] {key}", _("R[{n}] 跟 {ax} 的扭矩暫存器重疊（dt_start = {s} 佔用 R[{s}]–R[{e}]）",
                                                       n=n, ax=used[n], s=start, e=start + n_dt - 1))
            if n:
                used[n] = key
        if not int(reg.get("heartbeat") or 0):
            rep.add(WARN, "[registers] heartbeat", _("沒設心跳暫存器，BG Logic 停掉時不會發現"))

    h = cfg["health"]
    if float(h["caution_ratio"]) >= float(h["alert_ratio"]):
        rep.add(ERROR, "[health]", _("caution_ratio（{a}）要小於 alert_ratio（{b}）", a=h["caution_ratio"], b=h["alert_ratio"]))
    if float(h["caution_z"]) >= float(h["alert_z"]):
        rep.add(ERROR, "[health]", _("caution_z 要小於 alert_z"))
    if int(h["baseline_runs"]) < 5:
        rep.add(WARN, "[health] baseline_runs", _("基準次數太少（{n}），容易誤報；建議 10 以上", n=h["baseline_runs"]))
    v = cfg["vibration"]
    if float(v["caution_ratio"]) >= float(v["alert_ratio"]):
        rep.add(ERROR, "[vibration]", _("caution_ratio 要小於 alert_ratio"))
    iron = cfg["iron"]
    if float(iron["rv_caution_wt"]) >= float(iron["rv_alert_wt"]):
        rep.add(ERROR, "[iron]", _("rv_caution_wt 要小於 rv_alert_wt"))
    from .config import IRON_SAMPLES, WEIGHT_KEYS
    if str(iron.get("default_sample", "drain_mid")) not in IRON_SAMPLES:
        rep.add(ERROR, "[iron] default_sample", _("只能是 {opts}", opts=_("、").join(IRON_SAMPLES)))
    ic_d, ia_d = float(iron.get("interval_caution_days", 60)), float(iron.get("interval_alert_days", 21))
    if ic_d < 0 or ia_d < 0 or float(iron.get("interval_days", 0)) < 0:
        rep.add(ERROR, "[iron] interval_*_days", _("取樣間隔不能是負數"))
    elif ia_d and ic_d and ia_d > ic_d:
        rep.add(WARN, "[iron] interval_alert_days", _("警戒的取樣間隔（{a} 天）比注意（{c} 天）還長", a=ia_d, c=ic_d))
    th = cfg["thermal"]
    for k in ("rise_c", "day_rise_c", "post_grease_hold_c"):
        if float(th[k]) <= 0:
            rep.add(ERROR, f"[thermal] {k}", _("要大於 0"))
    if int(th["min_days"]) < 3 or int(th["min_days"]) > int(th["baseline_days"]):
        rep.add(ERROR, "[thermal] min_days", _("要在 3 到 baseline_days（{n}）之間", n=th["baseline_days"]))
    if float(th["overheat_c"]) and not 40 <= float(th["overheat_c"]) <= 150:
        rep.add(WARN, "[thermal] overheat_c", _("過熱門檻 {v}°C 不太合理（一般 60–90°C；0 = 停用）", v=th["overheat_c"]))
    w = (cfg["decision"].get("weights") or {})
    for k, val in w.items():
        if k not in WEIGHT_KEYS:
            rep.add(ERROR, f"[decision.weights] {k}", _("不認得這個比重；可用：{opts}", opts=_("、").join(WEIGHT_KEYS)))
            continue
        try:
            f = float(val)
        except (TypeError, ValueError):
            rep.add(ERROR, f"[decision.weights] {k}", _("要是數字"))
            continue
        if not 0 <= f <= 1.5:
            rep.add(ERROR, f"[decision.weights] {k}", _("要在 0–1.5（目前 {v}）", v=val))
        elif f == 0:
            rep.add(WARN, f"[decision.weights] {k}", _("比重 0 = 這項證據完全不採用"))
    if float(w.get("temp_rise", 0.6)) > 0.8:
        rep.add(WARN, "[decision.weights] temp_rise", _("溫升比重偏高：溫度受氣溫、負載影響大，建議 0.6 以下"))
    if float(cfg["life"]["grease_interval_h"]) <= 0:
        rep.add(ERROR, "[life] grease_interval_h", _("換脂週期要大於 0"))

    web = cfg["web"]
    if not 1 <= int(web["port"]) <= 65535:
        rep.add(ERROR, "[web] port", _("連接埠要在 1–65535"))
    _check_web_security(cfg, rep)
    lang = cfg.get("ui", {}).get("lang")
    if lang not in LANGS:
        rep.add(ERROR, "[ui] lang", _("只能是 {opts}", opts=_("、").join(LANGS)))

    st = cfg["storage"]
    db = os.path.abspath(st["db"])
    d = os.path.dirname(db)
    if not os.path.isdir(d):
        rep.add(ERROR, "[storage] db", _("資料夾不存在：{d}", d=d))
    elif not os.access(d, os.W_OK):
        rep.add(ERROR, "[storage] db", _("沒有寫入權限：{d}", d=d))
    if not str(st.get("backup_dir") or "").strip():
        rep.add(WARN, "[storage] backup_dir", _("沒有啟用每日備份"))
    if not 0 <= int(st["backup_hour"]) <= 23:
        rep.add(ERROR, "[storage] backup_hour", _("要在 0–23"))
    if int(st["backup_keep"]) < 1:
        rep.add(ERROR, "[storage] backup_keep", _("至少保留 1 份"))
    mir = str(st.get("backup_mirror_dir") or "").strip()
    if mir:
        from .backup import mirror_dir
        d = mirror_dir(cfg)
        if not str(st.get("backup_dir") or "").strip():
            rep.add(WARN, "[storage] backup_mirror_dir", _("要先啟用 backup_dir，網路資料夾是複製本機備份"))
        elif not os.path.isdir(d):
            rep.add(WARN, "[storage] backup_mirror_dir", _("目前連不上或不存在：{d}（啟動後會自動建立；網路斷線時本機備份照做）", d=d))
        elif not os.access(d, os.W_OK):
            rep.add(WARN, "[storage] backup_mirror_dir", _("沒有寫入權限：{d}（Windows 服務帳號也要能寫入網路資料夾）", d=d))
        else:
            rep.add(HINT, "[storage] backup_mirror_dir", _("網路資料夾可寫入：{d}", d=d))

    if cfg.get("workpack", {}).get("catalog"):
        from .workpack import load_catalog
        cat = load_catalog(cfg)
        if cat["error"]:
            rep.add(ERROR, "[workpack] catalog", cat["error"])
        else:
            from .config import drive_comp
            missing = [axis_name(cfg, ax) for ax in axes
                       if not any(p.get("comp") == drive_comp(cfg, ax) and (not p.get("axes") or ax in p["axes"])
                                  for p in cat["packs"])]
            if missing:
                rep.add(HINT, "[workpack] catalog", _("{axes} 沒有減速機／齒輪箱的作業包", axes=_("、").join(missing)))

    _check_sensors(cfg, axes, rep)
    _check_motion(cfg, rep)
    _check_pro(cfg, rep)
    _check_notify(cfg, rep)
    _check_fleet(cfg, rep)


def _check_pro(cfg: dict, rep: Report):
    """Pro 程式層級診斷的設定。"""
    p = cfg["pro"]
    if p["torque"] not in ("torque", "disturbance"):
        rep.add(ERROR, "[pro] torque", _("只能是 torque（傳遞扭矩）或 disturbance（干擾扭矩）"))
    elif p["torque"] == "disturbance":
        rep.add(HINT, "[pro] torque", _("用干擾扭矩計算損耗：干擾扭矩不含重力與慣性，逐行損耗只能當相對參考"))
    if p["torque_unit"] not in ("pct", "nm", "raw"):
        rep.add(ERROR, "[pro] torque_unit", _("只能是 pct（額定扭矩百分比）、nm 或 raw"))
    if not 10 <= float(p["sample_hz"]) <= 1000:
        rep.add(WARN, "[pro] sample_hz", _("診斷期取樣 {v} Hz 不太合理，範例 50–125", v=p["sample_hz"]))
    if not 0.5 <= float(p["max_days"]) <= 31:
        rep.add(WARN, "[pro] max_days", _("診斷期 {v} 天不太合理，範例 3–7 天", v=p["max_days"]))
    reg = cfg["registers"]
    used = set(range(int(reg["dt_start"]), int(reg["dt_start"]) + 7)) | {int(reg[k]) for k in ("seg", "run", "heartbeat", "override")}
    if int(p["line_reg"]) in used:
        rep.add(ERROR, "[pro] line_reg", _("R[{n}] 已被 DTM 其他用途占用，請改用空的暫存器", n=p["line_reg"]))
    if not 1 <= int(p["listen_port"]) <= 65535:
        rep.add(ERROR, "[pro] listen_port", _("連接埠要在 1–65535"))
    if int(p["keep_raw_every"]) < 1 or int(p["keep_raw_first"]) < 0:
        rep.add(ERROR, "[pro] keep_raw_every", _("keep_raw_every 至少 1，keep_raw_first 不能是負數"))
    if not 1 <= float(p["slack_q"]) <= 50:
        rep.add(WARN, "[pro] slack_q", _("等待餘裕取第 {v} 百分位不太合理，範例 10", v=p["slack_q"]))
    if float(p["lwi_caution"]) >= float(p["lwi_alert"]):
        rep.add(ERROR, "[pro] lwi_caution", _("lwi_caution 應該小於 lwi_alert"))


def _env_or(v) -> tuple[str, str]:
    """回傳（值, 找不到的環境變數名稱）。"""
    from .notify import secret
    if isinstance(v, str) and v.startswith("env:"):
        return secret(v), ("" if secret(v) else v[4:])
    return str(v or ""), ""


def _check_fleet(cfg: dict, rep: Report):
    """[system] role、邊緣主機的 [[robots]] 清單、上傳中央與中央伺服器的設定。"""
    from .fleet import ROBOT_KEYS
    from .runtime import resolve, slug
    sc = cfg["system"]
    role = sc.get("role", "standalone")
    c = cfg["central"]
    if role not in SYS_ROLES:
        rep.add(ERROR, "[system] role", _("只能是 {opts}", opts=_("、").join(SYS_ROLES)))
        return
    if role == "edge":
        if not str(sc.get("site_id") or "").strip():
            rep.add(ERROR, "[system] site_id", _("邊緣主機要設定廠區代碼，例如 \"P3\"（各廠不可重複）"))
        rs = [r for r in (cfg.get("robots") or []) if not isinstance(r, dict) or r.get("enabled", True)]
        if not rs:
            rep.add(ERROR, "[[robots]]", _("邊緣主機至少要列一台機器人（[[robots]]）"))
        ids: dict[str, int] = {}
        ports = {int(cfg["web"]["port"]): "[web] port"}
        base = int(cfg["fleet"].get("base_port", 8100))
        for i, r in enumerate(rs):
            w = f"[[robots]] #{i + 1}"
            if not isinstance(r, dict):
                rep.add(ERROR, w, _("應該是一個區段"))
                continue
            rid = str(r.get("id") or r.get("name") or "").strip()
            w = f"[[robots]] {rid or '#' + str(i + 1)}"
            for k, v in r.items():
                if k not in ROBOT_KEYS and not (isinstance(v, dict) and k in DEFAULTS):
                    rep.add(WARN, w, _("不認識 {k}，程式會忽略它", k=k))
            if not rid:
                rep.add(ERROR, w, _("要填 id（機器人代碼，例如 \"R14\"）"))
            elif slug(rid) in ids:
                rep.add(ERROR, w, _("代碼跟第 {n} 台重複", n=ids[slug(rid)] + 1))
            else:
                ids[slug(rid)] = i
            port = int(r.get("port") or base + i + 1)
            if port in ports:
                rep.add(ERROR, w + " port", _("連接埠 {p} 跟 {o} 重複", p=port, o=ports[port]))
            ports[port] = w
            src = r.get("source") or cfg["robot"]["source"]
            if src not in SOURCES:
                rep.add(ERROR, w + " source", _("只能是 {opts}", opts=_("、").join(SOURCES)))
            elif src in ("fanuc_http", "modbus") and not r.get("host"):
                rep.add(WARN, w + " host", _("沒填控制器 IP，會用 [source.{src}] host（多台時通常每台不同）", src=src))
            ax = r.get("axes")
            if ax is not None and (not isinstance(ax, list) or not all(isinstance(a, int) and 1 <= a <= 7 for a in ax)):
                rep.add(ERROR, w + " axes", _("要列出監控的軸，例如 [2, 3]；軸號 1 到 7（7 = 行走軸 E1）"))
            if r.get("impact") and r["impact"] not in ("高", "中", "低"):
                rep.add(WARN, w + " impact", _("影響度只能是 高、中、低"))
        if not c.get("url"):
            rep.add(HINT, "[central] url", _("小型模式（沒接中央）：通知、逐級通報、每日摘要由這台統一發，"
                                             "本廠壽命統計每 {h:.0f} 小時重算一次", h=float(c.get("rules_every_s", 3600)) / 3600))
            if len(rs) > 10:
                rep.add(HINT, "[[robots]]", _("超過 10 台：建議另設中央伺服器（多廠集中管理、負載分開）"))
            if not str(cfg["notify"].get("summary_time") or ""):
                rep.add(HINT, "[notify] summary_time", _("建議設定每日摘要時間：每天準時收到摘要＝系統還活著（心跳）"))
    if role == "central":
        tok, env = _env_or(c.get("token", ""))
        toks = c.get("tokens") or {}
        if not tok and not toks:
            rep.add(ERROR, "[central] token", _("找不到環境變數 {env}", env=env) if env else
                    _("中央要設定上傳金鑰（token 或 [central.tokens]），邊緣主機用同一組金鑰上傳"))
        for sid, t in toks.items():
            v, env = _env_or(t)
            if not v:
                rep.add(WARN, f"[central.tokens] {sid}", _("找不到環境變數 {env}", env=env) if env else _("金鑰空白"))
        if not str(cfg["web"].get("tls_cert") or ""):
            rep.add(HINT, "[web] tls_cert", _("各廠跨網段上傳時，建議中央設定 HTTPS（邊緣用 ca_file 驗證）"))
    if role in ("standalone", "edge") and c.get("url"):
        url = str(c["url"])
        if not url.startswith(("http://", "https://")):
            rep.add(ERROR, "[central] url", _("要以 http:// 或 https:// 開頭"))
        elif url.startswith("http://"):
            rep.add(HINT, "[central] url", _("用 http 上傳時金鑰以明文傳送；中央設定 HTTPS 後改成 https://"))
        if not str(sc.get("site_id") or "").strip():
            rep.add(ERROR, "[system] site_id", _("上傳中央要設定廠區代碼"))
        tok, env = _env_or(c.get("token", ""))
        if not tok:
            rep.add(ERROR, "[central] token", _("找不到環境變數 {env}", env=env) if env else _("上傳中央要設定金鑰"))
        if c.get("ca_file") and not os.path.exists(resolve(cfg, c["ca_file"])):
            rep.add(ERROR, "[central] ca_file", _("找不到檔案：{p}", p=resolve(cfg, c["ca_file"])))


def _check_devices(cfg: dict, axes: list, rep: Report):
    from .config import DEVICE_KINDS
    dv = cfg.get("devices") or {}
    if dv.get("ambient") is not None and not isinstance(dv["ambient"], bool):
        rep.add(ERROR, "[devices] ambient", _("只能是 true 或 false"))
    for k, v in (dv.get("axis") or {}).items():
        if not str(k).isdigit() or int(k) not in axes:
            rep.add(WARN, f"[devices.axis.{k}]", _("這個軸沒有在 [axes] enabled 裡，設定不會生效"))
            continue
        for kind, val in (v or {}).items():
            if kind not in DEVICE_KINDS:
                rep.add(WARN, f"[devices.axis.{k}] {kind}", _("不認得的量測，只能是 {opts}", opts=_("、").join(DEVICE_KINDS)))
            elif not isinstance(val, bool):
                rep.add(ERROR, f"[devices.axis.{k}] {kind}", _("只能是 true 或 false"))
            elif val and not cfg["sensors"].get("enabled"):
                rep.add(WARN, f"[devices.axis.{k}] {kind}", _("勾選了這個量測，但 [sensors] 沒有啟用，讀不到資料"))


def _check_sensors(cfg: dict, axes: list, rep: Report):
    _check_devices(cfg, axes, rep)
    sc = cfg["sensors"]
    if not sc.get("enabled"):
        return
    if sc.get("source") not in ("modbus", "sim"):
        rep.add(ERROR, "[sensors] source", _("只能是 modbus 或 sim"))
        return
    if sc.get("source") == "modbus":
        import importlib.util
        if importlib.util.find_spec("pymodbus") is None:
            rep.add(ERROR, "[sensors]", _("感測器閘道需要安裝 pymodbus：pip install pymodbus"))
        if not sc.get("map"):
            rep.add(ERROR, "[sensors.map]", _("感測器已啟用，但沒有設定任何通道"))
    from .sensors import VIB_KEYS
    for key, m in (sc.get("map") or {}).items():
        parts = str(key).split(".")
        ok = key == "ambient" or (len(parts) == 2 and parts[0].isdigit() and parts[1] == "temp") or (
            len(parts) == 3 and parts[0].isdigit() and parts[1] in ("radial", "axial") and parts[2] in VIB_KEYS)
        if not ok:
            rep.add(ERROR, f"[sensors.map] {key}", _("名稱格式：軸.方向.指標（例如 2.radial.rms）、軸.temp 或 ambient"))
        elif parts[0].isdigit() and int(parts[0]) not in axes:
            rep.add(WARN, f"[sensors.map] {key}", _("這個軸沒有在 [axes] enabled 裡，讀到也不會用"))
        if sc.get("source") == "modbus" and (not isinstance(m, dict) or "addr" not in m):
            rep.add(ERROR, f"[sensors.map] {key}", _("要寫成 {{ addr = 位址, type = \"float32\" }}"))
    vs = str(sc.get("vib_source", "diag"))
    if vs != "diag" and not (vs.isdigit() and 9000 <= int(vs) <= 9899):
        rep.add(ERROR, "[sensors] vib_source", _("只能是 \"diag\" 或 9000–9899 的動作代碼"))
    if not sc.get("only_changes") and float(sc.get("poll_hz", 2)) < 0.5:
        rep.add(HINT, "[sensors]", _("輪詢很慢；電池型無線感測器請設 only_changes = true"))


def _check_motion(cfg: dict, rep: Report):
    mc = cfg["motion"]
    if mc.get("metric") not in ("rms", "mean_abs", "peak"):
        rep.add(ERROR, "[motion] metric", _("只能是 rms、mean_abs 或 peak"))
    if float(mc["caution_ratio"]) >= float(mc["alert_ratio"]):
        rep.add(ERROR, "[motion]", _("caution_ratio 要小於 alert_ratio"))
    if float(mc["min_s"]) >= float(mc["max_s"]):
        rep.add(ERROR, "[motion]", _("min_s 要小於 max_s"))
    for k in (mc.get("codes") or {}):
        if not (str(k).isdigit() and 9000 <= int(k) <= 9899):
            rep.add(WARN, f"[motion.codes] {k}", _("動作代碼要在 9000–9899"))


def _check_notify(cfg: dict, rep: Report):
    from .notify import secret
    n = cfg["notify"]
    for old in ("line", "wecom", "teams", "slack"):
        if isinstance(n.get(old), dict) and n[old].get("enabled"):
            rep.add(WARN, f"[notify.{old}]", _("2.1 起已移除這個通道；請改用 Webhook（README 有接 LINE、Teams 等的範例）"))
    sms = n["sms"]
    people = n.get("people") or []
    if people:
        from . import sysconf as SC
        try:
            SC.clean_people(people, cfg)
        except ValueError as e:
            rep.add(ERROR, "[[notify.people]]", str(e))
    if sms["enabled"]:
        if sms["provider"] in ("mitake", "twilio"):
            rep.add(ERROR, "[notify.sms] provider",
                    _("2.1 起三竹、Twilio 改用通用 HTTP 閘道：provider = \"http\"，設定範例見 config.example.toml"))
        elif sms["provider"] not in ("http", "modem"):
            rep.add(ERROR, "[notify.sms] provider", _("只能是 http 或 modem"))
        if not sms["to"] and not any(isinstance(x, dict) and x.get("phone") for x in people):
            rep.add(WARN, "[notify.sms] to", _("簡訊已啟用但沒有收件人"))
        uses_http = sms["provider"] == "http" or "http" in (sms.get("fallback") or [])
        if uses_http and not (sms.get("http") or {}).get("url"):
            rep.add(ERROR, "[notify.sms.http] url", _("使用 HTTP 簡訊閘道要填 url"))
        rp = sms.get("report") or {}
        if rp.get("enabled") and uses_http:
            if not secret(rp.get("key") or ""):
                rep.add(WARN, "[notify.sms.report] key", _("沒有 key，HTTP 簡訊閘道的送達回呼不會開放，畫面會一直「等待手機回報」"))
            elif not n.get("public_url"):
                rep.add(HINT, "[notify] public_url", _("送達回呼網址是儀表板網址加上 /api/sms/report?key=…；請填 public_url，並把完整網址填到簡訊商的回呼設定"))
            if not (rp.get("id_regex") or rp.get("id_json")):
                rep.add(WARN, "[notify.sms.report] id_regex", _("要填 id_regex 或 id_json，才能把回呼對到發出的簡訊"))
    if n["email_enabled"] and (not n["smtp_host"] or not (n["mail_to"] or any(
            isinstance(x, dict) and x.get("email") for x in people))):
        rep.add(ERROR, "[notify]", _("Email 已啟用，要填 smtp_host 與 mail_to"))
    cur = str((cfg.get("maint") or {}).get("currency") or "")
    if not re.fullmatch(r"[A-Z]{3}", cur):
        rep.add(ERROR, "[maint] currency", _("請填 3 個英文大寫字母的幣別代碼，例如 TWD"))
    if str((cfg.get("maint") or {}).get("fx") or "auto") not in ("auto", "manual", "off"):
        rep.add(ERROR, "[maint] fx", _("只能是 auto、manual、off"))
    for k, v in ((cfg.get("maint") or {}).get("fx_rates") or {}).items():
        if not re.fullmatch(r"[A-Za-z]{3}", str(k)) or not isinstance(v, (int, float)) or v <= 0:
            rep.add(ERROR, f"[maint.fx_rates] {k}", _("要寫成三碼幣別 = 正數，例如 USD = 0.031"))
    if str((cfg.get("service") or {}).get("region") or "auto") not in ("auto", "tw", "cn", "us", "jp", "kr", "vn"):
        rep.add(ERROR, "[service] region", _("只能是 auto、tw、cn、us、jp、kr、vn"))
    for p in sms.get("fallback", []):
        if p not in ("http", "modem"):
            rep.add(ERROR, "[notify.sms] fallback", _("備援只能是 http 或 modem"))
    if n.get("webhook_body") and not isinstance(n["webhook_body"], dict):
        rep.add(ERROR, "[notify] webhook_body", _("要寫成 TOML 表格，例如 [notify.webhook_body]"))
    for key in ("quiet_hours",):
        v = n.get(key, "")
        if v and not re.fullmatch(r"\d{1,2}:\d{2}-\d{1,2}:\d{2}", v):
            rep.add(ERROR, f"[notify] {key}", _("格式要像 \"22:00-07:00\""))
    if n.get("summary_time") and not re.fullmatch(r"\d{1,2}:\d{2}", n["summary_time"]):
        rep.add(ERROR, "[notify] summary_time", _("格式要像 \"07:30\""))
    known = {"sms", "email", "webhook"}
    for c in n.get("summary_channels") or []:
        if c not in known:
            rep.add(WARN, "[notify] summary_channels", _("不認得的通道 {c}", c=c))
    enabled = {"sms": bool(sms["enabled"]), "email": bool(n["email_enabled"]), "webhook": bool(n["webhook_url"])}
    for i, r in enumerate(n.get("rules") or [], 1):
        w = f"[[notify.rules]] #{i} {r.get('name', '')}"
        bad = [c for c in r.get("channels", []) if c not in known]
        if bad or not r.get("channels"):
            rep.add(ERROR, w, _("channels 要列出通道：{opts}", opts=_("、").join(sorted(known))))
        if int(r.get("stage", 1)) not in (1, 2, 3):
            rep.add(ERROR, w, _("stage 只能是 1、2、3"))
        if r.get("lang") and not norm(r["lang"]):
            rep.add(ERROR, w, _("lang 只能是 {opts}", opts=_("、").join(LANGS)))
        for c in r.get("channels", []):
            if c in known and not enabled[c]:
                rep.add(WARN, w, _("{c} 通道沒有啟用，這條規則的 {c} 不會發送", c=c))
    if n.get("duty_file"):
        from .runtime import resolve
        if not os.path.exists(resolve(cfg, n["duty_file"])):
            rep.add(ERROR, "[notify] duty_file", _("找不到值班表：{p}", p=resolve(cfg, n["duty_file"])))
    if not any(enabled.values()):
        rep.add(HINT, "[notify]", _("沒有啟用任何通知，警報只會出現在儀表板"))
    if secret(n.get("webhook_url", "")) == "" and str(n.get("webhook_url", "")).startswith("env:"):
        rep.add(ERROR, "[notify] webhook_url", _("找不到環境變數 {env}", env=n["webhook_url"][4:]))


def check_connection(cfg: dict, rep: Report, seconds: float = 10.0, out=print):
    """實際讀幾秒資料。"""
    from .sources import make_source
    from .model import decode_seg, is_motion, vib_window_axis
    src_kind = cfg["robot"]["source"]
    out(_("連線測試：讀取 {s:.0f} 秒資料…", s=seconds))
    try:
        src = make_source(cfg, realtime=True)
    except Exception as e:
        rep.add(ERROR, _("連線"), str(e))
        return
    samples, errors = [], []
    t_end = time.time() + seconds
    try:
        while time.time() < t_end:
            try:
                s = src.read()
                if s is not None:
                    samples.append(s)
            except EOFError:
                break
            except Exception as e:
                errors.append(str(e))
                if len(errors) >= 3 and not samples:
                    break
                time.sleep(0.5)
    finally:
        try:
            src.close()
        except Exception:
            pass
    if not samples:
        tip = {"fanuc_http": _("確認 IP、網路線、控制器網頁伺服器有開（瀏覽器開 http://IP/MD/NUMREG.VA 看得到內容）"),
               "modbus": _("確認 IP、連接埠 502、PLC 的 Modbus 設定")}.get(src_kind, "")
        rep.add(ERROR, _("連線"), _("讀不到資料：{err}", err=errors[-1] if errors else _("沒有回應")) + (f"。{tip}" if tip else ""))
        return
    hz = len(samples) / seconds
    rep.add(OK, _("連線"), _("{n} 筆，約 {hz:.1f} 筆／秒（設定 {set} Hz）", n=len(samples), hz=hz, set=cfg["robot"]["poll_hz"]))
    if errors:
        rep.add(WARN, _("連線"), _("期間有 {n} 次讀取失敗：{err}", n=len(errors), err=errors[-1]))
    if hz < 0.5 * float(cfg["robot"]["poll_hz"]):
        rep.add(WARN, _("連線"), _("實際讀取速率不到設定的一半，網路或控制器回應慢；可把 poll_hz 調低"))
    hbs = [s.hb for s in samples if s.hb is not None]
    if hbs:
        if len(set(hbs)) <= 1:
            rep.add(ERROR, _("心跳"), _("心跳暫存器一直是 {v}，沒有在跳：BG Logic 沒有執行", v=hbs[0]))
        else:
            rep.add(OK, _("心跳"), _("有在跳（{a} → {b}）", a=hbs[0], b=hbs[-1]))
    elif int(cfg["registers"].get("heartbeat") or 0) or src_kind != "fanuc_http":
        rep.add(WARN, _("心跳"), _("讀不到心跳值"))
    lim = float(cfg["realtime"]["invalid_abs"])
    for ax in enabled_axes(cfg):
        vals = [s.dt.get(ax) for s in samples]
        name = axis_cfg(cfg, ax)["name"]
        reg = int(cfg["registers"]["dt_start"]) + ax - 1
        if all(v is None for v in vals):
            rep.add(ERROR, name, _("讀不到扭矩（R[{r}]）", r=reg))
        elif all(v == 0 for v in vals):
            rep.add(WARN, name, _("扭矩一直是 0（R[{r}]）：機器人沒在動，或 BG Logic 沒有複製 $MOR_GRP[1].$CUR_DIS_TRQ[{ax}]",
                                  r=reg, ax=ax))
        elif any(v is not None and abs(v) >= lim for v in vals):
            rep.add(ERROR, name, _("扭矩值超過 {lim}，資料不正常", lim=lim))
        else:
            vv = [v for v in vals if v is not None]
            rep.add(OK, name, _("扭矩 {a:.1f} ～ {b:.1f}", a=min(vv), b=max(vv)))
    ovr = [s.ovr for s in samples if s.ovr is not None]
    if ovr:
        if min(ovr) < 100:
            rep.add(HINT, _("倍率"), _("目前倍率 {v:.0f}%；診斷程式要在 100% 下執行才會被採用", v=min(ovr)))
        else:
            rep.add(OK, _("倍率"), "100%")
    segs = {s.seg for s in samples if s.seg}
    bad = [c for c in segs if decode_seg(c) is None and not is_motion(c) and vib_window_axis(c) is None]   # 動作代碼、振動取樣窗不是錯誤
    if bad:
        rep.add(WARN, _("診斷段代碼"), _("讀到無法辨識的代碼 {codes}", codes=bad[:5]))
    temps = [s.temp for s in samples if s.temp is not None]
    if temps and not all(-10 <= t <= 120 for t in temps):
        rep.add(WARN, _("溫度"), _("溫度值不合理（{a} ～ {b}），確認暫存器與單位", a=min(temps), b=max(temps)))


def run_check(path: str, connect: bool = False, seconds: float = 10.0, out=print) -> Report:
    import copy
    from .config import _merge
    rep = Report()
    raw = read_raw(path, rep)
    if raw is None:
        return rep
    bad = _walk(raw, DEFAULTS, (), rep)
    clean = copy.deepcopy(raw)                 # 型別錯的項目先用預設值代替，其他數值照樣檢查
    for p in bad:
        node = clean
        for k in p[:-1]:
            node = node.get(k, {})
        node.pop(p[-1], None)
    cfg = _merge(DEFAULTS, clean)
    cfg["_path"] = path
    try:
        check_values(cfg, rep)
    except Exception as e:                     # 型別對但值讓程式出錯
        rep.add(ERROR, "", _("檢查時發生錯誤：{e}", e=e))
    if connect and not rep.count(ERROR):
        check_connection(load(path), rep, seconds, out)
    return rep

"""備品價格換算（2.1）：依觀看者的畫面語言，把備品單價與保養材料費換算成當地貨幣（只供估算）。

- 繁中 → TWD、簡中 → CNY、英文 → USD、日文 → JPY、韓文 → KRW、越南文 → VND。單價一律用 [maint] currency 輸入與儲存，
  換算只影響畫面上的顯示。
- 匯率從網路抓：ExchangeRate-API 的免費公開資料（open.er-api.com），抓不到再用 fawazahmed0/currency-api（jsDelivr）。
  每 12 小時更新一次，存在 run/fx_rates.json；抓不到時沿用上次的匯率。
- 工廠不能連外：[maint] fx = "manual"，在 [maint.fx_rates] 手動填（1 單位原幣別 = 多少目標幣別）；"off" = 不換算。
- 這是 DTM 唯一主動連到網際網路的功能（只下載匯率表，不送出任何資料），在「設定 → 隱私與安全」的對外連線裡列出。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone

from .i18n import _, get_lang

log = logging.getLogger("dtm.fx")

LANG_CURRENCY = {"zh-TW": "TWD", "zh-CN": "CNY", "en": "USD", "ja": "JPY", "ko": "KRW", "vi": "VND"}
MODES = ("auto", "manual", "off")
RETRY_S = 1800                 # 抓失敗後隔多久再試
STALE_S = 3 * 86400            # 超過這個時間沒更新，畫面標示「匯率可能過時」


def _parse_er(j: dict, base: str) -> tuple[dict, float]:
    if j.get("result") != "success" or str(j.get("base_code", "")).upper() != base:
        raise ValueError("bad response")
    return {str(k).upper(): float(v) for k, v in (j.get("rates") or {}).items()}, float(j.get("time_last_update_unix") or 0)


def _parse_cdn(j: dict, base: str) -> tuple[dict, float]:
    d = j.get(base.lower())
    if not isinstance(d, dict):
        raise ValueError("bad response")
    ts = datetime.strptime(str(j.get("date")), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    return {str(k).upper(): float(v) for k, v in d.items() if isinstance(v, (int, float))}, ts


SOURCES = [
    {"name": "ExchangeRate-API", "home": "https://www.exchangerate-api.com", "host": "open.er-api.com",
     "url": "https://open.er-api.com/v6/latest/{BASE}", "parse": _parse_er},
    {"name": "currency-api", "home": "https://github.com/fawazahmed0/exchange-api", "host": "cdn.jsdelivr.net",
     "url": "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{base}.json", "parse": _parse_cdn},
]


def _http_get(url: str, proxy: str = "", timeout: float = 10) -> dict:
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    req = urllib.request.Request(url, headers={"User-Agent": "DTM-reducer-monitor", "Accept": "application/json"})
    with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class FX:
    def __init__(self, cfg: dict, fetch=None):
        self.cfg = cfg
        self.fetch = fetch or _http_get
        self.lock = threading.Lock()
        self.busy = False
        self.last_try = 0.0
        self.error = ""
        self.data: dict = {}
        try:
            from .runtime import resolve
            self.path = os.path.join(resolve(cfg, cfg["system"].get("run_dir", "run")), "fx_rates.json")
        except Exception:
            self.path = ""
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (OSError, ValueError):
                self.data = {}

    # -------------------------------------------------------- 設定
    @property
    def m(self) -> dict:
        return self.cfg.get("maint") or {}

    @property
    def base(self) -> str:
        return str(self.m.get("currency") or "TWD").upper()

    @property
    def mode(self) -> str:
        v = str(self.m.get("fx") or "auto")
        return v if v in MODES else "auto"

    # -------------------------------------------------------- 更新
    def refresh(self, force: bool = False, wait: bool = False):
        """需要時在背景更新（不讓畫面等網路）；wait = True 時同步執行（測試、指令列用）。"""
        if self.mode != "auto":
            return
        now = time.time()
        age = now - float(self.data.get("fetched") or 0)
        fresh = self.data.get("base") == self.base and age < float(self.m.get("fx_refresh_h", 12) or 12) * 3600
        if (fresh and not force) or self.busy or (not force and now - self.last_try < RETRY_S and self.error):
            return
        self.busy = True
        self.last_try = now
        if wait:
            self._fetch()
        else:
            threading.Thread(target=self._fetch, name="fx", daemon=True).start()

    def _fetch(self):
        base = self.base
        proxy = str((self.cfg.get("notify") or {}).get("proxy") or "")
        errs = []
        try:
            for src in SOURCES:
                try:
                    rates, ts = src["parse"](self.fetch(src["url"].format(BASE=base, base=base.lower()), proxy), base)
                    if not rates:
                        raise ValueError("empty")
                    data = {"base": base, "rates": rates, "updated": ts or time.time(), "fetched": time.time(),
                            "source": src["name"], "home": src["home"]}
                    with self.lock:
                        self.data = data
                        self.error = ""
                    self._save()
                    return
                except Exception as e:                      # 換下一個來源
                    errs.append(f"{src['name']}: {type(e).__name__}")
            self.error = "; ".join(errs)
            log.warning(_("抓不到匯率（%s），沿用上次的匯率或照原幣別顯示"), self.error)
        finally:
            self.busy = False

    def _save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("fx save: %s", e)

    # -------------------------------------------------------- 給畫面
    def view(self, lang: str | None = None) -> dict:
        """這位觀看者要看的幣別與匯率（1 單位原幣別 = rate 單位顯示幣別）；沒有匯率時 rate = None（照原幣別顯示）。"""
        base, mode = self.base, self.mode
        target = LANG_CURRENCY.get(lang or get_lang(), base)
        out = {"base": base, "display": base, "rate": 1.0, "mode": mode, "target": target, "updated": None, "source": "",
               "home": "", "error": "", "stale": False}
        if mode == "off" or target == base:
            return out
        manual = {str(k).upper(): v for k, v in (self.m.get("fx_rates") or {}).items()}
        if mode == "auto":
            self.refresh()
            d = self.data
            r = (d.get("rates") or {}).get(target) if d.get("base") == base else None
            if r:
                return dict(out, display=target, rate=float(r), updated=d.get("updated"), source=d.get("source", ""),
                            home=d.get("home", ""), stale=time.time() - float(d.get("updated") or 0) > STALE_S)
            if d.get("base") == base and d.get("rates") and not r:
                out["error"] = _("匯率表沒有 {c}", c=target)
            else:
                out["error"] = self.error or _("匯率更新中")
        if manual.get(target):
            try:
                return dict(out, display=target, rate=float(manual[target]), source=_("設定檔手動匯率"), error="")
            except (TypeError, ValueError):
                pass
        return out


_INST: dict[int, FX] = {}
_LOCK = threading.Lock()


def get(cfg: dict) -> FX:
    with _LOCK:
        fx = _INST.get(id(cfg))
        if fx is None or fx.cfg is not cfg:
            fx = FX(cfg)
            _INST[id(cfg)] = fx
    return fx


def outbound(cfg: dict) -> list[str]:
    """隱私頁用：抓匯率會連到哪些主機。"""
    m = (cfg.get("maint") or {})
    return [s["host"] for s in SOURCES] if str(m.get("fx") or "auto") == "auto" else []

"""多國語言：繁中（原文）、簡中、英文、日文、韓文、越南文。

- 原文（msgid）就是程式裡的繁體中文字串，翻譯放在 dtm/locales/<語言>.json。
- 系統語言由設定檔 [ui] lang 決定：通知、警報訊息、報告、工單、指令列都用它。
- 儀表板可以每位使用者各自切換語言（網址參數 lang），即時計算的內容（健康判定、維護決策、
  作業包）會依請求語言產生；已經存下來的警報與通知紀錄保留當時的系統語言。
- 缺翻譯時退回英文（簡中退回繁中原文），不會出錯。
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import string
from contextlib import contextmanager

LANGS = {"zh-TW": "繁體中文", "zh-CN": "简体中文", "en": "English", "ja": "日本語", "ko": "한국어",
         "vi": "Tiếng Việt"}
SOURCE = "zh-TW"
LOCALE_DIR = os.path.join(os.path.dirname(__file__), "locales")
HTML_LANG = {"zh-TW": "zh-Hant", "zh-CN": "zh-Hans", "en": "en", "ja": "ja", "ko": "ko", "vi": "vi"}
FONT_STACK = {
    "zh-TW": '"Microsoft JhengHei","Noto Sans TC","Noto Sans CJK TC","PingFang TC",sans-serif',
    "zh-CN": '"Microsoft YaHei","Noto Sans SC","Noto Sans CJK SC","PingFang SC",sans-serif',
    "ja": '"Yu Gothic UI","Meiryo","Noto Sans JP","Noto Sans CJK JP","Hiragino Sans",sans-serif',
    "ko": '"Malgun Gothic","Noto Sans KR","Noto Sans CJK KR","Apple SD Gothic Neo",sans-serif',
    "en": '"Segoe UI","Noto Sans",Roboto,Arial,sans-serif',
    "vi": '"Segoe UI","Noto Sans",Roboto,Arial,sans-serif',
}

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("dtm_lang", default=None)
_default = [SOURCE]
_catalogs: dict[str, dict[str, str]] = {}
_ALIASES = {"zh": "zh-TW", "zh-tw": "zh-TW", "zh-hant": "zh-TW", "zh_tw": "zh-TW", "tw": "zh-TW",
            "zh-cn": "zh-CN", "zh-hans": "zh-CN", "zh_cn": "zh-CN", "cn": "zh-CN", "zh-sg": "zh-CN",
            "en": "en", "en-us": "en", "en-gb": "en", "ja": "ja", "ja-jp": "ja", "jp": "ja",
            "ko": "ko", "ko-kr": "ko", "kr": "ko", "vi": "vi", "vi-vn": "vi", "vn": "vi"}


def norm(lang: str | None) -> str | None:
    """各種寫法（zh_TW、zh-Hant、JP…）轉成標準代碼；不支援的語言回傳 None。"""
    if not lang:
        return None
    return _ALIASES.get(str(lang).strip().lower().replace("_", "-"))


def set_default(lang: str | None):
    _default[0] = norm(lang) or SOURCE


def default_lang() -> str:
    return _default[0]


def get_lang() -> str:
    return _current.get() or _default[0]


@contextmanager
def use(lang: str | None):
    """在這個區塊內用指定語言產生文字（例如網頁請求帶 lang 參數）。"""
    tok = _current.set(norm(lang) or None)
    try:
        yield
    finally:
        _current.reset(tok)


def catalog(lang: str) -> dict[str, str]:
    if lang == SOURCE:
        return {}
    if lang not in _catalogs:
        path = os.path.join(LOCALE_DIR, f"{lang}.json")
        try:
            with open(path, encoding="utf-8") as f:
                _catalogs[lang] = json.load(f)
        except (OSError, ValueError):
            _catalogs[lang] = {}
    return _catalogs[lang]


class _Safe(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def lookup(msgid: str, lang: str | None = None) -> str:
    lang = lang or get_lang()
    if lang == SOURCE:
        return msgid
    tr = catalog(lang).get(msgid)
    if tr is None and lang not in ("en", "zh-CN"):       # 簡中缺翻譯時用原文（繁中）較好讀；其他語言退回英文
        tr = catalog("en").get(msgid)
    return msgid if tr is None else tr


class _Fmt(string.Formatter):
    """多一個轉換 !l：句中的名詞首字改小寫（英文、越南文用，例如 "the {comp!l} replacement"）。"""

    def convert_field(self, value, conversion):
        if conversion == "l":
            v = str(value)
            return v[:1].lower() + v[1:]
        return super().convert_field(value, conversion)


_FMT = _Fmt()


class Msg(str):
    """翻譯好的文字，同時記得原文（msgid）與參數，存下來以後可以換成其他語言重新產生。

    用起來跟一般字串一樣；用 + 串接、或用 join() 串接的結果仍然記得每一段（mid = None，parts = 各段）。
    f-string、切片、replace 等字串操作會變回一般字串（只剩當下語言的文字），不影響正確性。
    """

    def __new__(cls, text: str, mid: str | None = None, kw: dict | None = None, parts: tuple = ()):
        o = str.__new__(cls, text)
        o.mid, o.kw, o.parts = mid, kw or {}, parts
        return o

    def __reduce__(self):                        # deepcopy／pickle
        return (_remake, (str(self), self.mid, self.kw, self.parts, getattr(self, "packed", None)))

    def __add__(self, other):
        if not isinstance(other, str):
            return NotImplemented
        return cat(self, other)

    def __radd__(self, other):
        if not isinstance(other, str):
            return NotImplemented
        return cat(other, self)


def _remake(text, mid, kw, parts, packed=None):
    m = Msg(text, mid, kw, parts)
    if packed:
        m.packed = packed
    return m


def cat(*parts) -> str:
    """串接文字；只要有一段是 Msg，結果也記得每一段。"""
    flat: list = []
    for p in parts:
        if not p:
            continue
        if isinstance(p, Msg) and p.mid is None and p.parts:
            flat.extend(p.parts)
        else:
            flat.append(p)
    text = "".join(str(p) for p in flat)
    if any(isinstance(p, Msg) for p in flat):
        return Msg(text, None, None, tuple(flat))
    return text


def _(msgid: str, **kw) -> str:
    """翻譯並代入參數：_("{name} 健康{level}", name="J3", level=...)。"""
    s = lookup(msgid)
    if kw:
        try:
            s = _FMT.vformat(s, (), _Safe(kw))
        except (ValueError, IndexError, KeyError):
            s = _FMT.vformat(msgid, (), _Safe(kw))
    return Msg(s, msgid, kw)


# ---------------------------------------------------------------- 存下來的文字換語言
# 存法：pack(text) 得到可以寫進 JSON 的模板（不是 Msg 就是 None）；之後 render(模板, 原文) 依目前語言重新產生。
# 模板只在「每個原文都在目前語言的翻譯表裡」時才使用，否則顯示存下來的原文（例如舊版邊緣主機送來的文字）。

def _pack_val(v):
    if isinstance(v, Msg):
        return {"$": pack(v)}
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return str(v)


def pack(text) -> dict | None:
    if not isinstance(text, Msg):
        return None
    if getattr(text, "packed", None):
        return text.packed
    if text.mid is None and not text.parts:
        return None
    if text.mid is not None:
        out: dict = {"m": text.mid}
        if text.kw:
            out["a"] = {k: _pack_val(v) for k, v in text.kw.items()}
        return out
    return {"c": [pack(p) if isinstance(p, Msg) else str(p) for p in text.parts]}


def pack_json(text) -> str | None:
    t = pack(text)
    return json.dumps(t, ensure_ascii=False, separators=(",", ":")) if t else None


def _known(t, lang: str) -> bool:
    if isinstance(t, str):
        return True
    if not isinstance(t, dict):
        return False
    if "c" in t:
        return all(_known(p, lang) for p in t["c"] or [])
    mid = t.get("m")
    if not isinstance(mid, str) or (lang != SOURCE and mid not in catalog(lang)
                                     and not (lang not in ("en", "zh-CN") and mid in catalog("en"))):
        return False
    return all(_known(v["$"], lang) for v in (t.get("a") or {}).values() if isinstance(v, dict) and "$" in v)


def _render(t) -> str:
    if isinstance(t, str):
        return t
    if "c" in t:
        return "".join(_render(p) for p in t["c"])
    kw = {k: (_render(v["$"]) if isinstance(v, dict) and "$" in v else v) for k, v in (t.get("a") or {}).items()}
    s = lookup(t["m"])
    return _FMT.vformat(s, (), _Safe(kw)) if kw else s


def render(tmpl, text: str = "") -> str:
    """依目前語言重新產生存下來的文字；模板不能用（格式錯、缺翻譯）就回傳原文 text。"""
    if not tmpl:
        return text
    if isinstance(tmpl, str):
        try:
            tmpl = json.loads(tmpl)
        except ValueError:
            return text
    try:
        if not _known(tmpl, get_lang()):
            return text
        return _render(tmpl)
    except Exception:
        return text


def revive(tmpl, text: str):
    """從資料庫讀回的文字 + 模板 → Msg（之後可以再交給 render／pack，例如通知依規則語言重新產生）。"""
    if isinstance(tmpl, str):
        try:
            tmpl = json.loads(tmpl)
        except ValueError:
            tmpl = None
    if not isinstance(tmpl, dict):
        return text
    m = Msg(text or "")
    m.packed = tmpl
    return m


def in_lang(text, lang: str | None) -> str:
    """把 Msg 換成指定語言（不能換就原樣）。"""
    if not lang or not isinstance(text, Msg):
        return text
    with use(lang):
        out = render(pack(text), str(text))
    return revive(pack(text), out)


def templates(obj):
    """把一份資料（dict／list）裡所有 Msg 的模板挑出來，結構跟原資料一樣，只留有模板的位置；沒有就回傳 None。"""
    if isinstance(obj, Msg):
        return pack(obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            t = templates(v)
            if t is not None:
                out[str(k)] = t
        return out or None
    if isinstance(obj, (list, tuple)):
        out = {}
        for i, v in enumerate(obj):
            t = templates(v)
            if t is not None:
                out[str(i)] = t
        return out or None
    return None


def localize(obj, tmpl):
    """templates() 的反向：依目前語言把 obj 裡有模板的文字換掉（就地修改並回傳 obj）。"""
    if not tmpl:
        return obj
    if isinstance(tmpl, str):
        try:
            tmpl = json.loads(tmpl)
        except ValueError:
            return obj
    if isinstance(obj, str):
        return render(tmpl, obj)
    if not isinstance(tmpl, dict):
        return obj
    if isinstance(obj, dict):
        for k, t in tmpl.items():
            key = k if k in obj else (int(k) if k.lstrip("-").isdigit() and int(k) in obj else None)
            if key is not None:
                obj[key] = localize(obj[key], t)
    elif isinstance(obj, list):
        for k, t in tmpl.items():
            if k.isdigit() and int(k) < len(obj):
                obj[int(k)] = localize(obj[int(k)], t)
    return obj


def N_(msgid: str) -> str:
    """只標記要翻譯的字串（例如模組層級的常數），實際翻譯在顯示時做。"""
    return msgid


class TDict(dict):
    """值是 msgid 的對照表；取值時自動翻成目前語言。"""

    def __getitem__(self, k):
        return _(super().__getitem__(k))

    def get(self, k, default=None):
        return _(super().__getitem__(k)) if k in self else default

    def items(self):
        return [(k, _(v)) for k, v in super().items()]

    def values(self):
        return [_(v) for v in super().values()]

    def raw(self, k):
        return super().__getitem__(k)


def join(parts, sep: str = "；") -> str:
    """用目前語言的分隔符號串接（中文全形分號，其他語言用半形）；結果仍記得各段（見 Msg）。"""
    items = [p for p in parts if p]
    if not items:
        return ""
    s = _(sep)
    out: list = [items[0]]
    for p in items[1:]:
        out += [s, p]
    return cat(*out)


MIN_COVERAGE = 0.95      # 翻譯完成度達到這個比例，語言選單才顯示（未完成的翻譯檔仍保留在程式裡）
_coverage: dict[str, float] = {}


def coverage(lang: str) -> float:
    """這個語言翻好的介面文字比例（以 locales/_msgids.json 的全部文字為準）。"""
    if lang == SOURCE:
        return 1.0
    if lang not in _coverage:
        try:
            with open(os.path.join(LOCALE_DIR, "_msgids.json"), encoding="utf-8") as f:
                ids = json.load(f)
        except (OSError, ValueError):
            ids = []
        cat = catalog(lang)
        _coverage[lang] = (sum(1 for i in ids if cat.get(i)) / len(ids)) if ids else 0.0
    return _coverage[lang]


def visible_langs(extra: tuple = ()) -> dict[str, str]:
    """語言選單要顯示的語言：翻譯完成的，再加上目前正在用的（設定檔或網址指定的）。"""
    keep = set(x for x in extra if x)
    return {k: v for k, v in LANGS.items() if k == SOURCE or k in keep or coverage(k) >= MIN_COVERAGE}


def ui_messages(lang: str) -> dict[str, str]:
    """給儀表板的翻譯表（缺的項目已用英文補上）。"""
    lang = norm(lang) or SOURCE
    if lang == SOURCE:
        return {}
    out = dict(catalog("en")) if lang not in ("en", "zh-CN") else {}
    out.update(catalog(lang))
    return out


_CJK = re.compile(r"[㐀-鿿]")


def has_cjk(s: str) -> bool:
    return bool(_CJK.search(s or ""))

"""翻譯工具：從程式與儀表板抽出所有原文（繁中 msgid），檢查各語言是否缺翻譯。

    python tools/i18n_tool.py extract      # 列出全部 msgid（寫到 dtm/locales/_msgids.json）
    python tools/i18n_tool.py check        # 各語言缺少、多餘的項目
"""
from __future__ import annotations

import ast
import glob
import json
import os
import re
import sys
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CJK = re.compile(r"[㐀-鿿！-～　-〿]")
HAN = re.compile(r"[㐀-鿿]")
EXTRA = ["；", "、", "｜", "，"]          # join() 與畫面用的分隔符號


def from_python() -> set[str]:
    out = set()
    for f in glob.glob(os.path.join(ROOT, "dtm", "*.py")) + glob.glob(os.path.join(ROOT, "dtm", "pro", "*.py")):
        tree = ast.parse(open(f, encoding="utf-8").read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                if n.func.id in ("_", "N_", "t") and n.args and isinstance(n.args[0], ast.Constant) \
                        and isinstance(n.args[0].value, str):
                    out.add(n.args[0].value)
                if n.func.id == "TDict" and n.args and isinstance(n.args[0], ast.Dict):
                    for v in n.args[0].values:
                        if isinstance(v, ast.Constant) and isinstance(v.value, str):
                            out.add(v.value)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("th",):
                for a in n.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) and HAN.search(a.value):
                        out.add(a.value)
            # report.py / workpack.py: for m in ("軸", "研判", ...) 這類標題清單
            if isinstance(n, ast.comprehension) and isinstance(n.iter, ast.Tuple):
                for e in n.iter.elts:
                    if isinstance(e, ast.Constant) and isinstance(e.value, str) and HAN.search(e.value):
                        out.add(e.value)
    return out


class _HP(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out, self.stack, self.script = set(), [], []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)
        for k, v in attrs:
            if k in ("placeholder", "title", "label", "aria-label") and v and HAN.search(v):
                self.out.add(v.strip())

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self.stack and self.stack[-1] in ("script", "style"):
            if self.stack[-1] == "script":
                self.script.append(data)
            return
        t = data.strip()
        if t and HAN.search(t):
            self.out.add(t)


PAGES = ("index.html", "central.html", "pro.html")


def from_html() -> set[str]:
    out = set()
    for page in PAGES:
        out |= _from_page(page)
    return out


def _from_page(page: str) -> set[str]:
    hp = _HP()
    hp.feed(open(os.path.join(ROOT, "dtm", "static", page), encoding="utf-8").read())
    out = set(hp.out)
    js = "".join(hp.script)
    js = re.sub(r"(?m)^\s*//.*$", "", js)                       # 整行註解
    call = re.compile(r'\bT\("((?:[^"\\\n]|\\.)*)"')
    for m in call.finditer(js):                                 # T("…") 直接抽出（可能包在 HTML 屬性的雙引號裡）
        out.add(json.loads(f'"{m.group(1)}"'))
    js = call.sub('T(""', js)
    for m in re.finditer(r'"((?:[^"\\\n]|\\.)*)"', js):
        s = m.group(1)
        if CJK.search(s) or s in EXTRA:
            out.add(json.loads(f'"{s}"'))
    stripped = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', js)
    bad = [line.strip()[:80] for line in stripped.splitlines() if HAN.search(line) and "test(" not in line]
    if bad:
        print(f"警告：{page} 的 JS 有未用雙引號包住的中文：", *bad, sep="\n  ", file=sys.stderr)
    return out


def msgids() -> list[str]:
    return sorted(from_python() | from_html() | set(EXTRA))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    ids = msgids()
    if cmd == "extract":
        path = os.path.join(ROOT, "dtm", "locales", "_msgids.json")
        json.dump(ids, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
        print(f"{len(ids)} msgids → {path}")
        return
    for lang in ("zh-CN", "en", "ja", "ko", "vi"):
        path = os.path.join(ROOT, "dtm", "locales", f"{lang}.json")
        cat = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
        missing = [m for m in ids if m not in cat]
        extra = [k for k in cat if k not in ids]
        badfmt = [m for m in ids if m in cat and set(re.findall(r"\{(\w+)", m)) != set(re.findall(r"\{(\w+)", cat[m]))]
        print(f"{lang}: {len(cat)} 項，缺 {len(missing)}，多餘 {len(extra)}，參數不符 {len(badfmt)}")
        for m in missing[:5]:
            print("   缺:", m[:70])
        for m in badfmt[:5]:
            print("   參數:", m[:50], "→", cat[m][:50])


if __name__ == "__main__":
    main()

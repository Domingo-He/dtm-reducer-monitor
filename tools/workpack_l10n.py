"""作業包目錄的多國語言版：由繁中原檔產生 workpacks.<機型>.<語言>.toml。

    python tools/workpack_l10n.py                  # 產生全部語言
    python tools/workpack_l10n.py check            # 只檢查缺哪些翻譯

翻譯表在 tools/workpack_tr/<語言>.json（原文 → 譯文）；簡中用 OpenCC 轉換加術語表，不需要翻譯表。
只翻譯字串值（名稱、規格、單位、步驟、安全指令、備註）；料號、數量、工時、id、網址不動。
原檔修改後重新執行即可；新增的字串若沒有翻譯會列出來，不會產生半翻譯的檔案。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TR_DIR = os.path.join(ROOT, "tools", "workpack_tr")
SOURCES = ["workpacks.r2000ic_165f.toml"]
LANGS = ["zh-CN", "en", "ja", "ko", "vi"]
CJK = re.compile(r"[㐀-鿿　-〿＀-￯]")
QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')

HEADER = {
    "zh-CN": "# 备件与作业包目录（简体中文版）：由 {src} 自动生成，请修改繁中原档后执行 tools/workpack_l10n.py 重新生成。\n"
             "# 物料号来自公开资料，下单前请以机台 Parts list 与铭牌确认；工时为参考估算。\n",
    "en": "# Spare parts and work package catalog (English): generated from {src} by tools/workpack_l10n.py.\n"
          "# Edit the source file and regenerate. Part numbers come from public sources; verify against the\n"
          "# robot's parts list and nameplates before ordering. Hours are estimates, not FANUC standard times.\n",
    "ja": "# 予備品・作業パッケージカタログ（日本語版）：{src} から tools/workpack_l10n.py で自動生成。\n"
          "# 元ファイルを編集して再生成してください。部品番号は公開資料によるもので、発注前にパーツリストと銘板で確認すること。\n",
    "ko": "# 예비품 및 작업 패키지 카탈로그(한국어): {src}에서 tools/workpack_l10n.py로 자동 생성.\n"
          "# 원본 파일을 수정한 뒤 다시 생성하십시오. 부품 번호는 공개 자료 기준이므로 주문 전 부품 목록과 명판으로 확인하십시오.\n",
    "vi": "# Danh mục phụ tùng và gói công việc (tiếng Việt): tạo tự động từ {src} bằng tools/workpack_l10n.py.\n"
          "# Hãy sửa tệp gốc rồi tạo lại. Mã phụ tùng lấy từ tài liệu công khai; xác minh với parts list và nhãn máy trước khi đặt hàng.\n",
}

# 簡中術語：OpenCC tw2sp 之後再套用（大陸工廠習慣用語）
ZH_CN_TERMS = [
    ("伺服马达", "伺服电机"), ("马达", "电机"), ("料号", "物料号"), ("天车", "行车"), ("吊挂", "吊装"),
    ("环首螺栓", "吊环螺栓"), ("零点校正", "零点标定"), ("给脂口", "注脂口"), ("给脂嘴", "注脂嘴"),
    ("给脂", "注脂"), ("封止螺栓", "密封螺栓"), ("围篱", "围栏"), ("送电", "上电"), ("万用电表", "万用表"),
    ("持证人员", "持证人员"), ("班次", "班次"), ("护目镜", "护目镜"), ("扭力扳手", "扭矩扳手"),
    ("螺栓扭力", "螺栓扭矩"), ("扭力", "扭矩"), ("隨身碟", "U盘"), ("随身碟", "U盘"),
]


def strings(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for s in QUOTED.findall(line):
            if CJK.search(s) and s not in out:
                out.append(s)
    return out


def table(lang: str, needed: list[str]) -> dict[str, str]:
    if lang == "zh-CN":
        import opencc
        cc = opencc.OpenCC("tw2sp")
        out = {}
        for s in needed:
            t = cc.convert(s)
            for a, b in ZH_CN_TERMS:
                t = t.replace(a, b)
            out[s] = t
        return out
    path = os.path.join(TR_DIR, f"{lang}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def build(src: str, lang: str, tr: dict[str, str]) -> str:
    text = open(src, encoding="utf-8").read()
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            if CJK.search(line) or line.strip() == "#":
                continue                     # 中文註解（欄位說明等）不搬過去，說明見 README
            lines.append(line)
            continue
        lines.append(QUOTED.sub(lambda m: '"' + tr.get(m.group(1), m.group(1)).replace('"', '\\"') + '"', line))
    body = "\n".join(lines).lstrip("\n")
    body = re.sub(r"\n{3,}", "\n\n", body)
    return HEADER[lang].format(src=os.path.basename(src)) + "\n" + body + "\n"


def same_shape(a, b, path="") -> list[str]:
    """翻譯後的結構要跟原檔一樣：鍵、陣列長度、數字、布林、料號都不變。"""
    errs = []
    if isinstance(a, dict):
        if set(a) != set(b):
            errs.append(f"{path}: 鍵不同")
        for k in a:
            if k in b:
                errs += same_shape(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        if len(a) != len(b):
            errs.append(f"{path}: 長度不同")
        for i, (x, y) in enumerate(zip(a, b)):
            errs += same_shape(x, y, f"{path}[{i}]")
    elif isinstance(a, str):
        if not CJK.search(a) and a != b:
            errs.append(f"{path}: 非中文字串被改動")
    elif a != b:
        errs.append(f"{path}: 值不同")
    return errs


def main(argv):
    only_check = argv[:1] == ["check"]
    bad = 0
    for name in SOURCES:
        src = os.path.join(ROOT, name)
        text = open(src, encoding="utf-8").read()
        needed = strings(text)
        for lang in LANGS:
            tr = table(lang, needed)
            missing = [s for s in needed if s not in tr]
            extra = [s for s in tr if s not in needed]
            print(f"{name} {lang}: {len(needed)} 項，缺 {len(missing)}，多餘 {len(extra)}")
            for s in missing[:20]:
                print("   缺:", s)
            if missing:
                bad += 1
                continue
            out = build(src, lang, tr)
            errs = same_shape(tomllib.loads(text), tomllib.loads(out))
            leaked = [s for s in strings(out)] if lang in ("en", "ko", "vi") else []
            if errs or leaked:
                bad += 1
                print("   結構錯誤:", errs[:5], "殘留中文:", leaked[:5])
                continue
            if not only_check:
                root, ext = os.path.splitext(src)
                with open(f"{root}.{lang}{ext}", "w", encoding="utf-8") as f:
                    f.write(out)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

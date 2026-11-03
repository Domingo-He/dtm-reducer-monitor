"""產生 Windows 免安裝版：內含 Python（官方 embeddable 版），解壓縮後雙擊 .bat 就能用。

    python tools/build_windows.py                       # 自動下載 Python 3.12 embeddable（需要能連 python.org）
    python tools/build_windows.py --python-zip python-3.12.10-embed-amd64.zip   # 用自己下載好的檔案
    python tools/build_windows.py --sha256 <官方公布的 SHA-256>                  # 下載後比對雜湊

產出：dist/DTM-<版本>-win64/（資料夾）與 dist/DTM-<版本>-win64.zip
- 程式檔案取自 git ls-files（只包含納入版本管理的檔案，不會帶到 data/、logs/、config.toml 等）。
- python/ 底下是官方 embeddable Python；pythonXY._pth 會加上上一層資料夾，讓 python -m dtm 找得到程式。
- 各個 .bat 啟動檔會優先使用同資料夾的 python\\python.exe，所以不用另外安裝 Python。
- 選用套件（Modbus 的 pymodbus、4G 簡訊數據機的 pyserial）沒有包含；需要時在一般安裝的 Python 用 pip 安裝。
- 想做成安裝程式：用 Inno Setup 編譯 tools/installer/dtm.iss（見該檔說明）。

在這個專案的雲端開發環境裡連不到 python.org，所以這支腳本需要在 Windows（或任何能上網的電腦）上執行一次；
GitHub 的 release 流程（.github/workflows/release.yml）也會在 Windows 主機上自動執行並附在 Release。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_VERSION = "3.12.10"
URL = "https://www.python.org/ftp/python/{v}/python-{v}-embed-amd64.zip"


def version() -> str:
    ns: dict = {}
    with open(os.path.join(ROOT, "dtm", "__init__.py"), encoding="utf-8") as f:
        exec(f.read(), ns)
    return ns["__version__"]


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get_python(dest_dir: str, py_zip: str | None, py_version: str, expect: str | None) -> str:
    if not py_zip:
        url = URL.format(v=py_version)
        py_zip = os.path.join(dest_dir, os.path.basename(url))
        if not os.path.exists(py_zip):
            print(f"下載 {url}")
            try:
                urllib.request.urlretrieve(url, py_zip + ".part")
            except Exception as e:
                raise SystemExit(f"下載失敗：{e}\n請在能上網的電腦下載上面的檔案，再用 --python-zip 指定。")
            os.replace(py_zip + ".part", py_zip)
    digest = sha256(py_zip)
    print(f"Python embeddable：{os.path.basename(py_zip)}  SHA-256 {digest}")
    if expect and digest.lower() != expect.lower():
        raise SystemExit("SHA-256 跟指定的不一樣，檔案可能損壞或被竄改，停止。")
    if not expect:
        print("提醒：沒有指定 --sha256。請到 python.org 該版本的下載頁核對上面的雜湊值。")
    return py_zip


def build(out_dir: str, py_zip: str | None = None, py_version: str = PY_VERSION, expect: str | None = None) -> str:
    ver = version()
    name = f"DTM-{ver}-win64"
    os.makedirs(out_dir, exist_ok=True)
    target = os.path.join(out_dir, name)
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(target)
    for rel in tracked_files():
        if rel.startswith((".github/", "tests/")) or rel in (".gitignore", ".gitattributes"):
            continue
        src, dst = os.path.join(ROOT, rel), os.path.join(target, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    pz = get_python(out_dir, py_zip, py_version, expect)
    pydir = os.path.join(target, "python")
    with zipfile.ZipFile(pz) as z:
        z.extractall(pydir)
    pth = [f for f in os.listdir(pydir) if f.endswith("._pth")]
    if not pth:
        raise SystemExit("embeddable 檔案裡找不到 ._pth，可能不是官方 embeddable 版")
    p = os.path.join(pydir, pth[0])
    with open(p, encoding="utf-8") as f:
        lines = [ln.rstrip("\r\n") for ln in f]
    if ".." not in lines:
        lines.insert(lines.index(".") + 1 if "." in lines else len(lines), "..")    # 程式在上一層資料夾
    with open(p, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(target, "免安裝版說明.txt"), "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(f"DTM {ver}（Windows 免安裝版，內含 Python {py_version}）\n\n"
                "1. 把整個資料夾解壓縮到 C:\\DTM 之類的位置（路徑不要有空白比較保險）。\n"
                "2. 先雙擊 start_sim.bat 用模擬器試跑；看中央畫面雙擊 start_central_demo.bat。\n"
                "3. 正式使用請照 README.zh-TW.md（英文版 README.md）設定 config.toml 後雙擊 start.bat（10 台以下可用 start_small.bat）。\n"
                "4. 要裝成 Windows 服務：在這個資料夾開命令提示字元，執行\n"
                "   python\\python.exe -m dtm -c config.toml service install\n\n"
                "Modbus（pymodbus）與 4G 簡訊數據機（pyserial）需要另外安裝 Python 與套件。\n"
                "授權：Apache-2.0（LICENSE、NOTICE）；內含的 Python 依其授權（python\\LICENSE.txt）散布。\n")
    zpath = os.path.join(out_dir, name + ".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, files in os.walk(target):
            for fn in files:
                full = os.path.join(base, fn)
                z.write(full, os.path.relpath(full, out_dir))
    print(f"完成：{zpath}（{os.path.getsize(zpath) / 1e6:.1f} MB）")
    return zpath


def main(argv=None):
    ap = argparse.ArgumentParser(description="產生 Windows 免安裝版（內含 Python embeddable）")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--python-zip", default=None, help="自己下載好的 python-X.Y.Z-embed-amd64.zip")
    ap.add_argument("--python-version", default=PY_VERSION)
    ap.add_argument("--sha256", default=None, help="官方公布的 SHA-256（建議填）")
    a = ap.parse_args(argv)
    build(a.out, a.python_zip, a.python_version, a.sha256)


if __name__ == "__main__":
    sys.exit(main())

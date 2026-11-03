"""產生軟體物料清單 SBOM（CycloneDX 1.5 JSON）：DTM 本身、選用套件、內附的第三方檔案。

    python tools/sbom.py                       # 寫到 dtm-<版本>-sbom.cdx.json
    python tools/sbom.py --out sbom.json

只用標準函式庫。同一個版本產生的內容固定（序號由版本決定；時間用 SOURCE_DATE_EPOCH，沒有就用現在），
方便比對。Release 時由 .github/workflows/release.yml 自動產生並附加。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _sha256(rel: str) -> str:
    h = hashlib.sha256()
    with open(os.path.join(ROOT, rel), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def version() -> str:
    return re.search(r'__version__ = "(.+?)"', _read("dtm/__init__.py")).group(1)


def requirements() -> dict[str, str]:
    """requirements.txt 的選用套件：{名稱: 版本條件}。"""
    out = {}
    for ln in _read("requirements.txt").splitlines():
        ln = ln.split("#", 1)[0].strip()
        m = re.match(r"([A-Za-z0-9_.\-]+)\s*(.*)$", ln)
        if m:
            out[m.group(1).lower()] = m.group(2).strip()
    return out


def python_embed_version() -> str:
    m = re.search(r'PY_VERSION = "(.+?)"', _read("tools/build_windows.py"))
    return m.group(1) if m else ""


def build() -> dict:
    ver = version()
    ts = int(os.environ.get("SOURCE_DATE_EPOCH") or time.time())
    reqs = requirements()
    comps = []
    for name, why in (("pymodbus", "Modbus TCP data source ([source] type = \"modbus\")"),
                      ("pyserial", "4G SMS modem ([notify.sms] provider = \"modem\")")):
        if name in reqs:
            comps.append({
                "type": "library", "bom-ref": f"pypi-{name}", "name": name, "scope": "optional",
                "purl": f"pkg:pypi/{name}", "description": f"Optional, installed by the user only when needed: {why}",
                "properties": [{"name": "dtm:version-constraint", "value": reqs[name]}]})
    comps.append({
        "type": "library", "bom-ref": "npm-html2canvas", "name": "html2canvas", "version": "1.4.1", "scope": "required",
        "author": "Niklas von Hertzen", "purl": "pkg:npm/html2canvas@1.4.1",
        "licenses": [{"license": {"id": "MIT"}}],
        "hashes": [{"alg": "SHA-256", "content": _sha256("dtm/static/vendor/html2canvas.min.js")}],
        "description": "Bundled as dtm/static/vendor/html2canvas.min.js; runs in the browser for the full-page screenshot button",
        "externalReferences": [{"type": "website", "url": "https://html2canvas.hertzen.com"}]})
    comps.append({
        "type": "application", "bom-ref": "github-winsw", "name": "WinSW", "version": "2.12.0", "scope": "optional",
        "purl": "pkg:github/winsw/winsw@v2.12.0", "licenses": [{"license": {"id": "MIT"}}],
        "hashes": [{"alg": "SHA-256", "content": _sha256("tools/winsw/WinSW.NET461.exe")}],
        "description": "Bundled as tools/winsw/WinSW.NET461.exe; used only by 'python -m dtm service install' on Windows",
        "externalReferences": [{"type": "distribution", "url": "https://github.com/winsw/winsw/releases/tag/v2.12.0"}]})
    pv = python_embed_version()
    if pv:
        comps.append({
            "type": "platform", "bom-ref": "python-embed", "name": "python", "version": pv, "scope": "optional",
            "purl": f"pkg:generic/python@{pv}", "licenses": [{"license": {"id": "PSF-2.0"}}],
            "description": "Windows embeddable package, included only in the Windows portable build (DTM-*-win64.zip)",
            "externalReferences": [{"type": "distribution",
                                    "url": f"https://www.python.org/ftp/python/{pv}/python-{pv}-embed-amd64.zip"}]})
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.5",
        "serialNumber": "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"urn:dtm:sbom:{ver}")),
        "version": 1,
        "metadata": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
            "tools": {"components": [{"type": "application", "name": "dtm tools/sbom.py", "version": ver}]},
            "component": {
                "type": "application", "bom-ref": "dtm", "name": "dtm", "version": ver,
                "description": "DTM - Disturbance Torque Monitor for FANUC robot reducers (Beta)",
                "author": "Domingo He <domimgo491@gmail.com>",
                "licenses": [{"license": {"id": "Apache-2.0"}}],
                "properties": [{"name": "dtm:runtime", "value": "Python >= 3.11, standard library only for the core"}]},
        },
        "components": comps,
        "dependencies": [{"ref": "dtm", "dependsOn": [c["bom-ref"] for c in comps]}]
        + [{"ref": c["bom-ref"], "dependsOn": []} for c in comps],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="CycloneDX 1.5 SBOM for DTM")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    bom = build()
    out = a.out or f"dtm-{bom['metadata']['component']['version']}-sbom.cdx.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(bom, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"{out}: {len(bom['components'])} components")
    return out


if __name__ == "__main__":
    main()

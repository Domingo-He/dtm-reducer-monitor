"""HTTPS 自簽憑證（2.1）：不需要 OpenSSL 或其他套件，用 Python 標準函式庫產生 RSA 金鑰與 X.509 憑證。

- 憑證含這台電腦的名稱、IP 與 localhost（SAN），有效 825 天（瀏覽器接受的上限）。
- 自簽憑證瀏覽器第一次會顯示「不安全」警告；把憑證（dtm-cert.pem）匯入各台電腦的「受信任的根憑證授權單位」後就不會再出現。
  公司有內部 CA 時，請改用 CA 簽發的憑證（[web] tls_cert／tls_key）。
- 金鑰檔只放在這台主機，權限設成只有執行 DTM 的帳號可讀（Windows 上依資料夾權限）。
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import secrets
import socket
import time

# ------------------------------------------------------------ DER（ASN.1）編碼


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _len(len(body)) + body


def _int(v: int) -> bytes:
    b = v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big")      # 最高位元為 1 時補 0x00（正數）
    return _tlv(0x02, b)


def _seq(*items: bytes) -> bytes:
    return _tlv(0x30, b"".join(items))


def _set(*items: bytes) -> bytes:
    return _tlv(0x31, b"".join(items))


def _oid(dotted: str) -> bytes:
    parts = [int(x) for x in dotted.split(".")]
    body = bytes([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        enc = [p & 0x7F]
        p >>= 7
        while p:
            enc.append(0x80 | (p & 0x7F))
            p >>= 7
        body += bytes(reversed(enc))
    return _tlv(0x06, body)


def _null() -> bytes:
    return b"\x05\x00"


def _utf8(s: str) -> bytes:
    return _tlv(0x0C, s.encode("utf-8"))


def _bitstr(b: bytes) -> bytes:
    return _tlv(0x03, b"\x00" + b)


def _octet(b: bytes) -> bytes:
    return _tlv(0x04, b)


def _time(ts: float) -> bytes:
    t = time.gmtime(ts)
    if t.tm_year < 2050:
        return _tlv(0x17, time.strftime("%y%m%d%H%M%SZ", t).encode())        # UTCTime
    return _tlv(0x18, time.strftime("%Y%m%d%H%M%SZ", t).encode())            # GeneralizedTime


def _ctx(n: int, body: bytes, constructed: bool = True) -> bytes:
    return _tlv((0xA0 if constructed else 0x80) | n, body)


# ------------------------------------------------------------ RSA
_SMALL = [p for p in range(3, 2000, 2) if all(p % d for d in range(3, int(p ** 0.5) + 1, 2))]


def _probable_prime(n: int, rounds: int = 40) -> bool:
    if n < 2:
        return False
    for p in _SMALL:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, e: int) -> int:
    while True:
        c = secrets.randbits(bits) | (3 << (bits - 2)) | 1        # 最高兩位設 1：乘積剛好 2×bits 位元
        if (c - 1) % e and _probable_prime(c):
            return c


def rsa_key(bits: int = 2048, e: int = 65537) -> dict:
    while True:
        p, q = _prime(bits // 2, e), _prime(bits // 2, e)
        if p == q:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        phi = (p - 1) * (q - 1)
        d = pow(e, -1, phi)
        return {"n": n, "e": e, "d": d, "p": p, "q": q, "dp": d % (p - 1), "dq": d % (q - 1), "qi": pow(q, -1, p)}


def _sign(key: dict, data: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5 + SHA-256。"""
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(data).digest()
    k = (key["n"].bit_length() + 7) // 8
    em = b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3) + b"\x00" + digest_info
    m = int.from_bytes(em, "big")
    # 中國餘數定理加速
    s1, s2 = pow(m, key["dp"], key["p"]), pow(m, key["dq"], key["q"])
    h = (key["qi"] * (s1 - s2)) % key["p"]
    s = s2 + h * key["q"]
    return s.to_bytes(k, "big")


def _pem(label: str, der: bytes) -> str:
    b = base64.b64encode(der).decode()
    return f"-----BEGIN {label}-----\n" + "\n".join(b[i:i + 64] for i in range(0, len(b), 64)) + f"\n-----END {label}-----\n"


# ------------------------------------------------------------ 憑證
SHA256_RSA = _seq(_oid("1.2.840.113549.1.1.11"), _null())


def local_names() -> tuple[list[str], list[str]]:
    """這台電腦的名稱與 IP（盡量找齊；憑證要包含使用者會用來連線的名稱或 IP）。"""
    names, ips = ["localhost"], ["127.0.0.1"]
    host = socket.gethostname()
    for n in (host, socket.getfqdn()):
        if n and n not in names and "in-addr.arpa" not in n and not n.replace(".", "").isdigit():
            names.append(n)
    try:
        for info in socket.getaddrinfo(host, None):
            ip = info[4][0]
            if ":" not in ip and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    try:                                                    # 對外的那張網卡（不會真的送封包）
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip not in ips:
            ips.append(ip)
    except OSError:
        pass
    return names, ips


def self_signed(names: list[str], ips: list[str], days: int = 825, bits: int = 2048, org: str = "DTM",
                now: float | None = None) -> tuple[str, str, dict]:
    """回傳 (憑證 PEM, 私鑰 PEM, 資訊)。"""
    now = now or time.time()
    key = rsa_key(bits)
    cn = (names[0] if names and names[0] != "localhost" else (names[1] if len(names) > 1 else "DTM")) or "DTM"
    name = _seq(_set(_seq(_oid("2.5.4.10"), _utf8(org))), _set(_seq(_oid("2.5.4.3"), _utf8(cn[:64]))))
    spki = _seq(_seq(_oid("1.2.840.113549.1.1.1"), _null()), _bitstr(_seq(_int(key["n"]), _int(key["e"]))))
    gen = [_tlv(0x82, n.encode("ascii", "ignore")) for n in names if n]
    for ip in ips:
        try:
            gen.append(_tlv(0x87, ipaddress.ip_address(ip).packed))
        except ValueError:
            continue
    exts = _seq(
        _seq(_oid("2.5.29.17"), _octet(_seq(*gen))),                                      # subjectAltName
        _seq(_oid("2.5.29.19"), _octet(_seq(b"\x01\x01\xff"))),                          # basicConstraints CA:TRUE（可匯入成信任的根憑證）
        _seq(_oid("2.5.29.15"), b"\x01\x01\xff", _octet(_tlv(0x03, b"\x02\xa4"))),       # keyUsage: digitalSignature, keyEncipherment, keyCertSign
        _seq(_oid("2.5.29.37"), _octet(_seq(_oid("1.3.6.1.5.5.7.3.1")))),                # extKeyUsage: serverAuth
    )
    serial = secrets.randbits(63) | 1
    tbs = _seq(_ctx(0, _int(2)), _int(serial), SHA256_RSA, name, _seq(_time(now - 300), _time(now + days * 86400)),
               name, spki, _ctx(3, exts))
    cert = _seq(tbs, SHA256_RSA, _bitstr(_sign(key, tbs)))
    priv = _seq(_int(0), *[_int(key[k]) for k in ("n", "e", "d", "p", "q", "dp", "dq", "qi")])
    info = {"subject": cn, "names": names, "ips": ips, "not_after": now + days * 86400,
            "sha256": ":".join(f"{b:02X}" for b in hashlib.sha256(cert).digest())}
    return _pem("CERTIFICATE", cert), _pem("RSA PRIVATE KEY", priv), info


def write(folder: str, names: list[str], ips: list[str], days: int = 825) -> dict:
    """在 folder 寫入 dtm-cert.pem 與 dtm-key.pem（金鑰檔權限 600）；回傳路徑與憑證資訊。"""
    os.makedirs(folder, exist_ok=True)
    cert, key, info = self_signed(names, ips, days)
    cp, kp = os.path.join(folder, "dtm-cert.pem"), os.path.join(folder, "dtm-key.pem")
    with open(cp, "w", encoding="ascii") as f:
        f.write(cert)
    fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(key)
    return dict(info, cert=cp, key=kp)


def describe(path: str) -> dict:
    """讀現有憑證的基本資訊（主旨、到期日、指紋），給設定頁顯示。只解析需要的欄位。"""
    with open(path, encoding="ascii", errors="ignore") as f:
        pem = f.read()
    b64 = "".join(ln for ln in pem.split("-----BEGIN CERTIFICATE-----", 1)[1].split("-----END")[0].split())
    der = base64.b64decode(b64)
    out = {"sha256": ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest()), "not_after": None, "subject": ""}

    def read(buf, i):
        tag = buf[i]
        ln = buf[i + 1]
        j = i + 2
        if ln & 0x80:
            k = ln & 0x7F
            ln = int.from_bytes(buf[j:j + k], "big")
            j += k
        return tag, buf[j:j + ln], j + ln

    try:
        _t, cert, _ = read(der, 0)
        _t, tbs, _ = read(cert, 0)
        i, fields = 0, []
        while i < len(tbs):
            tag, body, i = read(tbs, i)
            fields.append((tag, body))
        if fields and fields[0][0] == 0xA0:
            fields = fields[1:]
        validity = fields[3][1]
        _t, nb, j = read(validity, 0)
        tag, na, _ = read(validity, j)
        s = na.decode()
        fmt = "%y%m%d%H%M%SZ" if tag == 0x17 else "%Y%m%d%H%M%SZ"
        import calendar
        out["not_after"] = calendar.timegm(time.strptime(s, fmt))
        subj = fields[4][1]
        k = 0
        while k < len(subj):
            _t, rdn, k = read(subj, k)
            _t, atv, _ = read(rdn, 0)
            _t, oid, m = read(atv, 0)
            _t, val, _ = read(atv, m)
            if oid == bytes.fromhex("550403"):
                out["subject"] = val.decode("utf-8", "replace")
    except (IndexError, ValueError):
        pass
    return out

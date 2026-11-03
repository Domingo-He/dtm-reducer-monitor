"""資料庫備份與還原。

- 每天自動備份一次（預設凌晨 3 點；電腦那時沒開機，開機後超過 26 小時沒備份就立刻補做）。
- 用 SQLite 的線上備份功能複製，DTM 不用停；複製完先做完整性檢查，通過才保留。
- 備份不含原始樣本（只保留 7 天、量大）；每分鐘統計、診斷特徵、事件、元件壽命、警報全部都在。
- 存成 zip（Windows 檔案總管可以直接開），保留最近 backup_keep 份，舊的自動刪除。
- 第二份（2.1）：backup_mirror_dir 設網路資料夾時，每份備份再複製一份過去，比對 SHA-256 才算成功；
  網路斷線時本機備份照做，下次自動補複製缺的份數。
- 還原：python -m dtm restore（還原精靈，見 restore.py；還原前會先把目前的資料庫另存一份）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime

from .i18n import _

log = logging.getLogger("dtm.backup")
NAME_RE = re.compile(r"^(?P<stem>.+)_(?P<ts>\d{8}_\d{4})\.zip$")


def db_path(cfg) -> str:
    return os.path.abspath(cfg["storage"]["db"])


def backup_dir(cfg) -> str | None:
    d = str(cfg["storage"].get("backup_dir") or "").strip()
    if not d:
        return None
    if not os.path.isabs(d):
        d = os.path.join(os.path.dirname(db_path(cfg)), d)
    return d


def mirror_dir(cfg) -> str | None:
    d = str(cfg["storage"].get("backup_mirror_dir") or "").strip()
    if not d:
        return None
    if not os.path.isabs(d) and not d.startswith(("\\\\", "//")):
        d = os.path.join(os.path.dirname(db_path(cfg)), d)
    return d


def list_backups(cfg, where: str = "local") -> list[dict]:
    """新到舊。where = local（backup_dir）或 mirror（backup_mirror_dir）。"""
    d = mirror_dir(cfg) if where == "mirror" else backup_dir(cfg)
    stem = os.path.splitext(os.path.basename(db_path(cfg)))[0]
    out = []
    if d and os.path.isdir(d):
        for f in os.listdir(d):
            m = NAME_RE.match(f)
            if m and m["stem"] == stem:
                p = os.path.join(d, f)
                out.append({"path": p, "name": f, "ts": datetime.strptime(m["ts"], "%Y%m%d_%H%M").timestamp(),
                            "size": os.path.getsize(p), "where": where})
    return sorted(out, key=lambda b: b["name"], reverse=True)


def last_backup_ts(cfg) -> float | None:
    b = list_backups(cfg)
    return b[0]["ts"] if b else None


def due(cfg, now: float | None = None) -> bool:
    """今天已過備份時間且今天還沒備份，或距上次超過 26 小時。"""
    if not backup_dir(cfg) or not os.path.exists(db_path(cfg)):
        return False
    now = now or time.time()
    last = last_backup_ts(cfg)
    if last is None:
        return True
    if now - last > 26 * 3600:
        return True
    n, lt = datetime.fromtimestamp(now), datetime.fromtimestamp(last)
    hour = int(cfg["storage"].get("backup_hour", 3))
    return n.date() > lt.date() and n.hour >= hour


def _copy_db(src_path: str, dst_path: str, keep_raw: bool):
    src = sqlite3.connect(src_path, timeout=30)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
        if not keep_raw:                     # 原始樣本不備份；診斷段與警報前後保留的片段照樣備份
            has_keep = dst.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_keep'").fetchone()
            if has_keep:
                from .store import outside_keep
                cond, p = outside_keep(dst)
                dst.execute(f"DELETE FROM samples WHERE {cond}", p)
            else:
                dst.execute("DELETE FROM samples")
            dst.commit()
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.execute("VACUUM")
        res = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if res != "ok":
            raise RuntimeError(_("備份完整性檢查失敗：{res}", res=res))
    finally:
        dst.close()
        src.close()


def backup_now(cfg, now: float | None = None, keep_raw: bool = False) -> str:
    """做一份備份，回傳 zip 路徑；並依 backup_keep 刪掉舊的。"""
    d = backup_dir(cfg)
    if not d:
        raise RuntimeError(_("設定檔 [storage] backup_dir 是空的，沒有啟用備份"))
    os.makedirs(d, exist_ok=True)
    src = db_path(cfg)
    if not os.path.exists(src):
        raise FileNotFoundError(_("找不到資料庫：{path}", path=src))
    stem = os.path.splitext(os.path.basename(src))[0]
    stamp = datetime.fromtimestamp(now or time.time()).strftime("%Y%m%d_%H%M")
    final = os.path.join(d, f"{stem}_{stamp}.zip")
    with tempfile.TemporaryDirectory(dir=d) as tmp:
        copy = os.path.join(tmp, os.path.basename(src))
        _copy_db(src, copy, keep_raw)
        part = final + ".part"
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.write(copy, os.path.basename(src))
        with zipfile.ZipFile(part) as z:
            bad = z.testzip()
        if bad:
            raise RuntimeError(_("備份壓縮檔檢查失敗：{name}", name=bad))
        os.replace(part, final)
    prune(cfg)
    log.info(_("資料庫已備份：%s（%.1f MB）"), final, os.path.getsize(final) / 1e6)
    return final


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def mirror_copy(cfg, src: str) -> str:
    """把一份備份複製到網路資料夾；先寫 .part，比對 SHA-256 一致才改成正式檔名。回傳目的地路徑。"""
    d = mirror_dir(cfg)
    if not d:
        raise RuntimeError(_("沒有設定 [storage] backup_mirror_dir"))
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, os.path.basename(src))
    part = dst + ".part"
    shutil.copyfile(src, part)
    if sha256(part) != sha256(src):
        try:
            os.remove(part)
        except OSError:
            pass
        raise RuntimeError(_("複製到網路資料夾後內容不一致（SHA-256 不同）：{path}", path=dst))
    os.replace(part, dst)
    return dst


def mirror_sync(cfg) -> dict:
    """本機有、網路資料夾沒有的備份補複製過去，再刪掉網路資料夾超過保留份數的舊檔。"""
    if not mirror_dir(cfg):
        return {"copied": [], "pruned": []}
    have = {b["name"] for b in list_backups(cfg, "mirror")}
    copied = []
    for b in reversed(list_backups(cfg)):                  # 舊到新，最新的最後複製
        if b["name"] not in have:
            copied.append(mirror_copy(cfg, b["path"]))
    return {"copied": copied, "pruned": prune(cfg, "mirror")}


def prune(cfg, where: str = "local") -> list[str]:
    keep = max(1, int(cfg["storage"].get("backup_keep", 30)))
    gone = []
    for b in list_backups(cfg, where)[keep:]:
        try:
            os.remove(b["path"])
            gone.append(b["path"])
        except OSError as e:
            log.warning(_("無法刪除舊備份 %s：%s"), b["path"], e)
    return gone


def restore(cfg, src: str) -> str:
    """把備份（zip 或 .sqlite3）還原成目前的資料庫；回傳還原前另存的檔名。DTM 必須先關閉。"""
    dst = db_path(cfg)
    if not os.path.exists(src):
        raise FileNotFoundError(_("找不到備份檔：{path}", path=src))
    with tempfile.TemporaryDirectory() as tmp:
        if zipfile.is_zipfile(src):
            with zipfile.ZipFile(src) as z:
                names = [n for n in z.namelist() if n.endswith((".sqlite3", ".db", ".sqlite"))]
                if not names:
                    raise ValueError(_("備份檔裡沒有資料庫"))
                z.extract(names[0], tmp)
                cand = os.path.join(tmp, names[0])
        else:
            cand = src
        c = sqlite3.connect(cand)
        try:
            ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            c.close()
        if not ok or not {"features", "events", "components"} <= tables:
            raise ValueError(_("這個檔案不是完整的 DTM 資料庫，沒有還原"))
        saved = ""
        if os.path.exists(dst):
            saved = os.path.join(os.path.dirname(dst),
                                 f"{os.path.splitext(os.path.basename(dst))[0]}_before_restore_{datetime.now():%Y%m%d_%H%M%S}.sqlite3")
            _copy_db(dst, saved, keep_raw=True)
        for ext in ("-wal", "-shm"):
            if os.path.exists(dst + ext):
                os.remove(dst + ext)
        shutil.copyfile(cand, dst + ".restoring")
        os.replace(dst + ".restoring", dst)
    return saved


class BackupScheduler:
    """背景執行緒：啟動 1 分鐘後檢查一次，之後每 10 分鐘看一次要不要備份。info 給儀表板顯示。"""

    def __init__(self, cfg, stop_event):
        self.cfg = cfg
        self.stop = stop_event
        self.info = {"enabled": bool(backup_dir(cfg)), "dir": backup_dir(cfg) or "", "last_ts": None, "count": 0,
                     "error": "", "mirror": mirror_dir(cfg) or "", "mirror_last_ts": None, "mirror_count": 0,
                     "mirror_error": ""}
        self._mirrored: set[str] = set()
        self._mirror_next = 0.0
        self._refresh()

    def _refresh(self):
        b = list_backups(self.cfg)
        self.info.update(last_ts=b[0]["ts"] if b else None, count=len(b))

    def mirror(self, now: float | None = None):
        """網路資料夾連不上不影響本機備份；錯誤顯示在儀表板，30 分鐘後再試。
        最新一份已經複製過就不再去讀網路資料夾（避免網路斷線時每次都卡住等逾時）。"""
        if not self.info["mirror"]:
            return
        now = now or time.time()
        local = list_backups(self.cfg)
        if not local or local[0]["name"] in self._mirrored or now < self._mirror_next:
            return
        try:
            r = mirror_sync(self.cfg)
            m = list_backups(self.cfg, "mirror")
            self.info.update(mirror_error="", mirror_last_ts=m[0]["ts"] if m else None, mirror_count=len(m))
            self._mirrored = {b["name"] for b in local}
            for p in r["copied"]:
                log.info(_("備份已複製到網路資料夾：%s"), p)
        except Exception as e:
            self.info["mirror_error"] = str(e)
            self._mirror_next = now + 1800
            log.warning(_("備份複製到網路資料夾失敗：%s（本機備份不受影響，下次自動補複製）"), e)

    def run(self):
        if not self.info["enabled"]:
            return
        wait = 60.0
        while not self.stop.wait(wait):
            wait = 600.0
            try:
                if due(self.cfg):
                    backup_now(self.cfg)
                self.info["error"] = ""
            except Exception as e:
                self.info["error"] = str(e)
                log.error(_("自動備份失敗：%s"), e)
                wait = 1800.0
            self.mirror()
            self._refresh()

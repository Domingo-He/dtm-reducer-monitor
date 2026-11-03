"""測試用：tempfile.mkdtemp 建的暫存資料夾，在這次測試結束時一起刪掉（避免越跑越多、塞滿磁碟）；
測試期間不連網際網路（匯率改成「抓不到」，需要匯率的測試自己帶假的下載函式）。

每個測試檔都 import 這個模組；重複 import 只會生效一次。
"""
import atexit
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dtm import fx as _fx  # noqa: E402


def _offline(*a, **k):
    raise OSError("tests do not use the internet")


_fx._http_get = _offline

if not getattr(tempfile.mkdtemp, "_dtm_tracked", False):
    _made: list[str] = []
    _orig = tempfile.mkdtemp

    def _mkdtemp(*a, **k):
        d = _orig(*a, **k)
        _made.append(d)
        return d

    _mkdtemp._dtm_tracked = True
    tempfile.mkdtemp = _mkdtemp

    @atexit.register
    def _remove():
        for d in _made:
            shutil.rmtree(d, ignore_errors=True)

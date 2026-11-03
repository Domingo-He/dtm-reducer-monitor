"""共用資料結構。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class Sample:
    ts: float                       # epoch 秒
    dt: dict[int, float | None]     # 軸號 -> 干擾扭矩原始值
    seg: int = 0                    # 診斷段代碼，0 = 生產中
    run: int = 0                    # 診斷程式執行旗標
    hb: float | None = None         # 心跳
    ovr: float | None = None        # 速度倍率 %
    temp: float | None = None       # 外殼溫度 °C（選用）
    ext: dict | None = None         # 感測器閘道讀到的振動與溫度（選用），鍵如 "2.radial.rms"、"2.temp"


@dataclass
class SegStat:
    code: int
    axis: int
    pct: int
    direction: int      # 1 = A->B，2 = B->A
    t0: float
    t1: float
    n: int
    mean: float | None
    std: float | None
    peak: float | None
    temp: float | None
    valid: bool
    reason: str = ""


@dataclass
class RunFeatures:
    ts: float
    axis: int
    F: float | None          # 參考速度下的摩擦 (τ+ − τ−)/2
    Fc: float | None         # 庫倫摩擦（截距）
    Fv: float | None         # 黏性摩擦（每 % 速度）
    G: float | None          # 重力型偏差 (τ+ + τ−)/2 平均
    ripple: float | None     # 等速段標準差平均
    peak: float | None       # 等速段最大絕對值
    temp: float | None
    valid: bool
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


# R[110] 代碼範圍：
#   1000–8999  診斷段：軸 × 1000 + 速度% × 10 + 方向（軸 1–7，7 = 行走軸 E1）
#   9000–9899  生產動作代碼：在生產程式固定動作前寫入、動作完寫回 0（例如 9001 有工件搬運、9002 空手回程）
#   9900–9999  振動取樣窗：99 + 軸號（例如 9902 = J2），讓電池型無線感測器至少有一筆落在等速往返段
MOTION_MIN, MOTION_MAX = 9000, 9899
VIBWIN_MIN, VIBWIN_MAX = 9900, 9999


def _int(code) -> int | None:
    try:
        return int(round(code))
    except (TypeError, ValueError):
        return None


def decode_seg(code: int) -> tuple[int, int, int] | None:
    """診斷段代碼 = 軸 × 1000 + 速度% × 10 + 方向；9000 以上不是診斷段。"""
    code = _int(code)
    if code is None or code <= 0 or code >= MOTION_MIN:
        return None
    axis, rest = divmod(code, 1000)
    pct, direction = divmod(rest, 10)
    if not (1 <= axis <= 8 and 1 <= pct <= 99 and direction in (1, 2)):
        return None
    return axis, pct, direction


def is_motion(code) -> bool:
    c = _int(code)
    return c is not None and MOTION_MIN <= c <= MOTION_MAX


def vib_window_axis(code) -> int | None:
    """振動取樣窗代碼 99AA → 軸號 AA；不是取樣窗回傳 None。"""
    c = _int(code)
    if c is None or not VIBWIN_MIN <= c <= VIBWIN_MAX:
        return None
    ax = c - VIBWIN_MIN
    return ax if 1 <= ax <= 8 else None


def encode_seg(axis: int, pct: int, direction: int) -> int:
    return axis * 1000 + pct * 10 + direction

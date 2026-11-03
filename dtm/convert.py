"""干擾扭矩原始值 → 減速機輸出側扭矩 (Nm)。

Fanuc 的 $CUR_DIS_TRQ 是控制器內部單位，沒有公開換算。本系統用「加重法」校正：
同一姿態、同一診斷程式，不加重與加上已知質量各跑一次，
重力型偏差 G 的差值 ΔG 對應已知的重力矩 m·g·d：

    k = m · g · d / |ΔG|      [Nm / 原始值]

G 取自正反轉平均，摩擦已互相抵消，所以校正不受摩擦影響。
未校正的軸以原始值顯示，健康判定使用比值，不受單位影響。
"""
from __future__ import annotations

from .config import axis_cfg
from .i18n import N_, _

G0 = 9.80665


class Converter:
    def __init__(self, cfg: dict, store=None):
        self.cfg = cfg
        self.store = store
        self.reload()

    def reload(self):
        self.ax: dict[int, dict] = {}
        for ax in range(1, 8):
            a = axis_cfg(self.cfg, ax)
            c = self.store.calib(ax) if self.store else None
            self.ax[ax] = {
                "name": a["name"],
                "k": float(c["k"]) if c else float(a["k_nm_per_raw"]),
                "zero": float(c["zero"]) if c else float(a["zero_offset"]),
                "rated": float(a["rated_nm"]),
                "source": N_("資料庫校正") if c else (N_("設定檔") if float(a["k_nm_per_raw"]) > 0 else N_("未校正")),
            }

    def calibrated(self, ax: int) -> bool:
        return self.ax[ax]["k"] > 0

    def unit(self, ax: int) -> str:
        return "Nm" if self.calibrated(ax) else _("原始值")

    def value(self, ax: int, raw: float | None) -> float | None:
        """顯示值：已校正為 Nm，未校正為原始值。"""
        if raw is None:
            return None
        a = self.ax[ax]
        return (raw - a["zero"]) * a["k"] if a["k"] > 0 else raw

    def nm(self, ax: int, raw: float | None) -> float | None:
        return self.value(ax, raw) if self.calibrated(ax) else None

    def pct_rated(self, ax: int, nm: float | None) -> float | None:
        r = self.ax[ax]["rated"]
        return None if nm is None or r <= 0 else nm / r * 100.0

    def feature(self, ax: int, name: str, v: float | None) -> float | None:
        """特徵值換算：F、Fc、Fv、ripple、peak 是差值或幅度，只乘 k；G 是準位，先扣零點。"""
        if v is None or not self.calibrated(ax):
            return v
        a = self.ax[ax]
        return (v - a["zero"]) * a["k"] if name == "G" else v * a["k"]


def calibrate_k(g_without: float, g_with: float, mass_kg: float, horiz_m: float) -> dict:
    dG = g_with - g_without
    if abs(dG) < 1e-9:
        raise ValueError(_("加重前後 G 沒有差異，請確認重物確實加上、或加大質量／力臂"))
    dtau = mass_kg * G0 * horiz_m
    return {"k": dtau / abs(dG), "dG": dG, "dtau_nm": dtau}

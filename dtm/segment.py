"""診斷程式切段與特徵擷取。

R[111]=1 期間視為一次診斷；每個非零的 R[110] 代碼是一段單軸等速運動。
每段去掉頭尾 trim_s 秒（加減速），剩下的等速部分算平均、標準差、峰值。
同一速度的正反轉兩段組合：
    F = (τ+ − τ−) / 2   摩擦（方向相反時變號）
    G = (τ+ + τ−) / 2   重力型偏差（不隨方向變號）
多個速度再做 F = Fc + Fv · 速度% 的線性回歸。
"""
from __future__ import annotations

import math
import statistics

from .config import axis_cfg
from .i18n import _
from .model import RunFeatures, Sample, SegStat, decode_seg, is_motion, vib_window_axis


def ols(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """回傳 (斜率, 截距)。"""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return slope, my - slope * mx


class Segmenter:
    def __init__(self, cfg: dict):
        d = cfg["diag"]
        self.cfg = cfg
        self.trim_s = float(d["trim_s"])
        self.min_const_s = float(d["min_const_s"])
        self.ref_pct = float(d["ref_pct"])
        self.run_gap_s = float(d["run_gap_s"])
        self.max_run_s = float(d["max_run_s"])
        self.invalid_abs = float(cfg["realtime"]["invalid_abs"])
        self.use_flag = bool(int(cfg["registers"].get("run") or 0))
        self.last_windows: list[tuple[int, float, float, str]] = []
        self._reset()

    def _reset(self):
        self.in_run = False
        self.run_t0 = 0.0
        self.last_seg_ts = 0.0
        self.code = 0
        self.buf: list[Sample] = []
        self.segs: list[SegStat] = []
        self.windows: list[tuple[int, float, float, str]] = []   # (軸, 開始, 結束, "const"／"vib")：給感測器資料切片

    @property
    def state(self) -> dict:
        info = decode_seg(self.code)
        return {"in_run": self.in_run, "code": self.code,
                "axis": info[0] if info else None, "pct": info[1] if info else None,
                "dir": info[2] if info else None, "segments": len(self.segs)}

    # ------------------------------------------------------------ 主流程
    def feed(self, s: Sample) -> list[RunFeatures] | None:
        """餵一筆樣本；診斷結束時回傳該次各軸特徵，否則回傳 None。"""
        diag_code = bool(s.seg) and not is_motion(s.seg)          # 9000–9899 是生產動作代碼，不是診斷
        active = bool(s.run) if self.use_flag else (diag_code or
                                                    (self.in_run and s.ts - self.last_seg_ts < self.run_gap_s))
        if active and not self.in_run:
            self._reset()
            self.in_run = True
            self.run_t0 = s.ts
        if not self.in_run:
            return None
        if diag_code:
            self.last_seg_ts = s.ts
        if s.seg != self.code:
            if self.code:
                self._close_seg()
            self.code = s.seg
            self.buf = []
        if self.code:
            self.buf.append(s)
        if not active or s.ts - self.run_t0 > self.max_run_s:
            if self.code:
                self._close_seg()
            feats = self._finish(s.ts)
            self.last_windows = list(self.windows)
            self._reset()
            return feats
        return None

    # ------------------------------------------------------------ 單段
    def _close_seg(self):
        info = decode_seg(self.code)
        buf, self.buf, code, self.code = self.buf, [], self.code, 0
        vax = vib_window_axis(code)
        if vax and buf:
            self.windows.append((vax, buf[0].ts, buf[-1].ts, "vib"))
            return
        if not info or not buf:
            return
        ax, pct, d = info
        t0, t1 = buf[0].ts, buf[-1].ts
        keep = [x for x in buf if t0 + self.trim_s <= x.ts <= t1 - self.trim_s]
        vals = [x.dt.get(ax) for x in keep]
        vals = [v for v in vals if v is not None and abs(v) < self.invalid_abs]
        temps = [x.temp for x in keep if x.temp is not None]
        reason = ""
        dur = (keep[-1].ts - keep[0].ts) if len(keep) > 1 else 0.0
        if dur < self.min_const_s or len(vals) < 3:
            reason = _("等速段太短（{dur:.1f}s）", dur=dur)
        elif any(x.ovr is not None and x.ovr < 99.5 for x in keep):
            reason = _("速度倍率不是 100%")
        ok = not reason
        if ok:
            self.windows.append((ax, keep[0].ts, keep[-1].ts, "const"))
        self.segs.append(SegStat(
            code=code, axis=ax, pct=pct, direction=d, t0=t0, t1=t1, n=len(vals),
            mean=statistics.fmean(vals) if vals else None,
            std=statistics.pstdev(vals) if len(vals) > 1 else None,
            peak=max(abs(v) for v in vals) if vals else None,
            temp=statistics.fmean(temps) if temps else None,
            valid=ok, reason=reason))

    # ------------------------------------------------------------ 整次診斷
    def _finish(self, ts: float) -> list[RunFeatures]:
        out = []
        for ax in sorted({s.axis for s in self.segs}):
            out.append(features_for_axis(ax, [s for s in self.segs if s.axis == ax],
                                         self.ref_pct, int(axis_cfg(self.cfg, ax)["dir_sign"]), ts))
        return out


def features_for_axis(ax: int, segs: list[SegStat], ref_pct: float, dir_sign: int, ts: float) -> RunFeatures:
    by_pct: dict[int, dict[int, SegStat]] = {}
    for s in segs:
        if s.valid:
            by_pct.setdefault(s.pct, {})[s.direction] = s   # 同速同向重複時取最後一段
    pts = []
    for pct, dd in sorted(by_pct.items()):
        if 1 in dd and 2 in dd:
            fwd, back = dd[1].mean, dd[2].mean
            F = dir_sign * (fwd - back) / 2
            G = (fwd + back) / 2
            pts.append((pct, F, G, (dd[1].std + dd[2].std) / 2, max(dd[1].peak, dd[2].peak)))
    temps = [s.temp for s in segs if s.temp is not None]
    temp = round(statistics.fmean(temps), 2) if temps else None
    detail = {"speeds": [{"pct": p, "F": F, "G": G, "ripple": r} for p, F, G, r, _ in pts],
              "invalid": [{"code": s.code, "reason": s.reason} for s in segs if not s.valid]}
    if not pts:
        return RunFeatures(ts=ts, axis=ax, F=None, Fc=None, Fv=None, G=None, ripple=None,
                           peak=None, temp=temp, valid=False, detail=detail)
    pcts = [p[0] for p in pts]
    Fs = [p[1] for p in pts]
    fit = ols(pcts, Fs)
    if fit:
        Fv, Fc = fit
    else:
        Fv, Fc = None, None
    if ref_pct in pcts:
        F_ref = Fs[pcts.index(ref_pct)]
    elif fit:
        F_ref = Fc + Fv * ref_pct
    else:
        F_ref = statistics.fmean(Fs)
    return RunFeatures(
        ts=ts, axis=ax, F=F_ref, Fc=Fc, Fv=Fv,
        G=statistics.fmean(p[2] for p in pts),
        ripple=statistics.fmean(p[3] for p in pts),
        peak=max(p[4] for p in pts),
        temp=temp, valid=all(math.isfinite(x) for x in Fs), detail=detail)

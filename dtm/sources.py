"""資料來源：模擬器、Fanuc 網頁伺服器（NUMREG.VA）、Modbus TCP、CSV 重播。

每個來源都提供 read() -> Sample | None。None 代表這次沒讀到（連線中斷等），
呼叫端會記錄錯誤並重試。
"""
from __future__ import annotations

import csv
import math
import random
import re
import struct
import time
import urllib.request

from .config import enabled_axes
from .model import Sample, encode_seg
from .i18n import N_, _


# ---------------------------------------------------------------- 模擬器
SIM_AXES = {
    # 真實值單位 Nm（減速機輸出側）；k_true = Nm / 原始值
    2: dict(Fc=60.0, Fv=2.0, G=8.0, ripple=4.0, k_true=2.0),
    3: dict(Fc=40.0, Fv=1.5, G=-5.0, ripple=3.0, k_true=1.5),
}
SIM_AXES[4] = dict(Fc=20.0, Fv=0.8, G=1.0, ripple=1.5, k_true=1.0)   # J4 齒輪箱
SIM_AXES[7] = dict(Fc=26.0, Fv=1.2, G=0.0, ripple=2.2, k_true=1.0)   # E1 行走軸：直線運動，水平軌道重力項 ≈ 0
SIM_DEFAULT_AXIS = dict(Fc=30.0, Fv=1.0, G=0.0, ripple=2.0, k_true=1.0)
SIM_GREASE_DAY = 120  # 模擬歷史中 J2 在第 120 天換脂
SIM_MOTOR_DAY = 100   # J4 馬達軸承從第 100 天開始劣化
SIM_GREASE_MASS = {2: 2500.0, 3: 1800.0, 4: 600.0, 7: 900.0}
SIM_VIB_BASE = {"radial": dict(rms=0.30, env=0.10, mesh=0.15, motor1x=0.05, kurt=3.0),
                "axial": dict(rms=0.20, env=0.07, mesh=0.08, motor1x=0.04, kurt=3.0)}


class SimRobot:
    """產生具物理意義的干擾扭矩：摩擦 + 重力型偏差 + 漣波 + 雜訊。

    情境：J2 油脂老化（黏性摩擦漸增，第 120 天換脂後回復）；
          J3 減速機從第 60 天開始磨耗並加速劣化。
    """

    def __init__(self, axes: list[int], seed: int = 7):
        self.axes = axes
        self.rng = random.Random(seed)

    def p(self, ax: int) -> dict:
        return SIM_AXES.get(ax, SIM_DEFAULT_AXIS)

    @staticmethod
    def temp(day: float) -> float:
        return 28.0 + 6.0 * math.sin(2 * math.pi * (day - 110) / 365.0)

    def state(self, ax: int, day: float, temp: float) -> tuple[float, float, float, float]:
        p = self.p(ax)
        visc = math.exp(-0.025 * (temp - 28.0))
        Fc, Fv, G, rip = p["Fc"], p["Fv"] * visc, p["G"], p["ripple"]
        if ax == 2:
            age = day + 20 if day < SIM_GREASE_DAY else day - SIM_GREASE_DAY
            Fv *= 1 + 0.6 * (age / 140.0) ** 2      # 油脂氧化：黏性摩擦加速上升
            Fc *= 1 + 0.0005 * age
        if ax == 3:
            x = max(0.0, day - 60) / 60.0
            Fc *= 1 + 0.25 * x + 0.15 * x ** 3
            rip *= 1 + 0.35 * x * x
        if ax == 4:
            y = max(0.0, day - SIM_MOTOR_DAY) / 80.0
            Fc *= 1 + 0.6 * y * y          # 馬達軸承劣化：摩擦上升，但不產生減速機鐵粉
            rip *= 1 + 0.3 * y * y
        return Fc, Fv, G, rip

    def vib(self, ax: int, day: float) -> dict[str, dict[str, float]]:
        """每日振動特徵（徑向、軸向）。"""
        out = {}
        x = max(0.0, day - 60) / 60.0
        y = max(0.0, day - SIM_MOTOR_DAY) / 80.0
        age = day + 20 if day < SIM_GREASE_DAY else day - SIM_GREASE_DAY
        for d, base in SIM_VIB_BASE.items():
            v = {k: b * (1 + self.rng.gauss(0, 0.05)) for k, b in base.items()}
            v["kurt"] = base["kurt"] + self.rng.gauss(0, 0.1)
            if ax == 2:
                v["env"] *= 1 + 0.4 * (age / 140.0) ** 2
            if ax == 3:
                if d == "radial":
                    v["env"] *= 1 + 1.2 * x * x
                    v["mesh"] *= 1 + 0.8 * x * x
                    v["rms"] *= 1 + 0.4 * x * x
                    v["kurt"] += 1.2 * x * x
                else:
                    v["rms"] *= 1 + 0.6 * x * x
                    v["env"] *= 1 + 0.8 * x * x
            if ax == 4:
                v["motor1x"] *= 1 + 3.0 * y * y
                if d == "radial":
                    v["rms"] *= 1 + 0.3 * y * y
                    v["env"] *= 1 + 0.4 * y * y
            out[d] = {k: round(val, 4) for k, val in v.items()}
        return out

    @staticmethod
    def wear_rate(ax: int, day: float) -> float:
        """減速機鐵粉生成速率 g/1000h。"""
        x = max(0.0, day - 60) / 60.0
        return {2: 0.08, 3: 0.08 * (1 + 4 * x * x), 4: 0.05}.get(ax, 0.06)

    def friction(self, ax: int, pct: float, day: float, temp: float) -> float:
        Fc, Fv, _, _ = self.state(ax, day, temp)
        return Fc + Fv * pct

    def diag_run(self, t0: float, day: float, poll_hz: float, speeds=(5, 10, 20),
                 const_s: float = 4.0, ramp_s: float = 0.5, gap_s: float = 0.6,
                 hb0: int = 0) -> list[Sample]:
        dt = 1.0 / poll_hz
        temp = self.temp(day) + self.rng.uniform(-1, 1)
        out: list[Sample] = []
        t = t0
        hb = hb0

        def emit(seg: int, run: int, vals: dict[int, float]):
            nonlocal t, hb
            hb += 1
            raw = {ax: v / self.p(ax)["k_true"] for ax, v in vals.items()}
            out.append(Sample(ts=t, dt=raw, seg=seg, run=run, hb=hb, ovr=100.0, temp=round(temp, 1)))
            t += dt

        def idle_vals():
            return {ax: self.rng.gauss(self.state(ax, day, temp)[2], 1.0) for ax in self.axes}

        for _i in range(int(1.0 / dt)):
            emit(0, 1, idle_vals())
        for ax in self.axes:
            Fc, Fv, G, rip = self.state(ax, day, temp)
            for pct in speeds:
                F = Fc + Fv * pct
                for d in (1, 2):
                    s = 1.0 if d == 1 else -1.0
                    code = encode_seg(ax, pct, d)
                    n_ramp = max(1, int(ramp_s / dt))
                    n_const = int(const_s / dt)
                    for i in range(n_ramp):          # 加速段：模型誤差造成的暫態
                        v = idle_vals()
                        v[ax] = s * F * (i + 1) / n_ramp + s * 0.6 * F + G
                        emit(code, 1, v)
                    for i in range(n_const):         # 等速段
                        v = idle_vals()
                        crank = 0.8 * rip * math.sin(2 * math.pi * 1.7 * i * dt)
                        v[ax] = s * F + G + s * crank + self.rng.gauss(0, rip * 0.6)
                        emit(code, 1, v)
                    for i in range(n_ramp):          # 減速段
                        v = idle_vals()
                        v[ax] = s * F * (n_ramp - i) / n_ramp - s * 0.5 * F + G
                        emit(code, 1, v)
                    for _i in range(max(1, int(gap_s / dt))):
                        emit(0, 1, idle_vals())
        for _i in range(int(1.0 / dt)):
            emit(0, 1, idle_vals())
        emit(0, 0, idle_vals())
        return out

    # 生產動作：9001 有工件搬運（3.0 秒）、9002 空手回程（2.4 秒）；每次路徑、速度都一樣
    MOTIONS = {9001: dict(T=3.0, payload=1.0, amp={1: 25, 2: 18, 3: 22, 4: 12, 5: 10, 6: 15, 7: 30}),
               9002: dict(T=2.4, payload=0.0, amp={1: -25, 2: -18, 3: -22, 4: -12, 5: -10, 6: -15, 7: -30})}

    def motion_torque(self, ax: int, code: int, frac: float, day: float, temp: float) -> float:
        """動作代碼 code 進行到 frac（0–1）時，ax 軸的干擾扭矩（Nm，減速機輸出側）。"""
        m = self.MOTIONS[code]
        Fc, Fv, G, rip = self.state(ax, day, temp)
        amp = m["amp"].get(ax, 15)
        vel = amp * math.sin(math.pi * frac)                     # 速度 %（正弦加減速）
        acc = amp * math.pi / m["T"] * math.cos(math.pi * frac)  # 加速度（任意單位）
        load = (1.0 + 0.35 * m["payload"]) if ax in (2, 3) else 1.0
        tau = math.copysign(Fc + Fv * abs(vel), vel) if abs(vel) > 0.2 else 0.0
        tau += (G * load) + 0.9 * acc * load
        return tau + self.rng.gauss(0, rip * 0.5)

    def motion_cycle(self, t0: float, day: float, poll_hz: float, code: int, hb0: int = 0,
                     temp: float | None = None) -> list[Sample]:
        """一個生產循環的一段動作（含前後各一筆代碼 0）。"""
        dt = 1.0 / poll_hz
        T = self.MOTIONS[code]["T"]
        temp = self.temp(day) if temp is None else temp
        out, hb, t = [], hb0, t0
        n = max(3, int(round(T / dt)))
        for i in range(n):
            hb += 1
            vals = {ax: self.motion_torque(ax, code, (i + 0.5) / n, day, temp) / self.p(ax)["k_true"]
                    for ax in self.axes}
            out.append(Sample(ts=t, dt=vals, seg=code, run=0, hb=hb, ovr=100.0, temp=round(temp, 1)))
            t += dt
        hb += 1
        out.append(Sample(ts=t, dt={ax: self.rng.gauss(self.state(ax, day, temp)[2], 1.0) / self.p(ax)["k_true"]
                                    for ax in self.axes}, seg=0, run=0, hb=hb, ovr=100.0, temp=round(temp, 1)))
        return out

    def production(self, t: float, day: float, spike: bool = False) -> dict[int, float]:
        temp = self.temp(day)
        vals = {}
        for i, ax in enumerate(self.axes):
            Fc, Fv, G, rip = self.state(ax, day, temp)
            vel = math.sin(2 * math.pi * t / (9.0 + 2 * i) + i)
            pct = abs(vel) * 25
            v = math.copysign(Fc + Fv * pct, vel) * min(1.0, abs(vel) * 4) + G
            v += self.rng.gauss(0, rip)
            if spike and ax == self.axes[-1]:
                v += math.copysign(900.0, vel or 1.0)
            vals[ax] = v / self.p(ax)["k_true"]
        return vals


class SimSource:
    def __init__(self, cfg: dict, realtime: bool = True):
        sc = cfg["source"]["sim"]
        self.cfg = cfg
        self.poll_hz = float(cfg["robot"]["poll_hz"])
        self.robot = SimRobot(enabled_axes(cfg), seed=int(sc["seed"]))
        self.day = float(sc["start_day"])
        self.realtime = realtime
        self._gen = self._run()
        self.name = N_("模擬器")      # 顯示時才翻譯（儀表板依使用者語言）

    def _run(self):
        sc = self.cfg["source"]["sim"]
        dt = 1.0 / self.poll_hz
        t = time.time()
        hb = 0
        spike_every = float(sc.get("spike_every_s") or 0)
        next_spike = t + spike_every if spike_every else float("inf")
        rng = random.Random(int(sc["seed"]) + 1)
        use_codes = bool(sc.get("motion_codes", True))
        while True:
            end = t + float(sc["diag_every_s"])
            next_motion = t + rng.uniform(2, 6)
            while t < end:
                if use_codes and t >= next_motion:            # 生產循環：搬運 9001 → 放料 → 空手回程 9002
                    for code in (9001, 9002):
                        for s in self.robot.motion_cycle(t, self.day, self.poll_hz, code, hb0=hb):
                            hb = int(s.hb)
                            yield s
                            t = s.ts + dt
                    next_motion = t + rng.uniform(2, 8)       # 等產品的時間不固定
                    continue
                hb += 1
                spike = next_spike <= t < next_spike + 3.0
                if t >= next_spike + 3.0:
                    next_spike += spike_every
                vals = self.robot.production(t, self.day, spike)
                yield Sample(ts=t, dt=vals, seg=0, run=0, hb=hb, ovr=100.0,
                             temp=round(self.robot.temp(self.day), 1))
                t += dt
            for s in self.robot.diag_run(t, self.day, self.poll_hz, const_s=float(sc["seg_const_s"]),
                                         ramp_s=float(sc["seg_ramp_s"]), hb0=hb):
                hb = int(s.hb)
                yield s
                t = s.ts + dt

    def read(self) -> Sample | None:
        s = next(self._gen)
        if self.realtime:
            wait = s.ts - time.time()
            if wait > 0:
                time.sleep(wait)
        return s

    def close(self):
        pass


# ---------------------------------------------------------------- Fanuc 網頁伺服器
_NUMREG_RE = re.compile(r"^\s*\[(\d+)\]\s*=\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)", re.M)


def parse_numreg(text: str) -> dict[int, float]:
    """解析 MD:NUMREG.VA，例如 '  [101] = 12.5  ''DT_J1'''。"""
    return {int(m.group(1)): float(m.group(2)) for m in _NUMREG_RE.finditer(text)}


def sample_from_regs(regs: dict[int, float], cfg: dict, ts: float) -> Sample:
    r = cfg["registers"]
    start = int(r["dt_start"])
    dt = {ax: regs.get(start + ax - 1) for ax in range(1, 8)}       # 第 7 個暫存器 = E1（沒有就是 None）

    def opt(key):
        n = int(r.get(key) or 0)
        return regs.get(n) if n else None

    seg = opt("seg")
    run = opt("run")
    return Sample(ts=ts, dt=dt, seg=int(seg or 0), run=int(run or 0),
                  hb=opt("heartbeat"), ovr=opt("override"), temp=opt("temp"))


class FanucHttpSource:
    """透過控制器內建網頁伺服器讀取 MD:NUMREG.VA（不需額外選配，但速率較低）。"""

    def __init__(self, cfg: dict):
        c = cfg["source"]["fanuc_http"]
        self.cfg = cfg
        self.url = f"http://{c['host']}{c['path']}"
        self.timeout = float(c["timeout_s"])
        self.period = 1.0 / float(cfg["robot"]["poll_hz"])
        self._next = time.time()
        self.name = f"Fanuc HTTP {c['host']}"

    def read(self) -> Sample | None:
        wait = self._next - time.time()
        if wait > 0:
            time.sleep(wait)
        self._next = max(self._next + self.period, time.time())
        with urllib.request.urlopen(self.url, timeout=self.timeout) as resp:
            text = resp.read().decode("latin-1", errors="replace")
        regs = parse_numreg(text)
        if not regs:
            raise RuntimeError(_("NUMREG.VA 內容無法解析"))
        return sample_from_regs(regs, self.cfg, time.time())

    def close(self):
        pass


# ---------------------------------------------------------------- Modbus TCP
def decode_words(words: list[int], typ: str, word_order: str = "big") -> float:
    if typ in ("int16", "uint16"):
        v = words[0]
        return float(v - 0x10000 if typ == "int16" and v >= 0x8000 else v)
    w = words[:2] if word_order == "big" else words[1::-1]
    raw = struct.pack(">HH", *w)
    if typ == "int32":
        return float(struct.unpack(">i", raw)[0])
    if typ == "uint32":
        return float(struct.unpack(">I", raw)[0])
    if typ == "float32":
        return float(struct.unpack(">f", raw)[0])
    raise ValueError(_("未知型別 {typ}", typ=typ))


class ModbusSource:
    """從 PLC 或閘道器讀保持暫存器。map 範例見 config.example.toml。"""

    FIELDS = ["dt1", "dt2", "dt3", "dt4", "dt5", "dt6", "dt7", "seg", "run", "hb", "ovr", "temp"]

    def __init__(self, cfg: dict):
        try:
            from pymodbus.client import ModbusTcpClient
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(_("需要安裝 pymodbus：pip install pymodbus")) from e
        c = cfg["source"]["modbus"]
        self.c = c
        self.client = ModbusTcpClient(c["host"], port=int(c["port"]))
        self.period = 1.0 / float(cfg["robot"]["poll_hz"])
        self._next = time.time()
        self.name = f"Modbus {c['host']}"

    def _read(self, addr: int, count: int) -> list[int]:
        unit = int(self.c["unit"])
        for kw in ("device_id", "slave", "unit"):
            try:
                rr = self.client.read_holding_registers(addr, count=count, **{kw: unit})
                break
            except TypeError:
                continue
        else:  # pragma: no cover
            raise RuntimeError(_("pymodbus 版本不相容"))
        if rr.isError():
            raise RuntimeError(_("Modbus 讀取錯誤 @ {addr}", addr=addr))
        return list(rr.registers)

    def read(self) -> Sample | None:
        wait = self._next - time.time()
        if wait > 0:
            time.sleep(wait)
        self._next = max(self._next + self.period, time.time())
        if not self.client.connected:
            self.client.connect()
        vals: dict[str, float | None] = {}
        for f in self.FIELDS:
            m = self.c["map"].get(f)
            if not m:
                vals[f] = None
                continue
            typ = m.get("type", "int16")
            words = self._read(int(m["addr"]), 1 if typ.endswith("16") else 2)
            vals[f] = decode_words(words, typ, self.c.get("word_order", "big")) * float(m.get("scale", 1.0))
        dt = {ax: vals[f"dt{ax}"] for ax in range(1, 8)}
        return Sample(ts=time.time(), dt=dt, seg=int(vals["seg"] or 0), run=int(vals["run"] or 0),
                      hb=vals["hb"], ovr=vals["ovr"], temp=vals["temp"])

    def close(self):
        self.client.close()


# ---------------------------------------------------------------- CSV 重播
CSV_COLUMNS = ["ts", "dt1", "dt2", "dt3", "dt4", "dt5", "dt6", "dt7", "seg", "run", "hb", "ovr", "temp"]


def _f(v):
    return None if v in (None, "", "None") else float(v)


class CsvSource:
    """重播先前匯出的 CSV（欄位見 CSV_COLUMNS），可用於離線分析或測試門檻。"""

    def __init__(self, cfg: dict, realtime: bool = True):
        c = cfg["source"]["csv"]
        self.path = c["path"]
        self.speed = float(c["speed"])
        self.loop = bool(c["loop"])
        self.realtime = realtime
        self._rows = self._load()
        self._i = 0
        self._t_start = time.time()
        self.name = f"CSV {self.path}"

    def _load(self):
        with open(self.path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def read(self) -> Sample | None:
        if self._i >= len(self._rows):
            if not self.loop:
                raise EOFError(_("CSV 重播結束"))
            self._i = 0
            self._t_start = time.time()
        row = self._rows[self._i]
        t0 = float(self._rows[0]["ts"])
        ts = float(row["ts"])
        if self.realtime:
            target = self._t_start + (ts - t0) / self.speed
            wait = target - time.time()
            if wait > 0:
                time.sleep(wait)
        self._i += 1
        return Sample(ts=ts, dt={ax: _f(row.get(f"dt{ax}")) for ax in range(1, 8)},
                      seg=int(float(row.get("seg") or 0)), run=int(float(row.get("run") or 0)),
                      hb=_f(row.get("hb")), ovr=_f(row.get("ovr")), temp=_f(row.get("temp")))

    def close(self):
        pass


def make_source(cfg: dict, realtime: bool = True):
    kind = cfg["robot"]["source"]
    if kind == "sim":
        return SimSource(cfg, realtime)
    if kind == "fanuc_http":
        return FanucHttpSource(cfg)
    if kind == "modbus":
        return ModbusSource(cfg)
    if kind == "csv":
        return CsvSource(cfg, realtime)
    raise ValueError(_("未知資料來源：{kind}", kind=kind))

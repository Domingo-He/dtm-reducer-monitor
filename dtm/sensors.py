"""振動與溫度感測器：IO-Link 主站或無線閘道（例如 Banner DXM）以 Modbus TCP 提供特徵值。

感測器自己在內部連續計算特徵值（RMS、包絡、峭度、溫度），DTM 只讀數字、不讀原始波形，所以頻寬很小。
讀到的值都帶時間戳，DTM 依「時間窗」切片：
- 診斷程式的等速段（以及 99xx 振動取樣窗）→ 每次診斷寫一筆振動（radial／axial），進交叉判斷；
- 生產動作代碼的每個循環 → 記在循環資料裡（vib_r、vib_a），看每日中位數。
2.1 起不用設定有線或無線：程式自動量每個鍵多久更新一次（數值變化的間隔）。超過 slow_s（預設 20 秒）
才更新的感測器（例如電池型無線，1–5 分鐘一次）只在數值變化時記一筆，主要用於每日趨勢；
落在等速段或取樣窗裡的才採用，量到加減速或等待的讀值會被丟掉。
哪些鍵要用由 [devices] 設備設定決定（沒設定就依 map 自動判斷）。

map 的鍵：「軸.方向.指標」或「軸.temp」，另外可以有 "ambient"（環境溫度）。
  方向：radial（徑向）、axial（軸向）；指標：rms、env（包絡）、mesh（嚙合）、motor1x（馬達轉頻）、kurt（峭度）
  例：[sensors.map] "2.radial.rms" = { addr = 200, type = "float32" }
"""
from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque

from .i18n import _

log = logging.getLogger("dtm.sensors")
VIB_KEYS = ("rms", "env", "mesh", "motor1x", "kurt")


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


class SensorHub:
    def __init__(self, cfg: dict, client=None, sim_robot=None):
        sc = cfg["sensors"]
        self.cfg = cfg
        self.sc = sc
        self.enabled = bool(sc.get("enabled"))
        self.kind = sc.get("source", "modbus")
        self.map: dict = dict(sc.get("map") or {})
        self.period = 1.0 / max(0.05, float(sc.get("poll_hz", 2.0)))
        self.only_changes = bool(sc.get("only_changes", False))    # 舊設定：全部當作慢速
        self.slow_s = float(sc.get("slow_s", 20.0))
        self.changes: dict[str, deque] = {}       # 每個鍵最近幾次數值變化的時間，用來判斷更新間隔
        self.prev_vals: dict = {}
        self.started_ts: float | None = None
        self.hist: deque = deque()
        self.keep_s = float(sc.get("keep_s", 7200))
        self.latest: dict = {}
        self.latest_ts: float | None = None
        self.last_change_ts: float | None = None
        self.error = ""
        self.reads = 0
        self.lock = threading.Lock()
        self.client = client
        self.sim = sim_robot
        if self.kind == "sim" and not self.map:          # 模擬：每個監測軸都有徑向、軸向 RMS、包絡與溫度
            from .config import enabled_axes
            for ax in enabled_axes(cfg):
                for k in ("radial.rms", "radial.env", "radial.kurt", "axial.rms", "temp"):
                    self.map[f"{ax}.{k}"] = {}
            self.map["ambient"] = {}
        self.refresh_flags()

    def refresh_flags(self):
        """依 [devices] 設定決定要用哪些鍵（設定頁儲存後呼叫）。"""
        from .config import device_flags
        self.flags = device_flags(self.cfg)

    # ------------------------------------------------------------ 更新間隔（自動判斷有線／無線）
    def update_interval(self, key: str, now: float | None = None) -> float | None:
        """這個鍵的典型更新間隔（秒）：最近幾次數值變化的間隔中位數；還沒變過就用已輪詢的時間當下限。"""
        ch = list(self.changes.get(key) or [])
        if len(ch) >= 3:
            return statistics.median(b - a for a, b in zip(ch[:-1], ch[1:]))
        now = now or self.latest_ts
        if self.started_ts and now and (not ch or len(ch) < 3) and now - (ch[-1] if ch else self.started_ts) > self.slow_s:
            return now - (ch[-1] if ch else self.started_ts)
        return None

    def is_slow(self, key: str, now: float | None = None) -> bool:
        if self.only_changes:
            return True
        iv = self.update_interval(key, now)
        return iv is not None and iv > self.slow_s

    # ------------------------------------------------------------ 讀取
    def _client(self):
        if self.client is None:
            try:
                from pymodbus.client import ModbusTcpClient
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(_("感測器閘道需要安裝 pymodbus：pip install pymodbus")) from e
            self.client = ModbusTcpClient(self.sc.get("host", "192.168.0.30"), port=int(self.sc.get("port", 502)))
        if hasattr(self.client, "connected") and not self.client.connected:
            self.client.connect()
        return self.client

    def _read_regs(self, addr: int, count: int) -> list[int]:
        cl = self._client()
        unit = int(self.sc.get("unit", 1))
        for kw in ("device_id", "slave", "unit"):
            try:
                rr = cl.read_holding_registers(addr, count=count, **{kw: unit})
                break
            except TypeError:
                continue
        else:  # pragma: no cover
            raise RuntimeError(_("pymodbus 版本不相容"))
        if rr.isError():
            raise RuntimeError(_("感測器閘道讀取錯誤 @ {addr}", addr=addr))
        return list(rr.registers)

    def _sim_values(self, now: float) -> dict:
        from .sources import SimRobot
        if self.sim is None:
            from .config import enabled_axes
            self.sim = SimRobot(enabled_axes(self.cfg), seed=int(self.cfg["source"]["sim"]["seed"]) + 5)
        day = float(self.cfg["source"]["sim"].get("start_day", 180))
        out = {}
        for key in self.map:
            if key == "ambient":
                out[key] = round(self.sim.temp(day) + self.sim.rng.uniform(-0.3, 0.3), 2)
                continue
            parts = key.split(".")
            ax = int(parts[0])
            if parts[1] == "temp":
                out[key] = round(self.sim.temp(day) + 9.0 + self.sim.rng.uniform(-0.5, 0.5), 2)
            else:
                v = self.sim.vib(ax, day)[parts[1]].get(parts[2])
                out[key] = v
        return out

    def poll_once(self, now: float | None = None) -> dict:
        from .sources import decode_words
        now = now or time.time()
        if self.kind == "sim":
            vals = self._sim_values(now)
        else:
            vals = {}
            wo = self.sc.get("word_order", "big")
            for key, m in self.map.items():
                typ = m.get("type", "float32")
                words = self._read_regs(int(m["addr"]), 1 if typ.endswith("16") else 2)
                vals[key] = decode_words(words, typ, wo) * float(m.get("scale", 1.0))
        from .config import device_key_on
        vals = {k: v for k, v in vals.items() if device_key_on(self.flags, k)}
        with self.lock:
            if self.started_ts is None:
                self.started_ts = now
            changed = vals != self.latest
            for k, v in vals.items():
                if k in self.prev_vals and v != self.prev_vals[k]:
                    self.changes.setdefault(k, deque(maxlen=12)).append(now)
            # 慢速的鍵（電池型無線，1–5 分鐘才更新）只在數值變化時記，避免舊讀值被算進新的時間窗
            rec = {k: v for k, v in vals.items()
                   if not self.is_slow(k, now) or k not in self.prev_vals or v != self.prev_vals[k]}
            self.prev_vals = dict(vals)
            self.latest, self.latest_ts = vals, now
            self.reads += 1
            if changed:
                self.last_change_ts = now
            if rec:
                self.hist.append((now, rec))
            while self.hist and self.hist[0][0] < now - self.keep_s:
                self.hist.popleft()
            self.error = ""
        return vals

    def run(self, stop: threading.Event):
        while not stop.is_set():
            t0 = time.time()
            try:
                self.poll_once(t0)
            except Exception as e:
                with self.lock:
                    self.error = str(e)
                log.warning(_("感測器閘道讀取失敗：%s"), e)
                try:
                    if self.client is not None and hasattr(self.client, "close"):
                        self.client.close()
                except Exception:
                    pass
                stop.wait(5.0)
                continue
            stop.wait(max(0.0, self.period - (time.time() - t0)))

    # ------------------------------------------------------------ 時間窗
    def readings(self, t0: float, t1: float) -> list[dict]:
        with self.lock:
            return [v for ts, v in self.hist if t0 <= ts <= t1]

    def window(self, ax: int, t0: float, t1: float) -> dict | None:
        """時間窗內這個軸的中位數：vib_r（徑向 RMS）、vib_a（軸向 RMS）、temp；以及完整的 radial／axial 指標。"""
        rs = self.readings(t0, t1)
        if not rs:
            return None
        out = {"n": len(rs), "radial": {}, "axial": {}}
        for d in ("radial", "axial"):
            for k in VIB_KEYS:
                v = _med([r.get(f"{ax}.{d}.{k}") for r in rs])
                if v is not None:
                    out[d][k] = v
        out["vib_r"] = out["radial"].get("rms")
        out["vib_a"] = out["axial"].get("rms")
        out["temp"] = _med([r.get(f"{ax}.temp") for r in rs])
        out["amb"] = _med([r.get("ambient") for r in rs])
        return out

    def windows(self, ax: int, spans: list[tuple[float, float]]) -> dict | None:
        rs = []
        for t0, t1 in spans:
            rs += self.readings(t0, t1)
        if not rs:
            return None
        out = {"n": len(rs), "radial": {}, "axial": {}}
        for d in ("radial", "axial"):
            for k in VIB_KEYS:
                v = _med([r.get(f"{ax}.{d}.{k}") for r in rs])
                if v is not None:
                    out[d][k] = v
        out["temp"] = _med([r.get(f"{ax}.temp") for r in rs])
        return out

    def status(self) -> dict:
        with self.lock:
            return {"enabled": self.enabled, "source": self.kind, "channels": len(self.map), "reads": self.reads,
                    "latest_ts": self.latest_ts, "last_change_ts": self.last_change_ts, "error": self.error,
                    "ambient": self.latest.get("ambient"),
                    "latest": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.latest.items()},
                    "update_s": {k: (round(iv, 1) if iv is not None else None)
                                 for k in self.latest for iv in [self.update_interval(k)]},
                    "slow": sorted(k for k in self.latest if self.is_slow(k)), "slow_s": self.slow_s,
                    "flags": self.flags}

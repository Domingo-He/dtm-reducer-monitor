"""P2–P6 逐循環處理：循環切割 → 行號對位 → 逐行損耗與角度累積 → 異常循環 → 保留原始波形。

資料以「欄位區塊」餵進來（{"t": [...], "q1": [...], "tq1": [...], "line": [...]}），
來源可以是 CSV 匯入、控制器串流或演示資料產生器；處理是逐循環增量進行，7 天的資料不必一次載入記憶體。
"""
from __future__ import annotations

import logging
import math
import time

from ..i18n import _
from . import metrics
from .align import V_STILL, Aligner, speeds
from .store import Session

log = logging.getLogger("dtm.pro")
SWEEP_CODES = range(8000, 9000)


def _med(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _mad(xs, med):
    return _med([abs(x - med) for x in xs])


class Processor:
    def __init__(self, cfg: dict, sess: Session, axes: list[int] | None = None, sweep_sink=None):
        self.cfg, self.s = cfg, sess
        p = cfg["pro"]
        self.hz = float(sess.meta.get("sample_hz") or p["sample_hz"])
        self.unit = sess.meta.get("torque_unit", p["torque_unit"])
        self.prog = sess.program()
        if not self.prog.motion_lines():
            raise ValueError(_("程式的主流程沒有動作行"))
        self.p = p
        self.axes: list[int] = []
        self.axp: list[dict] = []
        self.al: Aligner | None = None
        self.learned = False
        ax = axes or sess.meta.get("axes")
        if ax:
            self._set_axes([int(a) for a in ax])
        self.learn_n = max(3, int(p["learn_cycles"]))
        self.keep_first, self.keep_every = int(p["keep_raw_first"]), max(1, int(p["keep_raw_every"]))
        self.sweep_sink = sweep_sink
        self.buf: dict[str, list] | None = None
        self.pending: list[tuple[dict, str]] = []
        self.rev: dict = {}
        self.hist = sess.get("hist") or {"mv": [], "dmg": {}, "pk": {}}
        self.eval = sess.get("eval") or {"n": 0, "est": 0, "raw": 0}
        r = sess.db.execute("SELECT MAX(cyc), SUM(status IN ('ok','anomaly')) FROM cyc").fetchone()
        self.cyc_no = (r[0] or 0) + 1
        self.n_ok = int(r[1] or 0)
        cnt = sess.db.execute("SELECT why, COUNT(*) FROM raw GROUP BY why").fetchall()
        self.raw_n = dict(cnt)
        self.prev_t = None
        self.last_state = None
        self.code_mode = None
        self.near = None            # 只用位置時：是否在循環起點附近
        self.near_idx = None
        self.far_seen = False
        self.gap = max(2.0, 20.0 / self.hz)
        self.n_samples = int(sess.meta.get("n_samples") or 0)
        self._since_save = 0
        self.closed = False

    def _set_axes(self, axes: list[int]):
        """軸 = 資料裡同時有角度與扭矩的軸（對位用全部軸的角度，比只用監測軸準）。"""
        self.axes = axes
        if self.s.meta.get("axes") != axes:
            self.s.save_meta(axes=axes)
        self.axp = [metrics.axis_params(self.cfg, a, self.unit) for a in axes]
        self.al = Aligner(self.prog, axes, self.p.get("lookahead", 2))
        self.learned = self.al.load(self.s.get("align"))

    # ------------------------------------------------------------ 餵資料
    def feed(self, block: dict[str, list]):
        if self.closed:
            return
        ts = block["t"]
        n = len(ts)
        if not n:
            return
        if not self.axes:
            ax = [a for a in range(1, 8) if f"q{a}" in block and f"tq{a}" in block]
            if not ax:
                raise ValueError(_("資料沒有任何一軸同時有角度與扭矩欄位"))
            self._set_axes(ax)
        if self.s.meta.get("t0") is None:
            self.s.save_meta(t0=ts[0])
        keys = list(block)
        codes = block.get("line")
        if self.code_mode is None:
            self.code_mode = bool(codes) and self.al.has_codes(block)
            if not self.code_mode and not self.learned and not self.al.chord_templates():
                raise ValueError("need line reports (instrumented program) or joint-value taught points")
            if not self.code_mode and self.al.tpl:
                self.qref = self.al.tpl[-1].pts[-1]
        sc = self.al.sc
        for i in range(n):
            t = ts[i]
            if self.prev_t is not None and t - self.prev_t > self.gap:
                self._close("gap")
            self.prev_t = t
            code = int(codes[i]) if codes else 0
            if code in SWEEP_CODES:
                if self.buf:
                    self._close("gap")
                if self.sweep_sink:
                    self.sweep_sink({k: block[k][i] for k in keys})
                continue
            if self.code_mode:
                st = self.al.state_of(code)
                if st == 0 and self.last_state not in (None, 0):
                    self._close("")
                if st is not None:
                    self.last_state = st
            else:
                q = [block[f"q{a}"][i] * c for a, c in zip(self.axes, sc)]
                d = math.dist(q, self.qref)
                if d < 1.0:
                    self.near = True
                    self.near_idx = len(self.buf["t"]) if self.buf else 0
                elif d > 2.0 and self.near:
                    self.near = False
                    if self.buf and self.far_seen and self.near_idx is not None and self.near_idx > 0:
                        self._split(self.near_idx)       # 離開起點：前面是一個完整循環
                    self.far_seen = True
                elif d > 2.0:
                    self.far_seen = True
            if self.buf is None:
                self.buf = {k: [] for k in keys}
            b = self.buf
            for k in keys:
                b[k].append(block[k][i])
        self.n_samples += n
        self.s.meta["t1"] = ts[-1]

    def _split(self, at: int):
        head = {k: v[:at] for k, v in self.buf.items()}
        tail = {k: v[at:] for k, v in self.buf.items()}
        self.buf = head
        self._close("")
        self.buf = tail
        self.near_idx = None
        self.far_seen = False

    def _close(self, why: str):
        b, self.buf = self.buf, None
        self.far_seen = False
        if not b or len(b["t"]) < max(5, int(0.5 * self.hz)):
            return
        if not self.learned:
            self.pending.append((b, why))
            complete = [c for c, w in self.pending if self._looks_complete(c)]
            if len(complete) >= self.learn_n:
                self._learn()
            return
        self._process(b, why)

    def _looks_complete(self, cols: dict) -> bool:
        if not self.code_mode:
            return len(cols["t"]) > self.hz
        seen, first = set(), None
        for c in cols["line"]:
            st = self.al.state_of(int(c))
            if st is not None:
                seen.add(st)
                if first is None:
                    first = st
        return first == 0 and len(seen) >= 0.9 * len(self.al.states)

    def _learn(self):
        cyc = [c for c, w in self.pending if self._looks_complete(c)][: self.learn_n]
        if not cyc:
            cyc = [c for c, w in self.pending][: self.learn_n]
        if not cyc:
            return
        model = self.al.learn(cyc, self.hz)
        self.s.put("align", model)
        self.learned = True
        pend, self.pending = self.pending, []
        for c, w in pend:
            self._process(c, w)

    def flush(self):
        """資料結束：最後一個循環收尾、還沒學習的先用現有循環學習、存檔。"""
        if self.buf:
            self._close("end")
        if not self.learned and self.pending:
            self._learn()
        self._save(force=True)

    # ------------------------------------------------------------ 處理一個循環
    def _process(self, cols: dict, why: str):
        hz = self.hz
        res = self.al.align(cols, hz)
        spd = speeds(cols, self.axes, hz)
        still = [v < V_STILL for v in spd]
        t0 = cols["t"][0]
        lines, dmg, peak, angle, osc = metrics.cycle_metrics(cols, res["seg"], self.axp, hz, self.rev, still, t0)
        visited = len(set(res["state"])) / len(self.al.states)
        mv = sum(r["m"] for r in lines.values())
        if visited < 0.9 or why == "end" and visited < 1.0:
            status = "partial"
        elif res["conf"] < 0.6:
            status = "bad"
        else:
            status = "anomaly" if self._anomalous(mv, dmg, peak) else "ok"
        rec = {"cyc": self.cyc_no, "t0": t0, "t1": cols["t"][-1], "dur": round(cols["t"][-1] - t0 + 1.0 / hz, 3),
               "status": status, "conf": round(res["conf"], 3), "fit": round(res["fit"], 3),
               "dmg": {k: float(f"{v:.5g}") for k, v in dmg.items()}, "peak": {k: round(v, 4) for k, v in peak.items()},
               "lines": lines}
        why_raw = None
        if status in ("ok", "anomaly"):
            self.n_ok += 1
            if self.n_ok <= self.keep_first:
                why_raw = "first"
            elif self.n_ok % self.keep_every == 0:
                why_raw = "sample"
            elif status == "anomaly" and self.raw_n.get("anomaly", 0) < 200:
                why_raw = "anomaly"
        elif self.raw_n.get(status, 0) < 50:
            why_raw = status
        raw = None
        if why_raw:
            self.raw_n[why_raw] = self.raw_n.get(why_raw, 0) + 1
            st_line = [self.al.states[s] for s in res["state"]]
            raw = {"t": [x - t0 for x in cols["t"]]}
            for a in self.axes:
                raw[f"q{a}"] = cols[f"q{a}"]
            for a in self.axes:
                raw[f"tq{a}"] = cols[f"tq{a}"]
            if cols.get("line"):
                raw["line"] = cols["line"]
            raw["st"] = st_line
            if cols.get("true_line"):
                raw["true_line"] = cols["true_line"]
        if cols.get("true_line") and status in ("ok", "anomaly"):
            tr = cols["true_line"]
            est = res["state"]
            S = self.al.states
            self.eval["n"] += len(tr)
            self.eval["est"] += sum(1 for s, x in zip(est, tr) if S[s] == int(x))
            if cols.get("line"):
                self.eval["raw"] += sum(1 for c, x in zip(cols["line"], tr) if int(c) == int(x))
        self.s.add_cycle(rec, raw, why_raw or "")
        self.s.add_angle({a: v for a, v in angle.items()}, osc)
        self.cyc_no += 1
        self._since_save += 1
        self._save()

    def _anomalous(self, mv: float, dmg: dict, peak: dict) -> bool:
        h = self.hist
        bad = False
        if len(h["mv"]) >= 20:
            def z(x, xs):
                m = _med(xs)
                s = 1.4826 * _mad(xs, m) + 0.03 * abs(m) + 1e-12
                return abs(x - m) / s
            if z(mv, h["mv"]) > 4:
                bad = True
            for k, v in dmg.items():
                if z(v, h["dmg"].get(k, [v])) > 4:
                    bad = True
            for k, v in peak.items():
                if z(v, h["pk"].get(k, [v])) > 4:
                    bad = True
        if not bad:
            h["mv"] = (h["mv"] + [mv])[-60:]
            for k, v in dmg.items():
                h["dmg"][k] = (h["dmg"].get(k, []) + [v])[-60:]
            for k, v in peak.items():
                h["pk"][k] = (h["pk"].get(k, []) + [v])[-60:]
        return bad

    def _save(self, force: bool = False):
        if not force and self._since_save < 25:
            return
        self._since_save = 0
        self.s.put("hist", self.hist)
        self.s.put("eval", self.eval)
        cnt = self.s.cycle_counts()
        self.s.save_meta(n_samples=self.n_samples, n_cycles=sum(cnt.values()), counts=cnt,
                         t1=self.s.meta.get("t1"), updated=time.time())

    def close_session(self):
        self.flush()
        self.closed = True
        self.s.save_meta(status="closed", closed=time.time())

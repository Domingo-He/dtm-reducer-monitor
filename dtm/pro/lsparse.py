"""P1 程式匯入與解析：FANUC 教導程式的文字版（.LS）。

逐行解析：行號、指令類型（J／L／C／A 動作、WAIT、I/O、暫存器、CALL、JMP、LBL、IF…）、目標點（P[n]、PR[n]）、
速度與單位、定位方式（FINE／CNTn）、加速度倍率（ACC）、偏移（Offset、Tool_Offset）、等待（秒數或條件）。
/POS 區段的教導點：關節值（J1–J6、E1）或直角座標（X Y Z W P R、CONFIG），以及使用者座標／工具座標。

程式版本 = 內容的 SHA-256（前 12 碼）：只要改一個字，診斷資料就會切到新版本。
只解析文字，不需要控制器選配；其他品牌（KUKA .src、ABB .mod、安川 .JBI）可以照同樣的資料結構另寫解析器。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field

MOTION_KINDS = ("J", "L", "C", "A")
_LINE = re.compile(r"^\s*(\d+)\s*:\s?(.*)$")
_POINT = re.compile(r"\b(P|PR)\[\s*(R\[\d+\]|\d+)\s*(?::([^\]]*))?\]")
_SPEED = re.compile(r"\s(R\[\d+\]|\d+(?:\.\d*)?|\.\d+)\s*(%|mm/sec|cm/min|inch/min|deg/sec|msec|sec)(?![\w/])")
_TERM = re.compile(r"\b(FINE|CNT\s*(\d+|R\[\d+\]))(?![\w\]])")
_ACC = re.compile(r"\bACC\s*(\d+)\b")
_WAIT_T = re.compile(r"^WAIT\s+(\d*\.?\d+)\s*\(sec\)", re.I)
_WAIT_C = re.compile(r"^WAIT\s+(.+?)(?:\s+TIMEOUT,LBL\[(\d+)\])?\s*$", re.I)
_IO = re.compile(r"^(DO|RO|GO|AO|UO|SO|F)\[(\d+)(?::[^\]]*)?\]\s*=\s*(.+)$")
_REG = re.compile(r"^(R|PR|SR)\[(\d+)(?::([^\]]*))?\]\s*=\s*(.+)$")
_CALL = re.compile(r"^(CALL|RUN)\s+([A-Za-z0-9_]+)")
_JMP = re.compile(r"\bJMP\s+LBL\[(\d+)")
_LBL = re.compile(r"^LBL\[(\d+)(?::([^\]]*))?\]")
_IF = re.compile(r"^(IF|SELECT|ELSE|ENDIF|FOR|ENDFOR|=)(?=[\s(,=\d]|$)")
_POS_HEAD = re.compile(r"^(P)\[(\d+)(?::\"?([^\]\"]*)\"?)?\]\s*\{")
_POS_KV = re.compile(r"\b(J\d|E\d|X|Y|Z|W|P|R)\s*=\s*(-?\d*\.?\d+)")
_UFUT = re.compile(r"\bUF\s*:\s*(\d+)\s*,\s*UT\s*:\s*(\d+)")
_CONF = re.compile(r"CONFIG\s*:\s*'([^']*)'")


@dataclass
class Line:
    no: int
    text: str                      # 原文（去掉行號與結尾分號）
    kind: str = "other"            # J L C A wait io reg call jmp lbl if comment payload frame override end other
    point: str = ""                # P[3]、PR[50]
    point_no: int | None = None
    point_reg: bool = False        # True = PR[]（位置暫存器，執行時才知道座標）
    point_name: str = ""
    via: str = ""                  # 圓弧的中間點
    speed: float | None = None
    speed_unit: str = ""
    speed_reg: str = ""            # 速度用暫存器時，例如 R[5]
    term: str = ""                 # FINE、CNT
    cnt: int | None = None         # CNT 值（FINE = None）
    acc: int | None = None         # ACC 值（沒寫 = 100）
    offset: str = ""               # Offset、Tool_Offset（含 PR 編號）
    options: list = field(default_factory=list)
    wait_s: float | None = None    # WAIT n(sec)
    wait_cond: str = ""            # WAIT DI[1]=ON 之類
    io: str = ""                   # DO[1]=ON
    reg: str = ""                  # R[110]=2051
    reg_no: int | None = None
    reg_value: str = ""
    call: str = ""
    jmp: int | None = None         # 跳到 LBL[n]
    label: int | None = None
    conditional: bool = False      # IF／SELECT 的條件跳躍（流程分支）
    comment: str = ""

    @property
    def is_motion(self) -> bool:
        return self.kind in MOTION_KINDS

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_motion"] = self.is_motion
        return d


@dataclass
class Pos:
    no: int
    name: str = ""
    uf: int | None = None
    ut: int | None = None
    config: str = ""
    joint: dict = field(default_factory=dict)      # {"J1": 0.0, …, "E1": …}
    cart: dict = field(default_factory=dict)       # {"X": …, "W": …}

    def joint_vec(self, n: int = 6) -> list[float] | None:
        if not self.joint or any(f"J{i}" not in self.joint for i in range(1, n + 1)):
            return None
        return [self.joint[f"J{i}"] for i in range(1, n + 1)]


@dataclass
class Program:
    name: str
    comment: str
    lines: list[Line]
    positions: dict[int, Pos]
    version: str                     # 內容雜湊（前 12 碼）
    main_path: list[int]             # 主流程依序經過的行號（照順序、跟無條件 JMP、條件分支走「不成立」）
    warnings: list[str] = field(default_factory=list)

    def line(self, no: int) -> Line | None:
        return self._by_no().get(no)

    def _by_no(self) -> dict[int, Line]:
        return {ln.no: ln for ln in self.lines}

    def motion_lines(self, path: bool = True) -> list[Line]:
        """主流程上的動作行（依執行順序）；path=False 時依檔案順序列出全部動作行。"""
        by = self._by_no()
        if path:
            return [by[n] for n in self.main_path if by[n].is_motion]
        return [ln for ln in self.lines if ln.is_motion]

    def between(self, a: int | None, b: int | None) -> list[Line]:
        """主流程上，動作行 a 之後、下一個動作行 b 之前的非動作行（等待、I/O、暫存器…）。"""
        by = self._by_no()
        out, started = [], a is None
        for n in self.main_path:
            if not started:
                started = n == a
                continue
            if n == b:
                break
            if not by[n].is_motion:
                out.append(by[n])
        return out

    def to_dict(self) -> dict:
        return {"name": self.name, "comment": self.comment, "version": self.version, "main_path": self.main_path,
                "warnings": self.warnings, "lines": [ln.to_dict() for ln in self.lines],
                "positions": {str(k): asdict(v) for k, v in self.positions.items()}}


def _strip(s: str) -> str:
    s = s.strip()
    if s.endswith(";"):
        s = s[:-1]
    return s.strip()


def _parse_motion(ln: Line, body: str):
    ln.kind = body[0]
    pts = _POINT.findall(body)
    if pts:
        typ, num, name = pts[0]
        ln.point = f"{typ}[{num}]"
        ln.point_reg = typ == "PR"
        ln.point_no = int(num) if num.isdigit() else None
        ln.point_name = (name or "").strip()
        if ln.kind == "C" and len(pts) > 1:
            ln.via = ln.point                              # C 的第一點是經過點，第二點是終點
            typ2, num2, name2 = pts[1]
            ln.point = f"{typ2}[{num2}]"
            ln.point_reg = typ2 == "PR"
            ln.point_no = int(num2) if num2.isdigit() else None
            ln.point_name = (name2 or "").strip()
    m = _SPEED.search(" " + body)
    if m:
        v, unit = m.group(1), m.group(2)
        ln.speed_unit = unit
        if v.startswith("R["):
            ln.speed_reg = v
        else:
            ln.speed = float(v)
    m = _TERM.search(body)
    if m:
        if m.group(1) == "FINE":
            ln.term = "FINE"
        else:
            ln.term = "CNT"
            c = m.group(2)
            ln.cnt = int(c) if c and c.isdigit() else None
    m = _ACC.search(body)
    if m:
        ln.acc = int(m.group(1))
    for key in ("Tool_Offset", "Offset"):
        i = body.find(key)
        if i >= 0:
            frag = body[i:].split()[0]
            ln.offset = frag
            break
    for opt in ("Skip", "TB", "TA", "DB", "INC", "Wjnt", "RTCP", "PTH", "COORD", "VOFFSET", "Ind.EV", "Max_speed"):
        if re.search(r"\b" + re.escape(opt) + r"\b", body):
            ln.options.append(opt)


def parse_line(no: int, text: str) -> Line:
    body = _strip(text)
    ln = Line(no=no, text=body)
    if not body:
        ln.kind = "comment"
        return ln
    if body.startswith("!") or body.startswith("//"):
        ln.kind = "comment"
        ln.comment = body.lstrip("!/").strip()
        return ln
    if body[0] in MOTION_KINDS and len(body) > 1 and body[1] == " " and _POINT.search(body):
        _parse_motion(ln, body)
        return ln
    up = body.upper()
    m = _WAIT_T.match(body)
    if m:
        ln.kind, ln.wait_s = "wait", float(m.group(1))
        return ln
    if up.startswith("WAIT"):
        m = _WAIT_C.match(body)
        ln.kind, ln.wait_cond = "wait", (m.group(1) if m else body[4:].strip())
        if m and m.group(2):
            ln.jmp, ln.conditional = int(m.group(2)), True
        return ln
    m = _IF.match(up)
    if m:
        ln.kind, ln.conditional = "if", True
        j = _JMP.search(body)
        if j:
            ln.jmp = int(j.group(1))
        c = _CALL.search(body.split(",", 1)[-1].strip())
        if c:
            ln.call = c.group(2)
        return ln
    m = _LBL.match(body)
    if m:
        ln.kind, ln.label = "lbl", int(m.group(1))
        ln.comment = (m.group(2) or "").strip()
        return ln
    m = _JMP.search(body)
    if up.startswith("JMP") and m:
        ln.kind, ln.jmp = "jmp", int(m.group(1))
        return ln
    m = _CALL.match(body)
    if m:
        ln.kind, ln.call = "call", m.group(2)
        return ln
    m = _IO.match(body)
    if m:
        ln.kind, ln.io = "io", body
        return ln
    m = _REG.match(body)
    if m:
        ln.kind, ln.reg = "reg", body
        ln.reg_no = int(m.group(2)) if m.group(1) == "R" else None
        ln.reg_value = m.group(4).strip()
        return ln
    if up.startswith("PAYLOAD"):
        ln.kind = "payload"
    elif up.startswith(("UFRAME_NUM", "UTOOL_NUM", "UFRAME[", "UTOOL[")):
        ln.kind = "frame"
    elif up.startswith("OVERRIDE"):
        ln.kind = "override"
    elif up in ("END", "ABORT", "PAUSE"):
        ln.kind = "end"
    return ln


def _parse_pos(block: list[str]) -> dict[int, Pos]:
    out: dict[int, Pos] = {}
    cur: Pos | None = None
    for raw in block:
        s = raw.strip()
        m = _POS_HEAD.match(s)
        if m:
            cur = Pos(no=int(m.group(2)), name=(m.group(3) or "").strip())
            out[cur.no] = cur
            continue
        if cur is None:
            continue
        if s.startswith("GP") and not s.startswith("GP1"):
            cur = None                                     # 只取第一群組（機器人本體＋E1）
            continue
        m = _UFUT.search(s)
        if m:
            cur.uf, cur.ut = int(m.group(1)), int(m.group(2))
        m = _CONF.search(s)
        if m:
            cur.config = m.group(1)
        for k, v in _POS_KV.findall(s):
            (cur.joint if k[0] in "JE" and len(k) == 2 and k[1].isdigit() else cur.cart)[k] = float(v)
        if s.startswith("};"):
            cur = None
    return out


def _main_path(lines: list[Line]) -> tuple[list[int], list[str]]:
    by_idx = {ln.no: i for i, ln in enumerate(lines)}
    labels = {ln.label: ln.no for ln in lines if ln.kind == "lbl" and ln.label is not None}
    warns, path, seen = [], [], set()
    i = 0
    while 0 <= i < len(lines):
        ln = lines[i]
        if ln.no in seen:                     # 回到已經走過的行 = 迴圈，主流程到這裡結束
            break
        seen.add(ln.no)
        path.append(ln.no)
        if ln.kind == "end":
            break
        if ln.kind == "jmp" and ln.jmp is not None:
            if ln.jmp not in labels:
                warns.append(f"line {ln.no}: LBL[{ln.jmp}] not found")
                break
            i = by_idx[labels[ln.jmp]]
            continue
        i += 1
    return path, warns


def parse(text: str, name: str = "") -> Program:
    """解析 .LS 文字。容錯：看不懂的行當成 other，不會中斷。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    sect, mn, pos = "", [], []
    prog_name, comment = name, ""
    for raw in text.split("\n"):
        s = raw.strip()
        if s.startswith("/PROG"):
            parts = s.split()
            prog_name = parts[1] if len(parts) > 1 else prog_name
            sect = "prog"
            continue
        if s.startswith("/ATTR"):
            sect = "attr"
            continue
        if s.startswith("/APPL"):
            sect = "appl"
            continue
        if s.startswith("/MN"):
            sect = "mn"
            continue
        if s.startswith("/POS"):
            sect = "pos"
            continue
        if s.startswith("/END"):
            sect = "end"
            continue
        if sect == "attr" and s.startswith("COMMENT"):
            m = re.search(r'"([^"]*)"', s)
            comment = m.group(1) if m else ""
        elif sect == "mn":
            mn.append(raw)
        elif sect == "pos":
            pos.append(raw)
    lines: list[Line] = []
    for raw in mn:
        m = _LINE.match(raw)
        if m:
            lines.append(parse_line(int(m.group(1)), m.group(2)))
        elif raw.strip().startswith(":") and lines:          # 圓弧指令的第二行（:  P[5] 200mm/sec FINE）
            prev = lines[-1]
            joined = prev.text + " " + _strip(raw.strip()[1:])
            lines[-1] = parse_line(prev.no, joined)
    path, warns = _main_path(lines)
    if not lines:
        warns.append("no /MN lines")
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return Program(name=prog_name or "PROGRAM", comment=comment, lines=lines, positions=_parse_pos(pos),
                   version=version, main_path=path, warnings=warns)


def parse_file(path: str) -> Program:
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse(f.read())


# ------------------------------------------------------------ 給修改建議用：把一行改寫成建議版本（只產生文字，不寫回控制器）
def with_term(text: str, term: str) -> str:
    return _TERM.sub(term, text, count=1)


def with_acc(text: str, acc: int) -> str:
    if _ACC.search(text):
        return _ACC.sub(f"ACC{acc}", text, count=1)
    m = _TERM.search(text)
    if not m:
        return text + f" ACC{acc}"
    return text[:m.end()] + f" ACC{acc}" + text[m.end():]


def with_speed(text: str, speed: float) -> str:
    m = _SPEED.search(" " + text)
    if not m or m.group(1).startswith("R["):
        return text
    i, j = m.start(1) - 1, m.end(1) - 1
    if m.group(2) in ("mm/sec", "cm/min", "inch/min") and speed >= 100:
        speed = round(speed / 10.0) * 10
    v = f"{speed:.0f}" if speed >= 10 else f"{speed:.1f}"
    return text[:i] + v + text[j:]


# ------------------------------------------------------------ 提前執行與加註版程式（P3）
BLOCKING = ("wait", "call", "end")


def effective_terms(prog: Program) -> dict[int, str]:
    """每個動作行實際上會不會停：FINE，或到下一個動作行之前有 WAIT／CALL／END 擋住提前執行，都視為 FINE。"""
    ml = prog.motion_lines()
    out = {}
    for i, ln in enumerate(ml):
        nxt = ml[i + 1].no if i + 1 < len(ml) else None
        block = nxt is None or any(x.kind in BLOCKING for x in prog.between(ln.no, nxt))
        out[ln.no] = "CNT" if ln.term == "CNT" and not block else "FINE"
    return out


def instrument(text: str, reg: int = 114) -> str:
    """加註版程式：每個動作行前插入 R[reg]=原行號（只加行號回報，不改任何動作、速度與點位）。

    診斷期間載入（需 ASCII Upload 選配；沒有的話照清單在示教器手動加入），診斷結束後換回原程式。
    """
    text = text.replace("\r\n", "\n")
    out, sect, n = [], "", 0
    rows = text.split("\n")
    i = 0
    while i < len(rows):
        raw = rows[i]
        s = raw.strip()
        if s.startswith("/"):
            sect = s.split()[0]
            out.append(raw)
            if sect == "/MN":
                n += 1
                out.append(f"{n:4d}:  !DTM line report R[{reg}] (diag only) ;")
            i += 1
            continue
        if sect != "/MN":
            out.append(raw)
            i += 1
            continue
        m = _LINE.match(raw)
        if not m:
            i += 1                                     # 續行在處理動作行時一起帶走
            continue
        cont = []
        j = i + 1
        while j < len(rows) and rows[j].strip().startswith(":"):
            cont.append(rows[j])
            j += 1
        no, body = int(m.group(1)), raw[raw.index(":") + 1:]
        ln = parse_line(no, body + (" " + " ".join(c.strip()[1:] for c in cont) if cont else ""))
        if ln.is_motion:
            n += 1
            out.append(f"{n:4d}:  R[{reg}:DTM_LINE]={no}    ;")
        n += 1
        out.append(f"{n:4d}:{body}")
        out.extend(cont)
        i = j
    res = "\n".join(out)
    res = re.sub(r"LINE_COUNT\s*=\s*\d+;", f"LINE_COUNT\t= {n};", res)
    res = re.sub(r'COMMENT\s*=\s*"([^"]*)";', lambda mm: f'COMMENT\t\t= "{(mm.group(1) + " DTM")[:32]}";', res, count=1)
    return res

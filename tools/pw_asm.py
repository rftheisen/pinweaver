"""PinWeaver assembler.

Turns a text program into 16-bit instruction words. Syntax (see docs/isa.md):

    .side_set 1 [opt] [pindirs]  ; side-set pin count, optional enable bit,
                                 ; drive output enables instead of values
    .wrap_target           ; loop start (default: first instruction)
    .wrap                  ; loop end   (default: last instruction)
    label:
    jmp  [!x|x--|!y|y--|x!=y|pin|timeout] label
    wait 0|1 pin|gpio|flag|edge N [timeout]
    in   pins|x|y|null|osr, N
    out  pins|x|y|null|pindirs|pc|isr, N
    push [block|noblock]      pull [block|noblock]
    mov  pins|x|y|isr|osr|pc|pindirs, [~|::|+]pins|x|y|isr|osr|status|flags|null
    set  pins|x|y|pindirs, VALUE
    flag set|clear N
    nop
    crc init|x|y|osr_lo|osr_hi|hold   crc feed N   (N low bits of X, LSB first)
    line reset                        (line coder: level = idle, run = 0, bit clock = 0)
  Any instruction may end with `side V` and/or `[DELAY]`. Comments start with ; or #.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

OPS = {"jmp": 0, "wait": 1, "in": 2, "out": 3, "pushpull": 4, "mov": 5, "set": 6}
JMP_COND = {"": 0, "!x": 1, "x--": 2, "!y": 3, "y--": 4, "x!=y": 5, "pin": 6, "timeout": 7}
WAIT_SRC = {"pin": 0, "gpio": 1, "flag": 2, "edge": 3}
IN_SRC = {"pins": 0, "x": 1, "y": 2, "null": 3, "osr": 4}
OUT_DST = {"pins": 0, "x": 1, "y": 2, "null": 3, "pindirs": 4, "pc": 5, "isr": 6}
MOV_DST = {"pins": 0, "x": 1, "y": 2, "isr": 3, "osr": 4, "pc": 5, "pindirs": 6}
MOV_SRC = {"pins": 0, "x": 1, "y": 2, "isr": 3, "osr": 4, "status": 5, "flags": 6, "null": 7}
MOV_OP = {"": 0, "~": 1, "!": 1, "::": 2, "+": 3}
SET_DST = {"pins": 0, "x": 1, "y": 2, "pindirs": 3}


class AsmError(Exception):
    pass


@dataclass
class Program:
    name: str
    words: list[int]
    origin: int
    wrap_bottom: int
    wrap_top: int
    side_count: int = 0
    side_opt: bool = False
    side_pindirs: bool = False
    labels: dict[str, int] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)

    def config(self) -> dict:
        """Config fields this program decides itself (merge with pin settings)."""
        return {
            "side_count": self.side_count,
            "side_en": int(self.side_opt),
            "side_pindirs": int(self.side_pindirs),
            "wrap_bottom": self.wrap_bottom,
            "wrap_top": self.wrap_top,
            "start_pc": self.origin,
        }


def _num(tok: str) -> int:
    tok = tok.strip().lower()
    try:
        return int(tok, 0)
    except ValueError:
        raise AsmError(f"expected a number, got {tok!r}") from None


def assemble(src: str, origin: int = 0, name: str = "prog") -> Program:
    side_count, side_opt, side_pindirs = 0, False, False
    body: list[tuple[int, str]] = []
    labels: dict[str, int] = {}
    wrap_bottom = wrap_top = None

    # pass 1: labels and directives
    for lineno, raw in enumerate(src.splitlines(), 1):
        line = re.split(r"[;#]", raw, 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^(\w+):\s*(.*)$", line)
        while m:
            labels[m.group(1).lower()] = origin + len(body)
            line = m.group(2)
            m = re.match(r"^(\w+):\s*(.*)$", line)
        if not line:
            continue
        if line.startswith("."):
            parts = line.split()
            d = parts[0].lower()
            if d == ".side_set":
                side_count = _num(parts[1])
                flags = [p.lower() for p in parts[2:]]
                side_opt = "opt" in flags
                side_pindirs = "pindirs" in flags
                if not 0 <= side_count <= 2:
                    raise AsmError("side_set count must be 0..2")
            elif d == ".wrap_target":
                wrap_bottom = origin + len(body)
            elif d == ".wrap":
                wrap_top = origin + len(body) - 1
            elif d == ".program":
                name = parts[1] if len(parts) > 1 else name
            else:
                raise AsmError(f"line {lineno}: unknown directive {d}")
            continue
        body.append((lineno, line))

    if not body:
        raise AsmError("empty program")
    if origin + len(body) > 64:
        raise AsmError("program does not fit in 64 words")

    nside = side_count + int(side_opt)
    delay_max = (1 << (4 - nside)) - 1

    words = []
    for lineno, line in body:
        try:
            words.append(_encode(line, labels, side_count, side_opt, nside, delay_max))
        except AsmError as e:
            raise AsmError(f"line {lineno}: {e} :: {line}") from None

    return Program(
        name=name,
        words=words,
        origin=origin,
        wrap_bottom=origin if wrap_bottom is None else wrap_bottom,
        wrap_top=origin + len(body) - 1 if wrap_top is None else wrap_top,
        side_count=side_count,
        side_opt=side_opt,
        side_pindirs=side_pindirs,
        labels=labels,
        lines=[l for _, l in body],
    )


def _encode(line, labels, side_count, side_opt, nside, delay_max) -> int:
    delay = 0
    m = re.search(r"\[\s*([^\]]+)\]\s*$", line)
    if m:
        delay = _num(m.group(1))
        line = line[: m.start()].strip()
    side = None
    m = re.search(r"\bside\s+(\S+)\s*$", line)
    if m:
        side = _num(m.group(1))
        line = line[: m.start()].strip()
    if delay > delay_max:
        raise AsmError(f"delay {delay} too big (max {delay_max} with this side_set)")

    ds = delay
    if side_count:
        if side is None and not side_opt:
            raise AsmError("side value required (.side_set without opt)")
        if side is not None:
            if side >= (1 << side_count):
                raise AsmError("side value too big")
            ds |= side << (4 - nside)
            if side_opt:
                ds |= 1 << 3
    elif side is not None:
        raise AsmError("side used without .side_set")

    toks = line.replace(",", " ").split()
    mn, args = toks[0].lower(), [t.lower() for t in toks[1:]]

    def word(op, arg):
        return (op << 13) | (ds << 9) | (arg & 0x1FF)

    def target(t):
        if t in labels:
            return labels[t]
        return _num(t)

    if mn == "nop":
        return word(OPS["mov"], (MOV_DST["y"] << 6) | MOV_SRC["y"])
    if mn == "jmp":
        cond = args[0] if len(args) == 2 else ""
        if cond not in JMP_COND:
            raise AsmError(f"bad jmp condition {cond}")
        return word(OPS["jmp"], (JMP_COND[cond] << 6) | (target(args[-1]) & 0x3F))
    if mn == "wait":
        pol, src, idx = _num(args[0]), args[1], _num(args[2])
        if src not in WAIT_SRC or pol not in (0, 1):
            raise AsmError("bad wait")
        tmo = int(len(args) > 3 and args[3] == "timeout")
        return word(OPS["wait"], (pol << 8) | (WAIT_SRC[src] << 6) | (tmo << 5) | (idx & 0x1F))
    if mn in ("in", "out"):
        table = IN_SRC if mn == "in" else OUT_DST
        if args[0] not in table:
            raise AsmError(f"bad {mn} operand {args[0]}")
        n = _num(args[1])
        if not 1 <= n <= 8:
            raise AsmError("bit count must be 1..8")
        return word(OPS[mn], (table[args[0]] << 6) | (n & 7 if n != 8 else 0))
    if mn in ("push", "pull"):
        block = 0 if (args and args[0] == "noblock") else 1
        return word(OPS["pushpull"], ((mn == "pull") << 8) | (block << 7))
    if mn == "mov":
        dst, src = args[0], "".join(args[1:])
        op = ""
        for p in ("::", "~", "!", "+"):
            if src.startswith(p):
                op, src = p, src[len(p):]
                break
        if src.endswith("+1"):
            op, src = "+", src[:-2]
        if dst not in MOV_DST or src not in MOV_SRC:
            raise AsmError("bad mov operands")
        return word(OPS["mov"], (MOV_DST[dst] << 6) | (MOV_OP[op] << 4) | MOV_SRC[src])
    if mn == "set":
        if args[0] not in SET_DST:
            raise AsmError(f"bad set destination {args[0]}")
        v = _num(args[1])
        if not 0 <= v <= 31:
            raise AsmError("set value must be 0..31")
        return word(OPS["set"], (SET_DST[args[0]] << 6) | v)
    if mn == "flag":
        v = 1 if args[0] == "set" else 0
        return word(OPS["set"], (4 << 6) | (v << 4) | (_num(args[1]) & 3))
    if mn == "crc":
        subs = {"init": 0, "x": 1, "y": 2, "osr_lo": 3, "osr_hi": 4, "feed": 5, "hold": 7}
        if not args or args[0] not in subs:
            raise AsmError("crc needs init|x|y|osr_lo|osr_hi|feed N|hold")
        n = 0
        if args[0] == "feed":
            n = _num(args[1])
            if not 1 <= n <= 8:
                raise AsmError("crc feed bit count must be 1..8")
            n &= 7
        return word(7, (subs[args[0]] << 6) | n)
    if mn == "line" and args == ["reset"]:
        return word(7, 6 << 6)
    raise AsmError(f"unknown instruction {mn}")


def main(argv):
    if len(argv) < 2:
        print("usage: pw_asm.py file.pw [origin]")
        return 1
    origin = int(argv[2], 0) if len(argv) > 2 else 0
    prog = assemble(open(argv[1]).read(), origin)
    for i, (w, l) in enumerate(zip(prog.words, prog.lines)):
        print(f"{origin + i:02d}: {w:04x}   {l}")
    print(f"wrap {prog.wrap_bottom}..{prog.wrap_top}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Reference model of one PinWeaver engine, tick by tick.

This is the executable spec: tests run random programs on the RTL and on this
model and check that every register matches after every tick.
"""
from __future__ import annotations

from dataclasses import dataclass, field

M8 = 0xFF


def _rev8(v):
    return int(f"{v & M8:08b}"[::-1], 2)


def _map_pins(cur, data, base, count):
    for i in range(min(count, 8)):
        idx = (base + i) & 7
        cur = (cur & ~(1 << idx)) | (((data >> i) & 1) << idx)
    return cur & M8


def crc_feed(crc, bits, n, lsb_first, poly):
    """Feed the n low bits of `bits` into the 16-bit bit-serial CRC (same as the RTL).
    LSB first, or bit n-1 first. Shorter CRCs live in the top bits."""
    for i in range(n):
        b = (bits >> i) & 1 if lsb_first else (bits >> (n - 1 - i)) & 1
        fb = b ^ ((crc >> 15) & 1)
        crc = ((crc << 1) ^ (poly if fb else 0)) & 0xFFFF
    return crc


@dataclass
class Config:
    clkdiv: int = 0
    out_base: int = 0
    out_count: int = 0
    set_base: int = 0
    set_count: int = 0
    side_base: int = 0
    side_count: int = 0
    side_en: int = 0
    side_pindirs: int = 0
    in_base: int = 0
    jmp_pin: int = 0
    out_shr: int = 0
    in_shr: int = 0
    autopull: int = 0
    autopush: int = 0
    pull_thresh: int = 0
    push_thresh: int = 0
    wrap_bottom: int = 0
    wrap_top: int = 127
    start_pc: int = 0
    crc_poly: int = 0
    crc_init: int = 0
    crc_on_out: int = 0
    crc_on_in: int = 0
    lc_nrzi: int = 0
    lc_diff: int = 0
    lc_bitclk: int = 0
    lc_resync: int = 0
    lc_stuff_n: int = 0
    lc_stuff_any: int = 0
    lc_bit_m1: int = 0
    lc_sample: int = 0
    lc_arb: int = 0


def _next_run(run, last, bit, any_):
    if (bit == last) if any_ else bit:
        return min(run + 1, 7)
    return 1 if any_ else 0


@dataclass
class Engine:
    cfg: Config
    imem: list[int]
    tx: list[int] = field(default_factory=list)   # host -> engine FIFO
    rx: list[int] = field(default_factory=list)   # engine -> host FIFO
    fifo_depth: int = 8
    pc: int = 0
    x: int = 0
    y: int = 0
    isr: int = 0
    osr: int = 0
    isr_cnt: int = 0
    osr_cnt: int = 8
    dly: int = 0
    to: int = 0
    prev_pins: int = 0
    pins_out: int = 0
    pins_oe: int = 0
    crc: int = 0
    crc_hold: int = 0
    lvl: int = 1
    last: int = 0
    run: int = 0
    ph: int = 0

    def restart(self, pins=0):
        self.pc = self.cfg.start_pc
        self.x = self.y = self.isr = self.osr = self.isr_cnt = self.dly = self.to = 0
        self.osr_cnt = 8
        self.prev_pins = pins
        self.crc = self.cfg.crc_init
        self.crc_hold, self.lvl, self.last, self.run, self.ph = 0, 1, 0, 0, 0

    def state(self):
        return dict(pc=self.pc, x=self.x, y=self.y, isr=self.isr, osr=self.osr,
                    isr_cnt=self.isr_cnt, osr_cnt=self.osr_cnt, dly=self.dly, to=self.to,
                    pins_out=self.pins_out, pins_oe=self.pins_oe, crc=self.crc,
                    crc_hold=self.crc_hold, lvl=self.lvl, last=self.last, run=self.run, ph=self.ph)

    def tick(self, pins, flags):
        """Run one tick. Returns (flag_set, flag_clr) masks."""
        c = self.cfg
        fset = fclr = 0
        pins &= M8
        rot = ((pins | (pins << 8)) >> c.in_base) & M8
        prot = ((self.prev_pins | (self.prev_pins << 8)) >> c.in_base) & M8
        # bit clock: runs every tick, an edge on the IN pin re-aligns it
        if c.lc_resync and ((rot ^ prot) & 1):
            new_ph = 1
        else:
            new_ph = 0 if self.ph == c.lc_bit_m1 else (self.ph + 1) & M8
        arb_hit = c.lc_arb and self.ph == c.lc_sample and self.lvl and not (rot & 1)
        if self.dly:
            self.dly -= 1
            self.prev_pins = pins
            self.ph = new_ph
            if arb_hit:
                self.to = 1
            return fset, fclr

        w = self.imem[self.pc] & 0xFFFF
        op, ds, arg = w >> 13, (w >> 9) & 0xF, w & 0x1FF
        nside = (c.side_count + c.side_en) & 3
        delay = ds & ((1 << (4 - nside)) - 1)
        if c.side_en:
            side = (ds >> 1) & 3 if c.side_count == 2 else (ds >> 2) & 1
        else:
            side = (ds >> 2) & 3 if c.side_count == 2 else (ds >> 3) & 1
        side_valid = c.side_count != 0 and (not c.side_en or (ds >> 3) & 1)

        n = arg & 0xF
        n = 8 if n == 0 or n > 8 else n
        mask = M8 >> (8 - n)
        pull_t = 8 if c.pull_thresh in (0,) or c.pull_thresh > 8 else c.pull_thresh
        push_t = 8 if c.push_thresh in (0,) or c.push_thresh > 8 else c.push_thresh
        tx_empty = not self.tx
        rx_full = len(self.rx) >= self.fifo_depth
        status = (self.to << 2) | (int(rx_full) << 1) | int(tx_empty)

        # line coder
        line_mode = c.lc_nrzi or c.lc_diff or c.lc_bitclk or c.lc_stuff_n
        strobe_out = self.ph == 0
        strobe_in = self.ph == c.lc_sample
        stuff_hit = c.lc_stuff_n != 0 and self.run == c.lc_stuff_n
        stuff_bit = (1 - self.last) if c.lc_stuff_any else 0
        se0 = c.lc_diff and not (rot & 1) and not ((rot >> 1) & 1)
        rx_bit = int((rot & 1) == self.lvl) if c.lc_nrzi else rot & 1
        emit = None

        # next state starts as the current state
        s = self.state()
        s["ph"] = new_ph
        done, jump, jt = True, False, 0

        if op == 0:  # JMP
            cond = (arg >> 6) & 7
            if cond == 0:
                ok = True
            elif cond == 1:
                ok = self.x == 0
            elif cond == 2:
                ok = self.x != 0
                s["x"] = (self.x - 1) & M8
            elif cond == 3:
                ok = self.y == 0
            elif cond == 4:
                ok = self.y != 0
                s["y"] = (self.y - 1) & M8
            elif cond == 5:
                ok = self.x != self.y
            elif cond == 6:
                ok = bool((pins >> c.jmp_pin) & 1)
            else:
                ok = bool(self.to)
            if ok:
                jump, jt = True, (self.pc & 0x40) | (arg & 0x3F)

        elif op == 1:  # WAIT
            pol, src, tmo, idx = (arg >> 8) & 1, (arg >> 6) & 3, (arg >> 5) & 1, arg & 7
            if src == 0:
                ok = ((rot >> idx) & 1) == pol
            elif src == 1:
                ok = ((pins >> idx) & 1) == pol
            elif src == 2:
                ok = ((flags >> (arg & 3)) & 1) == pol
            else:
                a, b = (rot >> idx) & 1, (prot >> idx) & 1
                ok = bool(a and not b) if pol else bool(b and not a)
            if ok:
                s["to"] = 0
                if src == 2 and pol:
                    fclr |= 1 << (arg & 3)
            elif tmo:
                if self.y == 0:
                    s["to"] = 1
                else:
                    s["y"] = (self.y - 1) & M8
                    done = False
            else:
                done = False

        elif op == 2:  # IN
            src = (arg >> 6) & 7
            v = {0: rot, 1: self.x, 2: self.y, 4: self.osr}.get(src, 0)
            line_io = line_mode and src == 0 and n == 1
            if line_io and c.lc_bitclk and not strobe_in:
                done = False
            elif line_io and se0:
                s["to"] = 1
            elif line_io and stuff_hit:
                s["lvl"], s["last"] = rot & 1, rx_bit
                s["run"] = _next_run(self.run, self.last, rx_bit, c.lc_stuff_any)
                jump, jt = True, self.pc
            else:
                if line_io:
                    v = rx_bit
                    s["to"] = 0
                if c.in_shr:
                    t = ((self.isr >> n) | (v << (8 - n))) & M8
                else:
                    t = ((self.isr << n) | (v & mask)) & M8
                cnt = min(self.isr_cnt + n, 8)
                if c.autopush and cnt >= push_t:
                    if rx_full:
                        done = False
                    else:
                        self.rx.append(t)
                        s["isr"], s["isr_cnt"] = 0, 0
                else:
                    s["isr"], s["isr_cnt"] = t, cnt
                if done and c.crc_on_in and not self.crc_hold:
                    s["crc"] = crc_feed(self.crc, v, n, c.in_shr, c.crc_poly)
                if done and line_io:
                    s["lvl"], s["last"] = rot & 1, rx_bit
                    s["run"] = _next_run(self.run, self.last, rx_bit, c.lc_stuff_any)
                if not done:
                    s["to"] = self.to

        elif op == 3:  # OUT
            need = c.autopull and self.osr_cnt >= pull_t
            line_io = line_mode and ((arg >> 6) & 7) == 0
            if line_io and c.lc_bitclk and not strobe_out:
                done = False
            elif line_io and stuff_hit:
                emit = stuff_bit
                jump, jt = True, self.pc
            elif need and tx_empty:
                done = False
            else:
                src = self.tx.pop(0) if need else self.osr
                cnt = min((0 if need else self.osr_cnt) + n, 8)
                if c.out_shr:
                    t, new = src & mask, src >> n
                else:
                    t, new = src >> (8 - n), (src << n) & M8
                s["osr"], s["osr_cnt"] = new & M8, cnt
                if c.crc_on_out and not self.crc_hold:
                    s["crc"] = crc_feed(self.crc, t, n, c.out_shr, c.crc_poly)
                dst = (arg >> 6) & 7
                if dst == 0 and line_io and n == 1:
                    emit = t & 1
                elif dst == 0:
                    s["pins_out"] = _map_pins(self.pins_out, t, c.out_base, c.out_count)
                    if line_io:
                        s["lvl"] = t & 1
                elif dst == 1:
                    s["x"] = t
                elif dst == 2:
                    s["y"] = t
                elif dst == 4:
                    s["pins_oe"] = _map_pins(self.pins_oe, t, c.out_base, c.out_count)
                elif dst == 5:
                    jump, jt = True, (self.pc & 0x40) | (t & 0x3F)
                elif dst == 6:
                    s["isr"], s["isr_cnt"] = t, n

        elif op == 4:  # PUSH / PULL
            block = (arg >> 7) & 1
            if not (arg >> 8) & 1:
                if rx_full:
                    done = not block
                else:
                    self.rx.append(self.isr)
                    s["isr"], s["isr_cnt"] = 0, 0
            else:
                if tx_empty:
                    if block:
                        done = False
                    else:
                        s["osr"], s["osr_cnt"] = self.x, 0
                else:
                    s["osr"], s["osr_cnt"] = self.tx.pop(0), 0

        elif op == 5:  # MOV
            src = arg & 7
            v = [rot, self.x, self.y, self.isr, self.osr, status, flags & 0xF, 0][src]
            mop = (arg >> 4) & 3
            t = [v, ~v & M8, _rev8(v), (v + 1) & M8][mop]
            dst = (arg >> 6) & 7
            if dst == 0:
                s["pins_out"] = _map_pins(self.pins_out, t, c.out_base, c.out_count)
            elif dst == 1:
                s["x"] = t
            elif dst == 2:
                s["y"] = t
            elif dst == 3:
                s["isr"], s["isr_cnt"] = t, 0
            elif dst == 4:
                s["osr"], s["osr_cnt"] = t, 0
            elif dst == 5:
                jump, jt = True, (self.pc & 0x40) | (t & 0x3F)
            elif dst == 6:
                s["pins_oe"] = _map_pins(self.pins_oe, t, c.out_base, c.out_count)
            elif dst == 7:
                s["to"] = t & 1

        elif op == 6:  # SET
            dst, v = (arg >> 6) & 7, arg & 0x1F
            if dst == 0:
                s["pins_out"] = _map_pins(self.pins_out, v, c.set_base, c.set_count)
            elif dst == 1:
                s["x"] = v
            elif dst == 2:
                s["y"] = v
            elif dst == 3:
                s["pins_oe"] = _map_pins(self.pins_oe, v, c.set_base, c.set_count)
            elif dst == 4:
                if (v >> 4) & 1:
                    fset |= 1 << (v & 3)
                else:
                    fclr |= 1 << (v & 3)

        elif op == 7:  # CRC
            sub = (arg >> 6) & 7
            if sub == 0:
                s["crc"], s["crc_hold"] = c.crc_init, 0
            elif sub == 1:
                s["x"] = self.crc & M8
            elif sub == 2:
                s["y"] = (self.crc >> 8) & M8
            elif sub == 3:
                s["osr"], s["osr_cnt"] = self.crc & M8, 0
            elif sub == 4:
                s["osr"], s["osr_cnt"] = (self.crc >> 8) & M8, 0
            elif sub == 5:
                s["crc"] = crc_feed(self.crc, self.x, n, 1, c.crc_poly)
            elif sub == 6:  # line reset
                s["lvl"], s["last"], s["run"], s["ph"] = 1, 0, 0, 0
            else:  # crc hold
                s["crc_hold"] = 1

        if emit is not None:
            if c.lc_arb and self.to:
                e_lvl = 1
            else:
                e_lvl = (self.lvl if emit else 1 - self.lvl) if c.lc_nrzi else emit
            s["lvl"], s["last"] = e_lvl, emit
            s["run"] = _next_run(self.run, self.last, emit, c.lc_stuff_any)
            if c.lc_diff:
                s["pins_out"] = _map_pins(self.pins_out, e_lvl | ((1 - e_lvl) << 1), c.out_base, 2)
            else:
                s["pins_out"] = _map_pins(self.pins_out, e_lvl, c.out_base, 1)

        if side_valid:
            key = "pins_oe" if c.side_pindirs else "pins_out"
            s[key] = _map_pins(s[key], side, c.side_base, c.side_count)

        if done:
            s["dly"] = delay
            if jump:
                s["pc"] = jt
            elif self.pc == c.wrap_top:
                s["pc"] = c.wrap_bottom
            else:
                s["pc"] = (self.pc & 0x40) | ((self.pc + 1) & 0x3F)   # wraps inside the page

        if arb_hit:
            s["to"] = 1
        for k, v in s.items():
            setattr(self, k, v)
        self.prev_pins = pins
        return fset, fclr


class Chip:
    """Both engines + shared flags, for running whole programs in Python."""

    def __init__(self):
        self.imem = [0] * 128
        self.engines = [Engine(Config(), self.imem), Engine(Config(), self.imem)]
        self.flags = 0
        self.div = [0, 0]
        self.enabled = [False, False]

    def load(self, engine, prog, **pins):
        for i, w in enumerate(prog.words):
            self.imem[prog.origin + i] = w
        cfg = Config(**{**prog.config(), **pins})
        e = self.engines[engine]
        e.cfg = cfg
        e.restart()
        self.enabled[engine] = True
        self.div[engine] = 0

    def clock(self, pins_in):
        """One system clock. pins_in = the (already synchronised) GPIO inputs."""
        fs = fc = 0
        for i, e in enumerate(self.engines):
            if not self.enabled[i]:
                continue
            if self.div[i] == 0:
                a, b = e.tick(pins_in, self.flags)
                fs, fc = fs | a, fc | b
                self.div[i] = e.cfg.clkdiv
            else:
                self.div[i] -= 1
        self.flags = ((self.flags & ~fc) | fs) & 0xF

    def pins(self):
        out = oe = 0
        for i, e in enumerate(self.engines):
            if self.enabled[i]:
                out |= e.pins_out & e.pins_oe
                oe |= e.pins_oe
        return out, oe

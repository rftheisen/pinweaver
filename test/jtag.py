"""JTAG TAP models (IEEE 1149.1) and host-side vector helpers, for the tests.

Each TAP has the 16-state controller, a 4-bit IR (IDCODE 0b1110, BYPASS
0b1111, USER 0b1000 = an 8-bit read/write register) and resets to IDCODE.
TAPs can be chained: the first TAP's TDO feeds the next TAP's TDI.
"""
from cocotb.triggers import RisingEdge

IR_LEN = 4
IDCODE, BYPASS, USER = 0b1110, 0b1111, 0b1000

# state -> (next if TMS = 0, next if TMS = 1)
NEXT = {
    "reset": ("idle", "reset"), "idle": ("idle", "sel_dr"),
    "sel_dr": ("cap_dr", "sel_ir"), "cap_dr": ("sh_dr", "ex1_dr"),
    "sh_dr": ("sh_dr", "ex1_dr"), "ex1_dr": ("pa_dr", "up_dr"),
    "pa_dr": ("pa_dr", "ex2_dr"), "ex2_dr": ("sh_dr", "up_dr"),
    "up_dr": ("idle", "sel_dr"),
    "sel_ir": ("cap_ir", "reset"), "cap_ir": ("sh_ir", "ex1_ir"),
    "sh_ir": ("sh_ir", "ex1_ir"), "ex1_ir": ("pa_ir", "up_ir"),
    "pa_ir": ("pa_ir", "ex2_ir"), "ex2_ir": ("sh_ir", "up_ir"),
    "up_ir": ("idle", "sel_dr"),
}


class Tap:
    def __init__(self, idcode):
        self.idcode = idcode
        self.state = "reset"
        self.ir = IDCODE
        self.user = 0
        self.sr, self.sr_len = 0, 1
        self.tdo = 0

    def dr_len(self):
        return {IDCODE: 32, USER: 8}.get(self.ir, 1)

    def rise(self, tms, tdi):
        """TCK rising edge: shift if in a shift state, then move to the next state."""
        s = self.state
        if s in ("sh_dr", "sh_ir"):
            self.sr = (self.sr >> 1) | (tdi << (self.sr_len - 1))
        nxt = NEXT[s][tms]
        if nxt == "reset":
            self.ir = IDCODE
        elif nxt == "cap_dr":
            self.sr_len = self.dr_len()
            self.sr = {IDCODE: self.idcode, USER: self.user}.get(self.ir, 0)
        elif nxt == "cap_ir":
            self.sr, self.sr_len = 0b0001, IR_LEN       # IR capture value ends in 01
        elif nxt == "up_dr" and self.ir == USER:
            self.user = self.sr & 0xFF
        elif nxt == "up_ir":
            self.ir = self.sr & 0xF
        self.state = nxt

    def fall(self):
        """TCK falling edge: TDO shows the shift register's low bit while shifting."""
        if self.state in ("sh_dr", "sh_ir"):
            self.tdo = self.sr & 1


class JtagChain:
    """TAPs in a chain on the engine's pins. TDI enters TAP 0; the last TAP drives TDO."""

    def __init__(self, dut, pins, tck, tms, tdi, tdo, taps):
        self.dut, self.pins = dut, pins
        self.tck, self.tms, self.tdi, self.tdo = tck, tms, tdi, tdo
        self.taps = taps

    async def run(self):
        p = self.pins
        prev = 0
        while True:
            await RisingEdge(self.dut.clk)
            tck = p.out(self.tck) if p.oe(self.tck) else 0
            if tck == prev:
                continue
            prev = tck
            if tck:
                tms, bit = p.out(self.tms), p.out(self.tdi)
                outs = [t.tdo for t in self.taps]         # each TAP's TDO before the edge
                for i, t in enumerate(self.taps):
                    t.rise(tms, bit if i == 0 else outs[i - 1])
            else:
                for t in self.taps:
                    t.fall()
                p.drive(self.tdo, self.taps[-1].tdo)


# ---- host-side vectors: lists of (tms, tdi)

def reset_to_idle():
    return [(1, 0)] * 5 + [(0, 0)]


def shift(bits, ir=False):
    """From Run-Test/Idle: shift `bits` (LSB first) through IR or DR, back to idle.
    Returns (vectors, index of the first shifted cycle)."""
    head = [(1, 0), (1, 0), (0, 0), (0, 0)] if ir else [(1, 0), (0, 0), (0, 0)]
    body = [(1 if i == len(bits) - 1 else 0, b) for i, b in enumerate(bits)]
    return head + body + [(1, 0), (0, 0)], len(head)


def pack(vectors):
    """Host bytes for the jtag program: count - 1, then 4 cycles per byte."""
    out = []
    for i in range(0, len(vectors), 4):
        byte = 0
        for k, (tms, tdi) in enumerate(vectors[i:i + 4]):
            byte |= (tms | (tdi << 1)) << (2 * k)
        out.append(byte)
    return [len(vectors) - 1] + out


def unpack(tdo_bytes, n):
    """TDO bits back from the RX bytes (n cycles)."""
    bits = []
    for b in tdo_bytes[:n // 8]:
        bits += [(b >> i) & 1 for i in range(8)]
    left = n % 8
    if left:
        last = tdo_bytes[n // 8] >> (8 - left)
        bits += [(last >> i) & 1 for i in range(left)]
    return bits


def to_bits(v, n):
    return [(v >> i) & 1 for i in range(n)]


def from_bits(bits):
    return sum(b << i for i, b in enumerate(bits))

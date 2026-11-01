"""A small SWD target (ARM debug port) model for the tests.

It needs a line reset (50+ cycles of SWDIO high) first. Requests are 8 bits
sampled on SWCLK rising edges; the target drives SWDIO on falling edges.
Read: turnaround, ACK (3), data (32), parity, turnaround.
Write: turnaround, ACK (3), turnaround, data (32) + parity from the host.
"""
from cocotb.triggers import RisingEdge

IDCODE = 0x2BA01477
OK, WAIT, FAULT = 0b001, 0b010, 0b100


def parity(v):
    return bin(v).count("1") & 1


def request(apndp, rnw, addr):
    """The request byte as sent on the wire, LSB first."""
    a2, a3 = (addr >> 2) & 1, (addr >> 3) & 1
    p = (apndp + rnw + a2 + a3) & 1
    return 1 | (apndp << 1) | (rnw << 2) | (a2 << 3) | (a3 << 4) | (p << 5) | (0 << 6) | (1 << 7)


class SwdTarget:
    def __init__(self, dut, pins, swclk, swdio):
        self.dut, self.pins, self.clk_pin, self.dio = dut, pins, swclk, swdio
        self.dp = {0x0: IDCODE, 0x4: 0, 0x8: 0, 0xC: 0}
        self.ap = {}
        self.wait_next = 0       # answer WAIT to this many requests
        self.log = []            # (kind, apndp, addr, value / ack)
        self.errors = []

    def _respond(self, req):
        apndp, rnw, addr = (req >> 1) & 1, (req >> 2) & 1, ((req >> 3) & 3) << 2
        if self.wait_next:
            self.wait_next -= 1
            ack = WAIT
        else:
            ack = OK
        acts = [("none",)]                                   # turnaround
        acts += [("drive", (ack >> i) & 1) for i in range(3)]
        if ack != OK:
            self.log.append(("ack", apndp, addr, ack))
            return acts + [("release",)]
        if rnw:
            regs = self.ap if apndp else self.dp
            v = regs.get(addr, 0)
            self.log.append(("read", apndp, addr, v))
            acts += [("drive", (v >> i) & 1) for i in range(32)] + [("drive", parity(v))]
            return acts + [("release",)]
        acts += [("release",)]                                 # turnaround
        self.write_to = (apndp, addr)
        return acts + [("sample",)] * 33

    def _sampled(self, bits):
        v = sum(b << i for i, b in enumerate(bits[:32]))
        if bits[32] != parity(v):
            self.errors.append(f"write parity wrong for {v:#010x}")
        apndp, addr = self.write_to
        (self.ap if apndp else self.dp)[addr] = v
        self.log.append(("write", apndp, addr, v))

    async def run(self):
        p = self.pins
        prev_clk, ones, reset, idle_seen = 0, 0, False, False
        bits, acts, got, driving = None, [], [], False
        while True:
            await RisingEdge(self.dut.clk)
            clk = p.out(self.clk_pin) if p.oe(self.clk_pin) else 0
            if clk == prev_clk:
                continue
            prev_clk = clk
            if clk == 0:                                     # falling edge
                if acts:
                    a = acts[0]
                    if a[0] == "drive":
                        driving = True
                        p.drive(self.dio, a[1])
                    elif a[0] == "release":
                        driving = False
                        p.drive(self.dio, 1)                 # pull-up
                continue
            # rising edge
            dio = p.pad(self.dio) if not driving else None
            if acts:
                a = acts.pop(0)
                if a[0] == "sample":
                    got.append(dio)
                    if len(got) == 33:
                        self._sampled(got)
                        got = []
                continue
            if bits is None:                                 # idle: line reset, then requests
                if dio == 1:
                    if reset and idle_seen:
                        bits = [1]                           # start bit
                    else:
                        ones += 1
                        reset = reset or ones >= 50
                else:
                    ones = 0
                    idle_seen = reset
                continue
            bits.append(dio)
            if len(bits) == 8:
                req = sum(b << i for i, b in enumerate(bits))
                bits = None
                ok = (req & 1) and not (req >> 6) & 1 and (req >> 7) & 1 \
                    and ((req >> 5) & 1) == parity((req >> 1) & 0xF)
                if not ok:
                    self.errors.append(f"bad request {req:#04x}")
                    continue
                acts = self._respond(req)

"""Test helpers: talk to PinWeaver over SPI and model simple devices on its pins."""
import os
import sys

from cocotb.triggers import ClockCycles, RisingEdge, Timer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from pw_asm import assemble  # noqa: E402

PROGRAMS = os.path.join(HERE, "..", "programs")

# config field -> (register, bit offset, width); see docs/isa.md
FIELDS = {
    "out_base": (2, 0, 3), "out_count": (2, 3, 4),
    "set_base": (3, 0, 3), "set_count": (3, 3, 3),
    "side_base": (4, 0, 3), "side_count": (4, 3, 2), "side_en": (4, 5, 1),
    "in_base": (5, 0, 3), "jmp_pin": (5, 3, 3),
    "out_shr": (6, 0, 1), "in_shr": (6, 1, 1), "autopull": (6, 2, 1), "autopush": (6, 3, 1),
    "pull_thresh": (7, 0, 4), "push_thresh": (7, 4, 4),
    "wrap_bottom": (8, 0, 6), "wrap_top": (9, 0, 6), "start_pc": (10, 0, 6),
}


def load_program(name, origin=0):
    with open(os.path.join(PROGRAMS, name + ".pw")) as f:
        return assemble(f.read(), origin, name)


def config_bytes(cfg):
    regs = [0] * 11
    div = cfg.get("clkdiv", 0)
    regs[0], regs[1] = div & 0xFF, div >> 8
    for key, val in cfg.items():
        if key == "clkdiv":
            continue
        reg, off, width = FIELDS[key]
        assert 0 <= val < (1 << width), f"{key}={val} does not fit"
        regs[reg] |= val << off
    return regs


class Host:
    """SPI master that drives ui_in[2:0] and reads uo_out[0]."""

    def __init__(self, dut, half_ns=160):
        self.dut = dut
        self.half = half_ns
        dut.ui_in.value = 0b100

    async def xfer(self, data):
        dut, half = self.dut, self.half
        dut.ui_in.value = 0b000
        await Timer(half, unit="ns")
        out = []
        for b in data:
            r = 0
            for i in range(7, -1, -1):
                mosi = (b >> i) & 1
                dut.ui_in.value = mosi << 1
                await Timer(half, unit="ns")
                r = (r << 1) | (int(dut.uo_out.value) & 1)
                dut.ui_in.value = (mosi << 1) | 1
                await Timer(half, unit="ns")
            out.append(r)
        dut.ui_in.value = 0b000
        await Timer(half, unit="ns")
        dut.ui_in.value = 0b100
        await Timer(2 * half, unit="ns")
        return out

    async def load(self, engine, prog, **pins):
        words = []
        for w in prog.words:
            words += [w >> 8, w & 0xFF]
        await self.xfer([0x01, prog.origin] + words)
        cfg = dict(prog.config())
        cfg.update(pins)
        await self.xfer([0x02, engine * 16] + config_bytes(cfg))

    async def ctrl(self, enable, restart=0):
        await self.xfer([0x03, enable | (restart << 4)])

    async def debug(self):
        """Returns (status, pc0, pc1)."""
        r = await self.xfer([0x06, 0, 0, 0])
        return r[3], r[1], r[2]

    async def status(self):
        return (await self.xfer([0x00]))[0]

    async def push(self, engine, data):
        """Push bytes, waiting whenever the 4-byte TX FIFO is full."""
        full_bit = 1 << (4 + 2 * engine)
        for b in data:
            while (await self.status()) & full_bit:
                pass
            await self.xfer([0x04, engine, b])

    async def pop(self, engine, n):
        return (await self.xfer([0x05, engine] + [0] * n))[2:]

    async def rx_ready(self, engine):
        return bool((await self.status()) & (1 << (5 + 2 * engine)))

    async def flags(self, set_mask=0, clr_mask=0):
        await self.xfer([0x07, set_mask | (clr_mask << 4)])


class Pins:
    """Drives uio_in one bit at a time and reads uio_out / uio_oe."""

    def __init__(self, dut):
        self.dut = dut
        self.val = 0
        dut.uio_in.value = 0

    def drive(self, n, bit):
        self.val = (self.val & ~(1 << n)) | ((bit & 1) << n)
        self.dut.uio_in.value = self.val

    def out(self, n):
        return (int(self.dut.uio_out.value) >> n) & 1

    def oe(self, n):
        return (int(self.dut.uio_oe.value) >> n) & 1


async def uart_recv(dut, pins, n, nbytes, bit_clks):
    """Decode `nbytes` 8N1 frames from output pin `n`."""
    while not (pins.oe(n) and pins.out(n)):
        await RisingEdge(dut.clk)
    got = []
    for _ in range(nbytes):
        while pins.out(n) == 1:
            await RisingEdge(dut.clk)
        await ClockCycles(dut.clk, bit_clks // 2)
        assert pins.out(n) == 0, "start bit too short"
        v = 0
        for i in range(8):
            await ClockCycles(dut.clk, bit_clks)
            v |= pins.out(n) << i
        await ClockCycles(dut.clk, bit_clks)
        assert pins.out(n) == 1, f"missing stop bit after {v:#04x}"
        got.append(v)
    return got


async def uart_send(dut, pins, n, data, bit_clks, bad_stop=False):
    """Drive 8N1 frames on input pin `n`."""
    for b in data:
        bits = [0] + [(b >> i) & 1 for i in range(8)] + [0 if bad_stop else 1]
        for bit in bits:
            pins.drive(n, bit)
            await ClockCycles(dut.clk, bit_clks)
        pins.drive(n, 1)
        await ClockCycles(dut.clk, bit_clks)


async def spi_device(dut, pins, sck, mosi, miso, cs, replies, received):
    """Mode-0 SPI device: one byte per CS pulse, answers with `replies` in order."""
    replies = list(replies)
    last_sck, last_cs = 0, 1
    shift_in = shift_out = bits = 0
    while True:
        await RisingEdge(dut.clk)
        c = pins.out(cs) if pins.oe(cs) else 1  # CS has a pull-up
        s = pins.out(sck)
        if last_cs == 1 and c == 0:
            shift_out = replies.pop(0) if replies else 0
            shift_in = bits = 0
            pins.drive(miso, (shift_out >> 7) & 1)
        elif c == 0 and last_sck == 0 and s == 1:
            shift_in = ((shift_in << 1) | pins.out(mosi)) & 0xFF
            bits += 1
            if bits == 8:
                received.append(shift_in)
        elif c == 0 and last_sck == 1 and s == 0 and bits < 8:
            shift_out = (shift_out << 1) & 0xFF
            pins.drive(miso, (shift_out >> 7) & 1)
        last_sck, last_cs = s, c

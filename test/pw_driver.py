"""Test helpers: talk to PinWeaver over SPI and model simple devices on its pins."""
import os
import sys

from cocotb.triggers import ClockCycles, Lock, RisingEdge, Timer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from pw_asm import assemble  # noqa: E402

PROGRAMS = os.path.join(HERE, "..", "programs")

# config field -> (register, bit offset, width); see docs/isa.md
FIELDS = {
    "clkdiv": (0, 0, 16),
    "out_base": (2, 0, 3), "out_count": (2, 3, 4),
    "set_base": (3, 0, 3), "set_count": (3, 3, 3),
    "side_base": (4, 0, 3), "side_count": (4, 3, 2), "side_en": (4, 5, 1), "side_pindirs": (4, 6, 1),
    "in_base": (5, 0, 3), "jmp_pin": (5, 3, 3),
    "out_shr": (6, 0, 1), "in_shr": (6, 1, 1), "autopull": (6, 2, 1), "autopush": (6, 3, 1),
    "pull_thresh": (7, 0, 4), "push_thresh": (7, 4, 4),
    "wrap_bottom": (8, 0, 7), "wrap_top": (9, 0, 7), "start_pc": (10, 0, 7),
    "crc_poly": (11, 0, 16), "crc_init": (13, 0, 16),
    "crc_on_out": (15, 4, 1), "crc_on_in": (15, 5, 1),
    # line coder: registers 40 + 4 * engine (LC_REG + offset below)
    "lc_nrzi": (100, 0, 1), "lc_diff": (100, 1, 1), "lc_bitclk": (100, 2, 1), "lc_resync": (100, 3, 1),
    "lc_stuff_n": (100, 4, 3), "lc_stuff_any": (100, 7, 1), "lc_bit_m1": (101, 0, 8), "lc_sample": (102, 0, 8), "lc_arb": (103, 0, 1),
}


def load_program(name, origin=0):
    with open(os.path.join(PROGRAMS, name + ".pw")) as f:
        return assemble(f.read(), origin, name)


def config_bytes(cfg):
    """The 16 engine registers (line coder fields are left out; see lc_bytes)."""
    regs = [0] * 16
    for key, val in cfg.items():
        reg, off, width = FIELDS[key]
        if reg >= 100:
            continue
        assert 0 <= val < (1 << width), f"{key}={val} does not fit"
        if width == 16:
            regs[reg], regs[reg + 1] = val & 0xFF, val >> 8
        else:
            regs[reg] |= val << off
    return regs


def lc_bytes(cfg):
    """The 4 line coder registers of an engine."""
    regs = [0] * 4
    for key, val in cfg.items():
        reg, off, width = FIELDS[key]
        if reg >= 100:
            assert 0 <= val < (1 << width), f"{key}={val} does not fit"
            regs[reg - 100] |= val << off
    return regs


class Host:
    """SPI master that drives ui_in[2:0] and reads uo_out[0]."""

    def __init__(self, dut, half_ns=160):
        self.dut = dut
        self.half = half_ns
        self.lock = Lock()
        dut.ui_in.value = 0b100

    async def xfer(self, data):
        async with self.lock:
            return await self._xfer(data)

    async def _xfer(self, data):
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
        await self.xfer([0x02, 40 + 4 * engine] + lc_bytes(cfg))

    async def ctrl(self, enable, restart=0, la_arm=0, la_stop=0):
        await self.xfer([0x03, enable | (restart << 4) | (la_arm << 6) | (la_stop << 7)])

    async def debug(self):
        """Returns (status, pc0, pc1)."""
        r = await self.xfer([0x06, 0, 0, 0])
        return r[3], r[1], r[2]

    async def la_status(self):
        """Returns (state, ptr, trigger word, trigger half)."""
        r = await self.xfer([0x06, 0, 0, 0, 0, 0, 0])
        return r[4] & 3, r[5], r[6], (r[4] >> 2) & 1

    async def la_config(self, start, last, post, div=0, mask=0, value=0, mode=0):
        await self.xfer([0x02, 32, start, last, post, div & 0xFF, div >> 8, mask, value, mode])

    async def read_imem(self, addr, n):
        r = (await self.xfer([0x08, addr] + [0] * (2 * n)))[2:]
        return [(r[2 * i] << 8) | r[2 * i + 1] for i in range(n)]

    async def la_samples(self, start, last):
        """Read the capture ring, oldest sample first. Returns (samples, trigger index)."""
        state, ptr, tw, th = await self.la_status()
        words = await self.read_imem(start, last - start + 1)
        order = list(range(ptr, last + 1)) + list(range(start, ptr))
        samples, trig = [], None
        for w in order:
            if w == tw:
                trig = len(samples) + th
            v = words[w - start]
            samples += [v >> 8, v & 0xFF]
        return samples, trig

    async def status(self):
        return (await self.xfer([0x00]))[0]

    async def push(self, engine, data):
        """Push bytes, waiting whenever the 4-byte TX FIFO is full."""
        full_bit = 1 << (4 + 2 * engine)  # TX FIFO is 8 deep
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

    def pad(self, n):
        """What the pad sees: our output when driving, else the input we drive."""
        if self.oe(n):
            return self.out(n)
        return (self.val >> n) & 1

    async def mirror(self, pull_up=0xFF):
        """Make uio_in follow the pads, like real silicon (undriven pins pulled up)."""
        ext = self.val | pull_up
        while True:
            await RisingEdge(self.dut.clk)
            oe = int(self.dut.uio_oe.value)
            out = int(self.dut.uio_out.value)
            self.dut.uio_in.value = (out & oe) | (ext & ~oe & 0xFF)

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


class I2CDevice:
    """I2C device model: a 256-byte memory at `addr`, like a small EEPROM.

    Write [reg, data...] sets the pointer then stores bytes; read returns bytes
    from the pointer. Holds SCL low for `stretch` clocks after each byte (clock
    stretching), or forever when `stuck` is set. Records START / STOP events.
    """

    def __init__(self, dut, pins, sda, scl, addr=0x50, stretch=0):
        self.dut, self.pins, self.sda, self.scl = dut, pins, sda, scl
        self.addr, self.stretch = addr, stretch
        self.mem = [0] * 256
        self.events = []
        self.stuck = False
        self.violations = []
        self.stretched = 0  # clocks the master spent waiting on our SCL hold

    async def run(self):
        p, sda, scl = self.pins, self.sda, self.scl
        sda_low = scl_low = False
        hold = 0
        prev_sda = prev_scl = 1
        state, bits, shift, ptr, first, rw, tx, master_ack = "idle", 0, 0, 0, True, 0, 0, 1
        while True:
            await RisingEdge(self.dut.clk)
            if hold:
                hold -= 1
                scl_low = hold > 0
            if self.stuck:
                scl_low = True
            bus_sda = int(not p.oe(sda) and not sda_low)
            bus_scl = int(not p.oe(scl) and not scl_low)
            if scl_low and not p.oe(scl):
                self.stretched += 1
            # master must only drive 0 (open drain)
            if (p.oe(sda) and p.out(sda)) or (p.oe(scl) and p.out(scl)):
                self.violations.append("drove a line high")
            p.drive(sda, bus_sda)
            p.drive(scl, bus_scl)
            rise = not prev_scl and bus_scl
            fall = prev_scl and not bus_scl
            if prev_scl and bus_scl and prev_sda != bus_sda:
                if bus_sda == 0:
                    self.events.append("S")
                    state, bits, shift, sda_low = "addr", 0, 0, False
                else:
                    self.events.append("P")
                    state, sda_low = "idle", False
            elif rise:
                if state in ("addr", "wr") and bits < 8:
                    shift = ((shift << 1) | bus_sda) & 0xFF
                    bits += 1
                elif state == "rd":
                    bits += 1
                    if bits == 9:
                        master_ack = bus_sda
            elif fall:
                if state in ("addr", "wr"):
                    if bits == 8:
                        ack = True
                        if state == "addr":
                            ack = (shift >> 1) == self.addr
                            rw = shift & 1
                            self.events.append(f"A{shift:02x}{'+' if ack else '-'}")
                            if not ack:
                                state = "ignore"
                        else:
                            if first:
                                ptr, first = shift, False
                            else:
                                self.mem[ptr] = shift
                                ptr = (ptr + 1) & 0xFF
                        sda_low = ack
                        bits = 9
                        if self.stretch:
                            hold, scl_low = self.stretch, True
                    elif bits == 9:
                        sda_low, bits, shift = False, 0, 0
                        if state == "addr" and rw:
                            state, tx = "rd", self.mem[ptr]
                            sda_low = not ((tx >> 7) & 1)
                        elif state == "addr":
                            state, first = "wr", True
                elif state == "rd":
                    if bits < 8:
                        sda_low = not ((tx >> (7 - bits)) & 1)
                    elif bits == 8:
                        sda_low = False
                    else:
                        ptr = (ptr + 1) & 0xFF
                        if master_ack == 0:
                            tx, bits = self.mem[ptr], 0
                            sda_low = not ((tx >> 7) & 1)
                        else:
                            state, sda_low = "done", False
                        if self.stretch:
                            hold, scl_low = self.stretch, True
                elif state == "ignore":
                    sda_low = False
            prev_sda, prev_scl = bus_sda, bus_scl

"""I2C target tests: PinWeaver plays an I2C device that a host script controls.

Engine 0 runs the i2c_master program, engine 1 runs i2c_target on the same two
wires, and a plain I2CDevice model (address 0x51) shares the bus. The host
script below (Emulator) decides every ACK and every byte the target sends, so
it can also misbehave: refuse its address, answer slowly, or never answer.
"""
import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

from pw_driver import I2CDevice, load_program
from test import start
from test_i2c import I2C_PINS, READ_ACK, READ_NACK, SCL, SDA, START, STOP, run, w

ADDR = 0x50                      # the emulated device
OTHER = 0x51                     # the I2CDevice model on the same bus
FLAG_ADDR, FLAG_STOP, FLAG_BUSERR = 0b0010, 0b0100, 0b0001
# The master waits at most 31 of its ticks for a stretched SCL; slow it down so
# the host script (talking SPI) can answer in time, like a real 100 kHz bus.
MASTER = dict(I2C_PINS, clkdiv=255)
TARGET = dict(clkdiv=0, set_base=SDA, set_count=1, out_base=SDA, out_count=1,
              side_base=SCL, in_base=SDA, jmp_pin=SDA)
ACK, NACK, ACK_READ = 0x00, 0x80, 0x40


class Emulator:
    """A host script that makes the target act like a 256-byte EEPROM."""

    def __init__(self, host, addr=ADDR, delay=0, present=True, hang_after=None):
        self.host, self.addr = host, addr
        self.delay, self.present, self.hang_after = delay, present, hang_after
        self.mem = [0] * 256
        self.log = []                # what the target reported: ("A", byte) / ("D", byte) / ("K", ack)
        self.ptr, self.first, self.sending = 0, False, False

    async def reply(self, *data):
        if self.delay:
            await ClockCycles(self.host.dut.clk, self.delay)
        for b in data:
            await self.host.xfer([0x04, 1, b])

    async def run(self):
        host = self.host
        while True:
            st = await host.status()
            if not st & 0x80:                    # engine 1 has nothing for us
                await ClockCycles(host.dut.clk, 20)
                continue
            b = (await host.pop(1, 1))[0]
            if self.sending:                     # the master's ACK bit after a byte we sent
                self.log.append(("K", b))
                if b == 0:
                    await self.reply(self.mem[self.ptr])
                    self.ptr = (self.ptr + 1) & 0xFF
                else:
                    self.sending = False
            elif st & FLAG_ADDR:                 # an address byte
                self.log.append(("A", b))
                if b >> 1 != self.addr or not self.present:
                    await self.reply(NACK)
                elif b & 1:                      # the master reads
                    self.sending = True
                    await self.reply(ACK_READ, self.mem[self.ptr])
                    self.ptr = (self.ptr + 1) & 0xFF
                else:
                    self.first = True
                    await self.reply(ACK)
            else:                                # a data byte the master wrote
                self.log.append(("D", b))
                if self.hang_after is not None and len(self.log) > self.hang_after:
                    return                       # never answer: SCL stays low
                if self.first:
                    self.ptr, self.first = b, False
                else:
                    self.mem[self.ptr] = b
                    self.ptr = (self.ptr + 1) & 0xFF
                await self.reply(ACK)


async def setup(dut, **emu_args):
    host, pins = await start(dut)
    dev = I2CDevice(dut, pins, SDA, SCL, addr=OTHER)      # also the bus pull-ups
    cocotb.start_soon(dev.run())
    await host.load(0, load_program("i2c_master", origin=0), **MASTER)
    target = load_program("i2c_target", origin=64)
    assert len(target.words) <= 64, f"i2c_target is {len(target.words)} words"
    await host.load(1, target, **TARGET)
    await host.ctrl(enable=0b11, restart=0b11)
    emu = Emulator(host, **emu_args)
    cocotb.start_soon(emu.run())
    return host, pins, dev, emu


async def longest_scl_low(dut, pins, out):
    """Track the longest time SCL stays low (to see clock stretching)."""
    run_len = 0
    while True:
        await RisingEdge(dut.clk)
        if pins.oe(SCL):
            run_len += 1
            out[0] = max(out[0], run_len)
        else:
            run_len = 0


async def eeprom_write_read(host, emu):
    acks = await run(host, [START] + w(ADDR << 1, 0x10, 0xDE, 0xAD, 0x42) + [STOP], 5, 400000)
    assert acks == [0] * 5, f"write ACKs {acks}"
    assert emu.mem[0x10:0x13] == [0xDE, 0xAD, 0x42], emu.mem[0x10:0x13]
    got = await run(host, [START] + w(ADDR << 1, 0x10) + [START] + w(ADDR << 1 | 1)
                    + [READ_ACK, READ_ACK, READ_NACK, STOP], 6, 600000)
    assert got[:3] == [0, 0, 0], f"ACKs {got[:3]}"
    assert got[3:] == [0xDE, 0xAD, 0x42], f"read {[hex(b) for b in got[3:]]}"


@cocotb.test()
async def test_i2c_target_eeprom(dut):
    """The master writes three bytes to the emulated EEPROM and reads them back."""
    host, pins, dev, emu = await setup(dut)
    await eeprom_write_read(host, emu)
    for _ in range(50):                         # the script may still be reading the last ACK bit
        if len(emu.log) >= 11:
            break
        await ClockCycles(dut.clk, 200)
    assert emu.log == [("A", 0xA0), ("D", 0x10), ("D", 0xDE), ("D", 0xAD), ("D", 0x42),
                       ("A", 0xA0), ("D", 0x10), ("A", 0xA1), ("K", 0), ("K", 0), ("K", 1)], emu.log
    st = await host.status()
    assert st & FLAG_STOP, "the target saw the STOPs"
    assert not st & FLAG_BUSERR, "no bus error"
    assert not dev.violations, dev.violations


@cocotb.test()
async def test_i2c_target_shares_the_bus(dut):
    """Traffic for another device (0x51) is NACKed by the target and handled by that device."""
    host, pins, dev, emu = await setup(dut)
    acks = await run(host, [START] + w(OTHER << 1, 0x20, 0x77) + [STOP], 3, 400000)
    assert acks == [0, 0, 0], f"the other device ACKs: {acks}"
    assert dev.mem[0x20] == 0x77
    assert emu.log == [("A", OTHER << 1)], f"the target only saw the address: {emu.log}"
    await eeprom_write_read(host, emu)          # and the target still works afterwards
    assert dev.mem[0x10:0x13] == [0, 0, 0], "the other device ignored the EEPROM traffic"


@cocotb.test()
async def test_i2c_target_absent(dut):
    """Fault: the device refuses its own address; the master gets a NACK."""
    host, pins, dev, emu = await setup(dut, present=False)
    got = await run(host, [START] + w(ADDR << 1) + [STOP], 1, 200000)
    assert got == [1], "address NACKed"
    assert not (await host.status()) & FLAG_BUSERR


@cocotb.test()
async def test_i2c_target_slow(dut):
    """Fault: the device answers slowly; clock stretching makes the master wait."""
    host, pins, dev, emu = await setup(dut, delay=1500)
    low = [0]
    mon = cocotb.start_soon(longest_scl_low(dut, pins, low))
    await eeprom_write_read(host, emu)
    mon.cancel()
    dut._log.info(f"longest SCL low: {low[0]} clocks")
    assert low[0] > 1500, "the target should have held SCL while the host thought"
    assert not (await host.status()) & FLAG_BUSERR


@cocotb.test()
async def test_i2c_target_hangs(dut):
    """Fault: the device stops answering mid-write. The master's stuck-bus timeout fires,
    and restarting the target frees the bus."""
    host, pins, dev, emu = await setup(dut, hang_after=2)
    await host.push(0, [START] + w(ADDR << 1, 0x10, 0x55))
    for _ in range(200):
        if (await host.status()) & FLAG_BUSERR:
            break
        await ClockCycles(dut.clk, 500)
    assert (await host.status()) & FLAG_BUSERR, "the master reports a stuck bus"
    assert pins.oe(SCL), "the hung target is still holding SCL"
    await host.ctrl(enable=0b11, restart=0b10)  # restart only the target
    await ClockCycles(dut.clk, 10)
    assert not pins.oe(SCL) and not pins.oe(SDA), "bus released"

"""I2C master tests: the i2c_master program talking to an I2C memory device model."""
import cocotb
from cocotb.triggers import ClockCycles

from pw_driver import I2CDevice, load_program
from test import start

SDA, SCL = 6, 7
START, STOP, WRITE, READ_ACK, READ_NACK = 0x80, 0xC0, 0x00, 0x60, 0x40
I2C_PINS = dict(clkdiv=3, set_base=SDA, set_count=1, out_base=SDA, out_count=1,
                side_base=SCL, in_base=SDA)


async def setup(dut, **device_args):
    host, pins = await start(dut)
    dev = I2CDevice(dut, pins, SDA, SCL, **device_args)
    cocotb.start_soon(dev.run())
    await host.load(0, load_program("i2c_master"), **I2C_PINS)
    await host.ctrl(enable=0b01, restart=0b01)
    return host, dev


def w(*data):
    """Commands to write each byte."""
    cmds = []
    for b in data:
        cmds += [WRITE, b]
    return cmds


async def run(host, cmds, n_results, max_clocks=40000):
    """Send commands while collecting results (the RX FIFO only holds 4)."""
    sender = cocotb.start_soon(host.push(0, cmds))
    results = []
    waited = 0
    while len(results) < n_results:
        if await host.rx_ready(0):
            results += await host.pop(0, 1)
        else:
            await ClockCycles(host.dut.clk, 50)
            waited += 50
            assert waited < max_clocks, f"timed out with results {results}"
    await sender
    return results


async def write_then_read(host, dev):
    acks = await run(host, [START] + w(0xA0, 0x10, 0xDE, 0xAD, 0x42) + [STOP], 5)
    assert acks == [0] * 5, f"write ACKs {acks}"
    assert dev.mem[0x10:0x13] == [0xDE, 0xAD, 0x42]
    got = await run(host, [START] + w(0xA0, 0x10) + [START] + w(0xA1)
                    + [READ_ACK, READ_ACK, READ_NACK, STOP], 6)
    assert got[:3] == [0, 0, 0], f"ACKs {got[:3]}"
    assert got[3:] == [0xDE, 0xAD, 0x42], f"read {[hex(b) for b in got[3:]]}"
    await ClockCycles(host.dut.clk, 300)
    assert dev.events == ["S", "Aa0+", "P", "S", "Aa0+", "S", "Aa1+", "P"], dev.events
    assert not dev.violations, dev.violations


@cocotb.test()
async def test_i2c_write_read(dut):
    """Write three bytes to a memory device, then read them back with a repeated START."""
    host, dev = await setup(dut)
    await write_then_read(host, dev)
    assert (await host.status()) & 1 == 0, "no bus error"


@cocotb.test()
async def test_i2c_clock_stretching(dut):
    """The same transfer works when the device holds SCL low after every byte."""
    host, dev = await setup(dut, stretch=60)
    await write_then_read(host, dev)
    dut._log.info(f"master waited {dev.stretched} clocks on a stretched SCL")
    assert dev.stretched > 200, "the master should have waited for SCL"
    assert (await host.status()) & 1 == 0, "no bus error"


@cocotb.test()
async def test_i2c_nack(dut):
    """Nobody at address 0x58: the address byte is NACKed."""
    host, dev = await setup(dut)
    got = await run(host, [START] + w(0xB0) + [STOP], 1)
    assert got == [1]
    await ClockCycles(dut.clk, 300)
    assert dev.events == ["S", "Ab0-", "P"], dev.events


@cocotb.test()
async def test_i2c_stuck_bus(dut):
    """A device holding SCL low forever trips the timeout and raises flag 0."""
    host, dev = await setup(dut)
    dev.stuck = True
    await host.push(0, [START])
    await ClockCycles(dut.clk, 400)
    assert (await host.status()) & 1 == 1, "bus error flag should be set"
    assert int(dut.uio_oe.value) & ((1 << SDA) | (1 << SCL)) == 0, "lines released"

"""SWD tests: the swd program talking to an ARM debug port model."""
import cocotb
from cocotb.triggers import ClockCycles

import swd
from pw_driver import load_program
from test import start

SWCLK, SWDIO = 4, 5
SWD_PINS = dict(clkdiv=1, set_base=SWCLK, set_count=2, side_base=SWCLK,
                out_base=SWDIO, out_count=1, in_base=SWDIO,
                out_shr=1, in_shr=1, autopull=1, autopush=1,
                crc_poly=0x8000, crc_init=0, crc_on_out=1, crc_on_in=1)


async def setup(dut):
    host, pins = await start(dut)
    pins.drive(SWDIO, 1)                         # pull-up
    target = swd.SwdTarget(dut, pins, SWCLK, SWDIO)
    cocotb.start_soon(target.run())
    await host.load(0, load_program("swd"), **SWD_PINS)
    await host.ctrl(enable=0b01, restart=0b01)
    # line reset: 56 cycles high, then 8 idle cycles low
    await host.push(0, [2, 63] + [0xFF] * 7 + [0x00])
    return host, target


async def pop_n(host, n, max_polls=300):
    got = []
    for _ in range(max_polls):
        if await host.rx_ready(0):
            got += await host.pop(0, 1)
            if len(got) == n:
                return got
        else:
            await ClockCycles(host.dut.clk, 40)
    raise AssertionError(f"only got {got}")


async def swd_read(host, apndp, addr):
    await host.push(0, [0, swd.request(apndp, 1, addr)])
    ack = (await pop_n(host, 1))[0]
    if ack != swd.OK:
        return ack, None
    r = await pop_n(host, 6)
    value = int.from_bytes(bytes(r[:4]), "little")
    rx_parity, our_parity = r[4] & 1, r[5] >> 7
    assert rx_parity == swd.parity(value), "parity bit from the target"
    assert our_parity == swd.parity(value), "parity computed by the CRC unit"
    return ack, value


async def swd_write(host, apndp, addr, value):
    await host.push(0, [1, swd.request(apndp, 0, addr)])
    ack = (await pop_n(host, 1))[0]
    if ack == swd.OK:
        await host.push(0, list(value.to_bytes(4, "little")))
    return ack


@cocotb.test()
async def test_swd_idcode(dut):
    """Line reset, then read the debug port's IDCODE."""
    host, target = await setup(dut)
    ack, value = await swd_read(host, 0, 0x0)
    assert (ack, value) == (swd.OK, swd.IDCODE), (ack, hex(value or 0))
    assert not target.errors, target.errors


@cocotb.test()
async def test_swd_write_read(dut):
    """Write DP SELECT and an AP register, then read the AP register back."""
    host, target = await setup(dut)
    assert await swd_write(host, 0, 0x8, 0x0000_00F0) == swd.OK
    assert await swd_write(host, 1, 0x4, 0xDEAD_BEEF) == swd.OK
    assert await swd_write(host, 1, 0xC, 0x8000_0001) == swd.OK
    ack, value = await swd_read(host, 1, 0x4)
    assert (ack, value) == (swd.OK, 0xDEAD_BEEF), (ack, hex(value or 0))
    ack, value = await swd_read(host, 1, 0xC)
    assert value == 0x8000_0001
    await ClockCycles(dut.clk, 200)
    writes = [e for e in target.log if e[0] == "write"]
    assert writes == [("write", 0, 0x8, 0xF0), ("write", 1, 0x4, 0xDEADBEEF),
                      ("write", 1, 0xC, 0x80000001)], target.log
    assert not target.errors, target.errors     # includes the write parity check


@cocotb.test()
async def test_swd_wait(dut):
    """A busy target answers WAIT: no data phase, and the next try works."""
    host, target = await setup(dut)
    target.wait_next = 1
    ack, _ = await swd_read(host, 0, 0x0)
    assert ack == swd.WAIT, ack
    ack, value = await swd_read(host, 0, 0x0)
    assert (ack, value) == (swd.OK, swd.IDCODE)
    assert not target.errors, target.errors

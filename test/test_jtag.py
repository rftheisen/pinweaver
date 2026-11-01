"""JTAG tests: the jtag vector program driving TAP models."""
import cocotb
from cocotb.triggers import ClockCycles

import jtag
from jtag import from_bits, pack, reset_to_idle, shift, to_bits, unpack
from pw_driver import load_program
from test import start
from test_swd import pop_n

TCK, TMS, TDI, TDO = 0, 1, 2, 3
JTAG_PINS = dict(clkdiv=1, set_base=TCK, set_count=3, side_base=TCK,
                 out_base=TMS, out_count=2, in_base=TDO,
                 out_shr=1, in_shr=1, autopull=1, autopush=1)
ID_A, ID_B = 0x4BA00477, 0x16D4A093


async def setup(dut, taps):
    host, pins = await start(dut)
    chain = jtag.JtagChain(dut, pins, TCK, TMS, TDI, TDO, taps)
    cocotb.start_soon(chain.run())
    await host.load(0, load_program("jtag"), **JTAG_PINS)
    await host.ctrl(enable=0b01, restart=0b01)
    return host


async def run(host, vectors):
    """Clock the vectors (up to 256 per command); returns TDO for every cycle."""
    tdo = []
    for i in range(0, len(vectors), 256):
        part = vectors[i:i + 256]
        sender = cocotb.start_soon(host.push(0, pack(part)))
        got = await pop_n(host, len(part) // 8 + 1)
        await sender
        tdo += unpack(got, len(part))
    return tdo


async def shift_dr(host, bits):
    vec, first = shift(bits)
    tdo = await run(host, vec)
    return tdo[first:first + len(bits)]


async def shift_ir(host, bits):
    vec, first = shift(bits, ir=True)
    tdo = await run(host, vec)
    return tdo[first:first + len(bits)]


@cocotb.test()
async def test_jtag_idcode(dut):
    """After a TAP reset the IDCODE instruction is selected: read the 32-bit ID."""
    tap = jtag.Tap(ID_A)
    host = await setup(dut, [tap])
    await run(host, reset_to_idle())
    assert tap.state == "idle"
    got = from_bits(await shift_dr(host, [0] * 32))
    assert got == ID_A, hex(got)


@cocotb.test()
async def test_jtag_user_register_and_bypass(dut):
    """Load an instruction, write and read back a data register; BYPASS delays one bit."""
    tap = jtag.Tap(ID_A)
    host = await setup(dut, [tap])
    await run(host, reset_to_idle())
    captured = await shift_ir(host, to_bits(jtag.USER, 4))
    assert from_bits(captured) & 0b11 == 0b01, "IR capture ends in 01"
    await shift_dr(host, to_bits(0xA5, 8))
    assert tap.user == 0xA5
    assert from_bits(await shift_dr(host, to_bits(0x3C, 8))) == 0xA5, "old value comes out"
    assert tap.user == 0x3C
    await shift_ir(host, to_bits(jtag.BYPASS, 4))
    pattern = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1]
    out = await shift_dr(host, pattern)
    assert out == [0] + pattern[:-1], f"bypass is one bit: {out}"


@cocotb.test()
async def test_jtag_two_chip_chain(dut):
    """Two TAPs in a chain: one 64-bit DR scan reads both IDCODEs."""
    a, b = jtag.Tap(ID_A), jtag.Tap(ID_B)
    host = await setup(dut, [a, b])
    await run(host, reset_to_idle())
    out = await shift_dr(host, [0] * 64)
    # the TAP nearest TDO comes out first
    assert from_bits(out[:32]) == ID_B and from_bits(out[32:]) == ID_A, \
        (hex(from_bits(out[:32])), hex(from_bits(out[32:])))
    # put the far TAP in BYPASS: IR scan is 8 bits (TAP b's IR comes first)
    await shift_ir(host, to_bits(jtag.IDCODE, 4) + to_bits(jtag.BYPASS, 4))
    assert a.ir == jtag.BYPASS and b.ir == jtag.IDCODE, (a.ir, b.ir)
    out = await shift_dr(host, [0] * 33)
    assert from_bits(out[:32]) == ID_B, hex(from_bits(out[:32]))
    await ClockCycles(dut.clk, 10)

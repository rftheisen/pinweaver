"""PS/2 tests: a keyboard model sends scancodes; the engine follows its clock."""
import cocotb
from cocotb.triggers import ClockCycles, Timer

from pw_driver import load_program
from test import start

DATA, CLK = 6, 7
PS2_PINS = dict(in_base=DATA, jmp_pin=DATA, in_shr=1, autopush=1,
                crc_poly=0x8000, crc_init=0, crc_on_in=1)


async def keyboard_send(pins, byte, half_us=20, bad_parity=False):
    """One PS/2 frame from the device: data changes while CLK is high."""
    par = 1 ^ (bin(byte).count("1") & 1)            # odd parity
    if bad_parity:
        par ^= 1
    bits = [0] + [(byte >> i) & 1 for i in range(8)] + [par, 1]
    for b in bits:
        pins.drive(DATA, b)
        await Timer(half_us // 2, unit="us")
        pins.drive(CLK, 0)                           # host reads on this fall
        await Timer(half_us, unit="us")
        pins.drive(CLK, 1)
        await Timer(half_us - half_us // 2, unit="us")
    await Timer(4 * half_us, unit="us")


@cocotb.test()
async def test_ps2_scancodes(dut):
    """'A' pressed and released (1C, F0 1C), then a frame with a bad parity bit."""
    host, pins = await start(dut)
    pins.drive(DATA, 1)
    pins.drive(CLK, 1)
    await host.load(0, load_program("ps2_rx"), **PS2_PINS)
    await host.ctrl(enable=0b01, restart=0b01)
    await ClockCycles(dut.clk, 20)
    for code in (0x1C, 0xF0, 0x1C):
        await keyboard_send(pins, code)
    await keyboard_send(pins, 0x5A, bad_parity=True)
    got = await host.pop(0, 8)
    codes, status = got[0::2], got[1::2]
    assert codes == [0x1C, 0xF0, 0x1C, 0x5A], [hex(c) for c in codes]
    assert [s >> 7 for s in status] == [1, 1, 1, 0], "parity OK for 3, bad for the last"

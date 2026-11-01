"""Hardware CRC tests: standard CRCs over "123456789" must give their published check values."""
import cocotb

from pw_driver import load_program
from test import start
from test_i2c import run

CHECK = b"123456789"


def rev(v, n):
    return int(f"{v:0{n}b}"[::-1], 2)


async def chip_crc(dut, poly, width, init, lsb_first, data=CHECK):
    host, _ = await start(dut)
    # an n-bit CRC sits in the top n bits of the 16-bit register
    shift = 16 - width
    await host.load(0, load_program("crc_demo"), crc_poly=poly << shift, crc_init=init << shift,
                    crc_on_out=1, out_shr=int(lsb_first))
    await host.ctrl(enable=0b01, restart=0b01)
    hi, lo = await run(host, [len(data) - 1] + list(data), 2)
    return ((hi << 8) | lo) >> shift


@cocotb.test()
async def test_crc16_xmodem(dut):
    assert await chip_crc(dut, 0x1021, 16, 0, False) == 0x31C3


@cocotb.test()
async def test_crc15_can(dut):
    assert await chip_crc(dut, 0x4599, 15, 0, False) == 0x059E


@cocotb.test()
async def test_crc16_arc_lsb_first(dut):
    assert rev(await chip_crc(dut, 0x8005, 16, 0, True), 16) == 0xBB3D


@cocotb.test()
async def test_crc5_usb(dut):
    assert rev(await chip_crc(dut, 0x05, 5, 0x1F, True), 5) ^ 0x1F == 0x19


@cocotb.test()
async def test_crc16_usb(dut):
    assert rev(await chip_crc(dut, 0x8005, 16, 0xFFFF, True), 16) ^ 0xFFFF == 0xB4C8

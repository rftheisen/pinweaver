"""Two big programs at once: I2C in page 0 on engine 0, USB in page 1 on engine 1."""
import cocotb
from cocotb.triggers import ClockCycles

import usb
from pw_driver import I2CDevice, load_program
from test import start
from test_i2c import I2C_PINS, SCL, SDA, write_then_read
from test_usb import BIT, DATA0, DM, PAYLOAD_TX, USB_TX, record


@cocotb.test()
async def test_two_pages_i2c_and_usb(dut):
    host, pins = await start(dut)
    dev = I2CDevice(dut, pins, SDA, SCL)
    cocotb.start_soon(dev.run())
    pins.drive(DM, 1)

    i2c = load_program("i2c_master", origin=0)
    usb_tx = load_program("usb_tx", origin=64)
    assert len(i2c.words) + len(usb_tx.words) > 64, "this needs both pages"
    await host.load(0, i2c, **I2C_PINS)
    await host.load(1, usb_tx, **USB_TX)
    await host.ctrl(enable=0b11, restart=0b11)

    line = []
    rec = cocotb.start_soon(record(dut, pins, line, 400000))
    await host.push(1, [len(PAYLOAD_TX) * 8 - 1, DATA0] + PAYLOAD_TX)
    await host.flags(set_mask=0b0100)
    await write_then_read(host, dev)          # I2C traffic while USB sends
    for _ in range(100):
        if usb.SE0 in line and not pins.oe(DM):
            break
        await ClockCycles(dut.clk, 100)
    rec.cancel()
    pid, payload, _ = usb.decode(line, BIT)
    assert (pid, payload) == (DATA0, PAYLOAD_TX)
    _, pc0, pc1 = await host.debug()
    assert pc0 < 64 <= pc1, f"engines in their own pages: pc0={pc0} pc1={pc1}"

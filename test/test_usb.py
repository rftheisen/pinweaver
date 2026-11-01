"""Low-speed USB tests: packets on the wire, checked by an independent decoder."""
import cocotb
from cocotb.triggers import ClockCycles, RisingEdge, Timer

import usb
from pw_driver import load_program
from test import start

DM, DP = 0, 1                 # D- and D+
BIT = 33                      # ticks per bit at 50 MHz (1.5 Mbit/s is 33.33 clocks)
DATA0 = 0xC3
RX_ORIGIN = 44

USB_TX = dict(out_base=DM, out_count=2, set_base=DM, set_count=2, out_shr=1, autopull=1,
              crc_poly=0x8005, crc_init=0xFFFF, crc_on_out=1,
              lc_nrzi=1, lc_diff=1, lc_bitclk=1, lc_stuff_n=6, lc_bit_m1=BIT - 1, lc_sample=0)
USB_RX = dict(in_base=DM, in_shr=1, autopush=1,
              lc_nrzi=1, lc_diff=1, lc_bitclk=1, lc_resync=1, lc_stuff_n=6,
              lc_bit_m1=BIT - 1, lc_sample=14)

# lots of 1s so bit stuffing has to happen
PAYLOAD_TX = [0xFF, 0xFF, 0x3F, 0x00, 0x81, 0x7E]
PAYLOAD_RX = [0xFF, 0x7F, 0x00, 0xA5]


async def record(dut, pins, out, clocks):
    for _ in range(clocks):
        await RisingEdge(dut.clk)
        out.append(usb.state(pins.pad(DM), pins.pad(DP)))


async def send_packet(host, pid, payload):
    await host.push(0, [len(payload) * 8 - 1, pid] + payload)
    await host.flags(set_mask=0b0100)       # flag 2: go


@cocotb.test()
async def test_usb_tx_packet(dut):
    """The engine sends a DATA0 packet; the decoder checks timing, NRZI, stuffing, CRC and EOP."""
    host, pins = await start(dut)
    pins.drive(DM, 1)
    await host.load(0, load_program("usb_tx"), **USB_TX)
    await host.ctrl(enable=0b01, restart=0b01)
    line = []
    rec = cocotb.start_soon(record(dut, pins, line, 200000))
    await send_packet(host, DATA0, PAYLOAD_TX)
    for _ in range(400):                    # until the bus is released
        await ClockCycles(dut.clk, 100)
        if line and line[-1] == usb.J and usb.SE0 in line and not pins.oe(DM):
            break
    rec.cancel()
    pid, payload, info = usb.decode(line, BIT)
    assert (pid, payload) == (DATA0, PAYLOAD_TX), (hex(pid), payload)
    assert info["stuffed"] >= 2, f"expected bit stuffing, got {info}"
    dut._log.info(f"decoded DATA0 {payload}, {info['stuffed']} stuff bits, {info['bits']} bits on the wire")


async def drive(pins, states, bit_ns):
    for s in states:
        dm, dp = {usb.J: (1, 0), usb.K: (0, 1), usb.SE0: (0, 0)}[s]
        pins.drive(DM, dm)
        pins.drive(DP, dp)
        await Timer(round(bit_ns * 1000), unit="ps")


async def receive(host, dut, n):
    for _ in range(300):
        if (await host.status()) & 0b0010:  # flag 1: packet done
            await host.flags(clr_mask=0b0010)
            return await host.pop(1, n)
        await ClockCycles(dut.clk, 100)
    raise AssertionError("no packet")


@cocotb.test()
async def test_usb_rx_packet(dut):
    """The engine receives a packet sent at true 1.5 Mbit/s (its own bit clock is 1% off)."""
    host, pins = await start(dut)
    pins.drive(DM, 1)
    pins.drive(DP, 0)
    await host.load(1, load_program("usb_rx", RX_ORIGIN), **USB_RX)
    await host.ctrl(enable=0b10, restart=0b10)
    await ClockCycles(dut.clk, 50)
    for speed in (1.0, 1.012, 0.988):       # also +-1.2% clock error on the sender
        await drive(pins, usb.encode(DATA0, PAYLOAD_RX), 1000 / 1.5 * speed)
        await Timer(5, unit="us")
        got = await receive(host, dut, 8)
        assert got == usb.packet_bytes(DATA0, PAYLOAD_RX), f"speed {speed}: got {[hex(b) for b in got]}"


@cocotb.test()
async def test_usb_loopback(dut):
    """Engine 0 sends, engine 1 receives the same pins."""
    host, pins = await start(dut)
    cocotb.start_soon(pins.mirror(pull_up=1 << DM))
    await host.load(0, load_program("usb_tx"), **USB_TX)
    await host.load(1, load_program("usb_rx", RX_ORIGIN), **USB_RX)
    await host.ctrl(enable=0b11, restart=0b11)
    await ClockCycles(dut.clk, 50)
    await send_packet(host, DATA0, PAYLOAD_RX)
    got = await receive(host, dut, 8)
    assert got == usb.packet_bytes(DATA0, PAYLOAD_RX), [hex(b) for b in got]

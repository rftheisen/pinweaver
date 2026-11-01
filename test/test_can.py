"""CAN tests: frames, ACK, and two engines arbitrating for the same bus."""
import cocotb
from cocotb.triggers import ClockCycles

import can
from pw_driver import load_program
from test import start

TX0, RX, TX1 = 0, 1, 2


def can_pins(tx):
    return dict(clkdiv=4, out_base=tx, out_count=1, set_base=tx, set_count=1, in_base=RX,
                out_shr=0, autopull=1, crc_poly=0x4599 << 1, crc_init=0, crc_on_out=1,
                lc_bitclk=1, lc_stuff_n=5, lc_stuff_any=1, lc_bit_m1=19, lc_sample=14, lc_arb=1)


async def setup(dut, ack=True, both=False):
    host, pins = await start(dut)
    pins.drive(RX, 1)
    bus = can.CanBus(dut, pins, [TX0, TX1], RX, clocks_per_bit=100, ack=ack)
    cocotb.start_soon(bus.run())
    await host.load(0, load_program("can_tx", 0), **can_pins(TX0))
    if both:
        await host.load(1, load_program("can_tx", 64), **can_pins(TX1))
    await host.ctrl(enable=0b11 if both else 0b01, restart=0b11 if both else 0b01)
    return host, bus


async def result(host, engine, max_polls=400):
    for _ in range(max_polls):
        if await host.rx_ready(engine):
            return (await host.pop(engine, 1))[0]
        await ClockCycles(host.dut.clk, 200)
    raise AssertionError(f"engine {engine} never reported")


@cocotb.test()
async def test_can_frame_acked(dut):
    """One frame with stuffing; the receiving node decodes it and ACKs; the engine sees the ACK."""
    host, bus = await setup(dut)
    data = [0x00, 0xFF, 0x55]
    await host.push(0, can.host_bytes(0x123, data))
    r = await result(host, 0)
    await ClockCycles(dut.clk, 1500)
    assert bus.frames and bus.frames[0][:3] == (0x123, data, True), bus.frames
    assert bus.frames[0][3] >= 2, "expected stuff bits"
    assert not bus.errors, bus.errors
    assert r & 0b100, f"engine should see the ACK, status {r:#04x}"


@cocotb.test()
async def test_can_no_ack(dut):
    """Nobody ACKs: the engine reports it."""
    host, bus = await setup(dut, ack=False)
    await host.push(0, can.host_bytes(0x042, [0xA5]))
    r = await result(host, 0)
    assert r != 0xFF and not (r & 0b100), f"status {r:#04x}"
    await ClockCycles(dut.clk, 1500)
    assert bus.frames and bus.frames[0][:3] == (0x042, [0xA5], True)


@cocotb.test()
async def test_can_arbitration(dut):
    """Both engines start the same bit. The lower ID wins untouched; the other
    backs off, reports the loss, and its frame goes out after a resend."""
    host, bus = await setup(dut, both=True)
    hi_id, lo_id = 0x456, 0x123
    await host.flags(set_mask=0b0100)            # hold both senders
    await host.push(0, can.host_bytes(hi_id, [0x11, 0x22]))
    await host.push(1, can.host_bytes(lo_id, [0x33]))
    await host.flags(clr_mask=0b0100)            # go, together
    r1 = await result(host, 1)
    r0 = await result(host, 0)
    assert r1 & 0b100 and r1 != 0xFF, f"winner (engine 1) status {r1:#04x}"
    assert r0 == 0xFF, f"loser (engine 0) should report lost arbitration, got {r0:#04x}"
    await host.push(0, can.host_bytes(hi_id, [0x11, 0x22]))
    r0 = await result(host, 0)
    assert r0 & 0b100 and r0 != 0xFF, f"resent frame status {r0:#04x}"
    await ClockCycles(dut.clk, 1500)
    got = [f[:3] for f in bus.frames]
    assert got == [(lo_id, [0x33], True), (hi_id, [0x11, 0x22], True)], got
    assert not bus.errors, bus.errors

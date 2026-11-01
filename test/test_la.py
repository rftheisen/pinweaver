"""Logic analyzer tests: capture into the instruction memory, then read it back."""
import cocotb
from cocotb.triggers import ClockCycles

from pw_driver import load_program, uart_recv
from test import UART_BIT, UART_TX_PINS, start

BUF_START, BUF_LAST = 16, 63          # 48 words = 96 samples; programs live below 16
STATE_DONE = 3


async def wait_done(host, max_polls=200):
    for _ in range(max_polls):
        state, *_ = await host.la_status()
        if state == STATE_DONE:
            return
    raise AssertionError("capture never finished")


@cocotb.test()
async def test_la_captures_uart(dut):
    """Trigger on the UART start bit, then decode the byte from the captured samples."""
    host, pins = await start(dut)
    cocotb.start_soon(pins.mirror())
    await host.load(0, load_program("uart_tx"), **UART_TX_PINS)
    await host.ctrl(enable=0b01, restart=0b01)
    await ClockCycles(dut.clk, 100)
    # 4 samples per UART bit; trigger when GPIO 0 is low
    per_bit = 4
    await host.la_config(BUF_START, BUF_LAST, post=24, div=UART_BIT // per_bit - 1,
                         mask=0x01, value=0x00, mode=1)
    await host.ctrl(enable=0b01, la_arm=1)
    rx = cocotb.start_soon(uart_recv(dut, pins, 0, 1, UART_BIT))
    await host.push(0, [0xA3])
    assert await rx == [0xA3]
    await wait_done(host)
    samples, trig = await host.la_samples(BUF_START, BUF_LAST)
    line = [s & 1 for s in samples]
    assert trig is not None and line[trig] == 0
    assert all(b == 1 for b in line[trig - 20:trig]), "idle high before the start bit"
    # decode from the samples, in the middle of each bit
    v = 0
    for i in range(8):
        v |= line[trig + per_bit * (i + 1) + per_bit // 2] << i
    assert v == 0xA3, f"decoded {v:#04x} from the capture"
    assert line[trig + per_bit * 9 + per_bit // 2] == 1, "stop bit"


@cocotb.test()
async def test_la_firmware_trigger_and_pretrigger(dut):
    """A program raises the trigger; the ring keeps the samples from before it."""
    host, pins = await start(dut)
    await host.load(0, load_program("la_trigger"))
    await host.la_config(BUF_START, BUF_LAST, post=8, div=7, mode=3)   # mode 3 = flag 3
    await host.ctrl(enable=0b01, restart=0b01, la_arm=1)

    async def counter():
        n = 0
        while True:
            for b in range(6):
                pins.drive(b, (n >> b) & 1)
            n += 1
            await ClockCycles(dut.clk, 8)

    cocotb.start_soon(counter())
    await ClockCycles(dut.clk, 2000)          # long enough to wrap the ring
    pins.drive(7, 1)                          # the event the program watches for
    await wait_done(host)
    samples, trig = await host.la_samples(BUF_START, BUF_LAST)
    count = [s & 0x3F for s in samples]
    for a, b in zip(count, count[1:]):
        assert b == (a + 1) & 0x3F, f"ring out of order: {count}"
    first_high = next(i for i, s in enumerate(samples) if s & 0x80)
    assert 0 <= trig - first_high <= 4, f"trigger at {trig}, GPIO 7 rose at {first_high}"
    assert len(samples) - trig >= 16, "post-trigger samples kept"
    assert trig > 60, "most of the buffer is pre-trigger history"

"""PinWeaver tests: real protocols running as firmware on the pin engines."""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from pw_driver import Host, Pins, load_program, spi_device, uart_recv, uart_send

CLK_NS = 20          # 50 MHz
UART_DIV = 3         # tick every 4 clocks; 8 ticks per bit -> 32 clocks per bit
UART_BIT = 8 * (UART_DIV + 1)


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    dut.ena.value = 1
    pins = Pins(dut)
    host = Host(dut)
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)
    return host, pins


UART_TX_PINS = dict(clkdiv=UART_DIV, set_base=0, set_count=1, out_base=0, out_count=1, out_shr=1)
UART_RX_PINS = dict(clkdiv=UART_DIV, in_base=1, jmp_pin=1, in_shr=1)


@cocotb.test()
async def test_spi_host_interface(dut):
    """The host can write and read back engine state over SPI."""
    host, _ = await start(dut)
    status, pc0, pc1 = await host.debug()
    assert (pc0, pc1) == (0, 0)
    assert status & 0b1010_0000 == 0, "RX FIFOs should start empty"
    await host.flags(set_mask=0b0101)
    assert (await host.status()) & 0xF == 0b0101
    assert (int(dut.uo_out.value) >> 3) & 0xF == 0b0101, "flags show on uo_out[6:3]"
    await host.flags(clr_mask=0b0001)
    assert (await host.status()) & 0xF == 0b0100


@cocotb.test()
async def test_uart_tx(dut):
    """Engine 0 sends bytes as UART on GPIO 0."""
    host, pins = await start(dut)
    await host.load(0, load_program("uart_tx"), **UART_TX_PINS)
    await host.ctrl(enable=0b01, restart=0b01)
    message = list(b"Hi!\n") + [0x00, 0xFF, 0x55, 0xA3]
    rx = cocotb.start_soon(uart_recv(dut, pins, 0, len(message), UART_BIT))
    await host.push(0, message)
    got = await rx
    assert got == message, f"got {got}"


@cocotb.test()
async def test_uart_rx(dut):
    """Engine 1 receives UART on GPIO 1 and drops a frame with a bad stop bit."""
    host, pins = await start(dut)
    pins.drive(1, 1)
    await host.load(1, load_program("uart_rx", origin=20), **UART_RX_PINS)
    await host.ctrl(enable=0b10, restart=0b10)
    await ClockCycles(dut.clk, 50)
    await uart_send(dut, pins, 1, [0x41, 0x00, 0xFF], UART_BIT)
    await uart_send(dut, pins, 1, [0x77], UART_BIT, bad_stop=True)
    await uart_send(dut, pins, 1, [0xA5], UART_BIT)
    await ClockCycles(dut.clk, UART_BIT * 2)
    got = await host.pop(1, 4)
    assert got == [0x41, 0x00, 0xFF, 0xA5], f"got {[hex(b) for b in got]}"
    assert not await host.rx_ready(1)


@cocotb.test()
async def test_uart_loopback_two_engines(dut):
    """Both engines run at once: engine 0 sends on GPIO 0, wired to engine 1's RX on GPIO 1."""
    host, pins = await start(dut)
    pins.drive(1, 1)

    async def wire():
        while True:
            await RisingEdge(dut.clk)
            pins.drive(1, pins.out(0) if pins.oe(0) else 1)

    cocotb.start_soon(wire())
    await host.load(0, load_program("uart_tx"), **UART_TX_PINS)
    await host.load(1, load_program("uart_rx", origin=20), **UART_RX_PINS)
    await host.ctrl(enable=0b11, restart=0b11)
    message = list(b"PinWeaver!!!")
    got = []
    for i in range(0, len(message), 4):
        chunk = message[i:i + 4]
        await host.push(0, chunk)
        await ClockCycles(dut.clk, UART_BIT * 10 * len(chunk) + 200)
        got += await host.pop(1, len(chunk))
    assert bytes(got) == bytes(message), f"got {bytes(got)}"


@cocotb.test()
async def test_spi_master(dut):
    """Engine 0 is an SPI master (mode 0) talking to a device model."""
    host, pins = await start(dut)
    SCK, MOSI, MISO, CS = 2, 3, 4, 5
    replies = [0xC2, 0x5A, 0x01, 0xFF]
    received = []
    cocotb.start_soon(spi_device(dut, pins, SCK, MOSI, MISO, CS, replies, received))
    await host.load(0, load_program("spi_master"), clkdiv=1,
                    set_base=SCK, set_count=4, side_base=SCK,
                    out_base=MOSI, out_count=1, in_base=MISO)
    await host.ctrl(enable=0b01, restart=0b01)
    sent = [0x9F, 0x00, 0xA5, 0x3C]
    await host.push(0, sent)
    await ClockCycles(dut.clk, 400)
    got = await host.pop(0, 4)
    assert received == sent, f"device got {[hex(b) for b in received]}"
    assert got == replies, f"engine read {[hex(b) for b in got]}"
    assert pins.out(CS) == 1 and pins.out(SCK) == 0, "bus idle: CS high, SCK low"


@cocotb.test()
async def test_wait_timeout(dut):
    """WAIT ... timeout gives up after Y ticks; an arriving signal wins if it's in time."""
    host, pins = await start(dut)
    prog = load_program("timeout_demo")
    await host.load(0, prog, clkdiv=3, set_base=0, set_count=2)

    # nobody answers: pin 1 (timeout) after ~30 ticks (~120 clocks)
    await host.ctrl(enable=0b01, restart=0b01)
    clocks = 0
    while not (pins.oe(1) and pins.out(1)):
        await RisingEdge(dut.clk)
        clocks += 1
        assert clocks < 400, "never timed out"
    assert pins.out(0) == 0
    _, pc0, _ = await host.debug()
    assert pc0 == prog.labels["fail_hang"]

    # answer in time: pin 0
    await host.ctrl(enable=0b00)
    await host.ctrl(enable=0b01, restart=0b01)
    await ClockCycles(dut.clk, 10)
    pins.drive(4, 1)
    await ClockCycles(dut.clk, 60)
    assert pins.out(0) == 1 and pins.out(1) == 0
    _, pc0, _ = await host.debug()
    assert pc0 == prog.labels["ok_hang"]

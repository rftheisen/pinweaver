"""Memory read-back: it borrows engine 1's read port, pausing engine 1 meanwhile."""
import cocotb
from cocotb.triggers import ClockCycles

from pw_driver import load_program, uart_recv, uart_send
from test import UART_BIT, UART_RX_PINS, UART_TX_PINS, start


@cocotb.test()
async def test_readback_while_engines_run(dut):
    """Read both programs back while engine 0 sends UART and engine 1 waits for
    UART input: the words match, engine 0's bytes are untouched, and engine 1
    still receives correctly afterwards."""
    host, pins = await start(dut)
    pins.drive(1, 1)
    tx = load_program("uart_tx", 0)
    rx = load_program("uart_rx", 64)
    await host.load(0, tx, **UART_TX_PINS)
    await host.load(1, rx, **UART_RX_PINS)
    await host.ctrl(enable=0b11, restart=0b11)

    message = list(b"read-back!")
    got = cocotb.start_soon(uart_recv(dut, pins, 0, len(message), UART_BIT))
    sender = cocotb.start_soon(host.push(0, message))
    await ClockCycles(dut.clk, 200)

    words_tx = await host.read_imem(tx.origin, len(tx.words))   # engine 0 is mid-transfer
    words_rx = await host.read_imem(rx.origin, len(rx.words))
    assert words_tx == tx.words, [hex(w) for w in words_tx]
    assert words_rx == rx.words, [hex(w) for w in words_rx]

    await sender
    assert await got == message, "engine 0 kept its timing during the read-back"

    await uart_send(dut, pins, 1, [0x5A, 0xC3], UART_BIT)        # engine 1 resumed fine
    await ClockCycles(dut.clk, UART_BIT * 2)
    assert await host.pop(1, 2) == [0x5A, 0xC3]

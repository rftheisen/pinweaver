# PinWeaver instruction set (v0.1)

PinWeaver has **2 pin engines**. Each one is a tiny CPU that runs one 16-bit
instruction per *tick*. A tick happens every `CLKDIV + 1` system clocks, so a
50 MHz clock with `CLKDIV = 0` gives 50 M ticks/s. The engines share one
64-word instruction memory. Each engine has its own start address and wrap
range, so two programs can sit side by side.

## Engine state

| Register | Width | Use |
|---|---|---|
| `PC` | 6 | program counter (0–63) |
| `X`, `Y` | 8 | scratch / loop counters (`Y` is also the WAIT timeout counter) |
| `OSR` | 8 | output shift register (filled from the host's TX FIFO) |
| `ISR` | 8 | input shift register (pushed to the host's RX FIFO) |
| `TO` | 1 | set when the last WAIT timed out |

FIFOs: 4 bytes each way per engine. Four **flags** are shared by both
engines and the host, for handshakes and triggers.

## Pins

The 8 bidirectional pins `uio[7:0]` are GPIO 0–7. Each engine has pin groups
that wrap around modulo 8:

* **OUT** group: `out_base`, `out_count` (OUT / MOV to PINS or PINDIRS)
* **SET** group: `set_base`, `set_count` (SET PINS / PINDIRS, 5-bit values)
* **SIDE** group: `side_base`, `side_count` 0–2, optional enable bit
* **IN** base: `in_base` (IN PINS, WAIT PIN and EDGE read pins starting here)
* **JMP pin**: `jmp_pin` (absolute)

If two engines drive the same pin, the outputs are ORed together.

## Encoding

```
 15  13 12     9 8                 0
+------+--------+-------------------+
|  op  | ds[3:0]|      arg[8:0]     |
+------+--------+-------------------+
```

`ds` holds the side-set bits (top) and the delay (bottom). With
`side_count = n` and the optional-enable bit `e`, the top `n + e` bits are
side-set and the rest are the delay. After an instruction finishes, the
engine idles for `delay` ticks. Side-set values are driven on every tick the
instruction is current, including while it is stalled.

| op | Name | arg |
|---|---|---|
| 0 | `JMP cond, addr` | `[8:6]` cond, `[5:0]` addr |
| 1 | `WAIT pol src idx [timeout]` | `[8]` pol, `[7:6]` src, `[5]` timeout, `[4:0]` idx |
| 2 | `IN src, n` | `[8:6]` src, `[3:0]` n (0 = 8) |
| 3 | `OUT dst, n` | `[8:6]` dst, `[3:0]` n (0 = 8) |
| 4 | `PUSH` / `PULL` | `[8]` 0 = push, 1 = pull; `[7]` block |
| 5 | `MOV dst, op src` | `[8:6]` dst, `[5:4]` op, `[2:0]` src |
| 6 | `SET dst, value` | `[8:6]` dst, `[4:0]` value |
| 7 | `CRC sub` | `[8:6]` sub-op, `[3:0]` n for `feed` (0 = 8) |

**JMP conditions**: 0 always, 1 `!x` (X==0), 2 `x--` (X!=0, then X−1),
3 `!y`, 4 `y--`, 5 `x!=y`, 6 `pin` (jmp_pin high), 7 `timeout` (TO set).

**WAIT sources**: 0 `pin` (relative to in_base), 1 `gpio` (absolute),
2 `flag` (waiting for 1 clears the flag), 3 `edge` (pol 1 = rising, 0 = falling,
relative to in_base).
With the timeout bit set, each tick that the condition is false decrements
`Y`. If `Y` is already 0, the WAIT gives up and sets `TO`. Success clears `TO`.
*(Not in RP2040 PIO: timeouts and edge waits make I2C clock stretching, PS/2 and
"is anyone there?" checks simple.)*

**IN sources**: 0 pins, 1 X, 2 Y, 3 null, 4 OSR.
**OUT destinations**: 0 pins, 1 X, 2 Y, 3 null, 4 pindirs, 5 PC, 6 ISR.
**MOV**: dst 0 pins, 1 X, 2 Y, 3 ISR, 4 OSR, 5 PC, 6 pindirs;
op 0 none, 1 invert (`~`), 2 bit-reverse (`::`), 3 increment (`+1`);
src 0 pins, 1 X, 2 Y, 3 ISR, 4 OSR, 5 status `{TO, rx_full, tx_empty}`, 6 flags, 7 null.
**SET destinations**: 0 pins, 1 X, 2 Y, 3 pindirs, 4 flag (value bit 4 = set/clear,
bits 1:0 = flag number).

**CRC unit** (one per engine; not in RP2040 PIO): a 16-bit bit-serial CRC
register. With `crc_on_out` / `crc_on_in` set, every bit an OUT sends or an IN
reads also feeds the CRC, in wire order and at no extra cost (an 8-bit OUT
feeds 8 bits in one tick). An n-bit CRC lives in the top n bits: write
`poly << (16 - n)` and `init << (16 - n)`, and read the result from the top.
Sub-ops: 0 `crc init` (CRC ← init), 1 `crc x` (X ← CRC[7:0]), 2 `crc y`
(Y ← CRC[15:8]), 3 `crc osr_lo`, 4 `crc osr_hi` (OSR ← a CRC byte, ready to
send), 5 `crc feed n` (feed the n low bits of X, LSB first). Tested against the
published check values of CRC-16/XMODEM, CRC-15/CAN, CRC-16/ARC, CRC-5/USB and
CRC-16/USB.

**Shifting**: `out_shr` / `in_shr` choose LSB-first (shift right) or MSB-first.
With autopull, an OUT on an empty OSR (`shifted ≥ pull_thresh`) refills it from
the TX FIFO first, stalling while the FIFO is empty. With autopush, an IN that
reaches `push_thresh` bits pushes the ISR, stalling while the RX FIFO is full.
A blocking PULL/PUSH stalls the same way. A non-blocking PULL on an empty FIFO
copies X into OSR, and a non-blocking PUSH on a full FIFO is dropped.

## Config registers (per engine; engine 1 = address + 16)

| Addr | Bits |
|---|---|
| 0, 1 | CLKDIV low / high |
| 2 | `[2:0]` out_base, `[6:3]` out_count |
| 3 | `[2:0]` set_base, `[5:3]` set_count |
| 4 | `[2:0]` side_base, `[4:3]` side_count, `[5]` side_en (optional side-set), `[6]` side_pindirs (side-set drives output enables, for open-drain buses like I2C) |
| 5 | `[2:0]` in_base, `[5:3]` jmp_pin |
| 6 | `[0]` out_shr, `[1]` in_shr, `[2]` autopull, `[3]` autopush |
| 7 | `[3:0]` pull_thresh, `[7:4]` push_thresh (0 = 8) |
| 8 | wrap_bottom |
| 9 | wrap_top (reset 63) |
| 10 | start PC |
| 11, 12 | CRC polynomial low / high |
| 13, 14 | CRC init value low / high |
| 15 | `[4]` crc_on_out, `[5]` crc_on_in |

## Host SPI interface

SPI mode 0, MSB first, SCK at most clk/8. Pins: `ui[0]` SCK, `ui[1]` MOSI,
`ui[2]` CS_n, `uo[0]` MISO. The first byte clocked out of every transaction is
the status byte `{e1_rx_avail, e1_tx_full, e0_rx_avail, e0_tx_full, flags[3:0]}`.

| Cmd | Bytes after the command | Effect |
|---|---|---|
| `0x01` | addr, then (hi, lo) pairs | write instruction memory, auto-increment |
| `0x02` | addr, then data… | write config registers, auto-increment |
| `0x03` | `[1:0]` enable, `[5:4]` restart | run / stop engines; restart = PC ← start, clear FIFOs and registers |
| `0x04` | engine, then data… | push bytes to that engine's TX FIFO |
| `0x05` | engine, then dummy bytes | each dummy byte reads one RX FIFO byte |
| `0x06` | dummy bytes | read PC0, PC1, status (debug) |
| `0x07` | `[3:0]` set, `[7:4]` clear | change the shared flags |

Other outputs: `uo[1]` / `uo[2]` = engine 0 / 1 has RX data (interrupt lines),
`uo[6:3]` = flags.

## Example programs (`programs/`)

| Program | Words | What it does |
|---|---|---|
| `uart_tx.pw` | 8 | UART transmitter, 8N1, 8 ticks per bit |
| `uart_rx.pw` | 10 | UART receiver, samples mid-bit, drops frames with a bad stop bit |
| `spi_master.pw` | 10 | SPI mode 0 master, one byte per CS pulse |
| `i2c_master.pw` | 50 | I2C master: START / STOP / WRITE / READ commands, clock stretching, stuck-bus timeout (flag 0) |
| `timeout_demo.pw` | 10 | WAIT with a timeout |
| `crc_demo.pw` | 12 | Hardware CRC over host bytes, returns the CRC |

Assemble with `python tools/pw_asm.py programs/<name>.pw [origin]`. Run the tests
from Windows with `wsl -d Ubuntu -- bash run.sh test` (needs OSS CAD Suite in
`~/tools/oss-cad-suite`).

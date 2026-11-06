# PinWeaver instruction set (v0.1)

PinWeaver has **2 pin engines**. Each one is a tiny CPU that runs one 16-bit
instruction per *tick*. A tick happens every `CLKDIV + 1` system clocks, so a
33 MHz clock with `CLKDIV = 0` gives 33 M ticks/s (the chip is rated
33 MHz; typical parts run at 50 MHz, see `docs/info.md`). The engines share one
128-word instruction memory (built from latches) made of two 64-word
**pages** (0–63, 64–127).
Each engine has its own start address and wrap range, so several programs
can sit side by side. Jumps, `MOV PC` / `OUT PC` and the PC's own +1 stay
inside the current page (the page is PC bit 6), so a program lives in one
page and the assembler checks that it does not straddle two.

## Engine state

| Register | Width | Use |
|---|---|---|
| `PC` | 7 | program counter (0–127; bit 6 = page) |
| `X`, `Y` | 8 | scratch / loop counters (`Y` is also the WAIT timeout counter) |
| `OSR` | 8 | output shift register (filled from the host's TX FIFO) |
| `ISR` | 8 | input shift register (pushed to the host's RX FIFO) |
| `TO` | 1 | set when the last WAIT timed out |

FIFOs: 8 bytes each way per engine. Four **flags** are shared by both
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
**MOV**: dst 0 pins, 1 X, 2 Y, 3 ISR, 4 OSR, 5 PC, 6 pindirs, 7 `to` (the TO flag ← bit 0);
op 0 none, 1 invert (`~`), 2 bit-reverse (`::`), 3 increment (`+1`);
src 0 pins, 1 X, 2 Y, 3 ISR, 4 OSR, 5 status `{TO, rx_full, tx_empty}`, 6 flags, 7 null.
**SET destinations**: 0 pins, 1 X, 2 Y, 3 pindirs, 4 flag (value bit 4 = set/clear,
bits 1:0 = flag number).

**CRC unit** (one per engine; not in RP2040 PIO; with polynomial 0x8000 it is
a parity unit, used by SWD and PS/2): a 16-bit bit-serial CRC
register. With `crc_on_out` / `crc_on_in` set, every bit an OUT sends or an IN
reads also feeds the CRC, in wire order and at no extra cost (an 8-bit OUT
feeds 8 bits in one tick). An n-bit CRC lives in the top n bits: write
`poly << (16 - n)` and `init << (16 - n)`, and read the result from the top.
Sub-ops: 0 `crc init` (CRC ← init), 1 `crc x` (X ← CRC[7:0]), 2 `crc y`
(Y ← CRC[15:8]), 3 `crc osr_lo`, 4 `crc osr_hi` (OSR ← a CRC byte, ready to
send), 5 `crc feed n` (feed the n low bits of X, LSB first), 6 `line reset`
(see the line coder), 7 `crc hold` (stop feeding the CRC until the next
`crc init`, so it can be sent without changing). Tested against the
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
| 9 | wrap_top (reset 127) |
| 10 | start PC |
| 11, 12 | CRC polynomial low / high |
| 13, 14 | CRC init value low / high |
| 15 | `[4]` crc_on_out, `[5]` crc_on_in |

Line coder registers (engine 0 at 40–43, engine 1 at 44–47):

| Addr | Bits |
|---|---|
| 40 | `[0]` nrzi, `[1]` diff, `[2]` bitclk, `[3]` resync, `[6:4]` stuff_n (0 = off), `[7]` stuff_any |
| 41 | bit clock period − 1, in ticks |
| 42 | bit clock sample point (phase that fires a coded IN; coded OUTs fire at phase 0) |
| 43 | `[0]` arb (CAN read-back check) |

## Host SPI interface

SPI mode 0, MSB first, SCK at most clk/8. Pins: `ui[0]` SCK, `ui[1]` MOSI,
`ui[2]` CS_n, `uo[0]` MISO. The first byte clocked out of every transaction is
the status byte `{e1_rx_avail, e1_tx_full, e0_rx_avail, e0_tx_full, flags[3:0]}`.

| Cmd | Bytes after the command | Effect |
|---|---|---|
| `0x01` | addr, then (hi, lo) pairs | write instruction memory, auto-increment |
| `0x02` | addr, then data… | write config registers, auto-increment |
| `0x03` | `[1:0]` enable, `[5:4]` restart, `[6]` arm analyzer, `[7]` stop analyzer | run / stop engines; restart = PC ← start, clear FIFOs and registers. Always send the enable bits you want to keep |
| `0x04` | engine, then data… | push bytes to that engine's TX FIFO |
| `0x05` | engine, then dummy bytes | each dummy byte reads one RX FIFO byte |
| `0x06` | dummy bytes | read PC0, PC1, status, analyzer `{trig_half, state}`, analyzer pointer, trigger word |
| `0x07` | `[3:0]` set, `[7:4]` clear | change the shared flags |
| `0x08` | addr, then dummy bytes | read instruction memory (hi, lo, next word…): programs or captured samples. Uses engine 1's memory port, so **engine 1 pauses** while the transaction lasts (engine 0 is not affected) |

Other outputs: `uo[1]` / `uo[2]` = engine 0 / 1 has RX data (interrupt lines),
`uo[6:3]` = flags.

## Example programs (`programs/`)

| Program | Words | What it does |
|---|---|---|
| `uart_tx.pw` | 8 | UART transmitter, 8N1, 8 ticks per bit |
| `uart_rx.pw` | 9 | UART receiver, samples mid-bit, drops frames with a bad stop bit |
| `spi_master.pw` | 10 | SPI mode 0 master, one byte per CS pulse |
| `i2c_master.pw` | 50 | I2C master: START / STOP / WRITE / READ commands, clock stretching, stuck-bus timeout (flag 0) |
| `i2c_target.pw` | 61 | I2C device side: the host decides every ACK and every byte sent, holding SCL (clock stretching) while it thinks; reports START (flag 1) / STOP (flag 2). Emulates a sensor or EEPROM, or a faulty one |
| `timeout_demo.pw` | 9 | WAIT with a timeout |
| `crc_demo.pw` | 12 | Hardware CRC over host bytes, returns the CRC |
| `la_trigger.pw` | 3 | Fires the logic analyzer (flag 3) when GPIO 7 goes high |
| `usb_tx.pw` | 38 | Low-speed USB packet sender: SYNC, PID, payload, hardware CRC16, EOP |
| `usb_rx.pw` | 8 | Low-speed USB packet receiver: NRZI, destuffing, DPLL, end of packet |
| `can_tx.pw` | 41 | CAN 2.0A sender: stuffing, CRC-15, bus-idle wait, arbitration with back-off, ACK check |
| `swd.pw` | 56 | ARM SWD host: line reset, DP / AP reads and writes, turnaround, 32-bit parity via the CRC unit, WAIT / FAULT |
| `ps2_rx.pw` | 13 | PS/2 keyboard / mouse receiver: follows the device clock with edge waits, odd parity via the CRC unit |
| `jtag.pw` | 9 | JTAG host as cycle vectors (TMS + TDI in, TDO out per TCK): any TAP sequence, scan chains |

Assemble with `python tools/pw_asm.py programs/<name>.pw [origin]`. Run the tests
from Windows with `wsl -d Ubuntu -- bash run.sh test` (needs OSS CAD Suite in
`~/tools/oss-cad-suite`).

## Logic analyzer

The analyzer records the 8 GPIO pads (what is really on the pins, including the
chip's own outputs) into a ring buffer **inside the instruction memory**: any
words not holding programs can be the buffer, so it costs no extra memory. Two
samples per 16-bit word, the first in the high byte.

It runs from the moment it is armed, overwriting the oldest words, until the
trigger fires, then captures `post` more words after the word holding the
trigger sample and stops. So the buffer shows both what led up to the trigger
and what followed. Read it back with command `0x08`. The oldest word is at the
analyzer pointer once the ring has wrapped.

Triggers: 0 = immediately, 1 = pins match `value` under `mask`, 2 = any masked
pin changes, 3 = **flag 3**. Because engines can set flags, a program can be
the trigger: on an I2C START, a framing error or a bad CRC
(`programs/la_trigger.pw`).

| Addr | Field |
|---|---|
| 32 | buffer first word |
| 33 | buffer last word (reset 127) |
| 34 | words after the trigger word |
| 35, 36 | sample divider low / high (sample every div + 1 clocks) |
| 37 | trigger mask |
| 38 | trigger value |
| 39 | `[1:0]` trigger mode |

The analyzer writes win over host writes to the same memory. Don't let it
overwrite a running program.

## Line coder and bit clock

For serial buses that need more than plain bits (USB, CAN, ...), each engine
has a line coder between OUT / IN and the pins. It is on when any of nrzi,
diff, bitclk or stuff_n is set, and it changes `OUT pins, 1` and `IN pins, 1`
(other widths and destinations are untouched; a wider `OUT pins` is written raw
but still waits for the bit clock and any pending stuff bit).

* **NRZI** (`nrzi`): sending, a 0 bit toggles the line and a 1 keeps it;
  receiving, a bit is 1 when the line did not change.
* **Differential pair** (`diff`): drives pin and pin + 1 as a pair (level,
  inverted level). Receiving, both low (SE0, e.g. USB end of packet) makes the
  IN set `TO` and read no bit, so `jmp timeout` can catch the end of a packet.
* **Bit stuffing** (`stuff_n`, `stuff_any`): after `stuff_n` 1s in a row (USB:
  6), or `stuff_n` equal bits when `stuff_any` is set (CAN: 5), the sender
  inserts the opposite bit and the receiver drops it. A stuffed OUT sends the
  stuff bit and then runs again; a dropped bit makes the IN run again. Stuff
  bits never reach the CRC.
* **Bit clock** (`bitclk`): a phase counter that wraps every `period` ticks.
  Coded OUTs wait for phase 0 (the start of a bit) and coded INs for phase
  `sample`, so a program's
  timing comes from the clock, not from counted delays. With **`resync`**, an
  edge on the IN pin resets the phase, a small digital PLL that keeps a
  receiver locked to a sender whose clock is a little off (USB needs this:
  22.2 ticks per bit at 33.3 MHz, 33.3 at 50 MHz).
* **Read-back / arbitration** (`arb`, for CAN): at each sample point, if the
  last level we put on the pin was recessive (1) but the IN pin reads dominant
  (0), TO is set, and from then on coded OUTs send recessive whatever the
  data. So a node that loses arbitration goes quiet by itself within the bit
  and never disturbs the winner. The same check sees a receiver's ACK
  (we send the ACK slot recessive; TO set = ACKed). `mov to, null` clears it.
  Raw line-mode OUTs (2+ bits) also update the remembered level.
* `line reset` sets the line level to idle (1 = J), clears the run counter and
  restarts the bit clock.

The USB tests check the transmitted packets with an independent decoder
(timing, NRZI, stuffing, SYNC, CRC, EOP) and receive packets sent 1.2% fast
and slow. With resync off the receiver fails even at nominal speed.

![](../../workflows/gds/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/docs/badge.svg)

# PinWeaver

**A reprogrammable protocol emulator chip.** Two tiny CPUs ("pin engines") wiggle and
read pins with cycle-exact timing, so serial protocols run as firmware instead of fixed
logic. Load a different program over SPI and the same chip speaks UART, SPI, I2C,
low-speed USB, CAN, ARM SWD, PS/2 or JTAG, or *pretends to be a device*, including a
broken one.

Built for Jane Street's [protocol emulator ASIC competition](https://blog.janestreet.com/protocol-emulator-asic-competition/)
on [Tiny Tapeout](https://tinytapeout.com) (IHP 130 nm CMOS5L, 6x4 tiles).
[View the layout in your browser](https://rftheisen.github.io/pinweaver/).

| | |
|---|---|
| Engines | 2, each running one 16-bit instruction per tick, with its own clock divider |
| Program memory | 128 x 16 words (two 64-word pages), built from latches, shared by both engines and the logic analyzer |
| I/O | 8 GPIO (`uio[7:0]`); each engine's pin groups can sit on any of them |
| Host interface | SPI slave (mode 0) to load programs and config, move data, read back state |
| Per engine | 8-byte TX and RX FIFOs, a 16-bit CRC unit, a line coder (NRZI / differential / bit stuffing / bit clock) |
| Shared | 4 flags for handshakes and triggers, a logic analyzer that records the pins |
| Clock | Rated **33 MHz** at every process corner, about **50 MHz** on a typical chip ([why](docs/info.md#clock)) |
| Size | ~20,400 standard cells, 6x4 Tiny Tapeout tiles |

## What makes it different

PinWeaver follows the idea of the RP2040's PIO state machines, then adds the hardware
that real protocols kept asking for:

* **WAIT with a timeout, and edge waits.** I2C clock stretching, PS/2's device-driven clock
  and "is anyone there?" checks become one instruction instead of a counting loop.
* **A CRC unit per engine**, fed automatically by every bit an OUT sends or an IN reads, in
  wire order and at no extra cost. Any polynomial up to 16 bits; with a 1-bit polynomial it
  is the parity unit that SWD and PS/2 need.
* **A line coder per engine:** NRZI, differential pairs with end-of-packet detection, USB
  and CAN bit stuffing, a bit clock that re-locks on every edge (a small digital PLL), and a
  CAN read-back check that makes a node that loses arbitration go quiet by itself.
* **A built-in logic analyzer** that records the 8 pins into whatever program memory is
  free, keeps samples from before the trigger, and can be triggered by a running program
  (for example on a bad CRC or an I2C START).
* **Device emulation with fault injection.** The I2C target program holds the clock while
  a host script decides every answer, so the host can play a sensor, an EEPROM or an
  optical module's management port, or one that NACKs, answers slowly or hangs.

## Protocols

Every program is in [`programs/`](programs) and has tests that run it against an
independent model of the other side of the wire.

| Program | Words | What it does |
|---|---|---|
| `uart_tx.pw` / `uart_rx.pw` | 8 / 9 | UART 8N1; the receiver samples mid-bit and drops frames with a bad stop bit |
| `spi_master.pw` | 10 | SPI mode 0 master |
| `i2c_master.pw` | 50 | I2C master: START / STOP / read / write, clock stretching, stuck-bus timeout |
| `i2c_target.pw` | 61 | I2C device side, scripted by the host (see above) |
| `usb_tx.pw` / `usb_rx.pw` | 38 / 8 | Low-speed USB (1.5 Mbit/s): SYNC, NRZI, stuffing, CRC16, end of packet |
| `can_tx.pw` | 41 | CAN 2.0A: stuffing, CRC-15, arbitration with back-off, ACK check |
| `swd.pw` | 56 | ARM SWD host: DP / AP reads and writes, parity, WAIT / FAULT |
| `ps2_rx.pw` | 13 | PS/2 keyboard / mouse receiver |
| `jtag.pw` | 9 | JTAG host: any TAP sequence, scan chains |
| `crc_demo.pw`, `la_trigger.pw`, `timeout_demo.pw` | 12, 3, 9 | Small demos of the CRC unit, the analyzer trigger and WAIT timeouts |

The two engines run independently, so two protocols can run at once: for example I2C on
one engine and USB on the other, or an I2C master talking to an I2C target on the same
two wires.

A whole UART transmitter:

```
.program uart_tx
    set pins, 1             ; idle high
    set pindirs, 1
.wrap_target
    pull                    ; wait for a byte
    set x, 7
    set pins, 0 [7]         ; start bit
bitloop:
    out pins, 1 [6]         ; data bit (7 ticks + the jmp = 8)
    jmp x-- bitloop
    set pins, 1 [6]         ; stop bit
.wrap
```

The full instruction set, config registers and host commands are in
[docs/isa.md](docs/isa.md).

## Using the chip

1. Assemble a program: `python tools/pw_asm.py programs/uart_tx.pw`.
2. Over SPI (`ui[0]` SCK, `ui[1]` MOSI, `ui[2]` CS_n, `uo[0]` MISO; mode 0, SCK at most
   clk / 8): write it to program memory (command `0x01`) and set the engine's pins and
   options (`0x02`).
3. Start the engine (`0x03`), push data with `0x04` and read results with `0x05`.

`uo[1]` / `uo[2]` signal that engine 0 / 1 has data for the host, and `uo[6:3]` show the
flags. On the Tiny Tapeout demo board the RP2040 can be the SPI host. The tests in
[`test/`](test) show complete host sequences for every protocol
([`test/pw_driver.py`](test/pw_driver.py) is the host side).

## How it is verified

* **38 cocotb tests** run every program against an independent model of the device or bus
  on the other end: UART decoders, an I2C EEPROM, a USB packet decoder (timing, NRZI,
  stuffing, CRC), a CAN bus with ACKs and a competing node, an SWD target, a PS/2
  keyboard and a JTAG chain.
* **Random differential testing:** random programs, configurations, pin values and data
  run on the RTL and on a separate Python model ([`tools/pw_model.py`](tools/pw_model.py)),
  and every engine register is compared on every clock.
* **Fault injection:** slow, missing and hung devices, USB packets sent 1.2% fast and slow,
  CAN arbitration losses.
* **Gate-level simulation:** the tests run again on the netlist that comes out of place and
  route, with the IHP cell models.
* **Timing** is met at the slow, typical and fast corners at the rated clock, and the
  layout passes Tiny Tapeout's precheck (IHP's DRC deck) with no errors.

Formal proofs of the engine are planned.

## Building it yourself

The design builds with the standard Tiny Tapeout flow (LibreLane). The GitHub Actions in
this repository run it on every push: the chip build takes about 45 minutes there, then the
gate-level tests and Tiny Tapeout's precheck run.

Locally, [`run.sh`](run.sh) wraps everything (from Windows: `wsl -- bash run.sh <cmd>`):

| Command | What it does | Needs |
|---|---|---|
| `run.sh test [module]` | RTL tests (about 2 minutes) | [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build) in `~/tools/oss-cad-suite` |
| `run.sh harden` | Full chip build with the same tools as GitHub, then IHP's DRC deck (about 30 minutes on an 8-thread PC) | Docker, Tiny Tapeout's `tt-support-tools` and the IHP PDK in `~/tools/tt` |
| `run.sh gltest [module]` | Gate-level tests on the netlist from the last `harden` | as above |
| `run.sh synth_ihp` | Quick synthesis onto the IHP cells to see the area | IHP liberty files in `~/tools/ihp` |

## Repository layout

```
src/        Verilog: project.v (top), pw_engine.v, pw_latch_mem.v, pw_host.v,
            pw_spi_slave.v, pw_fifo.v, pw_la.v; config.json + pinweaver.sdc for the flow
programs/   example programs (.pw)
tools/      pw_asm.py (assembler), pw_model.py (cycle-accurate Python model)
test/       cocotb tests, device models, the SPI host driver
docs/       isa.md (instruction set, registers, host commands), info.md (datasheet page)
```

## Design notes

* **Latch memory.** The 128-word program memory is built from latches instead of
  flip-flops, about half the area. Writes are registered and land in the low half of the
  clock. Nothing ever uses a word in the cycle it is written, so paths that start at the
  latches are not timed; reads still are, from the engine's program counter
  ([`src/pinweaver.sdc`](src/pinweaver.sdc) explains it).
* **One cycle per instruction.** An engine fetches through the 128-to-1 read mux, decodes
  and executes in one clock. That path (about 29 ns on a slow chip) sets the 33 MHz rating.
* **Shared memory, on purpose.** Giving each engine its own 64-word page made the logic 28%
  smaller and faster (about 37 MHz), but made routing slower, so the build took 52 minutes
  instead of 20. Build time won for now.

## License

[Apache 2.0](LICENSE).

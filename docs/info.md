<!---
This file is used to generate the project datasheet.
-->

## How it works

PinWeaver is a programmable protocol emulator: two tiny CPUs ("pin engines")
built to wiggle and read pins with exact timing, so serial protocols run as
firmware instead of fixed logic. It is in the spirit of the RP2040's PIO, with
extra hardware that PIO lacks.

* **Two pin engines** share a 128 x 16 instruction memory (two 64-word pages,
  built from latches). Each runs one 16-bit instruction per tick: JMP, WAIT
  (pins, flags, rising / falling edges, with a timeout), IN, OUT, PUSH / PULL,
  MOV, SET and CRC. Side-set, delays, autopull / autopush and a clock divider
  per engine give cycle-exact timing.
* **CRC unit** per engine: a 16-bit bit-serial CRC (any polynomial, shorter
  CRCs left-aligned) fed automatically by every OUT / IN bit. Set to a 1-bit
  polynomial it is a parity unit.
* **Line coder** per engine: NRZI, differential pairs with SE0 detect, USB /
  CAN bit stuffing, a bit clock that re-aligns on edges (a small digital PLL),
  and a CAN read-back check that makes a node that loses arbitration go quiet
  by itself.
* **Logic analyzer**: records the 8 GPIO pads into spare instruction memory,
  keeping samples from before the trigger. Triggers: immediately, a pin
  pattern, a pin change, or a flag that a running program raises.
* **Host interface**: an SPI slave loads programs and configuration, moves
  data through 8-byte FIFOs per engine, and reads state and captures back.

Example programs (in `programs/` of the repository): UART TX / RX, SPI master,
I2C master with clock stretching, an I2C target that lets the host script
a device (including faulty ones: NACKs, slow answers, a hung bus), low-speed USB send / receive, CAN with
arbitration, ARM SWD, PS/2 and JTAG. The instruction set is in `docs/isa.md`.

## Clock

PinWeaver is rated for **33 MHz** at every process corner, including the
slow one (1.08 V, 125 C). Each engine fetches its instruction from the latch
memory, decodes and executes it in a single clock, and that path takes about
29 ns on a slow chip. A typical chip at room temperature and 1.2 V has room
for **50 MHz**. Programs take their timing from each engine's clock divider
and bit period, so the same program runs at either speed with different
settings (the example programs list both).

## How to test

Use an SPI host (for example the RP2040 on the Tiny Tapeout demo board) on
`ui[0]` SCK, `ui[1]` MOSI, `ui[2]` CS_n and `uo[0]` MISO; SPI mode 0, SCK at
most clk / 8. The first byte of every transaction returns a status byte.

1. Assemble a program: `python tools/pw_asm.py programs/uart_tx.pw`.
2. Write it to instruction memory (command `0x01`, address, then words high
   byte first) and the engine's configuration (command `0x02`).
3. Start the engine (command `0x03`), push bytes with `0x04` and read results
   with `0x05`.

The protocols then appear on the GPIO pins `uio[7:0]`. `uo[1]` and `uo[2]`
signal that engine 0 / 1 has received data, and `uo[6:3]` show the shared flags.
The repository's cocotb tests (`test/`) show complete host sequences for
every protocol.

## External hardware

Whatever device you want to talk to: a UART adapter, SPI / I2C sensors or
memories, a USB low-speed device (with a 1.5 kOhm pull-up on D-), a CAN
transceiver, an ARM chip's SWD port, a PS/2 keyboard or a JTAG chain.
Open-drain buses (I2C, PS/2) need external pull-ups.

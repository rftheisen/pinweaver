"""Random differential test: the RTL engine must match tools/pw_model.py on every clock.

Each run loads a random 64-word program and a random config into engine 0,
feeds random pin values and random TX bytes, and compares every engine
register after every clock edge.
"""
import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

from pw_driver import FIELDS, Host, Pins, config_bytes, lc_bytes
from pw_model import Chip, Config

RUNS = int(os.environ.get("PW_RUNS", "8"))
CYCLES = int(os.environ.get("PW_CYCLES", "3000"))

# opcode weights: fewer WAITs (they tend to stall forever on random pins)
OP_WEIGHTS = [3, 1, 3, 3, 2, 3, 4, 2]


def random_word(rng):
    op = rng.choices(range(8), OP_WEIGHTS)[0]
    return (op << 13) | rng.getrandbits(13)


def random_config(rng):
    cfg = {k: rng.getrandbits(w) for k, (_, _, w) in FIELDS.items()}
    cfg["clkdiv"] = rng.choice([0, 0, 0, 1, 2])
    if rng.random() < 0.5:   # half the runs use the line coder
        for k in ("lc_nrzi", "lc_diff", "lc_bitclk", "lc_resync", "lc_stuff_n", "lc_stuff_any"):
            cfg[k] = 0
    cfg["lc_bit_m1"] = rng.choice([0, 3, 10, 32, cfg["lc_bit_m1"]])
    cfg["lc_sample"] = rng.randrange(cfg["lc_bit_m1"] + 1) if rng.random() < 0.9 else cfg["lc_sample"]
    return cfg


REGS = ["pc", "x", "y", "isr", "osr", "isr_cnt", "osr_cnt", "dly", "to_flag", "pins_out", "pins_oe", "crc",
        "crc_hold", "lvl", "last", "run", "ph"]


def rtl_state(eng):
    s = {r: int(getattr(eng, r).value) for r in REGS}
    s["to"] = s.pop("to_flag")
    return s


@cocotb.test()
async def test_random_vs_model(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.ena.value = 1
    pins = Pins(dut)
    host = Host(dut)
    top = dut.user_project
    seed = int(os.environ.get("PW_SEED", "1234"))
    ops_seen = [0] * 8

    for run in range(RUNS):
        rng = random.Random(seed + run)
        dut.rst_n.value = 0
        await ClockCycles(dut.clk, 3)
        dut.rst_n.value = 1
        await ClockCycles(dut.clk, 2)

        words = [random_word(rng) for _ in range(64)]
        cfg = random_config(rng)
        data = []
        for w in words:
            data += [w >> 8, w & 0xFF]
        await host.xfer([0x01, 0] + data)
        await host.xfer([0x02, 0] + config_bytes(cfg))
        await host.xfer([0x02, 40] + lc_bytes(cfg))

        chip = Chip()
        chip.imem[:] = words
        eng = chip.engines[0]
        eng.cfg = Config(**cfg)
        tx_bytes = [rng.getrandbits(8) for _ in range(12)]

        ctrl = cocotb.start_soon(host.ctrl(enable=0b01, restart=0b01))
        started = False
        pusher = None
        cyc = 0
        for _ in range(CYCLES + 20000):
            await FallingEdge(dut.clk)
            if started:
                cyc += 1
                want, got = eng.state(), rtl_state(top.eng0)
                assert got == want, (
                    f"run {run} (seed {seed + run}) cycle {cyc}: RTL {got} != model {want}")
                assert int(top.flags.value) == chip.flags, f"flags differ at cycle {cyc}"
                if cyc >= CYCLES:
                    break
            restart = int(top.restart.value) & 1
            enable = int(top.en.value) & 1
            p = int(top.pin_s2.value)
            tx_push = int(top.tx_push.value) & 1
            tx_data = int(top.tx_data.value)
            if restart:
                eng.restart(p)
                chip.div[0] = 0
                chip.enabled[0] = True
                started = True
                pusher = cocotb.start_soon(host.push(0, tx_bytes))
            elif started and enable:
                if eng.dly == 0 and chip.div[0] == 0:
                    ops_seen[eng.imem[eng.pc] >> 13] += 1
                chip.clock(p)
            if started and tx_push and len(eng.tx) < 4:
                eng.tx.append(tx_data)
            pins.val = rng.getrandbits(8)
            dut.uio_in.value = pins.val
        assert started, "engine never restarted"
        await ctrl
        if pusher:
            pusher.cancel()
        dut.ui_in.value = 0b100
        dut._log.info(f"run {run}: ok, pc={eng.pc} rx={len(eng.rx)} tx_left={len(eng.tx)}")

    dut._log.info(f"instructions executed by opcode: {ops_seen}")
    assert all(n > 0 for n in ops_seen), f"some opcodes never ran: {ops_seen}"

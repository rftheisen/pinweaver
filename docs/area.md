# Area (IHP CMOS5L standard cells)

Measured with `wsl -d Ubuntu -- bash run.sh synth_ihp` (Yosys + ABC onto
`sg13cmos5l_stdcell_typ_1p20V_25C.lib` from IHP-Open-PDK, `dev` branch).
This is cell area only; place-and-route adds clock buffers, timing buffers,
fill and routing space.

| Date | Commit | Cells | Cell area | Share of a 6x4 area (~0.72 mm²) |
|---|---|---|---|---|
| 2026-11-01 | 128-word memory (flip-flops) | 21,313 | 0.337 mm² | ~47% |
| 2026-11-01 | latch memory | 16,653 | 0.249 mm² | ~34% |
| 2026-11-01 | + CAN arbitration | — | 0.250 mm² | ~35% |

Where it goes: the instruction memory is now 2,048 latches
(`sg13cmos5l_dlhq_1`, 0.063 mm²) instead of flip-flops plus write muxes;
the remaining ~1,100 flip-flops are config, FIFOs and engine state. The
memory's three 128-word read multiplexers are the next biggest block.

The latch memory (`src/pw_latch_mem.v`) registers each write, then opens the
chosen word during the low half of the clock, with inputs held steady by
flip-flops. Its timing (latch enable vs. clock) is something to check in the
real flow and the gate-level simulation.

Timing can't be judged from synthesis alone here: unbuffered nets with
1,000+ loads (memory write enable, reset) dominate ABC's delay estimate. The
real number needs the full Tiny Tapeout flow.

# Area (IHP CMOS5L standard cells)

Measured with `wsl -d Ubuntu -- bash run.sh synth_ihp` (Yosys + ABC onto
`sg13cmos5l_stdcell_typ_1p20V_25C.lib` from IHP-Open-PDK, `dev` branch).
This is cell area only; place-and-route adds clock buffers, timing buffers,
fill and routing space.

| Date | Commit | Cells | Cell area | Share of a 6x4 area (~0.72 mm²) |
|---|---|---|---|---|
| 2026-11-01 | 128-word memory | 21,313 | 0.337 mm² | ~47% |

Where it goes: flip-flops are 46% (3,148 of them; 2,048 are the
instruction memory), and most of the rest is the memory's three 128-word
read multiplexers.

If place-and-route struggles, the main lever is the memory: a latch cell is
30.8 µm² against 49 µm² for a flip-flop plus 18 µm² for its write mux, so a
latch-based memory would save about 0.07 mm² (a fifth of the chip).

Timing can't be judged from synthesis alone here: unbuffered nets with
1,000+ loads (memory write enable, reset) dominate ABC's delay estimate. The
real number needs the full Tiny Tapeout flow.

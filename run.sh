#!/usr/bin/env bash
# Run from Windows with:  wsl -d Ubuntu -- bash run.sh [test|synth]
# Uses OSS CAD Suite unpacked in ~/tools/oss-cad-suite (simulator, yosys, cocotb).
set -e
cd "$(dirname "$0")"
source ~/tools/oss-cad-suite/environment

case "${1:-test}" in
  test)
    cd test
    rm -rf sim_build results.xml
    python3 runner.py ${2:-}
    python3 -m cocotb_tools.check_results results.xml
    ;;
  synth)
    # Quick generic synthesis to see roughly how big the design is.
    # (The real IHP flow runs in GitHub Actions.)
    yosys -q -p "read_verilog src/*.v; synth -flatten -top tt_um_pinweaver; abc -g AND,NAND,OR,NOR,XOR,XNOR,MUX; opt_clean; tee -o synth_stats.txt stat"
    tail -40 synth_stats.txt
    ;;
esac

#!/usr/bin/env bash
# Run from Windows with:  wsl -d Ubuntu -- bash run.sh [test|gltest|synth|synth_ihp|harden]
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
  gltest)
    # Gate-level tests (what GitHub's gl_test job runs) on the netlist from the last
    # 'run.sh harden' (IHP uses the unpowered nl netlist). test_random is RTL-only.
    cp ~/build/pinweaver/runs/wokwi/final/nl/tt_um_rftheisen_pinweaver.nl.v test/gate_level_netlist.v
    cd test
    rm -rf sim_build/gl results.xml
    PDK_ROOT=~/tools/tt/pdk GATES=yes python3 runner.py ${2:-}
    python3 -m cocotb_tools.check_results results.xml
    ;;
  synth)
    # Quick generic synthesis to see roughly how big the design is.
    # (The real IHP flow runs in GitHub Actions.)
    yosys -q -p "read_verilog src/*.v; synth -flatten -top tt_um_rftheisen_pinweaver; abc -g AND,NAND,OR,NOR,XOR,XNOR,MUX; opt_clean; tee -o synth_stats.txt stat"
    tail -40 synth_stats.txt
    ;;
  synth_ihp)
    # Synthesis onto the real IHP CMOS5L standard cells (liberty files in ~/tools/ihp).
    # Gives the cell area; place-and-route adds clock buffers, fill and routing space.
    LIB=~/tools/ihp/sg13cmos5l_stdcell_${2:-typ_1p20V_25C}.lib
    yosys -q -l synth_ihp.log -p "read_verilog src/*.v; synth -flatten -top tt_um_rftheisen_pinweaver; \
      dfflibmap -liberty $LIB; abc -D 20000 -liberty $LIB; opt_clean; \
      tee -o synth_ihp_stats.txt stat -liberty $LIB"
    grep -E "Chip area|cells$|sg13cmos5l_(df|dl|sdf)" synth_ihp_stats.txt
    ;;
  harden)
    # Full chip build with the same tools as GitHub Actions: Tiny Tapeout's tt_tool,
    # LibreLane 3.1.0.dev3 (its Docker image pins Yosys, OpenROAD, Magic, KLayout...)
    # and the IHP PDK at the revision tt-gds-action installs. Set up in ~/tools/tt.
    # Builds a copy in ~/build/pinweaver (Docker is slow on /mnt/c), uncommitted edits included.
    TT=~/tools/tt; B=~/build/pinweaver
    mkdir -p "$B"
    rsync -a --delete --exclude runs --exclude tt --exclude sim_build --exclude tt_submission ./ "$B/"
    ln -sfn "$TT/tt-support-tools" "$B/tt"
    # Local only: cap OpenROAD's threads (default = every thread) so the PC stays usable,
    # e.g. with a VM running. PW_THREADS=16 bash run.sh harden for more.
    # Experiments: PW_DENSITY=30 overrides PL_TARGET_DENSITY_PCT in the build copy only.
    python3 - "$B/src/config.json" "${PW_THREADS:-8}" "${PW_DENSITY:-}" <<'PY'
import json, sys
p, n, d = sys.argv[1], int(sys.argv[2]), sys.argv[3]
c = json.load(open(p))
c["OPENROAD_THREADS"] = n
c["STA_THREADS"] = min(n, 3)
if d:
    c["PL_TARGET_DENSITY_PCT"] = int(d)
    print(f"(experiment) PL_TARGET_DENSITY_PCT = {d}")
json.dump(c, open(p, "w"), indent=2)
PY
    cd "$B"
    export PDK=ihp-sg13cmos5l PDK_ROOT="$TT/pdk" PYTHONPATH="$TT/pylib" PATH="$TT/bin:$TT/pylib/bin:$PATH"
    t0=$(date +%s)
    set +e
    python3 ./tt/tt_tool.py --create-user-config --ihp && python3 ./tt/tt_tool.py --harden --ihp 2>&1 | tee harden.log
    rc=${PIPESTATUS[0]}
    set -e
    t=$(( $(date +%s) - t0 ))
    echo
    echo "=== build $([ $rc -eq 0 ] && echo PASSED || echo FAILED) in $((t/60)) min $((t%60)) s (goal 20-25, limit 40) ==="
    # Per-step times, slowest first (LibreLane writes one folder per step).
    for d in runs/wokwi/[0-9]*-*/; do
      [ -f "$d/runtime.txt" ] && echo "$(cat "$d/runtime.txt") $(basename "$d")"
    done | sort -r | head -8
    if [ $rc -eq 0 ]; then
      # Official IHP DRC deck (what TT's precheck runs for IHP; Magic DRC is off in config.json),
      # in tiling mode so it runs on several threads.
      echo "=== IHP KLayout DRC ==="
      t1=$(date +%s)
      GDS="$B/runs/wokwi/final/gds/tt_um_rftheisen_pinweaver.gds"
      docker run --rm --entrypoint "" -v "$TT/pdk:$TT/pdk" -v "$B:$B" -w "$B"         ghcr.io/librelane/librelane:3.1.0.dev3 klayout -b         -r "$TT/pdk/ihp-sg13cmos5l/libs.tech/klayout/tech/drc/ihp-sg13cmos5l.drc"         -rd input="$GDS" -rd report="$B/drc_ihp.lyrdb" -rd threads="${PW_THREADS:-8}"         -rd run_mode=tiling > "$B/drc_ihp.log" 2>&1 || rc=1
      nviol=$(grep -c "<item>" "$B/drc_ihp.lyrdb" 2>/dev/null) || true
      nviol=${nviol:-?}
      [ "$nviol" = "0" ] || rc=1
      echo "IHP DRC: $nviol violations in $(( $(date +%s) - t1 )) s (report: $B/drc_ihp.lyrdb)"
      t=$(( $(date +%s) - t0 ))
      echo "=== total $([ $rc -eq 0 ] && echo PASSED || echo FAILED) in $((t/60)) min $((t%60)) s ==="
      python3 ./tt/tt_tool.py --print-stats --ihp || true
      python3 ./tt/tt_tool.py --print-cell-category --ihp | tail -15 || true
    fi
    exit $rc
    ;;
esac

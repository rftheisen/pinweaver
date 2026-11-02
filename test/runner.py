"""Run the cocotb tests without make:  python3 runner.py [module ...]

GATES=yes runs them on test/gate_level_netlist.v (copied from a build) with the IHP cell models
under $PDK_ROOT, like the Makefile's gate-level mode; test_random is skipped there."""
import os
import sys
from pathlib import Path

from cocotb_tools.runner import get_runner

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
SOURCES = ["project.v", "pw_engine.v", "pw_fifo.v", "pw_spi_slave.v", "pw_host.v", "pw_la.v", "pw_latch_mem.v"]


def main(modules):
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(HERE), str(HERE.parent / "tools"), os.environ.get("PYTHONPATH", "")])
    sys.path[:0] = [str(HERE), str(HERE.parent / "tools")]
    gates = os.environ.get("GATES") == "yes"
    if gates:
        lib = Path(os.environ["PDK_ROOT"]) / "ihp-sg13cmos5l" / "libs.ref"
        sources = [lib / "sg13cmos5l_io/verilog/sg13cmos5l_io.v",
                   lib / "sg13cmos5l_stdcell/verilog/sg13cmos5l_udp.v",
                   lib / "sg13cmos5l_stdcell/verilog/sg13cmos5l_stdcell.v",
                   HERE / "gate_level_netlist.v"]
        defines = {"GL_TEST": 1, "FUNCTIONAL": 1, "SIM": 1}
        modules = [m for m in modules if m != "test_random"]
    else:
        sources, defines = [SRC / s for s in SOURCES], {}
    build_dir = HERE / "sim_build" / ("gl" if gates else "rtl")
    runner = get_runner("icarus")
    runner.build(
        sources=sources + [HERE / "tb.v"],
        hdl_toplevel="tb",
        includes=[SRC],
        defines=defines,
        build_dir=build_dir,
        timescale=("1ns", "1ps"),
        always=True,
    )
    runner.test(
        hdl_toplevel="tb",
        test_module=",".join(modules),
        build_dir=build_dir,
        test_dir=HERE,
        results_xml=str(HERE / "results.xml"),
    )


if __name__ == "__main__":
    main(sys.argv[1:] or ["test", "test_i2c", "test_crc", "test_la", "test_usb", "test_pages", "test_can", "test_swd", "test_ps2", "test_jtag", "test_mem", "test_random"])

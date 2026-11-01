"""Run the cocotb tests without make:  python3 runner.py [module ...]"""
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
    runner = get_runner("icarus")
    runner.build(
        sources=[SRC / s for s in SOURCES] + [HERE / "tb.v"],
        hdl_toplevel="tb",
        includes=[SRC],
        build_dir=HERE / "sim_build" / "rtl",
        timescale=("1ns", "1ps"),
        always=True,
    )
    runner.test(
        hdl_toplevel="tb",
        test_module=",".join(modules),
        build_dir=HERE / "sim_build" / "rtl",
        test_dir=HERE,
        results_xml=str(HERE / "results.xml"),
    )


if __name__ == "__main__":
    main(sys.argv[1:] or ["test", "test_i2c", "test_crc", "test_la", "test_usb", "test_pages", "test_can", "test_swd", "test_ps2", "test_random"])

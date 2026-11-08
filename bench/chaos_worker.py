"""Child process for chaos experiments: does slow work so the parent can SIGKILL it mid-way."""

from __future__ import annotations

import sys
from pathlib import Path

from bench.chaos import FAST
from bench.common import NetSim, data_stream, make_system
from drivemesh.core.workers import health_check, repair


def main() -> None:
    cmd, work = sys.argv[1], Path(sys.argv[2])
    n_acc = 3 if cmd == "upload" else 4
    # Simulated latency slows the work down enough to be interrupted at a random point.
    dm, uid, _ = make_system(work, n_acc, fresh=False, net=NetSim(latency=0.03), **FAST)
    if cmd == "upload":
        dm.put(uid, "/victim.bin", data_stream(64 * 1024 * 1024), rf=2)
    elif cmd == "repair":
        health_check(dm)
        while repair(dm).scanned:
            pass


if __name__ == "__main__":
    main()

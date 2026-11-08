"""Reproducible benchmark harness.

Each benchmark run records wall time, CPU time, peak RSS of the process, parameters and the
environment into a JSON file under bench/out/. Run memory-sensitive benchmarks in a fresh
process (``python -m bench.<name>``) because peak RSS is per-process and monotonic.
"""

from __future__ import annotations

import json
import os
import platform
import resource
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def peak_rss_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux reports KiB.
    return int(rss if sys.platform == "darwin" else rss * 1024)


def environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
    }


@dataclass
class Measurement:
    name: str
    params: dict[str, Any]
    wall_seconds: float = 0.0
    cpu_seconds: float = 0.0
    peak_rss_bytes: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)
    env: dict[str, Any] = field(default_factory=environment)
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


@contextmanager
def measure(name: str, **params: Any) -> Iterator[Measurement]:
    m = Measurement(name=name, params=params)
    t0, c0 = time.perf_counter(), time.process_time()
    try:
        yield m
    finally:
        m.wall_seconds = time.perf_counter() - t0
        m.cpu_seconds = time.process_time() - c0
        m.peak_rss_bytes = peak_rss_bytes()


def save(m: Measurement, out_dir: str | os.PathLike[str] = "bench/out") -> Path:
    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    target = path / f"{m.name}-{m.timestamp.replace(':', '')}.json"
    target.write_text(m.to_json())
    return target

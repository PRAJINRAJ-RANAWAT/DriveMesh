"""drivemesh command-line interface."""

from __future__ import annotations

import argparse
import sys

from drivemesh import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="drivemesh", description=__doc__)
    p.add_argument("--version", action="version", version=f"drivemesh {__version__}")
    p.add_subparsers(dest="cmd")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""resume_line.py — print `<ledger_cli> resume --item <id>`, the last thing a topic close shows.

    python3 resume_line.py <item id> [--config <path>]

With the ledger on (the shared rule, `decide` in `skills/_lib/ledger_cli.py`:
the topic skill's `ledger_cli` key, or else a compatible `ledger` on `PATH` with
`LEDGER_DB`), prints the CLI's stdout for `resume --item <id>` exactly as it came
back, and exits 0. With the ledger off it prints nothing and exits 0, so a close
without a ledger is unchanged. When the CLI fails (not found, an older ledger with no `resume`, a
non-zero exit) it prints one line on stderr, nothing on stdout, and exits 1: the
close is already done, so report the line and stop.

Python 3 stdlib only.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Optional

# Loading the shared helper must not leave bytecode beside it.
sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
LEDGER_CLI_PATH = HERE.parent / "_lib" / "ledger_cli.py"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="This script: %s" % Path(__file__).resolve(),
    )
    parser.add_argument("item")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        spec = importlib.util.spec_from_file_location("topic_resume_ledger_cli", LEDGER_CLI_PATH)
        ledger = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ledger)
        cli = ledger.decide(args.config, who="topic", strict=True, required_shipped=True).cli
    except Exception as exc:
        print(f"topic: could not read the ledger setting ({exc})", file=sys.stderr)
        return 1
    if cli is None:
        return 0
    try:
        proc = ledger.call(cli, ["resume", "--item", args.item])
    except ledger.LedgerCallError as exc:
        print(f"topic: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(proc.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

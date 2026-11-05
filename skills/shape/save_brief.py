#!/usr/bin/env python3
"""save_brief.py — write the shape brief to the roadmaps directory, and link it when a ledger CLI is set.

    python3 save_brief.py --slug <slug> [--date <YYYY-MM-DD>] [--item <id>]   < brief text on stdin

Writes `shape-<slug>-<YYYY-MM-DD>.md` (the date defaults to today) into the
`roadmaps` directory that sharpen resolves (`../sharpen/paths.py`, reused, not
copied), and prints the file's absolute path on stdout. It never overwrites: a
file already there is refused (exit 2) so an earlier brief survives.

With a ledger CLI configured (`_lib/ledger_cli.py`) and `--item` given, it then
runs `<cli> link <item> --rel url --target <absolute path>`, so the brief is
attached to the open topic item and survives the session. With the CLI off, or
no `--item`, no ledger call is made. A failing CLI leaves the file in place and
exits 1 with one stderr line.

Exit status: 0 written (and linked when asked), 1 the ledger link failed,
2 refused (bad slug or date, empty brief, existing file, roadmaps unresolved).
Python 3 stdlib only.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import re
import sys
from pathlib import Path
from typing import Optional

# Loading the shared helper must not leave bytecode beside it.
sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
LEDGER_CLI_PATH = HERE.parent / "_lib" / "ledger_cli.py"
SHARPEN_PATHS = HERE.parent / "sharpen" / "paths.py"
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _refuse(message: str) -> int:
    print(f"shape: {message}", file=sys.stderr)
    return 2


def main(argv: Optional[list[str]] = None, stdin=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="This script: %s" % Path(__file__).resolve(),
    )
    parser.add_argument("--slug", required=True)
    parser.add_argument("--date", default=None)
    parser.add_argument("--item", default=None)
    args = parser.parse_args(argv)

    if not SLUG.fullmatch(args.slug):
        return _refuse(f"slug {args.slug!r} must be lowercase words joined by hyphens")
    date = args.date or datetime.date.today().isoformat()
    if not DATE.fullmatch(date):
        return _refuse(f"date {date!r} must be YYYY-MM-DD")
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        return _refuse(f"date {date!r} is not a calendar date")

    text = (stdin if stdin is not None else sys.stdin).read()
    if not text.strip():
        return _refuse("the brief on stdin is empty")

    if not SHARPEN_PATHS.is_file():
        return _refuse(
            f"the sharpen skill's paths.py is not installed beside this skill ({SHARPEN_PATHS}); "
            "install sharpen too, or print the brief inline without saving it"
        )
    try:
        roadmaps = Path(_load("shape_sharpen_paths", SHARPEN_PATHS).resolve_paths()["roadmaps"])
    except Exception as exc:
        return _refuse(f"could not resolve the roadmaps directory ({type(exc).__name__}: {exc})")

    target = roadmaps / f"shape-{args.slug}-{date}.md"
    try:
        roadmaps.mkdir(parents=True, exist_ok=True)
        with open(target, "x", encoding="utf-8") as handle:
            handle.write(text if text.endswith("\n") else text + "\n")
    except FileExistsError:
        return _refuse(f"{target} already exists; not overwriting it")
    except OSError as exc:
        return _refuse(f"could not write {target} ({exc})")
    absolute = str(target.resolve())
    print(absolute)

    if args.item is None:
        return 0
    try:
        ledger = _load("shape_ledger_cli", LEDGER_CLI_PATH)
        cli = ledger.resolve(who="shape", quiet=True)
    except Exception as exc:
        print(f"shape: could not read the ledger setting ({exc}) — brief saved, not linked", file=sys.stderr)
        return 0
    if cli is None:
        return 0
    try:
        ledger.call(cli, ledger.link_argv(args.item, absolute))
    except ledger.LedgerCallError as exc:
        print(f"shape: {exc}; the brief is saved at {absolute} but not linked", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""ledger_cli.py — find the optional ledger CLI, for every skill and hook that uses it.

One source: the `ledger_cli` key of the topic skill's config. It is read the way
`topics_dir` is: an explicit `--config` file alone, or else the shipped
`skills/topic/config.json` with `~/.claude/skill-config/topic.json` laid over it.
There is no other switch (no environment variable).

    python3 ledger_cli.py [--config <path>]

Prints one JSON object, `{"ledger_cli": <path or null>}`, and exits 0. The value
is null whenever the route is off: key absent, null, empty or blank, a value that
is not a string, or a config that is missing or unreadable. It never raises for a
config problem; a config file that exists but cannot be used is named in one
stderr line.

Also holds the small pieces the callers share: the argv of the `link` call and a
`call` that runs the CLI with a timeout and turns a failure into one
`LedgerCallError` line.

Python 3 stdlib only. It lives in the `_lib` directory beside the skills, so a
skill reaches it as `${CLAUDE_SKILL_DIR}/../_lib/ledger_cli.py` in the plugin
and in a standalone `install.sh` copy alike.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

LIB_DIR = Path(__file__).resolve().parent
CONFIG_PATH = LIB_DIR.parent / "topic" / "config.json"
OVERRIDE_PATH = "~/.claude/skill-config/topic.json"
CONFIG_KEY = "ledger_cli"
TIMEOUT_SECONDS = 60


class ConfigError(ValueError):
    """The `ledger_cli` key holds something other than a string."""


class LedgerCallError(Exception):
    """The CLI could not run, timed out, or exited non-zero: one line to report."""


def _read_object(path: Path, required: bool, who: str, quiet: bool) -> dict[str, Any]:
    """A JSON object from `path`, or `{}` with one stderr line when unusable."""

    def say(line: str) -> None:
        if not quiet:
            sys.stderr.write(line)

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            say(f"{who}: could not read {path} — {CONFIG_KEY} is unset\n")
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        say(f"{who}: could not read {path} ({exc}) — ignoring it\n")
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        say(f"{who}: {path} is not valid JSON ({exc}) — ignoring it\n")
        return {}
    if not isinstance(data, dict):
        say(f"{who}: {path} is not a JSON object — ignoring it\n")
        return {}
    return data


def resolve(
    config_path: Optional[Path] = None,
    *,
    who: str = CONFIG_KEY,
    strict: bool = False,
    required_shipped: bool = False,
    quiet: bool = False,
) -> Optional[str]:
    """The configured ledger CLI (a path or a name), or None when the route is off.

    `strict` raises ConfigError for a value that is not a string; otherwise such a
    value means off. `required_shipped` reports a missing shipped config on stderr
    (topic's long-standing behaviour); otherwise a missing one is silent, because a
    standalone install of one skill may not carry the topic skill. `quiet` mutes
    every stderr line.
    """
    if config_path is not None:
        config = _read_object(Path(config_path), True, who, quiet)
    else:
        config = _read_object(CONFIG_PATH, required_shipped, who, quiet)
        config = {
            **config,
            **_read_object(Path(OVERRIDE_PATH).expanduser(), False, who, quiet),
        }
    value = config.get(CONFIG_KEY)
    if value is None:
        return None
    if not isinstance(value, str):
        if strict:
            raise ConfigError(
                f"{CONFIG_KEY} must be a string naming the ledger executable, "
                f"got {type(value).__name__}"
            )
        return None
    value = value.strip()
    return os.path.expanduser(value) if value else None


def link_argv(item: str, target: str) -> list[str]:
    """The `link` call that attaches a document to an item."""
    return ["link", item, "--rel", "url", "--target", target]


def call(cli: str, argv: list[str]) -> subprocess.CompletedProcess:
    """Run `<cli> <argv>`; raise LedgerCallError unless it exits 0."""
    verb = argv[0] if argv else "?"
    try:
        proc = subprocess.run(
            [cli, *argv],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise LedgerCallError(
            f"ledger CLI {cli} timed out after {TIMEOUT_SECONDS}s running {verb}"
        )
    except OSError as exc:
        detail = " ".join((exc.strerror or str(exc)).split())
        raise LedgerCallError(f"ledger CLI {cli} could not run ({detail})")
    if proc.returncode != 0:
        detail = " ".join((proc.stderr.strip() or proc.stdout.strip() or "no output").split())
        raise LedgerCallError(
            f"ledger CLI {cli} {verb} failed (exit {proc.returncode}): {detail}"
        )
    return proc


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        cli = resolve(args.config)
    except Exception as exc:  # the contract: report, never raise
        sys.stderr.write(f"ledger_cli: {exc}\n")
        cli = None
    print(json.dumps({CONFIG_KEY: cli}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""ledger_item.py — open or resume a topic as a ledger item.

The ledger route is switched on by `config.json`'s `ledger_cli` key: the path
or name of the `ledger` executable. Absent, null or empty means off. The key
is read the same way `topics_dir` is: an explicit `--config` file alone, or
else the shipped `config.json` with `~/.claude/skill-config/topic.json` laid
over it.

    python3 ledger_item.py --open <slug> --ask <text> [--config <path>]

Key unset: this does exactly what `topics_dir.py --open` does (writes the
note file if it is absent, prints its path).

Key set: it finds the `topic` item `topic-<slug>` with `query` and creates it
with `new` only when none exists, then records the ask with `note`. A second
call for the same slug finds the item and creates nothing. It writes no
markdown note. It prints one line on stdout:

    <item id> TAB <created|resumed> TAB <ledger_cli>

A configured CLI that is missing, cannot run, or fails is reported in one
stderr line naming it, and the exit status is nonzero. It never falls back to
writing a note file. The environment passes through untouched, so the CLI's
own settings (database, harness, actor) are the caller's.

Python 3 stdlib only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
OVERRIDE_PATH = "~/.claude/skill-config/topic.json"
CONFIG_KEY = "ledger_cli"
ITEM_TYPE = "topic"
ITEM_PREFIX = "topic-"
TIMEOUT_SECONDS = 60
NOT_FOUND_CODES = ("empty", "missing_db")


class LedgerError(Exception):
    """A failure to report as one stderr line."""


def _load_topics_dir():
    spec = importlib.util.spec_from_file_location(
        "topic_ledger_item_topics_dir", HERE / "topics_dir.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_object(path: Path, required: bool) -> dict[str, Any]:
    """A JSON object from `path`, or `{}` with one stderr line when unusable."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            sys.stderr.write(f"topic: could not read {path} — {CONFIG_KEY} is unset\n")
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        sys.stderr.write(f"topic: could not read {path} ({exc}) — ignoring it\n")
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        sys.stderr.write(f"topic: {path} is not valid JSON ({exc}) — ignoring it\n")
        return {}
    if not isinstance(data, dict):
        sys.stderr.write(f"topic: {path} is not a JSON object — ignoring it\n")
        return {}
    return data


def configured_cli(config_path: Optional[Path] = None) -> Optional[str]:
    """The `ledger_cli` setting, or None when it is off."""
    if config_path is not None:
        config = _read_object(Path(config_path), True)
    else:
        config = _read_object(CONFIG_PATH, True)
        config = {**config, **_read_object(Path(OVERRIDE_PATH).expanduser(), False)}
    value = config.get(CONFIG_KEY)
    if value is None:
        return None
    if not isinstance(value, str):
        raise LedgerError(
            f"{CONFIG_KEY} must be a string naming the ledger executable, "
            f"got {type(value).__name__}"
        )
    value = value.strip()
    return os.path.expanduser(value) if value else None


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _call(cli: str, verb_argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [cli, *verb_argv],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise LedgerError(
            f"ledger CLI {cli} timed out after {TIMEOUT_SECONDS}s running {verb_argv[0]}"
        )
    except OSError as exc:
        raise LedgerError(
            f"ledger CLI {cli} could not run ({_one_line(exc.strerror or str(exc))})"
        )


def _envelope(proc: subprocess.CompletedProcess) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _failure(cli: str, verb: str, proc: subprocess.CompletedProcess) -> LedgerError:
    envelope = _envelope(proc)
    error = envelope.get("error") if envelope else None
    if isinstance(error, dict) and error.get("message"):
        detail = str(error["message"])
    else:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
    return LedgerError(
        f"ledger CLI {cli} {verb} failed (exit {proc.returncode}): {_one_line(detail)}"
    )


def _find(cli: str, item_id: str) -> bool:
    proc = _call(cli, ["query", "--type", ITEM_TYPE, "--item", item_id, "--json"])
    envelope = _envelope(proc)
    if proc.returncode == 0:
        data = envelope.get("data") if envelope else None
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list) and any(
            isinstance(it, dict) and it.get("item_id") == item_id for it in items
        ):
            return True
        raise LedgerError(f"ledger CLI {cli} query returned no item {item_id} and no error")
    error = envelope.get("error") if envelope else None
    # `empty`: no such item. `missing_db`: the store is not created yet, and a
    # write creates it, so there is no such item either.
    if (
        proc.returncode == 1
        and isinstance(error, dict)
        and error.get("code") in NOT_FOUND_CODES
    ):
        return False
    raise _failure(cli, "query", proc)


def _create(cli: str, slug: str, item_id: str, ask: str) -> None:
    new = _call(
        cli,
        ["new", "--type", ITEM_TYPE, "--title", slug, "--id", item_id, "--json"],
    )
    if new.returncode != 0:
        raise _failure(cli, "new", new)
    note = _call(cli, ["note", item_id, "--text", f"Ask: {ask}", "--json"])
    if note.returncode != 0:
        raise _failure(cli, "note", note)


def open_item(cli: str, slug: str, ask: str) -> tuple[str, str]:
    """Return `(item_id, "created" | "resumed")`; create only when none exists."""
    item_id = ITEM_PREFIX + slug
    if _find(cli, item_id):
        return item_id, "resumed"
    _create(cli, slug, item_id, ask)
    return item_id, "created"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="This script: %s" % Path(__file__).resolve(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--open", dest="slug", required=True)
    parser.add_argument("--ask", required=True)
    args = parser.parse_args(argv)
    topics = _load_topics_dir()
    try:
        topics._check_slug(args.slug)
        cli = configured_cli(args.config)
        if cli is None:
            print(topics.open_topic(args.slug, args.ask, args.config).path)
            return 0
        item_id, state = open_item(cli, args.slug, args.ask)
    except ValueError as exc:
        print(f"topic: {exc}", file=sys.stderr)
        return 2
    except LedgerError as exc:
        print(f"topic: {exc}", file=sys.stderr)
        return 1
    print(f"{item_id}\t{state}\t{cli}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

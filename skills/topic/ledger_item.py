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
with `new` only when none exists, then records two notes: the ask
(`Ask: <text>`) and the rails note (see RAILS_TEXT), whose fixed grammar is the
item's machine-readable staged-drafts and promote-or-park state. A second call
for the same slug finds the item and creates nothing; it reads the item's notes
with `export-item` and adds whichever of the two notes is missing (an earlier
call can have died between `new` and a `note`), so a retry never duplicates
either. When `new` loses a race to a concurrent open (a `conflict`), the item is
queried again and, if it now exists, resumed the same way. It writes no markdown
note. It prints one line on stdout:

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
CONFLICT_CODE = "conflict"
ASK_PREFIX = "Ask: "
# The rails note: first line is the tag, then one `key: value` line per rail.
# A newer note of the same grammar replaces the state; see SKILL.md.
RAILS_TAG = "topic-rails"
RAILS_TEXT = f"{RAILS_TAG}\nstaged_drafts: none\npromote_or_park: open"


class LedgerError(Exception):
    """A failure to report as one stderr line."""


class LedgerConflict(LedgerError):
    """`new` refused because the item id already exists."""


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
    kind = (
        LedgerConflict
        if verb == "new" and isinstance(error, dict) and error.get("code") == CONFLICT_CODE
        else LedgerError
    )
    return kind(
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


def _new(cli: str, slug: str, item_id: str) -> None:
    new = _call(
        cli,
        ["new", "--type", ITEM_TYPE, "--title", slug, "--id", item_id, "--json"],
    )
    if new.returncode != 0:
        raise _failure(cli, "new", new)


def _note(cli: str, item_id: str, text: str) -> None:
    note = _call(cli, ["note", item_id, "--text", text, "--json"])
    if note.returncode != 0:
        raise _failure(cli, "note", note)


def _note_texts(cli: str, item_id: str) -> list[str]:
    """Every note on the item, from `export-item` (the card caps its notes)."""
    proc = _call(cli, ["export-item", item_id, "--json"])
    if proc.returncode != 0:
        raise _failure(cli, "export-item", proc)
    envelope = _envelope(proc)
    data = envelope.get("data") if envelope else None
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        raise LedgerError(f"ledger CLI {cli} export-item returned no events for {item_id}")
    texts: list[str] = []
    for event in events:
        if isinstance(event, dict) and event.get("kind") == "item.note":
            payload = event.get("payload")
            text = payload.get("text") if isinstance(payload, dict) else None
            if isinstance(text, str):
                texts.append(text)
    return texts


def _reconcile(cli: str, item_id: str, ask: str) -> None:
    """Add the initial Ask note and the rails note when the item lacks them."""
    texts = _note_texts(cli, item_id)
    if not any(text.startswith(ASK_PREFIX) for text in texts):
        _note(cli, item_id, ASK_PREFIX + ask)
    if not any(text.strip().split("\n", 1)[0].strip() == RAILS_TAG for text in texts):
        _note(cli, item_id, RAILS_TEXT)


def open_item(cli: str, slug: str, ask: str) -> tuple[str, str]:
    """Return `(item_id, "created" | "resumed")`; create only when none exists.

    A resumed item is reconciled first: a call that died between `new` and a
    `note` left the item without its Ask or rails note. If `new` conflicts, a
    concurrent open created the item between the lookup and the create, so look
    it up again: resume it when it is there, otherwise the conflict stands.
    """
    item_id = ITEM_PREFIX + slug
    if _find(cli, item_id):
        _reconcile(cli, item_id, ask)
        return item_id, "resumed"
    try:
        _new(cli, slug, item_id)
    except LedgerConflict:
        if not _find(cli, item_id):
            raise
        _reconcile(cli, item_id, ask)
        return item_id, "resumed"
    _note(cli, item_id, ASK_PREFIX + ask)
    _note(cli, item_id, RAILS_TEXT)
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

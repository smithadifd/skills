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
queried again and, if it now exists, resumed the same way.

Every one of those notes is appended with `--expect-revision`, the ledger's
compare-and-set on the item's newest event id: the creator uses the id `new`
returned, a resumer the newest id in the `export-item` it just read. When
another writer got in first the append is refused as `stale_revision`, and the
resumer reads the item again and decides again, so two openers racing cannot
write the Ask or the rails note twice, and a rails update written in the gap
is never buried under the default. It writes no markdown note. It prints one
line on stdout:

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
STALE_CODE = "stale_revision"
# Re-reads after a refused (stale) append before giving up. A topic item has a
# handful of writers, so more than a few consecutive stale answers means the
# item is being written in a loop, not that an open is racing.
MAX_ATTEMPTS = 8
ASK_PREFIX = "Ask: "
# The rails note: first line is the tag, then one `key: value` line per rail.
# A newer note of the same grammar replaces the state; see SKILL.md.
RAILS_TAG = "topic-rails"
RAILS_TEXT = f"{RAILS_TAG}\nstaged_drafts: none\npromote_or_park: open"


class LedgerError(Exception):
    """A failure to report as one stderr line."""


class LedgerConflict(LedgerError):
    """`new` refused because the item id already exists."""


class LedgerStale(LedgerError):
    """`note` refused because the item's newest event is not the expected one."""


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
    code = error.get("code") if isinstance(error, dict) else None
    if verb == "new" and code == CONFLICT_CODE:
        kind = LedgerConflict
    elif verb == "note" and code == STALE_CODE:
        kind = LedgerStale
    else:
        kind = LedgerError
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


def _event_id(cli: str, verb: str, proc: subprocess.CompletedProcess) -> int:
    """The id of the event a successful write returned."""
    envelope = _envelope(proc)
    data = envelope.get("data") if envelope else None
    event = data.get("event") if isinstance(data, dict) else None
    event_id = event.get("id") if isinstance(event, dict) else None
    if type(event_id) is not int or event_id < 1:
        raise LedgerError(f"ledger CLI {cli} {verb} returned no event id")
    return event_id


def _new(cli: str, slug: str, item_id: str) -> int:
    """Create the item; return its revision (the id of its first event)."""
    new = _call(
        cli,
        ["new", "--type", ITEM_TYPE, "--title", slug, "--id", item_id, "--json"],
    )
    if new.returncode != 0:
        raise _failure(cli, "new", new)
    return _event_id(cli, "new", new)


def _note(cli: str, item_id: str, text: str, revision: int) -> int:
    """Append a note only if the item's newest event is `revision`.

    Returns the new revision. A refusal because another writer got in first
    raises LedgerStale and writes nothing.
    """
    note = _call(
        cli,
        ["note", item_id, "--text", text, "--expect-revision", str(revision), "--json"],
    )
    if note.returncode != 0:
        raise _failure(cli, "note", note)
    return _event_id(cli, "note", note)


def _read_item(cli: str, item_id: str) -> tuple[int, list[str]]:
    """The item's revision and every note on it, from `export-item`.

    The card caps its notes, so it is not used. The revision is the newest
    event id of any kind, which is what `--expect-revision` compares.
    """
    proc = _call(cli, ["export-item", item_id, "--json"])
    if proc.returncode != 0:
        raise _failure(cli, "export-item", proc)
    envelope = _envelope(proc)
    data = envelope.get("data") if envelope else None
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        raise LedgerError(f"ledger CLI {cli} export-item returned no events for {item_id}")
    texts: list[str] = []
    revision = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = event.get("id")
        if type(event_id) is int and event_id > revision:
            revision = event_id
        if event.get("kind") == "item.note":
            payload = event.get("payload")
            text = payload.get("text") if isinstance(payload, dict) else None
            if isinstance(text, str):
                texts.append(text)
    if revision < 1:
        raise LedgerError(f"ledger CLI {cli} export-item returned no event ids for {item_id}")
    return revision, texts


def _missing_note(texts: list[str], ask: str) -> Optional[str]:
    """The next initial note the item lacks, or None when it has both.

    The rails note is judged by its tag alone, so a rails update written by
    anyone counts and the default is never added after it.
    """
    if not any(text.startswith(ASK_PREFIX) for text in texts):
        return ASK_PREFIX + ask
    if not any(text.strip().split("\n", 1)[0].strip() == RAILS_TAG for text in texts):
        return RAILS_TEXT
    return None


def _complete(
    cli: str,
    item_id: str,
    ask: str,
    revision: Optional[int] = None,
    texts: Optional[list[str]] = None,
) -> None:
    """Add the initial Ask and rails notes the item lacks, each at most once.

    Every append is guarded by the revision it was decided against. When the
    guard refuses, someone else wrote to the item in between, so the item is
    read again and the decision is made again from what is there now. Only
    those stale refusals count toward the limit; a successful append never
    does, and the item is judged complete again right after each one.
    """
    stale = 0
    while True:
        if revision is None or texts is None:
            revision, texts = _read_item(cli, item_id)
        pending = _missing_note(texts, ask)
        if pending is None:
            return
        try:
            revision = _note(cli, item_id, pending, revision)
        except LedgerStale:
            stale += 1
            if stale >= MAX_ATTEMPTS:
                raise LedgerError(
                    f"ledger CLI {cli} note kept being refused as stale for {item_id}; "
                    "the item is changing faster than it can be completed"
                )
            revision, texts = None, None
            continue
        texts = [*texts, pending]


def _reconcile(cli: str, item_id: str, ask: str) -> None:
    """Add the initial Ask note and the rails note when the item lacks them."""
    _complete(cli, item_id, ask)


def open_item(cli: str, slug: str, ask: str) -> tuple[str, str]:
    """Return `(item_id, "created" | "resumed")`; create only when none exists.

    A resumed item is reconciled first: a call that died between `new` and a
    `note` left the item without its Ask or rails note. If `new` conflicts, a
    concurrent open created the item between the lookup and the create, so look
    it up again: resume it when it is there, otherwise the conflict stands.
    The creator's own notes and a resumer's are appended under the same
    revision guard, so neither can duplicate the other's.
    """
    item_id = ITEM_PREFIX + slug
    if _find(cli, item_id):
        _reconcile(cli, item_id, ask)
        return item_id, "resumed"
    try:
        revision = _new(cli, slug, item_id)
    except LedgerConflict:
        if not _find(cli, item_id):
            raise
        _reconcile(cli, item_id, ask)
        return item_id, "resumed"
    # The creator holds the revision `new` returned. If a resumer wrote to the
    # item first, the guard refuses and the item is read again, so the Ask and
    # rails notes are written once whichever opener gets there first.
    _complete(cli, item_id, ask, revision, [])
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

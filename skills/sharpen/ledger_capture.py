#!/usr/bin/env python3
"""ledger_capture.py — record a sharpen brief in the ledger, when a ledger CLI is configured.

    python3 ledger_capture.py link   --item <id> --brief <path>
    python3 ledger_capture.py ratify --item <id> --brief <path> --words <text> --quote <phrase>
                                     [--chosen-description <text>] [--quote-ref <event id>]

The CLI is the one `_lib/ledger_cli.py` resolves (the topic skill's `ledger_cli`
key). With it off, both commands print nothing, call nothing and exit 0.

`link` attaches the brief to the item: `<cli> link <item> --rel url --target
<absolute brief path>`.

`ratify` records the ratification as an election, never a note. It first
captures the person's words (`append` of an `utterance.captured` event on the
item), then elects: `<cli> elect <item> --question "Ratify <brief>" --chosen
ratified --chosen-description ... --rejected not-ratified --scope <brief path>
--quote-ref <the utterance's event id> --quote <phrase>`. The phrase must be a
verbatim substring of the words; when it is not, nothing is called and the exit
status is 2. The capture carries an idempotency key built from the item, the
brief and the words, so a rerun after a stop returns the same utterance instead
of capturing it twice. If the election fails after the capture succeeded, the
error line names the utterance's event id, and `--quote-ref <id>` retries the
election alone.

Exit status: 0 done (or off), 1 the CLI failed, 2 the arguments were refused.
Python 3 stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Optional

# Loading the shared helper must not leave bytecode beside it.
sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
LEDGER_CLI_PATH = HERE.parent / "_lib" / "ledger_cli.py"
DEFAULT_DESCRIPTION = "The brief is ratified as written."


def _load_ledger_cli():
    spec = importlib.util.spec_from_file_location(
        "sharpen_ledger_capture_ledger_cli", LEDGER_CLI_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def capture_argv(item: str, brief: str, words: str, session_id: Optional[str]) -> list[str]:
    """The `append` call that records the person's words as an utterance."""
    payload = {"text": words, "origin": "person"}
    if session_id:
        payload["session_id"] = session_id
    key = hashlib.sha256("\0".join((item, brief, words)).encode("utf-8")).hexdigest()[:32]
    return [
        "append",
        "--kind",
        "utterance.captured",
        "--payload",
        json.dumps(payload, ensure_ascii=False),
        "--item",
        item,
        "--idempotency-key",
        f"sharpen-utterance-{key}",
        "--json",
    ]


def elect_argv(
    item: str, brief: str, quote_ref: int, quote: str, description: str
) -> list[str]:
    """The `elect` call that records the ratification."""
    return [
        "elect",
        item,
        "--question",
        f"Ratify {Path(brief).name}",
        "--chosen",
        "ratified",
        "--chosen-description",
        description,
        "--rejected",
        "not-ratified",
        "--scope",
        brief,
        "--quote-ref",
        str(quote_ref),
        "--quote",
        quote,
    ]


def _event_id(stdout: str) -> int:
    try:
        envelope = json.loads(stdout)
        event_id = envelope["data"]["event"]["id"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("the capture call returned no event id")
    if type(event_id) is not int or event_id < 1:
        raise ValueError("the capture call returned no event id")
    return event_id


def _refuse(message: str) -> int:
    print(f"sharpen: {message}", file=sys.stderr)
    return 2


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="This script: %s" % Path(__file__).resolve(),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    link = sub.add_parser("link")
    ratify = sub.add_parser("ratify")
    for p in (link, ratify):
        p.add_argument("--item", required=True)
        p.add_argument("--brief", required=True, type=Path)
    ratify.add_argument("--words", default=None)
    ratify.add_argument("--quote", required=True)
    ratify.add_argument("--quote-ref", type=int, default=None)
    ratify.add_argument("--chosen-description", default=DEFAULT_DESCRIPTION)
    args = parser.parse_args(argv)

    try:
        ledger = _load_ledger_cli()
        cli = ledger.resolve(who="sharpen", quiet=True)
    except Exception as exc:
        print(f"sharpen: could not read the ledger setting ({exc}) — skipping the ledger step", file=sys.stderr)
        return 0
    if cli is None:
        return 0

    brief = Path(args.brief).expanduser().resolve()
    if not brief.is_file():
        return _refuse(f"brief {brief} is not a file")
    brief_text = str(brief)

    if args.command == "link":
        calls = [ledger.link_argv(args.item, brief_text)]
        quote_ref = None
    else:
        if not args.quote:
            return _refuse("--quote is empty")
        if args.quote_ref is None:
            if not args.words:
                return _refuse("--words is required unless --quote-ref names a captured utterance")
            if args.quote not in args.words:
                return _refuse("--quote is not a verbatim substring of --words; nothing was recorded")
        if args.quote_ref is not None and args.quote_ref < 1:
            return _refuse("--quote-ref must be a positive event id")
        calls = None
        quote_ref = args.quote_ref

    try:
        if calls is not None:
            ledger.call(cli, calls[0])
            print(f"linked\t{args.item}\t{brief_text}")
            return 0
        if quote_ref is None:
            session_id = os.environ.get("LEDGER_SESSION") or None
            proc = ledger.call(cli, capture_argv(args.item, brief_text, args.words, session_id))
            try:
                quote_ref = _event_id(proc.stdout)
            except ValueError as exc:
                print(f"sharpen: ledger CLI {cli} append: {exc}", file=sys.stderr)
                return 1
        try:
            ledger.call(
                cli,
                elect_argv(args.item, brief_text, quote_ref, args.quote, args.chosen_description),
            )
        except ledger.LedgerCallError as exc:
            print(
                f"sharpen: {exc}; the words are captured as event {quote_ref}, "
                f"retry the election with --quote-ref {quote_ref}",
                file=sys.stderr,
            )
            return 1
        print(f"elected\t{args.item}\tquote-ref={quote_ref}")
    except ledger.LedgerCallError as exc:
        print(f"sharpen: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

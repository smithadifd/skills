#!/usr/bin/env python3
"""Small argv-recording CLI fixture for the ticket store adapter."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def envelope(data=None, error=None):
    print(json.dumps({"ok": error is None, "data": data, "error": error}, sort_keys=True))


def option(args, name, default=None):
    try:
        return args[args.index(name) + 1]
    except ValueError:
        return default


def load(path, key, title):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"key": key, "title": title, "status": "open", "events": [], "next_id": 1}


def save(path, state):
    path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def main():
    args = sys.argv[1:]
    log_path = Path(os.environ["FAKE_LEDGER_LOG"])
    state_path = Path(os.environ["FAKE_LEDGER_STATE"])
    key = os.environ.get("FAKE_LEDGER_KEY", "EK-14")
    title = os.environ.get("FAKE_LEDGER_TITLE", "Add guarded ticket pass")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\n")
    state = load(state_path, key, title)

    known = {"doctor", "new", "append", "card", "export-item", "state"}
    verb = next((arg for arg in args if arg in known), None)
    if verb == "doctor":
        envelope({"path": str(state_path), "schema_version": 5})
        return 0
    if verb == "new":
        state["key"] = option(args, "--id")
        state["title"] = option(args, "--title")
        save(state_path, state)
        envelope({"item_id": state["key"]})
        return 0
    if verb == "append":
        event = {
            "id": state["next_id"],
            "kind": option(args, "--kind"),
            "item_id": option(args, "--item"),
            "payload": json.loads(option(args, "--payload")),
        }
        state["next_id"] += 1
        state["events"].append(event)
        save(state_path, state)
        envelope({"event": event})
        return 0
    if verb == "state":
        state["status"] = option(args, "--status")
        save(state_path, state)
        envelope({"item_id": state["key"], "status": state["status"]})
        return 0
    if verb == "card":
        envelope(
            {
                "item_id": state["key"],
                "title": state["title"],
                "status": state["status"],
                "decision_records": [],
                "reviews": [],
                "stop": None,
                "skills": [],
                "refs": [],
            }
        )
        return 0
    if verb == "export-item":
        envelope({"item_id": state["key"], "events": state["events"]})
        return 0
    envelope(error={"code": "usage", "message": "unknown verb"})
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

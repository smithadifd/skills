#!/usr/bin/env python3
"""Small argv-recording CLI fixture for the ticket store adapter.

Each call runs under an exclusive lock on the state file, the way the real
CLI runs each write in one database transaction, so separate processes see
whole calls. ``claim`` refuses atomically when another owner holds the item.
"""

from __future__ import annotations

import fcntl
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
    return {"key": key, "title": title, "status": "open", "owner": None, "events": [], "next_id": 1}


def save(path, state):
    path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def append(state, kind, item_id, payload):
    event = {"id": state["next_id"], "kind": kind, "item_id": item_id, "payload": payload}
    state["next_id"] += 1
    state["events"].append(event)
    return event


def main():
    args = sys.argv[1:]
    log_path = Path(os.environ["FAKE_LEDGER_LOG"])
    state_path = Path(os.environ["FAKE_LEDGER_STATE"])
    with open(f"{state_path}.lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return run(args, log_path, state_path)


def run(args, log_path, state_path):
    key = os.environ.get("FAKE_LEDGER_KEY", "EK-14")
    title = os.environ.get("FAKE_LEDGER_TITLE", "Add guarded ticket pass")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\n")
    state = load(state_path, key, title)
    state.setdefault("owner", None)

    known = {"doctor", "new", "append", "card", "export-item", "state", "claim"}
    verb = next((arg for arg in args if arg in known), None)
    if os.environ.get("FAKE_LEDGER_MISSING_DB"):
        envelope(error={"code": "missing_db", "message": "database does not exist: fake.db"})
        return 1
    refusal = os.environ.get("FAKE_LEDGER_REFUSE_WRITES")
    if refusal and verb in {"new", "append", "state", "claim"}:
        envelope(error={"code": "refused", "message": refusal})
        return 3
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
        event = append(state, option(args, "--kind"), option(args, "--item"), json.loads(option(args, "--payload")))
        save(state_path, state)
        envelope({"event": event})
        return 0
    if verb == "claim":
        owner = option(args, "--owner")
        if state["status"] == "claimed" and state["owner"] and state["owner"] != owner:
            envelope(error={"code": "claimed", "message": f"item already claimed by {state['owner']}"})
            return 3
        event = append(state, "item.state", state["key"], {"status": "claimed", "owner": owner})
        state["status"], state["owner"] = "claimed", owner
        save(state_path, state)
        envelope({"item_id": state["key"], "event": event})
        return 0
    if verb == "state":
        payload = {"status": option(args, "--status")}
        if option(args, "--owner"):
            payload["owner"] = option(args, "--owner")
        event = append(state, "item.state", state["key"], payload)
        state["status"], state["owner"] = payload["status"], payload.get("owner")
        save(state_path, state)
        envelope({"item_id": state["key"], "event": event})
        return 0
    if verb == "card":
        envelope(
            {
                "item_id": state["key"],
                "title": state["title"],
                "status": state["status"],
                "owner": state["owner"],
                "claim": None,
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

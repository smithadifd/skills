#!/usr/bin/env python3
"""Small argv-recording CLI fixture for the ticket store adapter.

Each call runs under an exclusive lock on the state file, the way the real
CLI runs each write in one database transaction, so separate processes see
whole calls. Like the real CLI, ``append`` stores an idempotency key once:
the same event again returns the stored one, and a different event under a
stored key is refused with code ``conflict``. ``card`` reads the status, owner
and claim from the latest ``item.state`` payload, as the real item view does,
and ``claim`` refuses atomically when another owner holds the item.
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


def options(args, name):
    return [args[index + 1] for index, arg in enumerate(args[:-1]) if arg == name]


def load(path, key, title):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"key": key, "title": title, "events": [], "next_id": 1}


def save(path, state):
    path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def append(state, kind, item_id, payload, key=None):
    event = {
        "id": state["next_id"],
        "kind": kind,
        "item_id": item_id,
        "payload": payload,
        "idempotency_key": key,
        "actor": None,
        "harness": "fake-ledger",
    }
    state["next_id"] += 1
    state["events"].append(event)
    return event


def current(state):
    """Status, owner and claim from the latest item.state payload, as the real view reads them."""

    latest = {}
    for event in state["events"]:
        if event["kind"] == "item.state":
            latest = event["payload"]
    return {
        "status": latest.get("status", "open"),
        "owner": latest.get("owner"),
        "claim": latest.get("claim"),
    }


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
        if state["events"]:
            envelope(error={"code": "conflict", "message": f"item already exists: {option(args, '--id')}"})
            return 3
        state["key"] = option(args, "--id")
        state["title"] = option(args, "--title")
        payload = {"type": option(args, "--type"), "title": state["title"]}
        event = append(state, "item.created", state["key"], payload)
        save(state_path, state)
        envelope({"item_id": state["key"], "event": event})
        return 0
    if verb == "append":
        kind, item_id = option(args, "--kind"), option(args, "--item")
        payload, idempotency_key = json.loads(option(args, "--payload")), option(args, "--idempotency-key")
        stored = next(
            (event for event in state["events"] if idempotency_key and event["idempotency_key"] == idempotency_key),
            None,
        )
        if stored is not None:
            if (stored["kind"], stored["item_id"], stored["payload"]) != (kind, item_id, payload):
                message = f"idempotency_key {idempotency_key!r} already recorded as a different event"
                envelope(error={"code": "conflict", "message": message})
                return 3
            envelope({"event": stored})
            return 0
        event = append(state, kind, item_id, payload, idempotency_key)
        save(state_path, state)
        envelope({"event": event})
        return 0
    if verb == "claim":
        owner = option(args, "--owner")
        row = current(state)
        holder = row["owner"] or row["claim"]
        if row["status"] == "claimed" and holder and holder != owner:
            envelope(error={"code": "claimed", "message": f"item already claimed by {holder}"})
            return 3
        event = append(state, "item.state", state["key"], {"status": "claimed", "owner": owner, "claim": owner})
        save(state_path, state)
        envelope({"item_id": state["key"], "event": event})
        return 0
    if verb == "state":
        payload = {"status": option(args, "--status")}
        for name in ("owner", "claim"):
            if option(args, f"--{name}"):
                payload[name] = option(args, f"--{name}")
        if options(args, "--blocked-by"):
            payload["blocked_by"] = options(args, "--blocked-by")
        event = append(state, "item.state", state["key"], payload)
        save(state_path, state)
        envelope({"item_id": state["key"], "event": event})
        return 0
    if verb == "card":
        row = current(state)
        envelope(
            {
                "item_id": state["key"],
                "title": state["title"],
                "status": row["status"],
                "owner": row["owner"],
                "claim": row["claim"],
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

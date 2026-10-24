"""A stand-in for the ledger CLI that the ticket skill's ledger store drives, for its tests.

It answers the verbs the store sends (``query --item``, ``export-item``,
``append-many`` and ``append-many --help``) with the CLI's ``{ok, data,
error}`` envelope and its exit statuses: an item's revision is the id of its
newest event, ``export-item`` hides an item's session records before its
latest close (all of them while it is closed), and ``append-many`` lands its
batch whole or not at all, replays a stored key, refuses a stored key that
holds another event (``conflict``) and checks ``--expect-revision`` once,
before the first event it writes (``stale_revision``, naming the newest
event's actor and session).

Its state is one JSON file, named by the first argument: the events, every
call (argv, stdin, exit status and error code), how the help probe answers,
and a queue of scripted answers. A scripted answer replaces the next answer to
its verb with any text (an envelope, garbage, or nothing: a lost response)
and any exit status, after carrying the call out or without it.
"""

import json
import os
import secrets
import sys

EPHEMERAL = ("session.opened", "session.closed", "item.progress")
FIELDS = ("kind", "payload", "item_id", "idempotency_key", "payload_version", "channel", "provenance_class")
HELP = {
    "ok": (0, "usage: ledger append-many [-h] [--json] [--db DB] [--expect-revision EVENT_ID|none] item\n"),
    "no --expect-revision": (0, "usage: ledger append-many [-h] [--json] [--db DB] item\n"),
    "no append-many": (2, "usage: ledger [-h] [--json] [--db DB] {query,export-item} ...\n"
                          "ledger: error: argument verb: invalid choice: 'append-many'\n"),
}


class Refusal(Exception):
    def __init__(self, code, message, status):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def envelope(ok, data=None, code=None, message=None):
    error = None if ok else {"code": code, "message": message}
    return json.dumps({"data": data, "error": error, "ok": ok}, sort_keys=True) + "\n"


def of_item(events, item):
    return [event for event in events if event["item_id"] == item]


def status_of(events):
    status = "open"
    for event in events:
        if event["kind"] in ("item.created", "item.state") and "status" in event["payload"]:
            status = event["payload"]["status"]
    return status


def query(state, item):
    events = of_item(state["events"], item)
    created = [event for event in events if event["kind"] == "item.created"]
    if not created:
        raise Refusal("empty", "no matching rows", 1)
    states = [event["payload"] for event in events if event["kind"] == "item.state"]
    latest = states[-1] if states else {}
    kind = {"type": created[0]["payload"]["type"]} if "type" in created[0]["payload"] else {}
    row = {
        "item_id": item, **kind, "title": created[0]["payload"]["title"],
        "status": status_of(events), "owner": latest.get("owner"), "claim": latest.get("claim"),
        "revision": events[-1]["id"],
    }
    return {"items": [row], "limit": 50, "truncated": False}


def export_item(state, item):
    events = of_item(state["events"], item)
    if not any(event["kind"] == "item.created" for event in events):
        raise Refusal("not_found", f"item not found: {item}", 1)
    if status_of(events) == "closed":
        boundary = float("inf")
    else:
        boundary = max(
            (event["id"] for event in events
             if event["kind"] in ("item.created", "item.state") and event["payload"].get("status") == "closed"),
            default=0,
        )
    shown = [event for event in events if not (event["kind"] in EPHEMERAL and event["id"] < boundary)]
    return {"item_id": item, "events": shown}


def append_many(state, item, expect, stdin):
    try:
        batch = json.loads(stdin)
    except ValueError:
        raise Refusal("usage", "stdin is not JSON", 2)
    if type(batch) is not list or not batch:
        raise Refusal("usage", "stdin must be a non-empty JSON array of event objects", 2)
    for index, element in enumerate(batch):
        if type(element) is not dict or set(element) - set(FIELDS) or not {"kind", "payload"} <= set(element):
            raise Refusal("usage", f"event {index} has an unknown or a missing field", 2)
        if element.get("item_id", item) != item:
            raise Refusal("usage", f"event {index} is for another item", 2)
    events = list(state["events"])
    answer = []
    pending = expect
    for element in batch:
        key = element.get("idempotency_key") or secrets.token_hex(16)
        stored = next((event for event in events if event["idempotency_key"] == key), None)
        if stored is not None:
            if (stored["kind"], stored["payload"], stored["item_id"]) != (element["kind"], element["payload"], item):
                raise Refusal("conflict", f"idempotency_key {key!r} already recorded as a different event", 3)
            answer.append(stored)
            continue
        if pending is not None:
            mine = of_item(events, item)
            current = mine[-1] if mine else None
            if not ((pending == "none" and current is None) or (current is not None and str(current["id"]) == pending)):
                if current is None:
                    detail = "item has no events"
                else:
                    detail = (f"event {current['id']} written by actor={current['actor'] or '-'} "
                              f"session={current['session'] or '-'}")
                raise Refusal(
                    "stale_revision",
                    f"stale revision for item {item}: expected {pending}, "
                    f"current is {'none' if current is None else current['id']} ({detail})",
                    5,
                )
            pending = None
        event = {
            "id": (events[-1]["id"] if events else 0) + 1, "kind": element["kind"], "payload": element["payload"],
            "payload_version": element.get("payload_version", 1), "item_id": item, "idempotency_key": key,
            "actor": os.environ.get("LEDGER_AGENT") or None, "session": os.environ.get("LEDGER_SESSION") or None,
        }
        events.append(event)
        answer.append(event)
    state["events"] = events
    return {"item_id": item, "events": answer}


def carry_out(state, verb, args, stdin):
    try:
        if verb == "query" and "--item" in args:
            data = query(state, args[args.index("--item") + 1])
        elif verb == "export-item" and len(args) == 2:
            data = export_item(state, args[1])
        elif verb == "append-many" and len(args) >= 2:
            expect = args[args.index("--expect-revision") + 1] if "--expect-revision" in args else None
            data = append_many(state, args[1], expect, stdin)
        else:
            raise Refusal("usage", f"the fake does not answer {' '.join(args)!r}", 2)
    except Refusal as refusal:
        return refusal.status, envelope(False, code=refusal.code, message=refusal.message)
    return 0, envelope(True, data)


def main():
    state_path, argv = sys.argv[1], sys.argv[2:]
    stdin = sys.stdin.read()
    with open(state_path, encoding="utf-8") as handle:
        state = json.load(handle)
    args = [arg for arg in argv if arg != "--json"]
    verb = args[0] if args else ""
    call = {"argv": argv, "stdin": stdin}
    state["calls"].append(call)
    if verb == "append-many" and "--help" in args:
        status, out = HELP[state["help"]]
    else:
        scripted = state["script"].pop(0) if state["script"] and state["script"][0]["verb"] == verb else None
        status, out = 0, ""
        if scripted is None or scripted.get("commit"):
            status, out = carry_out(state, verb, args, stdin)
        if scripted is not None:
            out = scripted.get("stdout", out)
            status = scripted.get("status", status)
    try:
        call["code"] = (json.loads(out).get("error") or {}).get("code")
    except (ValueError, AttributeError):
        call["code"] = None
    call["status"] = status
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    sys.stdout.write(out)
    return status


if __name__ == "__main__":
    sys.exit(main())

"""A stand-in for the ledger CLI that the ticket skill's ledger store drives, for its tests.

It answers the verbs the store sends (``query --item``, ``export-item``,
``append-many`` and ``append-many --help``), and the ones the ledger-item
source reads (``spec show ITEM`` and ``links ITEM``, as ledger 0.1.22 answers
them), with the CLI's ``{ok, data, error}`` envelope and its exit statuses:
``spec show`` refuses an unknown item ``not_found`` and an item with no spec
``no_spec``, and ``links`` refuses an unknown item ``not_found`` and an item no
link names ``empty``. An item's revision is the id of its
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
and any exit status, after carrying the call out or without it. A scripted
``drop_events`` (with ``commit``) tampers with an ``export-item`` answer: it
omits the newest N events.
"""

import json
import os
import secrets
import sys
import time

EPHEMERAL = ("session.opened", "session.closed", "item.progress")
# The spec's content fields and its provenance, as ``spec show`` prints them.
SPEC_FIELDS = ("title", "body_md", "acceptance", "size", "merge_class", "source", "source_rev")
# The link relations whose target is an item id.
ITEM_RELS = ("item", "blocks", "duplicate-of", "child-of")
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


def exists(state, item):
    return any(event["kind"] == "item.created" for event in of_item(state["events"], item))


def spec_show(state, item):
    if not exists(state, item):
        raise Refusal("not_found", f"item not found: {item}", 1)
    specs = [event for event in of_item(state["events"], item) if event["kind"] == "item.spec"]
    if not specs:
        raise Refusal("no_spec", f"item {item} has no spec", 1)
    event = specs[-1]
    payload = event["payload"]
    data = {
        "item_id": item, "version": payload["version"], "event_id": event["id"],
        "recorded_at": event.get("recorded_at"), "content_hash": payload["content_hash"],
    }
    data.update({name: payload.get(name) for name in SPEC_FIELDS})
    return data


def links(state, item):
    if not exists(state, item):
        raise Refusal("not_found", f"item not found: {item}", 1)
    titles = {}
    for event in state["events"]:
        if event["kind"] == "item.created":
            titles.setdefault(event["item_id"], event["payload"]["title"])
    outgoing, incoming = [], []
    for event in state["events"]:
        if event["kind"] != "item.linked":
            continue
        rel, target = event["payload"].get("rel"), event["payload"].get("target")
        if event["item_id"] == item:
            resolution = "item" if target in titles else ("unresolved" if rel in ITEM_RELS else "external")
            outgoing.append({"event_id": event["id"], "rel": rel, "target": target, "resolution": resolution,
                             "title": titles.get(target)})
        if target == item:
            resolution = "item" if event["item_id"] in titles else "unresolved"
            incoming.append({"event_id": event["id"], "rel": rel, "source": event["item_id"], "resolution": resolution,
                             "title": titles.get(event["item_id"])})
    if not outgoing and not incoming:
        raise Refusal("empty", f"no links name {item}", 1)
    return {"item_id": item, "title": titles[item], "outgoing": outgoing, "incoming": incoming}


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
        elif verb == "spec" and len(args) == 3 and args[1] == "show":
            data = spec_show(state, args[2])
        elif verb == "links" and len(args) == 2:
            data = links(state, args[1])
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
    if verb == "append-many" and "--help" in args and state["help"] == "hang":
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        time.sleep(60)
        return 0
    if verb == "append-many" and "--help" in args and state["help"] == "non-utf8":
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        sys.stdout.buffer.write(b"usage: ledger append-many \xff\xfe not text\n")
        sys.stdout.buffer.flush()
        return 0
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
            if scripted.get("drop_events"):  # tamper: the answer omits the newest N events
                answer = json.loads(out)
                answer["data"]["events"] = answer["data"]["events"][: -scripted["drop_events"]]
                out = json.dumps(answer, sort_keys=True) + "\n"
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

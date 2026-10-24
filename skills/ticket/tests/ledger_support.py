"""The fake ledger CLI behind the ledger store's tests, and builders for the card fixtures on it (matrix section H).

``FakeLedger`` writes an executable that runs ``fake_ledger_cli.py`` over a
state file of its own. The test reads the events and every call from that
file, appends an outsider's events straight into it, and scripts the next
answer to a verb.
"""

from __future__ import annotations

import json
import secrets
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

import ticket
from ticket_support import KEY, TITLE, changes, go_outward, go_plan, passing, present

FAKE_CLI = Path(__file__).resolve().parent / "fake_ledger_cli.py"
# The exit status the ledger CLI gives each error code (design 6.2.4).
EXIT = {
    "stale_revision": 5, "conflict": 3, "validation": 3, "missing_harness": 3, "shared_database": 3,
    "synced_path": 3, "not_found": 1, "empty": 1, "missing_db": 1, "integrity": 4, "usage": 2, "error": 3,
    "unusable": 3,
}
WRITER = "ticket"


@dataclass(frozen=True)
class Call:
    argv: list
    stdin: str
    status: int
    code: str | None

    @property
    def verb(self):
        return next((arg for arg in self.argv if arg != "--json"), "")

    @property
    def batch(self):
        return json.loads(self.stdin)


class FakeLedger:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.state_path = directory / "ledger-state.json"
        self._save({"events": [], "calls": [], "script": [], "help": "ok"})
        self.path = directory / "ledger"
        command = shlex.join([sys.executable, str(FAKE_CLI), str(self.state_path)])
        self.path.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="utf-8")
        self.path.chmod(0o755)

    def _load(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save(self, state):
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def _change(self, change):
        state = self._load()
        change(state)
        self._save(state)

    def store(self, title=TITLE):
        return ticket.LedgerStore(self.path, KEY, title)

    def events(self):
        return self._load()["events"]

    def calls(self, verb=None):
        calls = [Call(c["argv"], c["stdin"], c["status"], c["code"]) for c in self._load()["calls"]]
        return [call for call in calls if verb is None or call.verb == verb]

    def appends(self):
        return [call for call in self.calls("append-many") if "--help" not in call.argv]

    def help(self, mode):
        self._change(lambda state: state.update(help=mode))

    def script(self, verb, **answer):
        """The next answer to ``verb``: ``stdout`` and ``status`` replace the CLI's; ``commit`` carries the call out first."""

        self._change(lambda state: state["script"].append({"verb": verb, **answer}))

    def answer(self, verb, code, status=None, commit=False, message=None):
        """The next ``verb`` answers ``code`` with the exit status the CLI gives it, unless ``status`` pairs another."""

        message = message or f"the ledger refused this call ({code})"
        out = json.dumps({"data": None, "error": {"code": code, "message": message}, "ok": False}, sort_keys=True)
        self.script(verb, stdout=out + "\n", status=EXIT[code] if status is None else status, commit=commit)

    def outside(self, kind, payload, actor="outsider", session="outside-session"):
        """An outsider's event, appended straight into the ledger with a random key."""

        def append(state):
            events = state["events"]
            events.append({
                "id": (events[-1]["id"] if events else 0) + 1, "kind": kind, "payload": payload, "payload_version": 1,
                "item_id": KEY, "idempotency_key": secrets.token_hex(16), "actor": actor, "session": session,
            })

        self._change(append)


def open_ledger(cli, src, pass_id=None, handle=None):
    """Open a pass on a fresh ledger handle over ``cli`` (a ``FakeLedger`` or the path of a real CLI)."""

    if handle is None:
        handle = cli.store() if isinstance(cli, FakeLedger) else ticket.LedgerStore(cli, KEY, TITLE)
    return ticket.TicketPass.open(KEY, src, handle, pass_id=pass_id)


def build_ledger(cli, src, stage, rounds=0):
    """Drive a fresh pass on the ledger to a matrix stage and return it (live at S1-S4), as ``ticket_support.build``."""

    session = open_ledger(cli, src)
    if stage == "S0":
        return session
    present(session)
    if stage == "S1":
        return session
    go_plan(session)
    session.begin_build()
    if stage == "S2":
        for _ in range(rounds):
            changes(session)
        return session
    if stage == "S5":
        for _ in range(3):
            changes(session)
        return session
    for _ in range(max(rounds, 1) - 1):
        changes(session)
    passing(session)
    if stage == "S3":
        return session
    go_outward(session)
    if stage == "S4":
        return session
    session.close("Ticket pass complete.", refs=("example/repo#14",), next_skills=("ticket",))
    return session


class ReadHook(ticket.LedgerStore):
    """A ledger handle that runs ``after_read`` once, right after its first read returns."""

    def __init__(self, cli, key, title, after_read):
        super().__init__(cli, key, title)
        self._after_read = after_read

    def read(self):
        result = super().read()
        after, self._after_read = self._after_read, None
        if after is not None:
            after()
        return result

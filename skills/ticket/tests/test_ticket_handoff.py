"""Matrix section O: a parent's hand-off as the ticket source (design 6.8).

The hand-off source is the ledger-item source plus the hand-off's conditions. It reads the item's one open
``loop.intent`` of payload version 2 (ledger 0.7.0, written by the loop writer) for the controller and the seams,
joins the lease the loop holds under the holder handed to the open, and never takes one. The ledger store derives the
card's ``handoff`` field from that event on every read; nothing records it. Rows marked (real) run on the fake ledger
CLI always and also on a real ledger CLI when ``LEDGER_REAL_CLI`` names one, seeded by the loop library beside that
CLI (``ledger.loop.take_item``, ``claim_operation``, ``finish_operation``); every other row runs on the fake, where
the loop's events are planted with the shapes and payload versions the loop writer gives them. Every refusal also
shows the item's events unchanged and, on the fake, no ``append-many`` sent. The G rows pin what must not change.
"""

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import ticket
from ledger_support import (
    LIVE_STAMP, FakeLedger, ReadHook, ledger_library, machine, plant_done, plant_intent, plant_take,
)
from ticket_support import A, B, KEY, TITLE, Source, refusal, write_card

LEDGER_REAL_CLI = os.environ.get("LEDGER_REAL_CLI", "")
# The backends a (real) row runs on: the fake always, and the real ledger CLI when LEDGER_REAL_CLI names one.
BACKENDS = ["fake", *(["real"] if LEDGER_REAL_CLI else [])]
PAST = "2000-01-01T00:00:00.000Z"
HOLDER = "roll-seat-1"
DEFAULTS = {"build": "builder-core", "review": "reviewer", "outward": "push-and-open-draft"}
LOOP = {"build": "roll-dispatch", "review": "review-runner", "outward": "roll-land"}
DECLARED = {"build": ["roll-dispatch"], "review": ["review-runner"], "outward": ["roll-land"]}
BULLETS = f"- {A}\n- {B}"
FAIL_CLOSED = (
    "Where a seam's name is not the default, this session does not run the default action for that seam"
)


class Fake:
    def __init__(self, tmp_path):
        self.fake = FakeLedger(tmp_path / "ledger")
        self.path = self.fake.path

    def events(self):
        return [event for event in self.fake.events() if event["item_id"] == KEY]

    def write(self, kind, payload):
        """An outsider's event on the ticket's item."""

        self.fake.outside(kind, payload)

    def file_row(self, acceptance=BULLETS, spec=True):
        self.fake.new_item()
        if spec:
            self.fake.set_spec(acceptance)

    def take(self, holder=HOLDER, n=1):
        plant_take(self.fake, holder, n)

    def hand_off(self, operation_id="build-1", controller="director", seams=LOOP, holder=HOLDER):
        payload = {"operation": "build", "operation_id": operation_id, "controller": controller, "seams": dict(seams)}
        plant_intent(self.fake, "build", operation_id, holder, payload=payload, payload_version=2, hand_off=True)

    def intent_v1(self, operation="build", operation_id="build-1", holder=HOLDER):
        plant_intent(self.fake, operation, operation_id, holder, hand_off=operation == "build")

    def done(self, operation_id="build-1", outcome="handback", holder=HOLDER):
        plant_done(self.fake, "build", operation_id, holder, outcome)


class Real:
    """A real ledger CLI over a database of this test's own (``LEDGER_DB``); the loop library beside it seeds the loop."""

    def __init__(self, tmp_path, monkeypatch):
        self.db = tmp_path / "ledger.db"
        monkeypatch.setenv("LEDGER_DB", str(self.db))
        monkeypatch.setenv("LEDGER_HARNESS", "ticket-tests")
        self.path = LEDGER_REAL_CLI
        self.loop, self._open = ledger_library(self.path)
        # A read never creates the database, so the ledger holds another item first.
        self._run("append-many", "EK-1", stdin=json.dumps([{"kind": "item.created", "payload": {"type": "ticket", "title": "Another ticket"}}]))

    def _run(self, *args, stdin=""):
        subprocess.run([self.path, "--json", *args], input=stdin, text=True, check=True, capture_output=True)

    def events(self):
        with sqlite3.connect(self.db) as database:
            rows = database.execute(
                "SELECT id, kind, payload, payload_version, idempotency_key FROM events WHERE item_id = ? ORDER BY id",
                (KEY,),
            ).fetchall()
        return [{"id": i, "kind": k, "payload": json.loads(p), "payload_version": v, "idempotency_key": key}
                for i, k, p, v, key in rows]

    def write(self, kind, payload):
        self._run("append-many", KEY, stdin=json.dumps([{"kind": kind, "payload": payload}]))

    def file_row(self, acceptance=BULLETS, spec=True):
        self._run("new", "--id", KEY, "--type", "ticket", "--title", TITLE)
        if spec:
            arguments = [f"--title={TITLE}", "--body=Build the change the criteria describe."]
            if acceptance is not None:
                arguments.append(f"--acceptance={acceptance}")
            self._run("spec", "set", KEY, *arguments)

    def take(self, holder=HOLDER, n=1):
        with self._open(self.db, create=False) as lg:
            self.loop.take_item(lg, KEY, holder, idempotency_key=f"loop:{KEY}:managed", claim_key=f"take:{KEY}:{n}")

    def hand_off(self, operation_id="build-1", controller="director", seams=LOOP, holder=HOLDER):
        with self._open(self.db, create=False) as lg:
            self.loop.claim_operation(lg, KEY, "build", operation_id, holder=holder, hand_off=True,
                                      controller=controller, seams=dict(seams))

    def intent_v1(self, operation="build", operation_id="build-1", holder=HOLDER):
        with self._open(self.db, create=False) as lg:
            self.loop.claim_operation(lg, KEY, operation, operation_id, holder=holder, hand_off=operation == "build")

    def done(self, operation_id="build-1", outcome="handback", holder=HOLDER):
        with self._open(self.db, create=False) as lg:
            self.loop.finish_operation(lg, KEY, "build", operation_id, outcome=outcome, holder=holder)


@pytest.fixture(params=BACKENDS)
def backend(request, tmp_path, monkeypatch):
    chosen = Fake(tmp_path) if request.param == "fake" else Real(tmp_path, monkeypatch)
    monkeypatch.setenv("TICKET_LEDGER_CLI", str(chosen.path))
    return chosen


@pytest.fixture
def fake(tmp_path, monkeypatch):
    chosen = Fake(tmp_path)
    monkeypatch.setenv("TICKET_LEDGER_CLI", str(chosen.path))
    return chosen


def handed_off(backend, seams=LOOP, controller="director"):
    """An item filed with a spec, taken by the loop as ``HOLDER`` and handed off as ``build-1``."""

    backend.file_row()
    backend.take()
    backend.hand_off(controller=controller, seams=seams)


def intent_id(backend, operation_id="build-1"):
    return [event["id"] for event in backend.events()
            if event["kind"] == "loop.intent" and event["payload"].get("operation_id") == operation_id][-1]


def open_handoff(repo, declared=None, pass_id=None, holder=HOLDER, store=None):
    """Open through the hand-off source the way SKILL.md says: a fresh ``choose_store`` handle for every open."""

    src = ticket.handoff_source(KEY, implementations=declared)
    return ticket.TicketPass.open(KEY, src, store or ticket.choose_store(repo, KEY, TITLE), pass_id=pass_id, holder=holder)


def open_ledger_id(repo, holder=None, pass_id=None):
    src = ticket.ledger_item_source(KEY)
    return ticket.TicketPass.open(KEY, src, ticket.choose_store(repo, KEY, TITLE), pass_id=pass_id, holder=holder)


def pass_records(events):
    return [event for event in events if event["kind"] == "item.note" and event["payload"]["text"].startswith("ticket-pass:")]


def record_of(event):
    return json.loads(event["payload"]["text"].split(" ", 1)[1])


def latest_claim(backend):
    """The item's latest claim-bearing event (an ``item.state`` naming a holder): its id and holder."""

    claims = [(event["id"], event["payload"].get("owner") or event["payload"].get("claim")) for event in backend.events()
              if event["kind"] == "item.state" and (event["payload"].get("owner") or event["payload"].get("claim"))]
    return claims[-1] if claims else None


def row(backend):
    answer = subprocess.run([str(backend.path), "--json", "query", "--item", KEY], capture_output=True, text=True,
                            check=True)
    return json.loads(answer.stdout)["data"]["items"][0]


def refused(backend, call, code):
    """``call`` is refused ``code``; the item's events are unchanged and, on the fake, no ``append-many`` was sent."""

    before = backend.events()
    sent = len(backend.fake.appends()) if isinstance(backend, Fake) else None
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        call()
    assert refusal(excinfo) == code, str(excinfo.value)
    assert backend.events() == before
    if sent is not None:
        assert len(backend.fake.appends()) == sent
    return str(excinfo.value)


def expected_handoff(backend, seams=LOOP, controller="director", operation_id="build-1"):
    return {"controller": controller, "seams": dict(seams), "operation_id": operation_id,
            "event_id": intent_id(backend, operation_id)}


def fresh_card(backend):
    return ticket.LedgerStore(backend.path, KEY, TITLE).read()[0]


# --- O1-O4: the open, the resume and the derived field ----------------------------------------------------------


def test_O1_a_new_pass_through_the_hand_off_joins_the_loops_lease_and_carries_its_controller_and_seams(repo, backend):
    seams = {"build": "roll-dispatch", "review": "reviewer", "outward": "push-and-open-draft"}
    handed_off(backend, seams=seams)
    claim = latest_claim(backend)
    before = backend.events()
    session = open_handoff(repo, declared={"build": ["roll-dispatch"]})
    written = backend.events()[len(before):]
    assert [(event["kind"], event["idempotency_key"]) for event in written] == [
        ("item.note", f"ticket-pass/{KEY}/{session.pass_id}/record")]
    record = record_of(written[0])
    assert set(record) == {"id", "host", "started_at", "holder", "lease"}
    assert (record["id"], record["holder"], record["lease"]) == (session.pass_id, HOLDER, "joined")
    expected = expected_handoff(backend, seams)
    assert session.resume_card["handoff"] == expected
    assert ticket.seam_set(session.resume_card) == seams
    assert ticket.seam_set(session.resume_card, "build") == {"build": "roll-dispatch"}
    assert fresh_card(backend)["handoff"] == expected
    assert session.reconciliation[1] == (
        "hand-off: controller director; seams build roll-dispatch, review reviewer, outward push-and-open-draft; "
        f"operation build-1, event {expected['event_id']}"
    )
    assert (session.phase, session.live) == ("orient", True)
    if isinstance(backend, Fake):
        (sent,) = [call for call in backend.fake.appends() if "ticket-pass:" in call.stdin]
        assert [element["kind"] for element in sent.batch] == ["item.note"]
        assert sent.argv[-2] == "--expect-revision"
    mark = len(backend.events())
    session.end()
    assert [event["kind"] for event in backend.events()[mark:]] == ["session.closed"]
    assert latest_claim(backend) == claim


def test_O2_a_hand_off_whose_seams_are_the_defaults_opens_with_nothing_declared(repo, fake):
    handed_off(fake, seams=DEFAULTS)
    session = open_handoff(repo)
    assert session.live and session.resume_card["handoff"] == expected_handoff(fake, DEFAULTS)
    assert ticket.seam_set(session.resume_card) == DEFAULTS


def test_O3_a_resume_through_the_hand_off_writes_nothing_and_carries_the_hand_off_again(repo, backend):
    handed_off(backend)
    first = open_handoff(repo, declared=DECLARED)
    before = backend.events()
    sent = len(backend.fake.appends()) if isinstance(backend, Fake) else None
    resumed = open_handoff(repo, declared=DECLARED, pass_id=first.pass_id)
    assert (resumed.live, resumed.pass_id) == (True, first.pass_id)
    assert backend.events() == before
    if sent is not None:
        assert len(backend.fake.appends()) == sent
    assert resumed.resume_card["handoff"] == expected_handoff(backend)
    assert resumed.reconciliation[1].startswith("hand-off: controller director; seams build roll-dispatch, ")


def test_O4_every_read_derives_the_field_again_from_the_open_hand_off(repo, backend):
    handed_off(backend)
    open_handoff(repo, declared=DECLARED)
    assert fresh_card(backend)["handoff"]["controller"] == "director"
    backend.done("build-1")
    assert "handoff" not in fresh_card(backend)
    backend.hand_off("build-2", controller="director-2")
    card = fresh_card(backend)
    assert card["handoff"] == expected_handoff(backend, controller="director-2", operation_id="build-2")
    assert ticket.seam_set(card) == LOOP


# --- O5-O8: the holder, the store, the ledger, the version and the managed item --------------------------------


def test_O5_the_hand_off_source_requires_a_holder_before_any_cli_call(repo, fake):
    handed_off(fake)
    src = ticket.handoff_source(KEY, implementations=DECLARED)
    store = ticket.choose_store(repo, KEY, TITLE)
    calls = len(fake.fake.calls())
    refused(fake, lambda: ticket.TicketPass.open(KEY, src, store), "holder_invalid")
    assert len(fake.fake.calls()) == calls


@pytest.mark.parametrize("given", ["markdown", "another item"])
def test_O6_another_store_is_refused_before_anything_is_read(repo, fake, given):
    handed_off(fake)
    src = ticket.handoff_source(KEY, implementations=DECLARED)
    store = ticket.MarkdownStore(repo, KEY, TITLE) if given == "markdown" else ticket.LedgerStore(fake.path, "EK-2", TITLE)
    calls = len(fake.fake.calls())
    refused(fake, lambda: ticket.TicketPass.open(KEY, src, store, holder=HOLDER), "source_store_mismatch")
    assert len(fake.fake.calls()) == calls


@pytest.mark.parametrize("how", ["off by setting", "not configured"])
def test_O6b_with_the_ledger_off_the_source_is_refused_with_no_ledger_call(tmp_path, monkeypatch, how):
    fake = FakeLedger(tmp_path / "bin")
    machine(monkeypatch, tmp_path, fake)
    if how == "off by setting":
        monkeypatch.setenv("TICKET_LEDGER_CLI", "none")
    else:
        monkeypatch.delenv("LEDGER_DB")
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.handoff_source(KEY, implementations=DECLARED)
    assert refusal(excinfo) == "handoff_no_ledger"
    assert fake.calls() == []


REFUSED_LINES = {
    "0.6.3": ("ledger 0.6.3\n", 0),
    "0.6.99": ("ledger 0.6.99\n", 0),
    "0.7": ("ledger 0.7\n", 0),
    "0.7.0-dev": ("ledger 0.7.0-dev\n", 0),
    "other text": ("ledger version 0.7.0\n", 0),
    "empty": ("", 0),
    "non-zero exit": ("ledger 0.7.0\n", 1),
}


@pytest.mark.parametrize("name", list(REFUSED_LINES))
def test_O7_a_version_line_below_0_7_0_or_unreadable_is_refused_and_is_the_only_call(repo, fake, name):
    handed_off(fake)
    src = ticket.handoff_source(KEY, implementations=DECLARED)
    store = ticket.choose_store(repo, KEY, TITLE)
    line, status = REFUSED_LINES[name]
    fake.fake.script("--version", stdout=line, status=status)
    calls = len(fake.fake.calls())
    message = refused(fake, lambda: ticket.TicketPass.open(KEY, src, store, holder=HOLDER), "handoff_ledger_too_old")
    assert [call.argv for call in fake.fake.calls()[calls:]] == [["--version"]]
    assert repr(line) in message


@pytest.mark.parametrize("line", ["ledger 0.7.0\n", "ledger 0.10.0\n", "ledger 1.0.0\n"], ids=["0.7.0", "0.10.0", "1.0.0"])
def test_O7_a_version_line_at_0_7_0_or_later_passes(repo, fake, line):
    handed_off(fake)
    src = ticket.handoff_source(KEY, implementations=DECLARED)
    store = ticket.choose_store(repo, KEY, TITLE)
    fake.fake.script("--version", stdout=line, status=0)
    calls = len(fake.fake.calls())
    session = ticket.TicketPass.open(KEY, src, store, holder=HOLDER)
    assert session.live
    assert fake.fake.calls()[calls].argv == ["--version"]


def test_O7_a_version_call_with_no_answer_in_time_is_refused(repo, tmp_path, monkeypatch):
    silent = tmp_path / "silent-ledger"
    silent.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    silent.chmod(0o755)
    monkeypatch.setattr(ticket, "LEDGER_PROBE_TIMEOUT", 0.5)
    src = ticket.HandoffSource(str(silent), DECLARED)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.TicketPass.open(KEY, src, ticket.LedgerStore(str(silent), KEY, TITLE), holder=HOLDER)
    assert refusal(excinfo) == "handoff_ledger_too_old"
    assert "nothing within 0.5 seconds" in str(excinfo.value)


@pytest.mark.parametrize("case", ["no loop.managed", "an intent planted without one", "no such item"])
def test_O8_an_item_that_is_not_loop_managed_is_refused(repo, fake, case):
    if case != "no such item":
        fake.file_row()
        fake.write("item.state", {"status": "claimed", "claim": HOLDER, "expires_at": "2999-01-01T00:00:00.000Z"})
    if case == "an intent planted without one":
        payload = {"operation": "build", "operation_id": "build-1", "controller": "director", "seams": dict(LOOP)}
        plant_intent(fake.fake, "build", "build-1", HOLDER, payload=payload, payload_version=2, hand_off=True)
    src = ticket.handoff_source(KEY, implementations=DECLARED)
    store = ticket.choose_store(repo, KEY, TITLE)
    calls = len(fake.fake.calls())
    refused(fake, lambda: ticket.TicketPass.open(KEY, src, store, holder=HOLDER), "handoff_not_managed")
    made = [call.verb for call in fake.fake.calls()[calls:]]
    assert made == (["--version", "query"] if case == "no such item" else
                    ["--version", "query", "spec", "links", "query", "export-item"])


# --- O9-O13: the reader rule and the hand-off's grammar --------------------------------------------------------


@pytest.mark.parametrize("case", ["managed with no intent", "the hand-off closed by its done", "only a review intent open"])
def test_O9_an_item_with_no_open_build_hand_off_is_refused(repo, backend, case):
    backend.file_row()
    backend.take()
    if case == "the hand-off closed by its done":
        backend.hand_off()
        backend.done("build-1")
    elif case == "only a review intent open":
        backend.intent_v1("review", "review-1")
    refused(backend, lambda: open_handoff(repo, declared=DECLARED), "handoff_no_intent")


def test_O9b_a_done_of_another_operation_under_the_same_id_does_not_close_the_hand_off(repo, fake):
    handed_off(fake)
    plant_done(fake.fake, "review", "build-1", HOLDER)
    session = open_handoff(repo, declared=DECLARED)
    assert session.resume_card["handoff"] == expected_handoff(fake)


def test_O10_an_open_version_1_build_intent_is_not_a_hand_off(repo, backend):
    backend.file_row()
    backend.take()
    backend.intent_v1("build", "build-1")
    message = refused(backend, lambda: open_handoff(repo, declared=DECLARED), "handoff_intent_v1")
    assert "build-1" in message


def _payload(**change):
    payload = {"operation": "build", "operation_id": "build-1", "controller": "director", "seams": dict(LOOP)}
    for name, value in change.items():
        if value is None:
            payload.pop(name)
        else:
            payload[name] = value
    return payload


GRAMMAR = {
    "no controller": (_payload(controller=None), 2),
    "no seams": (_payload(seams=None), 2),
    "a seam missing": (_payload(seams={"build": "roll-dispatch", "review": "review-runner"}), 2),
    "an extra seam": (_payload(seams={**LOOP, "merge": "roll-land"}), 2),
    "a parent key": (_payload(parent="GATE-1"), 2),
    "controller empty": (_payload(controller=""), 2),
    "controller with a space": (_payload(controller="the director"), 2),
    "controller of 65 characters": (_payload(controller="d" * 65), 2),
    "controller with a Cyrillic letter": (_payload(controller="dirеctor"), 2),
    "a seam that is not a token": (_payload(seams={**LOOP, "build": "roll dispatch"}), 2),
    "operation review on version 2": (_payload(operation="review"), 2),
    "payload_version missing": (_payload(), None),
    "payload_version a string": (_payload(), "2"),
    "payload_version 3": (_payload(), 3),
}


@pytest.mark.parametrize("case", list(GRAMMAR))
def test_O11_a_hand_off_that_breaks_the_grammar_is_unreadable(repo, fake, case):
    fake.file_row()
    fake.take()
    payload, version = GRAMMAR[case]
    plant_intent(fake.fake, payload["operation"], "build-1", HOLDER, payload=payload, payload_version=version, hand_off=True)
    refused(fake, lambda: open_handoff(repo, declared=DECLARED), "handoff_unreadable")
    assert "handoff" not in fresh_card(fake)


def test_O11_a_loop_event_that_names_no_operation_id_is_unreadable_never_skipped(repo, fake):
    handed_off(fake)
    fake.fake.outside("loop.done", {"operation": "build", "outcome": "handback"}, actor="loop")
    refused(fake, lambda: open_handoff(repo, declared=DECLARED), "handoff_unreadable")
    assert "handoff" not in fresh_card(fake)


def test_O11_a_controller_of_64_characters_is_a_name(repo, fake):
    handed_off(fake, controller="d" * 64)
    assert open_handoff(repo, declared=DECLARED).resume_card["handoff"]["controller"] == "d" * 64


def test_O12_two_open_hand_offs_are_ambiguous_and_neither_is_picked(repo, fake):
    handed_off(fake)
    fake.hand_off("build-2", controller="director-2")
    message = refused(fake, lambda: open_handoff(repo, declared=DECLARED), "handoff_ambiguous")
    assert "build-1" in message and "build-2" in message
    assert "handoff" not in fresh_card(fake)


UNKNOWN = {
    "nothing declared": (LOOP, None, "build", "roll-dispatch"),
    "declared for another seam": ({**DEFAULTS, "review": "roll-dispatch"}, {"build": ["roll-dispatch"]}, "review", "roll-dispatch"),
    "another name declared": (LOOP, {"build": ["roll-dispatch-2"], "review": ["review-runner"], "outward": ["roll-land"]},
                              "build", "roll-dispatch"),
}


@pytest.mark.parametrize("case", list(UNKNOWN))
def test_O13_a_seam_naming_an_implementation_the_opener_did_not_declare_is_refused(repo, fake, case):
    seams, declared, seam, name = UNKNOWN[case]
    handed_off(fake, seams=seams)
    message = refused(fake, lambda: open_handoff(repo, declared=declared), "handoff_seam_unknown")
    assert f"its {seam} seam names the implementation {name}," in message


@pytest.mark.parametrize("declared", [{"merge": ["x"]}, {"build": []}, {"build": "roll-dispatch"}, {"build": ["roll dispatch"]},
                                      {"build": ["d" * 65]}, ["build"]],
                         ids=["unknown seam", "no names", "a bare string", "a space", "65 characters", "not a mapping"])
def test_O13_declared_implementations_that_break_the_grammar_are_refused_before_any_read(fake, declared):
    calls = len(fake.fake.calls())
    with pytest.raises(ticket.TicketError):
        ticket.handoff_source(KEY, implementations=declared)
    assert len(fake.fake.calls()) == calls


# --- O14-O18: the lease, the other sources, races and the inherited refusals ---------------------------------


def test_O14_an_open_handed_another_holder_is_lease_held_naming_the_loops_holder(repo, backend):
    handed_off(backend)
    message = refused(backend, lambda: open_handoff(repo, declared=DECLARED, holder="seat-z"), "lease_held")
    assert HOLDER in message


@pytest.mark.parametrize("case", ["a bare state open", "the holder's lease expired", "a resume after the release"])
def test_O15_an_item_not_leased_live_to_the_holder_is_refused_and_no_lease_is_taken(repo, backend, case):
    handed_off(backend)
    pass_id = None
    if case == "a resume after the release":
        pass_id = open_handoff(repo, declared=DECLARED).pass_id
    if case == "the holder's lease expired":
        backend.write("item.state", {"status": "claimed", "claim": HOLDER, "expires_at": PAST})
    else:
        backend.write("item.state", {"status": "open"})
    claim = latest_claim(backend)
    message = refused(backend, lambda: open_handoff(repo, declared=DECLARED, pass_id=pass_id), "handoff_not_leased")
    assert HOLDER in message
    assert latest_claim(backend) == claim


def test_O15_an_old_shape_pass_resumed_through_the_hand_off_after_the_release_is_refused(repo, fake):
    pass_id = "0123456789abcdef"
    fake.file_row()
    fake.fake.outside("session.opened", {"purpose": f"ticket pass {pass_id} on here at 2026-11-01T00:00:00Z", "source": "ticket"},
                      key=f"ticket-pass/{KEY}/{pass_id}/claimed")
    fake.take()
    fake.hand_off()
    fake.write("item.state", {"status": "open"})
    refused(fake, lambda: open_handoff(repo, declared=DECLARED, pass_id=pass_id), "handoff_not_leased")


def test_O16a_the_ledger_id_source_on_a_handed_off_item_is_refused_naming_the_holder(repo, backend):
    handed_off(backend)
    message = refused(backend, lambda: open_ledger_id(repo, holder=HOLDER), "handoff_source_required")
    assert HOLDER in message


def test_O16b_after_a_bare_state_open_a_default_pass_is_refused_and_the_loop_still_records(repo, backend):
    handed_off(backend)
    backend.write("item.state", {"status": "open"})
    refused(backend, lambda: open_ledger_id(repo), "handoff_source_required")
    # The loop's build result still records as its holder (on a real ledger the writer checks the claim first).
    mark = len(backend.events())
    backend.done("build-1", outcome="built")
    (done,) = [event for event in backend.events()[mark:] if event["kind"] == "loop.done"]
    assert (done["payload"]["operation_id"], done["payload"]["outcome"]) == ("build-1", "built")


def test_O16c_a_tracker_ticket_on_a_handed_off_item_takes_no_lease_and_writes_no_pass(repo, fake):
    # Known limit (design 6.8): a tracker, JSON-file or ad-hoc source records its spec before the card is read, so
    # a spec version may land before this refusal; no lease and no pass record do.
    handed_off(fake)
    before = len(fake.events())
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.TicketPass.open(KEY, Source(), ticket.choose_store(repo, KEY, TITLE))
    assert refusal(excinfo) == "handoff_source_required"
    assert [event["kind"] for event in fake.events()[before:] if event["kind"] != "item.spec"] == []


UNMATCHABLE = {
    "a version 2 intent naming no operation_id": (_payload(operation_id=None), 2),
    "an intent with no payload version": (_payload(), None),
}


@pytest.mark.parametrize("case", list(UNMATCHABLE))
def test_O16d_an_intent_no_done_can_match_or_whose_version_cannot_be_read_refuses_the_ledger_id_source(repo, fake, case):
    # Design 6.8: such an intent counts as open, so the item opens only through the hand-off source.
    fake.file_row()
    fake.take()
    payload, version = UNMATCHABLE[case]
    plant_intent(fake.fake, "build", "build-1", HOLDER, payload=payload, payload_version=version, hand_off=True)
    message = refused(fake, lambda: open_ledger_id(repo, holder=HOLDER), "handoff_source_required")
    assert HOLDER in message


@pytest.mark.parametrize("writer", ["a spec set", "another writer's note"])
def test_O17_a_write_between_the_read_and_the_claim_is_stale_and_nothing_is_written(repo, fake, writer):
    handed_off(fake)

    def land():
        if writer == "a spec set":
            fake.fake.set_spec(f"- {A}")
        else:
            fake.write("item.note", {"text": "see PR 12"})

    src = ticket.handoff_source(KEY, implementations=DECLARED)
    handle = ReadHook(str(fake.path), KEY, TITLE, land)
    before = len(fake.events())
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.TicketPass.open(KEY, src, handle, holder=HOLDER)
    assert refusal(excinfo) == "stale"
    landed = fake.events()[before:]
    assert [event["kind"] for event in landed] == ["item.spec" if writer == "a spec set" else "item.note"]
    assert pass_records(landed) == []


@pytest.mark.parametrize("case", ["no spec", "no acceptance"])
def test_O18_the_ledger_id_refusals_hold_on_the_hand_off(repo, fake, case):
    if case == "no spec":
        fake.file_row(spec=False)
    else:
        fake.file_row(acceptance=None)
    fake.take()
    fake.hand_off()
    refused(fake, lambda: open_handoff(repo, declared=DECLARED), case.replace(" ", "_"))


# --- O19-O21: decoys, SKILL.md and the markdown card -----------------------------------------------------------


def test_O19_the_controller_is_the_open_events_never_a_note_a_target_or_a_closed_hand_off(repo, fake):
    fake.file_row()
    fake.take()
    fake.hand_off("build-0", controller="decoy-closed")
    fake.done("build-0")
    fake.write("item.note", {"text": "controller: decoy-note"})
    fake.write("item.note", {"text": json.dumps({"controller": "decoy-note", "seams": DEFAULTS})})
    payload = {**_payload(), "target": "controller=decoy-target"}
    plant_intent(fake.fake, "build", "build-1", HOLDER, payload=payload, payload_version=2, hand_off=True)
    session = open_handoff(repo, declared=DECLARED)
    assert session.resume_card["handoff"] == expected_handoff(fake)
    assert fresh_card(fake)["handoff"]["controller"] == "director"


def test_O20_skill_md_gives_the_hand_off_its_own_section_its_refusals_and_the_fail_closed_seam_line():
    text = (Path(ticket.__file__).parent / "SKILL.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert "### A parent's hand-off as the ticket" in text
    order = text.split("`/ticket <KEY>` takes the first source that matches")[1].split("\n\n")[1]
    assert "hand" not in order.lower()
    for needle in ("ticket.handoff_source(", "holder=", "handoff_no_ledger", "handoff_ledger_too_old", "handoff_not_managed",
                   "handoff_unreadable", "handoff_ambiguous", "handoff_intent_v1", "handoff_no_intent",
                   "handoff_seam_unknown", "handoff_not_leased", "handoff_source_required", FAIL_CLOSED):
        assert needle in flat, needle


def test_O21_a_stored_markdown_card_that_carries_a_handoff_field_is_unreadable(repo):
    write_card(repo, {**ticket._blank_card(KEY, TITLE), "handoff": {"controller": "director", "seams": LOOP}})
    kept = (repo / ".ticket" / f"{KEY}.md").read_bytes()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.MarkdownStore(repo, KEY, TITLE).read()
    assert refusal(excinfo) == "card_unreadable"
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.TicketPass.open(KEY, Source(), ticket.MarkdownStore(repo, KEY, TITLE))
    assert refusal(excinfo) == "card_unreadable"
    assert (repo / ".ticket" / f"{KEY}.md").read_bytes() == kept


# --- O22-O23: a pass on a handed-off item never releases the loop's lease ---------------------------------------


def took_then_handed_off(repo, backend):
    """A ledger-id pass as ``HOLDER`` that took the lease (its record says ``taken``), left live; then the loop takes
    the item as the same holder and hands off ``build-1``. The pass is returned."""

    backend.file_row()
    first = open_ledger_id(repo, holder=HOLDER)
    (record,) = [record_of(event) for event in pass_records(backend.events())]
    assert (record["id"], record["holder"], record["lease"]) == (first.pass_id, HOLDER, "taken")
    backend.take()
    backend.hand_off()
    return first


def only_the_pass_end(backend, mark, pass_id, claim):
    assert [(event["kind"], event["idempotency_key"]) for event in backend.events()[mark:]] == [
        ("session.closed", f"ticket-pass/{KEY}/{pass_id}/released")]
    assert latest_claim(backend) == claim
    current = row(backend)
    assert (current["status"], current.get("claim")) == ("claimed", HOLDER)


@pytest.mark.parametrize("case", ["ended while handed off", "ended after the handback"])
def test_O22_a_hand_off_resume_of_a_pass_that_took_the_lease_never_releases_the_loops_lease(repo, backend, case):
    first = took_then_handed_off(repo, backend)
    resumed = open_handoff(repo, declared=DECLARED, pass_id=first.pass_id)
    assert (resumed.live, resumed.pass_id) == (True, first.pass_id)
    if case == "ended after the handback":
        backend.done("build-1")
    claim = latest_claim(backend)
    mark = len(backend.events())
    resumed.end()
    only_the_pass_end(backend, mark, first.pass_id, claim)


@pytest.mark.parametrize("case", ["the release command", "its own handle ends"])
def test_O23_a_pass_that_took_the_lease_before_the_hand_off_never_releases_the_loops_lease(repo, backend, case):
    first = took_then_handed_off(repo, backend)
    claim = latest_claim(backend)
    mark = len(backend.events())
    if case == "the release command":
        assert ticket.main(["release", KEY, first.pass_id]) == 0
    else:
        first.end()
    only_the_pass_end(backend, mark, first.pass_id, claim)


# --- O24-O27: a hand-off under the pass's own holder name protects it after the handback too -------------------


@pytest.mark.parametrize("case", ["the release command", "its own handle ends"])
def test_O24_after_the_handback_a_pass_that_took_the_lease_before_the_hand_off_still_never_releases_it(repo, backend, case):
    # The loop re-stamps its claim at the done and still works under it: the hand-off was handed under the pass's
    # own holder, so whichever handle ends the pass, the lease stays the loop's for the rest of its life (design 6.8).
    first = took_then_handed_off(repo, backend)
    backend.done("build-1")
    claim = latest_claim(backend)
    mark = len(backend.events())
    if case == "the release command":
        assert ticket.main(["release", KEY, first.pass_id]) == 0
    else:
        first.end()
    only_the_pass_end(backend, mark, first.pass_id, claim)


@pytest.mark.parametrize("case", ["while handed off", "after the handback"])
def test_O25_the_release_command_clears_a_crashed_hand_off_resume_and_leaves_the_loops_lease(repo, backend, case):
    first = took_then_handed_off(repo, backend)
    resumed = open_handoff(repo, declared=DECLARED, pass_id=first.pass_id)
    assert (resumed.live, resumed.pass_id) == (True, first.pass_id)
    # The resume crashes and never ends; a person clears it with the release command.
    if case == "after the handback":
        backend.done("build-1")
    claim = latest_claim(backend)
    mark = len(backend.events())
    assert ticket.main(["release", KEY, first.pass_id]) == 0
    only_the_pass_end(backend, mark, first.pass_id, claim)


@pytest.mark.parametrize("case", ["the release command", "its own handle ends"])
def test_O26_a_pass_opened_through_the_hand_off_never_releases_a_lease_it_takes_after_the_handback(repo, backend, case):
    # The pass began while the item was handed off under its holder, so a lease under that name is never its own to
    # release, even one it takes again through the ledger-id source after the loop let the item go.
    handed_off(backend)
    first = open_handoff(repo, declared=DECLARED)
    backend.done("build-1")
    backend.write("item.state", {"status": "open"})
    resumed = open_ledger_id(repo, holder=HOLDER, pass_id=first.pass_id)
    assert (resumed.live, resumed.pass_id) == (True, first.pass_id)
    (taken,) = [event for event in backend.events() if event["kind"] == "item.state"
                and (event["idempotency_key"] or "").startswith(f"ticket-lease/{KEY}/{first.pass_id}/")]
    assert taken["payload"]["owner"] == HOLDER
    claim = latest_claim(backend)
    mark = len(backend.events())
    if case == "the release command":
        assert ticket.main(["release", KEY, first.pass_id]) == 0
    else:
        resumed.end()
    only_the_pass_end(backend, mark, first.pass_id, claim)


@pytest.mark.parametrize("end", ["the release command", "its own handle ends"])
@pytest.mark.parametrize("case", ["never handed off", "taken after an earlier hand-off's handback"])
def test_O27_a_default_pass_still_releases_the_lease_it_took_on_an_item_not_handed_off_in_its_life(repo, backend, case, end):
    backend.file_row()
    if case == "taken after an earlier hand-off's handback":
        backend.take()
        backend.hand_off()
        backend.done("build-1")
        backend.write("item.state", {"status": "open"})
    first = open_ledger_id(repo, holder=HOLDER)
    (record,) = [record_of(event) for event in pass_records(backend.events())]
    assert (record["id"], record["holder"], record["lease"]) == (first.pass_id, HOLDER, "taken")
    mark = len(backend.events())
    if end == "the release command":
        assert ticket.main(["release", KEY, first.pass_id]) == 0
    else:
        first.end()
    landed = [(event["kind"], event["idempotency_key"]) for event in backend.events()[mark:]]
    assert landed[0] == ("session.closed", f"ticket-pass/{KEY}/{first.pass_id}/released")
    assert len(landed) == 2 and landed[1][0] == "item.state", landed
    assert landed[1][1].startswith(f"ticket-lease/{KEY}/{first.pass_id}/") and landed[1][1].endswith("/released"), landed
    assert row(backend)["status"] == "open"


# --- O28-O43: the lease decision at every end, from a fresh read (design 6.8) ----------------------------------
# P is the pass, H the holder its record names, and L the holder a hand-off was handed under: the holder of the
# latest claim-bearing state before the hand-off's intent. "Released" is the pass-end and `item.state {status:
# "open"}` in one batch; "retained" is the pass-end alone, the claim event unchanged.

SEAT = "seat-h"
NEXT = "seat-next"
ENDS = ["its own handle ends", "the release command", "clear_pass"]


def claim_event(backend):
    """The item's latest claim-bearing event, whole (an ``item.state`` naming a holder)."""

    claims = [event for event in backend.events() if event["kind"] == "item.state"
              and (event["payload"].get("owner") or event["payload"].get("claim"))]
    return claims[-1] if claims else None


def end_pass(backend, how, handle, pass_id):
    """End pass ``pass_id``: ``handle``'s ``end()``, ``ticket.py release K P``, or ``clear_pass`` on a fresh handle."""

    if how == "the release command":
        assert ticket.main(["release", KEY, pass_id]) == 0
    elif how == "clear_pass":
        ticket.LedgerStore(backend.path, KEY, TITLE).clear_pass(pass_id)
    else:
        handle.end()


def retained(backend, mark, pass_id, claim):
    """Since ``mark`` the pass-end alone landed; the claim event is ``claim``, whole; the row is claimed by its holder."""

    assert [(event["kind"], event["idempotency_key"]) for event in backend.events()[mark:]] == [
        ("session.closed", f"ticket-pass/{KEY}/{pass_id}/released")]
    assert claim_event(backend) == claim
    current = row(backend)
    holder = claim["payload"].get("owner") or claim["payload"].get("claim")
    assert (current["status"], current.get("owner") or current.get("claim")) == ("claimed", holder)


def released(repo, backend, mark, pass_id):
    """Since ``mark`` the pass-end and the lease's release landed in one batch and the row reads open; another holder
    then takes the item. That holder's pass is returned."""

    landed = [(event["kind"], event["idempotency_key"]) for event in backend.events()[mark:]]
    assert len(landed) == 2, landed
    assert landed[0] == ("session.closed", f"ticket-pass/{KEY}/{pass_id}/released")
    assert landed[1][0] == "item.state" and landed[1][1].startswith(f"ticket-lease/{KEY}/{pass_id}/"), landed
    assert landed[1][1].endswith("/released") and backend.events()[-1]["payload"]["status"] == "open"
    if isinstance(backend, Fake):
        assert [element["kind"] for element in backend.fake.appends()[-1].batch] == ["session.closed", "item.state"]
    assert row(backend)["status"] == "open"
    following = open_ledger_id(repo, holder=NEXT)
    (record,) = [record_of(event) for event in pass_records(backend.events()) if record_of(event)["id"] == following.pass_id]
    assert (record["holder"], record["lease"]) == (NEXT, "taken")
    current = row(backend)
    assert (current["status"], current.get("owner") or current.get("claim")) == ("claimed", NEXT)
    return following


def lease_taken_by(backend, pass_id, holder):
    """The leases pass ``pass_id`` wrote after its record (keyed ``ticket-lease/K/P/<R>``), each asserted ``holder``'s."""

    taken = [event for event in backend.events() if event["kind"] == "item.state"
             and (event["idempotency_key"] or "").startswith(f"ticket-lease/{KEY}/{pass_id}/")
             and not event["idempotency_key"].endswith("/released")]
    assert taken and all(event["payload"]["owner"] == holder for event in taken), taken
    return taken


@pytest.mark.parametrize("end", ENDS)
def test_lease_default_release(repo, backend, end):
    # O28: a default pass, never handed off, releases the lease it took as ticket-pass/P on every end path.
    backend.file_row()
    first = open_ledger_id(repo)
    (record,) = [record_of(event) for event in pass_records(backend.events())]
    assert (record["holder"], record["lease"]) == (f"ticket-pass/{first.pass_id}", "taken")
    mark = len(backend.events())
    end_pass(backend, end, first, first.pass_id)
    released(repo, backend, mark, first.pass_id)


@pytest.mark.parametrize("handle", ["the original", "a resume"])
def test_lease_ordinary_join(repo, backend, handle):
    # O29: a pass that joined a lease taken outside it, on an item never handed off, never releases it.
    backend.file_row()
    backend.write("item.state", {"status": "claimed", "owner": SEAT, "claim": SEAT, "expires_at": LIVE_STAMP})
    first = open_ledger_id(repo, holder=SEAT)
    (record,) = [record_of(event) for event in pass_records(backend.events())]
    assert (record["holder"], record["lease"]) == (SEAT, "joined")
    ending = first if handle == "the original" else open_ledger_id(repo, holder=SEAT, pass_id=first.pass_id)
    claim = claim_event(backend)
    mark = len(backend.events())
    ending.end()
    retained(backend, mark, first.pass_id, claim)


@pytest.mark.parametrize("end", ["its own handle ends", "a resume ends", "the release command", "clear_pass"])
@pytest.mark.parametrize("state", ["handed off", "handed back"])
def test_lease_preexisting_shared_holder(repo, backend, state, end):
    # O30: P took the lease as H; the loop then took the item and handed it off under the same name (L = H). While
    # handed off and after the handback, no end path of P releases it.
    first = took_then_handed_off(repo, backend)
    if state == "handed back":
        backend.done("build-1")
    ending = first
    if end == "a resume ends":
        if state == "handed off":
            ending = open_handoff(repo, declared=DECLARED, pass_id=first.pass_id)
        else:
            ending = open_ledger_id(repo, holder=HOLDER, pass_id=first.pass_id)
        assert (ending.live, ending.pass_id) == (True, first.pass_id)
    claim = claim_event(backend)
    mark = len(backend.events())
    end_pass(backend, end, ending, first.pass_id)
    retained(backend, mark, first.pass_id, claim)


def test_lease_different_current_holder(repo, backend, monkeypatch):
    # O31: P took the lease as its default holder H; it ran out; the loop took the item over and handed it off as
    # L, another name. The original handle's end leaves L's claim.
    backend.file_row()
    with monkeypatch.context() as clock:
        clock.setattr(ticket, "_lease_expiry", lambda: PAST)
        first = open_ledger_id(repo)
    backend.take(HOLDER)
    backend.hand_off()
    claim = claim_event(backend)
    assert claim["payload"]["claim"] == HOLDER
    mark = len(backend.events())
    first.end()
    retained(backend, mark, first.pass_id, claim)


@pytest.mark.parametrize("handle", ["the original", "a hand-off resume"])
def test_lease_handoff_open(repo, backend, handle):
    # O32: a pass opened through the open hand-off joins L's claim (L = H) and never writes or releases it.
    handed_off(backend)
    claim = claim_event(backend)
    first = open_handoff(repo, declared=DECLARED)
    assert claim_event(backend) == claim
    ending = first
    if handle == "a hand-off resume":
        ending = open_handoff(repo, declared=DECLARED, pass_id=first.pass_id)
        assert claim_event(backend) == claim
    mark = len(backend.events())
    ending.end()
    retained(backend, mark, first.pass_id, claim)


@pytest.mark.parametrize("handle", ["the original", "the resume"])
@pytest.mark.parametrize("lease", ["kept", "taken again"])
def test_lease_handoff_after_handback(repo, backend, lease, handle):
    # O33: the same pass after the handback, resumed through the ledger-id source as H = L, keeping L's restamped
    # lease or taking it again after a bare state open: still never released.
    handed_off(backend)
    first = open_handoff(repo, declared=DECLARED)
    backend.done("build-1")
    if lease == "taken again":
        backend.write("item.state", {"status": "open"})
    resumed = open_ledger_id(repo, holder=HOLDER, pass_id=first.pass_id)
    assert (resumed.live, resumed.pass_id) == (True, first.pass_id)
    if lease == "taken again":
        lease_taken_by(backend, first.pass_id, HOLDER)
    claim = claim_event(backend)
    mark = len(backend.events())
    (first if handle == "the original" else resumed).end()
    retained(backend, mark, first.pass_id, claim)


@pytest.mark.parametrize("end", ["the release command", "clear_pass"])
@pytest.mark.parametrize("state", ["handed off", "handed back"])
def test_lease_crashed_shared_resume(repo, backend, state, end):
    # O34: P took the lease before the hand-off (L = H); its resume through the hand-off source crashes, while
    # handed off or after the handback; a person ends it: the lease is retained.
    first = took_then_handed_off(repo, backend)
    resumed = open_handoff(repo, declared=DECLARED, pass_id=first.pass_id)
    assert (resumed.live, resumed.pass_id) == (True, first.pass_id)
    if state == "handed back":
        backend.done("build-1")
    claim = claim_event(backend)
    mark = len(backend.events())
    end_pass(backend, end, None, first.pass_id)
    retained(backend, mark, first.pass_id, claim)


def own_retake(repo, backend, monkeypatch, joined):
    """P's own name H, another name than the loop's L: P took H (its record says ``taken``, the lease already run
    out) or, with ``joined``, joined H taken outside it, which then ran out; the loop took the item over as L, handed
    it off and back, and L's restamped lease ran out; P is resumed through the ledger-id source and takes H again.
    The original handle, the resume and H are returned."""

    backend.file_row()
    if joined:
        backend.write("item.state", {"status": "claimed", "owner": SEAT, "claim": SEAT, "expires_at": LIVE_STAMP})
        first = open_ledger_id(repo, holder=SEAT)
        backend.write("item.state", {"status": "claimed", "owner": SEAT, "claim": SEAT, "expires_at": PAST})
        holder, lease = SEAT, "joined"
    else:
        with monkeypatch.context() as clock:
            clock.setattr(ticket, "_lease_expiry", lambda: PAST)
            first = open_ledger_id(repo)
        holder, lease = f"ticket-pass/{first.pass_id}", "taken"
    (record,) = [record_of(event) for event in pass_records(backend.events())]
    assert (record["holder"], record["lease"]) == (holder, lease)
    backend.take(HOLDER)
    backend.hand_off()
    backend.done("build-1")
    backend.write("item.state", {"status": "claimed", "claim": HOLDER, "expires_at": PAST})
    resumed = open_ledger_id(repo, holder=SEAT if joined else None, pass_id=first.pass_id)
    assert (resumed.live, resumed.pass_id) == (True, first.pass_id)
    lease_taken_by(backend, first.pass_id, holder)
    return first, resumed, holder


def test_lease_own_retake_resumer(repo, backend, monkeypatch):
    # O35: H is not L; after the handback P takes H again: the resuming handle's end releases it.
    first, resumed, _ = own_retake(repo, backend, monkeypatch, joined=False)
    mark = len(backend.events())
    resumed.end()
    released(repo, backend, mark, first.pass_id)


def test_lease_own_retake_original(repo, backend, monkeypatch):
    # O36: the same re-take, ended by P's original handle: released (H is not the hand-off's historical holder,
    # though it is the holder on today's row).
    first, _, _ = own_retake(repo, backend, monkeypatch, joined=False)
    mark = len(backend.events())
    first.end()
    released(repo, backend, mark, first.pass_id)


def test_lease_cached_join_original(repo, backend, monkeypatch):
    # O37: P's original handle joined H (it holds `joined` from its own claim); a second handle took H again after
    # the handback. The original handle ends first and, from a fresh read, releases; the second handle's later end
    # touches nothing of the next holder's pass.
    first, resumed, _ = own_retake(repo, backend, monkeypatch, joined=True)
    mark = len(backend.events())
    first.end()
    following = released(repo, backend, mark, first.pass_id)
    claim = claim_event(backend)
    mark = len(backend.events())
    resumed.end()
    assert backend.events()[mark:] == []
    assert claim_event(backend) == claim
    assert fresh_card(backend)["pass"]["id"] == following.pass_id
    assert (row(backend)["status"], row(backend).get("owner")) == ("claimed", NEXT)


@pytest.mark.parametrize("end", ["the release command", "clear_pass"])
def test_lease_crashed_own_retake(repo, backend, monkeypatch, end):
    # O38: the joined pass's resume took H again after the handback (H is not L) and crashed; a person's release
    # releases the lease the resume took.
    first, _, _ = own_retake(repo, backend, monkeypatch, joined=True)
    mark = len(backend.events())
    end_pass(backend, end, None, first.pass_id)
    released(repo, backend, mark, first.pass_id)


def test_lease_late_handle(repo, backend):
    # O39: two handles on P; P ends and releases; Q opens under the same H and takes it; P's late handle ends and
    # leaves Q's pass and claim alone.
    backend.file_row()
    first = open_ledger_id(repo, holder=SEAT)
    late = open_ledger_id(repo, holder=SEAT, pass_id=first.pass_id)
    assert (late.live, late.pass_id) == (True, first.pass_id)
    mark = len(backend.events())
    first.end()
    landed = [event["kind"] for event in backend.events()[mark:]]
    assert landed == ["session.closed", "item.state"] and row(backend)["status"] == "open"
    following = open_ledger_id(repo, holder=SEAT)
    (record,) = [record_of(event) for event in pass_records(backend.events()) if record_of(event)["id"] == following.pass_id]
    assert (record["holder"], record["lease"]) == (SEAT, "taken")
    claim = claim_event(backend)
    mark = len(backend.events())
    late.end()
    assert backend.events()[mark:] == []
    assert claim_event(backend) == claim
    assert fresh_card(backend)["pass"]["id"] == following.pass_id
    assert (row(backend)["status"], row(backend).get("owner")) == ("claimed", SEAT)


@pytest.mark.parametrize("end", ["its own handle ends", "a hand-off resume ends", "the release command", "clear_pass"])
def test_lease_coincident_holder_name(repo, backend, end):
    # O40: the loop takes the item and hands it off under ticket-pass/P, P's own default name: names are identity,
    # so the lease is retained on every end path.
    backend.file_row()
    first = open_ledger_id(repo)
    holder = f"ticket-pass/{first.pass_id}"
    backend.take(holder)
    backend.hand_off(holder=holder)
    ending = first
    if end == "a hand-off resume ends":
        ending = open_handoff(repo, declared=DECLARED, pass_id=first.pass_id, holder=holder)
        assert (ending.live, ending.pass_id) == (True, first.pass_id)
    claim = claim_event(backend)
    assert claim["payload"]["claim"] == holder
    mark = len(backend.events())
    end_pass(backend, end, ending, first.pass_id)
    retained(backend, mark, first.pass_id, claim)


@pytest.mark.parametrize("end", ["its own handle ends", "a resume ends", "the release command", "clear_pass"])
def test_lease_prior_handback(repo, backend, end):
    # O41: a hand-off done and let go before P began, under the name P then takes (H = the former L): every end
    # path releases.
    backend.file_row()
    backend.take()
    backend.hand_off()
    backend.done("build-1")
    backend.write("item.state", {"status": "open"})
    first = open_ledger_id(repo, holder=HOLDER)
    (record,) = [record_of(event) for event in pass_records(backend.events())]
    assert (record["holder"], record["lease"]) == (HOLDER, "taken")
    ending = first
    if end == "a resume ends":
        ending = open_ledger_id(repo, holder=HOLDER, pass_id=first.pass_id)
        assert (ending.live, ending.pass_id) == (True, first.pass_id)
    mark = len(backend.events())
    end_pass(backend, end, ending, first.pass_id)
    released(repo, backend, mark, first.pass_id)


def test_lease_nobody_state_before_handoff(repo, backend):
    # O42: P took H; the loop took the item as H; a bare state open (naming nobody) came before the hand-off, so the
    # loop wrote no hand-off claim. L is still H, read past the bare state: P's re-take after the handback is
    # retained.
    backend.file_row()
    first = open_ledger_id(repo, holder=HOLDER)
    backend.take()
    backend.write("item.state", {"status": "open"})
    if isinstance(backend, Fake):
        # The loop writer writes no hand-off claim and no restamp when the item's row names nobody.
        payload = {"operation": "build", "operation_id": "build-1", "controller": "director", "seams": dict(LOOP)}
        plant_intent(backend.fake, "build", "build-1", payload=payload, payload_version=2)
        intent = intent_id(backend)
        backend.fake.outside("loop.done", {"operation": "build", "operation_id": "build-1", "outcome": "handback",
                                           "intent_event_id": intent}, actor="loop", key=f"loop:{KEY}:build:build-1:done")
    else:
        backend.hand_off()
        backend.done("build-1")
    assert [event["payload"].get("claim") for event in backend.events()
            if event["kind"] == "item.state" and event["id"] > intent_id(backend)] == []
    resumed = open_ledger_id(repo, holder=HOLDER, pass_id=first.pass_id)
    assert (resumed.live, resumed.pass_id) == (True, first.pass_id)
    lease_taken_by(backend, first.pass_id, HOLDER)
    claim = claim_event(backend)
    mark = len(backend.events())
    resumed.end()
    retained(backend, mark, first.pass_id, claim)


@pytest.mark.parametrize("case", ["no claim before the hand-off", "a claim before it naming a holder that is not a string"])
def test_lease_unknown_handoff_holder(repo, fake, case):
    # O43: when the holder a hand-off was handed under cannot be established, the pass's own name is protected:
    # its re-take after the handback is retained.
    fake.file_row()
    fake.fake.outside("loop.managed", {}, actor="loop", key=f"loop:{KEY}:managed")
    if case != "no claim before the hand-off":
        fake.write("item.state", {"status": "claimed", "owner": 7, "expires_at": PAST})
    fake.hand_off()
    first = open_handoff(repo, declared=DECLARED)
    fake.done("build-1")
    fake.write("item.state", {"status": "open"})
    resumed = open_ledger_id(repo, holder=HOLDER, pass_id=first.pass_id)
    lease_taken_by(fake, first.pass_id, HOLDER)
    claim = claim_event(fake)
    mark = len(fake.events())
    resumed.end()
    retained(fake, mark, first.pass_id, claim)


# --- G: what must not change ------------------------------------------------------------------------------------

def _cli(*args):
    return ["--json", *args]


ASK_ITEM = "ask-1c33e6eb91f3"
# The CLI calls a default open makes on each source, in order, measured on origin/main (f71d43d) before the hand-off
# source existed and written out here: no --version call, and nothing else added, removed or moved.
DEFAULT_CALLS = {
    "ledger id": [
        _cli("query", "--item", KEY), _cli("spec", "show", KEY), _cli("links", KEY), _cli("query", "--item", KEY),
        _cli("export-item", KEY), _cli("spec", "show", KEY), _cli("append-many", KEY, "--expect-revision", "2"),
    ],
    "tracker key": [
        _cli("query", "--item", KEY), _cli("append-many", KEY, "--expect-revision", "none"),
        _cli("spec", "set", KEY, f"--title={TITLE}",
             "--body=Run one ticket from live orientation through approved outward work.",
             f"--acceptance=- {A}\n- {B}", f"--source=ticket:{KEY}"),
        _cli("query", "--item", KEY), _cli("export-item", KEY), _cli("spec", "show", KEY),
        _cli("append-many", KEY, "--expect-revision", "2"),
    ],
    "json file": [
        _cli("query", "--item", KEY), _cli("append-many", KEY, "--expect-revision", "none"),
        _cli("spec", "set", KEY, f"--title={TITLE}", "--body=Run one ticket.", f"--acceptance=- {A}\n- {B}",
             f"--source=ticket:{KEY} https://tracker.example/browse/EK-14"),
        _cli("query", "--item", KEY), _cli("export-item", KEY), _cli("spec", "show", KEY),
        _cli("append-many", KEY, "--expect-revision", "3"),
    ],
    "ad-hoc ask": [
        _cli("query", "--item", ASK_ITEM), _cli("append-many", ASK_ITEM, "--expect-revision", "none"),
        _cli("spec", "set", ASK_ITEM, f"--title={TITLE}", "--body=Retry the pull.", f"--acceptance=- {A}",
             "--source=ask:1c33e6eb91f3"),
        _cli("query", "--item", ASK_ITEM), _cli("export-item", ASK_ITEM), _cli("spec", "show", ASK_ITEM),
        _cli("append-many", ASK_ITEM, "--expect-revision", "2"),
    ],
}


@pytest.mark.parametrize("source", ["ledger id", "tracker key", "json file", "ad-hoc ask"])
def test_G2_a_default_open_on_each_source_makes_the_same_calls_in_the_same_order(repo, fake, tmp_path, source):
    if source == "ledger id":
        fake.file_row()
        src, key = ticket.ledger_item_source(KEY), KEY
    elif source == "tracker key":
        src, key = Source(), KEY
    elif source == "json file":
        path = tmp_path / "EK-14.json"
        path.write_text(json.dumps({"key": KEY, "title": TITLE, "status": "open", "description": "Run one ticket.",
                                    "acceptance_criteria": [A, B], "url": "https://tracker.example/browse/EK-14"}),
                        encoding="utf-8")
        src, key = ticket.FileTicketSource(path), KEY
    else:
        src = ticket.AskTicketSource("Add a retry to the sync job.", title=TITLE, description="Retry the pull.",
                                     acceptance=[A])
        key = src.key
    store = ticket.choose_store(repo, key, TITLE)
    calls = len(fake.fake.calls())
    session = ticket.TicketPass.open(key, src, store)
    assert [call.argv for call in fake.fake.calls()[calls:]] == DEFAULT_CALLS[source]
    assert not any("--version" in call.argv for call in fake.fake.calls())
    assert "handoff" not in session.resume_card


def test_G4_a_managed_item_with_only_a_version_1_build_intent_opens_through_the_ledger_id_source(repo, backend):
    backend.file_row()
    backend.take()
    backend.intent_v1("build", "build-1")
    before = len(backend.events())
    session = open_ledger_id(repo, holder=HOLDER)
    (record,) = [record_of(event) for event in pass_records(backend.events()[before:])]
    assert (record["holder"], record["lease"]) == (HOLDER, "joined")
    assert "handoff" not in session.resume_card
    assert ticket.seam_set(session.resume_card) == DEFAULTS


@pytest.mark.parametrize("text", [
    "ticket-handoff:v2 " + json.dumps({"controller": "director", "seams": LOOP}),
    "ticket-pass:v2 " + json.dumps({"id": "ab", "host": "h", "started_at": "t", "holder": "x", "lease": "joined",
                                    "controller": "director"}),
], ids=["a ticket-handoff note", "a pass record with a sixth key"])
def test_G5_a_framed_note_never_gives_the_card_a_hand_off(fake, text):
    fake.file_row()
    fake.write("item.note", {"text": text})
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.LedgerStore(fake.path, KEY, TITLE).read()
    assert refusal(excinfo) == "card_unreadable"


def test_G6_the_release_command_on_a_live_hand_off_pass_writes_the_pass_end_only(repo, backend):
    handed_off(backend)
    session = open_handoff(repo, declared=DECLARED)
    claim = latest_claim(backend)
    mark = len(backend.events())
    assert ticket.main(["release", KEY, session.pass_id]) == 0
    assert [(event["kind"], event["idempotency_key"]) for event in backend.events()[mark:]] == [
        ("session.closed", f"ticket-pass/{KEY}/{session.pass_id}/released")]
    assert latest_claim(backend) == claim
    current = row(backend)
    assert (current["status"], current.get("claim")) == ("claimed", HOLDER)


def test_G6_a_read_never_refuses_over_a_hand_off_so_a_crashed_pass_is_still_released(repo, fake):
    handed_off(fake)
    session = open_handoff(repo, declared=DECLARED)
    fake.hand_off("build-2", controller="director-2")  # two open hand-offs now: an open would be ambiguous
    card = fresh_card(fake)
    assert card["pass"]["id"] == session.pass_id and "handoff" not in card
    mark = len(fake.events())
    assert ticket.main(["release", KEY, session.pass_id]) == 0
    assert [event["kind"] for event in fake.events()[mark:]] == ["session.closed"]

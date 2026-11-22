"""Matrix section H: the ledger store, against a fake ledger CLI, and against the real one where a row needs it.

H19 and H24 need the ledger's own code: they run when ``LEDGER_CLI`` names
the ledger CLI (H24 loads ``ledger/schema.py`` from beside it) and are
skipped otherwise. H39–H47 bind a go to a captured utterance: H39, H40 and H42–H47
run against the fake; H41 runs when ``LEDGER_REAL_CLI`` names the ledger CLI
and is skipped otherwise. Every other row runs against the fake.
"""

import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import ticket
from ledger_support import WRITER, FakeLedger, ReadHook, build_ledger, open_ledger
from ticket_support import A, B, KEY, TITLE, Source, changes, go_plan, passing, present, refusal

SKILL_DIR = Path(__file__).resolve().parent.parent
LEDGER_CLI = os.environ.get("LEDGER_CLI", "")
needs_ledger_cli = pytest.mark.skipif(not LEDGER_CLI, reason="LEDGER_CLI does not name the ledger CLI")
LEDGER_REAL_CLI = os.environ.get("LEDGER_REAL_CLI", "")
needs_real_cli = pytest.mark.skipif(
    not LEDGER_REAL_CLI, reason="LEDGER_REAL_CLI does not name the ledger CLI",
)


@pytest.fixture
def fake(tmp_path):
    return FakeLedger(tmp_path / "ledger")


def kinds(events):
    return [event["kind"] for event in events]


def claims(fake):
    """The pass records: one per new pass's claim."""

    return [event for event in fake.events() if event["kind"] == "item.note" and event["payload"]["text"].startswith("ticket-pass:v2 ")]


def leases(fake):
    return [event for event in fake.events() if event["kind"] == "item.state" and event["payload"]["status"] == "claimed"]


def closed_states(fake):
    return [event for event in fake.events() if event["kind"] == "item.state" and event["payload"]["status"] == "closed"]


def note(text):
    return {"text": text}


def after(fake, before):
    """The events appended since ``before`` was taken."""

    return fake.events()[len(before):]


def pass_end(session):
    return [("session.closed", f"ticket-pass/{KEY}/{session.pass_id}/released")]


def released(session):
    """A pass's end that releases the lease it took: the pass-end, then the lease's open state (``keyed`` puts R for
    the revision of the fresh read in its key)."""

    return pass_end(session) + [("item.state", f"ticket-lease/{KEY}/{session.pass_id}/R/released")]


def keyed(events):
    return [
        (event["kind"], re.sub(r"^(ticket-lease/[^/]+/[0-9a-f]+/)[0-9]+(/released)$", r"\1R\2", event["idempotency_key"]))
        for event in events
    ]


# --- Claims, races and keys ---------------------------------------------------


def test_H1_two_sessions_racing_to_claim_get_one_pass(fake):
    src = Source()
    open_ledger(fake, src).end()
    opened, leased = len(claims(fake)), len(leases(fake))
    winner = []
    loser = ReadHook(fake.path, KEY, TITLE, lambda: winner.append(open_ledger(fake, src)))
    with pytest.raises(ticket.TicketHeld) as excinfo:
        open_ledger(fake, src, handle=loser)
    refused = fake.appends()[-1]
    assert refused.code == "stale_revision"
    assert kinds(refused.batch) == ["item.state", "item.note"] and refused.argv[-2] == "--expect-revision"
    assert excinfo.value.holder["id"] == winner[0].pass_id
    assert len(claims(fake)) == opened + 1
    assert len(leases(fake)) == leased + 1
    assert leases(fake)[-1]["payload"]["owner"] == f"ticket-pass/{winner[0].pass_id}"
    assert fake.store().read()[0]["pass"]["id"] == winner[0].pass_id


def test_H2_a_refused_close_writes_nothing_but_the_release(fake):
    src = Source()
    session = build_ledger(fake, src, "S4", 1)
    fake.outside("item.note", note("see PR 12"), actor="other-agent", session="other-session")
    before = fake.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        session.close("Ticket pass complete.", refs=("example/repo#14",), next_skills=("ticket",))
    assert refusal(excinfo) == "stale"
    assert "other-agent" in str(excinfo.value) and "other-session" in str(excinfo.value)
    assert keyed(after(fake, before)) == released(session)
    assert closed_states(fake) == []
    assert [e for e in fake.events() if e["kind"] == "item.stopped" and e["payload"]["phase"] == "close"] == []
    resumed = open_ledger(fake, src)
    assert resumed.phase == "outward_go"
    assert resumed.approved("outward") is True


def test_H3_a_handle_outside_a_pass_cannot_write_while_a_pass_is_live(fake):
    first = open_ledger(fake, Source())
    outsider = fake.store()
    _, token = outsider.read()
    before = fake.events()
    with pytest.raises(ticket.TicketHeld) as excinfo:
        outsider.apply(token, [("tried", {"what": "Probe the claim.", "phase": "orient"})])
    assert excinfo.value.holder["id"] == first.pass_id
    assert fake.events() == before


def test_H4_two_handles_at_one_revision_do_not_share_a_key(fake):
    open_ledger(fake, Source()).end()
    one, two = fake.store(), fake.store()
    (_, first), (_, second) = one.read(), two.read()
    assert first == second
    tried = [("tried", {"what": "Run the suite.", "phase": "build"})]
    one.apply(first, tried)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        two.apply(second, tried)
    assert refusal(excinfo) == "stale"
    assert fake.appends()[-1].code == "stale_revision"
    assert kinds(fake.events()).count("item.tried") == 1


def test_H5_a_lost_response_retries_with_the_same_keys(fake):
    session = build_ledger(fake, Source(), "S2", 0)
    fake.script("append-many", commit=True, stdout="", status=0)
    session.record_tried("Run the suite.", outcome="green")
    lost, retry = fake.appends()[-2:]
    assert retry.stdin == lost.stdin
    assert retry.code is None and retry.status == 0
    assert kinds(fake.events()).count("item.tried") == 1
    session.record_tried("Run the linter.")
    assert kinds(fake.events()).count("item.tried") == 2
    assert session.live is True


def test_H6_an_unparseable_response_is_resolved_by_the_batch_key_landed(fake):
    session = build_ledger(fake, Source(), "S2", 0)
    fake.script("append-many", commit=True, stdout="Traceback (most recent call last): {", status=1)
    session.record_tried("Run the suite.")
    assert [call.verb for call in fake.calls()[-2:]] == ["append-many", "export-item"]
    assert kinds(fake.events()).count("item.tried") == 1
    session.record_tried("Run the linter.")
    assert kinds(fake.events()).count("item.tried") == 2
    assert session.live is True


def test_H7_an_unparseable_response_is_resolved_by_the_batch_key_absent(fake):
    session = build_ledger(fake, Source(), "S2", 0)
    fake.script("append-many", stdout="Traceback (most recent call last): {", status=1)
    before = fake.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        session.record_tried("Run the suite.")
    assert refusal(excinfo) == "ledger_refused"
    assert "Traceback (most recent call last): {" in str(excinfo.value)
    assert keyed(after(fake, before)) == released(session)
    assert session.live is False


# --- The close ------------------------------------------------------------------


def test_H8_a_close_lands_under_the_bound_keys(fake):
    src = Source()
    open_ledger(fake, src).end()
    fake.outside("item.state", {"status": "open", "owner": "someone", "claim": "claim-7", "blocked_by": ["EK-1"]})
    session = build_ledger(fake, src, "S4", 1)
    revision = str(fake.events()[-1]["id"])
    session.close("Ticket pass complete.", refs=("example/repo#14",), next_skills=("ticket",))
    close, release = fake.appends()[-2:]
    assert close.argv[-2:] == ["--expect-revision", revision]
    stop, state = close.batch
    pass_id = session.pass_id
    token = stop["idempotency_key"].split("/")[3]
    assert stop == {
        "kind": "item.stopped", "item_id": KEY, "idempotency_key": f"ticket-close/{KEY}/{pass_id}/{token}/stop",
        "payload": {"point": "Ticket pass complete.", "phase": "close", "skills": ["ticket"], "refs": ["example/repo#14"],
                    "source": WRITER},
    }
    holder = f"ticket-pass/{pass_id}"
    assert state == {
        "kind": "item.state", "item_id": KEY, "idempotency_key": f"ticket-close/{KEY}/{pass_id}/{token}/state",
        "payload": {"status": "closed", "owner": holder, "claim": holder, "source": WRITER},
    }
    assert "--expect-revision" not in release.argv
    assert [(e["kind"], e["idempotency_key"]) for e in release.batch] == pass_end(session)


def test_H9_an_outside_closed_state_is_not_a_trusted_close(fake):
    src = Source()
    build_ledger(fake, src, "S4", 1).end()
    fake.outside("item.stopped", {"point": "Closed by hand.", "phase": "close", "skills": [], "refs": []})
    fake.outside("item.state", {"status": "closed"})
    before = fake.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_ledger(fake, src)
    assert refusal(excinfo) == "close_invalid"
    assert fake.events() == before


def test_H10_a_refused_close_overwrites_no_state(fake):
    src = Source()
    session = build_ledger(fake, src, "S4", 1)
    outside = {"status": "open", "owner": "other-owner", "blocked_by": ["EK-9"]}
    fake.outside("item.state", outside)
    fake.outside("item.note", note("see PR 12"))
    before = fake.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        session.close("Ticket pass complete.", refs=("example/repo#14",))
    assert refusal(excinfo) == "stale"
    assert keyed(after(fake, before)) == pass_end(session)
    assert [e["payload"] for e in fake.events() if e["kind"] == "item.state"][-1] == outside
    assert closed_states(fake) == []


def test_H48_an_outside_stop_before_close_is_refused_and_only_the_release_lands(fake):
    src = Source()
    session = build_ledger(fake, src, "S4", 1)
    point = "Waiting on the person."
    fake.outside(
        "item.stopped", {"point": point, "phase": "review", "skills": [], "refs": []},
        actor="other-agent", session="other-session",
    )
    before = fake.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        session.close("Ticket pass complete.", refs=("example/repo#14",), next_skills=("ticket",))
    assert refusal(excinfo) == "stale"
    assert "other-agent" in str(excinfo.value) and "other-session" in str(excinfo.value)
    assert keyed(after(fake, before)) == released(session)
    assert closed_states(fake) == []
    assert [e for e in fake.events() if e["kind"] == "item.stopped" and e["payload"]["phase"] == "close"] == []
    resumed = open_ledger(fake, src)
    assert resumed.phase == "outward_go"
    assert resumed.approved("outward") is True
    assert resumed.resume_card["stamps"][-1]["point"] == point


@pytest.mark.parametrize("later", ["stopped", "state"])
def test_H11_a_later_stop_or_state_invalidates_a_trusted_close(fake, later):
    src = Source()
    build_ledger(fake, src, "S6")
    if later == "stopped":
        fake.outside("item.stopped", {"point": "Closed again by hand.", "phase": "close", "skills": [], "refs": []})
    else:
        fake.outside("item.state", {"status": "open"})
    before = fake.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_ledger(fake, src)
    assert refusal(excinfo) == "close_invalid"
    assert fake.events() == before


_LATER = {
    "note": [("item.note", note("see PR 12"))],
    "link": [("item.linked", {"rel": "pr", "target": "#12"})],
    "review": [("review.recorded", {"round": 1, "verdict": "pass"})],
    "decision": [("decision.recorded", {"who": "a maintainer", "who_kind": "person", "standing": "lean",
                                        "effect": "Keep the follow-up small.", "tags": [{"type": "ticket", "id": KEY}],
                                        "verbatim": "Keep it small.", "phase": "close"})],
    "session pair": [("session.opened", {}), ("session.closed", {})],
    "lone opened": [("session.opened", {})],
}


@pytest.mark.parametrize("later", list(_LATER))
def test_H12_later_notes_links_and_session_records_leave_a_close_trusted(fake, later):
    src = Source()
    build_ledger(fake, src, "S6")
    for kind, payload in _LATER[later]:
        fake.outside(kind, payload)
    before = fake.events()
    resumed = open_ledger(fake, src)
    assert resumed.phase == "closed"
    assert resumed.live is False
    assert fake.events() == before


def test_H13_the_pass_end_lands_once_whatever_the_held_revision(fake):
    session = open_ledger(fake, Source())
    fake.outside("item.note", note("see PR 12"))
    revision = str(fake.events()[-1]["id"])
    session.end()
    first = fake.appends()[-1]
    assert first.argv[-2:] == ["--expect-revision", revision]
    assert keyed(fake.events()[-2:]) == released(session)
    count = len(fake.events())
    session.store.release()
    second = fake.appends()[-1]
    assert "--expect-revision" not in second.argv
    assert keyed(second.batch) == pass_end(session) and second.code is None and second.status == 0
    assert len(fake.events()) == count


def test_H14_release_after_a_refused_close_frees_the_ticket(fake):
    src = Source()
    session = build_ledger(fake, src, "S4", 1)
    fake.outside("item.note", note("see PR 12"))
    with pytest.raises(ticket.StoreUnavailable):
        session.close("Ticket pass complete.", refs=("example/repo#14",))
    assert fake.store().read()[0]["pass"] is None
    resumed = open_ledger(fake, src)
    assert resumed.live is True
    assert resumed.phase == "outward_go"


def test_H15_a_trusted_close_is_reported_closed_and_appends_nothing(fake):
    src = Source()
    build_ledger(fake, src, "S6")
    before = fake.events()
    resumed = open_ledger(fake, src)
    assert resumed.phase == "closed"
    assert fake.events() == before
    assert fake.store().read()[0]["pass"] is None


def test_H16_the_held_revision_comes_from_query_not_export(fake):
    build_ledger(fake, Source(), "S6")
    newest = fake.events()[-1]
    assert newest["kind"] == "session.closed"
    handle = fake.store()
    card, token = handle.read()
    assert card["status"] == "closed"
    assert token == str(newest["id"])
    landed = handle.apply(token, [("tried", {"what": "Note a follow-up ticket.", "phase": "close"})])
    assert landed == str(fake.events()[-1]["id"])
    assert fake.events()[-1]["kind"] == "item.tried"


@pytest.mark.parametrize("cli", ["no append-many", "no --expect-revision"])
def test_H17_an_old_cli_is_refused_at_selection(fake, repo, monkeypatch, cli):
    fake.help(cli)
    monkeypatch.setenv("TICKET_LEDGER_CLI", str(fake.path))
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.choose_store(repo, KEY, TITLE)
    assert refusal(excinfo) == "ledger_incompatible"
    result = subprocess.run(
        [sys.executable, str(SKILL_DIR / "ticket.py"), "release", KEY, "0123456789abcdef"],
        capture_output=True, text=True, env=dict(os.environ, TICKET_LEDGER_CLI=str(fake.path)), check=False,
    )
    assert result.returncode == 2
    assert len(result.stderr.strip().splitlines()) == 1
    assert [call.argv for call in fake.calls()] == [["append-many", "--help"]] * 2


def test_the_release_command_clears_a_crashed_pass_on_the_ledger(fake):
    # Design 6.3: the release command selects the store as choose_store does (E11d is the markdown form).
    session = open_ledger(fake, Source())
    env = dict(os.environ, TICKET_LEDGER_CLI=str(fake.path))
    command = [sys.executable, str(SKILL_DIR / "ticket.py"), "release", KEY]
    wrong = subprocess.run(command + ["0123456789abcdef"], capture_output=True, text=True, env=env, check=False)
    assert wrong.returncode == 1
    assert len(wrong.stderr.strip().splitlines()) == 1
    assert fake.store().read()[0]["pass"]["id"] == session.pass_id
    right = subprocess.run(command + [session.pass_id], capture_output=True, text=True, env=env, check=False)
    assert right.returncode == 0, right.stderr
    assert fake.store().read()[0]["pass"] is None
    assert open_ledger(fake, Source()).live is True


def test_H18_the_ledger_card_folds_every_kind(fake):
    src = Source()
    first = open_ledger(fake, src)
    present(first)
    first.end()
    src.edit("text")
    session = open_ledger(fake, src)
    present(session)
    go_plan(session)
    session.begin_build()
    changes(session)
    session.record_tried("Run the suite.", outcome="green", evidence="reviews/round-1.md")
    card, _ = fake.store().read()
    assert card == session._card
    plans = [e for e in fake.events() if e["kind"] == "item.note" and e["payload"]["text"].startswith("ticket-plan:v2 ")]
    assert len(plans) == 2
    assert card["plan"]["revision"] == 2 and card["plan"]["pass"] == session.pass_id
    assert [(d["gate"], d["plan_revision"], d["criteria"], d["pass"]) for d in card["decisions"]] == [
        ("plan", 2, list(src.criteria), session.pass_id)]
    assert [(r["round"], r["verdict"], r["plan_revision"], r["criteria"], r["passed"]) for r in card["reviews"]] == [
        (1, "changes", 2, list(src.criteria), [])]
    assert card["tried"] == [{"what": "Run the suite.", "phase": "build", "outcome": "green", "evidence": "reviews/round-1.md"}]
    assert [s["point"] for s in card["stamps"]] == [ticket.PLAN_GO_POINT.format(revision=n) for n in (1, 2)]
    assert card["pass"]["id"] == session.pass_id


@needs_ledger_cli
def test_H19_the_pass_is_hidden_on_a_closed_item(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("LEDGER_HARNESS", "ticket-tests")
    # A read never creates the database, so the ledger holds another item first.
    other = '[{"kind": "item.created", "payload": {"type": "ticket", "title": "Another ticket"}}]'
    subprocess.run([LEDGER_CLI, "--json", "append-many", "EK-1"], input=other, text=True, check=True, capture_output=True)
    session = build_ledger(LEDGER_CLI, Source(), "S6")
    card, _ = ticket.LedgerStore(LEDGER_CLI, KEY, TITLE).read()
    assert card["status"] == "closed"
    assert card["pass"] is None
    with sqlite3.connect(tmp_path / "ledger.db") as database:
        stored = database.execute(
            "SELECT count(*) FROM events WHERE kind = 'item.note' AND idempotency_key = ?",
            (f"ticket-pass/{KEY}/{session.pass_id}/record",),
        ).fetchone()[0]
    assert stored == 1


def test_H20_a_review_round_and_its_stamp_land_together_or_not_at_all(fake):
    session = build_ledger(fake, Source(), "S2", 0)
    fake.answer("append-many", "stale_revision")
    before = fake.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        passing(session)
    assert refusal(excinfo) == "stale"
    refused = next(call for call in fake.appends() if call.code == "stale_revision")
    assert kinds(refused.batch) == ["item.note", "item.stopped"]
    assert keyed(after(fake, before)) == released(session)
    assert session.live is False


def test_H21_a_stale_write_names_the_newest_writer(fake):
    session = build_ledger(fake, Source(), "S4", 1)
    fake.outside("item.note", note("see PR 12"), actor="other-agent", session="other-session")
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        session.close("Ticket pass complete.", refs=("example/repo#14",))
    assert "actor=other-agent" in str(excinfo.value)
    assert "session=other-session" in str(excinfo.value)


def test_H22_a_key_conflict_is_stale_not_success(fake):
    session = build_ledger(fake, Source(), "S2", 0)
    message = "idempotency_key 'ticket-write/x' already recorded as a different event"
    fake.answer("append-many", "conflict", message=message)
    before = fake.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        changes(session)
    assert refusal(excinfo) == "stale"
    assert message in str(excinfo.value)
    assert keyed(after(fake, before)) == released(session)
    assert [call.code for call in fake.appends()[-2:]] == ["conflict", None]
    assert session.live is False


CREATED = {"kind": "item.created", "item_id": KEY, "idempotency_key": f"ticket-item/{KEY}/created",
           "payload": {"type": "ticket", "title": TITLE, "source": WRITER}}


def test_H23_the_first_open_creates_the_item_before_it_records_the_spec_and_claims(fake):
    # Design 6.6: the intake needs the item, so the open creates it in a batch of its own under
    # --expect-revision none, records the spec, then reads the card, confirms the spec is still the one it
    # recorded, and claims under that revision.
    session = open_ledger(fake, Source())
    create, claim = fake.appends()
    assert create.argv[-2:] == ["--expect-revision", "none"]
    assert create.batch == [CREATED]
    assert [call.verb for call in fake.calls()] == [
        "query", "append-many", "spec", "query", "export-item", "spec", "append-many"]
    assert kinds(fake.events()) == ["item.created", "item.spec", "item.state", "item.note"]
    assert claim.argv[-2:] == ["--expect-revision", str(fake.events()[1]["id"])]
    taken, record = claim.batch
    holder = f"ticket-pass/{session.pass_id}"
    assert taken["idempotency_key"] == f"ticket-lease/{KEY}/{session.pass_id}/{fake.events()[1]['id']}"
    assert {name: taken["payload"][name] for name in ("status", "owner", "claim", "source")} == {
        "status": "claimed", "owner": holder, "claim": holder, "source": WRITER}
    assert record["kind"] == "item.note" and record["idempotency_key"] == f"ticket-pass/{KEY}/{session.pass_id}/record"
    body = json.loads(record["payload"]["text"].removeprefix("ticket-pass:v2 "))
    assert (body["id"], body["host"], body["holder"], body["lease"]) == (session.pass_id, ticket._host(), holder, "taken")
    rows = subprocess.run([str(fake.path), "--json", "query", "--item", KEY], capture_output=True, text=True, check=True)
    assert '"type": "ticket"' in rows.stdout


def test_H23_a_claim_on_none_still_creates_the_item_in_its_own_batch(fake):
    # A handle used without an open (the import, a direct apply) keeps the claim's creation row of 6.2.
    handle = fake.store()
    handle.apply(ticket.NO_TOKEN, [("claim", {"id": "0123456789abcdef", "host": "here", "started_at": "2026-11-13T00:00:00Z"})])
    (claim,) = fake.appends()
    assert claim.argv[-2:] == ["--expect-revision", "none"]
    assert [element["kind"] for element in claim.batch] == ["item.created", "item.state", "item.note"]
    assert claim.batch[0] == CREATED


class CreateRace(ticket.LedgerStore):
    """A ledger handle that runs ``racer`` once, just before its first ``append-many``."""

    def __init__(self, cli, key, title, racer):
        super().__init__(cli, key, title)
        self._racer = racer

    def _send(self, batch, expect):
        racer, self._racer = self._racer, None
        if racer is not None:
            racer()
        return super()._send(batch, expect)


@pytest.mark.parametrize("title", ["same title", "other title"])
def test_H23_a_racing_first_open_creates_the_item_once(fake, title):
    src = Source()
    first = []
    second = CreateRace(fake.path, KEY, TITLE if title == "same title" else "Another title",
                        lambda: first.append(open_ledger(fake, src)))
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_ledger(fake, src, handle=second)
    creates = [call for call in fake.appends() if call.batch[0]["kind"] == "item.created"]
    assert len(creates) == 2 and all(call.argv[-2:] == ["--expect-revision", "none"] for call in creates)
    if title == "same title":
        # The same key and payload replay: the second open goes on, records nothing new, and meets the live pass.
        assert isinstance(excinfo.value, ticket.TicketHeld)
        assert excinfo.value.holder["id"] == first[0].pass_id
        assert creates[1].code is None
    else:
        # Another title under the same key is a conflict: stale, and nothing more is written.
        assert refusal(excinfo) == "stale"
        assert creates[1].code == "conflict"
    assert kinds(fake.events()).count("item.created") == 1
    assert kinds(fake.events()).count("item.spec") == 1
    assert len(claims(fake)) == 1


@pytest.mark.parametrize(
    "payload",
    [{"type": "initiative", "title": "Existing"}, {"title": "Existing"}, {"type": 7, "title": "Existing"}],
    ids=["initiative", "missing type", "non-string type"],
)
def test_a_key_held_by_another_item_type_is_refused(fake, payload):
    fake.outside("item.created", payload)
    before = fake.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_ledger(fake, Source())
    assert refusal(excinfo) == "ledger_refused"
    assert KEY in str(excinfo.value) and "not a ticket" in str(excinfo.value)
    assert fake.events() == before
    assert fake.appends() == []
    assert fake.calls("spec") == []  # the intake refuses it before any spec is recorded (design 6.6)


@needs_ledger_cli
def test_H24_every_batch_the_store_builds_passes_the_ledger_schema(tmp_path):
    validate_payload = _ledger_validate_payload()
    kinds_sent = set()
    for stage, rounds in (("S0", 0), ("S1", 0), ("S2", 1), ("S3", 2), ("S4", 1), ("S5", 0), ("S6", 0)):
        fake = FakeLedger(tmp_path / stage)
        try:
            session = build_ledger(fake, Source(), stage, rounds)
            if stage == "S0":
                session.end()
                session = open_ledger(fake, Source())
                session.record_tried("Read the ticket.", outcome="understood", evidence="docs/ticket.md")
                session.more_info("person", "Which release is this for?")
            session.end()
        finally:
            # Every append-many batch the fake received, whether or not the stage
            # finished. The binder's elect and append calls are separate and are
            # excluded here: they are not append-many batches.
            for call in fake.appends():
                assert call.verb == "append-many"
                for element in call.batch:
                    assert set(element) == {"kind", "payload", "item_id", "idempotency_key"}
                    validate_payload(element["kind"], element["payload"])
                    assert element["payload"]["source"] == WRITER
                    assert element["kind"] not in ("decision.recorded", "review.recorded", "outside.recorded")
                    assert "source_event_id" not in element["payload"]
                    if element["kind"] == "item.stopped":
                        assert "close" not in element["payload"]
                    kinds_sent.add(element["kind"])
    assert kinds_sent == {
        "item.created", "session.closed", "item.note", "item.stopped", "item.tried", "item.state"}


def _ledger_validate_payload():
    """The ledger's own ``validate_payload``, from ``ledger/schema.py`` beside the CLI ``LEDGER_CLI`` names.

    It is imported as ``ledger.schema``, inside its package, because the schema's check of a lease stamp
    imports ``ledger.lease`` relatively.
    """

    cli = Path(LEDGER_CLI).resolve()
    for root in (cli.parent, cli.parent.parent / "skills" / "_lib"):
        if (root / "ledger" / "schema.py").is_file():
            saved = list(sys.path)
            sys.path.insert(0, str(root))
            try:
                return importlib.import_module("ledger.schema").validate_payload
            finally:
                sys.path[:] = saved
    raise AssertionError(f"no ledger/schema.py beside {cli}")


# --- Ticket records in notes ------------------------------------------------------


def _a_review():
    return {"round": 1, "verdict": "changes", "plan_revision": 1, "criteria": [A, B], "passed": [], "findings": [],
            "reviewer": "fresh-context reviewer", "evidence": "reviews/round-1.md"}


@pytest.mark.parametrize("frame", ["plan v1", "plan v3", "decision v1", "review v9", "kind stamp"])
def test_H25_a_ticket_note_with_an_unknown_frame_is_unreadable(fake, frame):
    src = Source()
    build_ledger(fake, src, "S2", 0).end()
    card, _ = fake.store().read()
    text = {
        "plan v1": "ticket-plan:v1 " + ticket._canonical(card["plan"]),
        "plan v3": "ticket-plan:v3 " + ticket._canonical(card["plan"]),
        "decision v1": "ticket-decision:v1 " + ticket._canonical(card["decisions"][0]),
        "review v9": "ticket-review:v9 " + ticket._canonical(_a_review()),
        "kind stamp": "ticket-stamp:v2 " + ticket._canonical(
            {"point": "Stopped by hand.", "phase": "build", "next_skills": [], "refs": [], "close": False}),
    }[frame]
    fake.outside("item.note", note(text))
    before = fake.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_ledger(fake, src)
    assert refusal(excinfo) == "card_unreadable"
    assert text.split(" ")[0] in str(excinfo.value)
    assert fake.events() == before


_A_PASS_RECORD = {"id": "0123456789abcdef", "host": "here", "started_at": "2026-11-22T00:00:00Z", "holder": "seat-a"}


@pytest.mark.parametrize("body", ["not json", "json list", "fails the validator", "a pass record that fails its validator"])
def test_H26_a_ticket_note_that_does_not_parse_is_unreadable(fake, body):
    src = Source()
    build_ledger(fake, src, "S2", 0).end()
    text = {
        "not json": "ticket-review:v2 {",
        "json list": "ticket-review:v2 [1]",
        "fails the validator": 'ticket-review:v2 {"round": "2", "verdict": "pass"}',
        "a pass record that fails its validator": "ticket-pass:v2 " + ticket._canonical({**_A_PASS_RECORD, "lease": "borrowed"}),
    }[body]
    fake.outside("item.note", note(text))
    before = fake.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_ledger(fake, src)
    assert refusal(excinfo) == "card_unreadable"
    assert fake.events() == before


@pytest.mark.parametrize("text", ["see PR 12", "ticket-tracker moved it"], ids=["plain", "ticket-prefixed prose"])
def test_H27_ordinary_notes_fold_into_nothing(fake, text):
    src = Source()
    build_ledger(fake, src, "S3", 1).end()
    fake.outside("item.note", note(text))
    resumed = open_ledger(fake, src)
    assert resumed.phase == "outward_go"
    assert resumed.reconciliation == ()
    assert resumed.review_round == 1
    assert resumed.plan.review is not None and resumed.plan.review["verdict"] == "pass"


# --- Reading the CLI's answer ----------------------------------------------------


def _refused_review(fake, code, status=None):
    """S2[0] live; the review batch is answered ``code``. Returns the refusal and the calls after the refused one."""

    session = build_ledger(fake, Source(), "S2", 0)
    message = f"the ledger refused this batch ({code} detail)"
    fake.answer("append-many", code, status=status, message=message)
    before = fake.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        changes(session)
    calls = fake.calls()
    index = max(n for n, call in enumerate(calls) if call.code == code)
    assert keyed(after(fake, before)) == released(session)
    assert session.live is False
    return excinfo, message, calls[index + 1:]


@pytest.mark.parametrize("code", ["validation", "missing_harness", "shared_database", "synced_path"])
def test_H28_a_refusal_that_is_not_a_race_is_not_stale(fake, code):
    excinfo, message, later = _refused_review(fake, code)
    assert refusal(excinfo) == "ledger_refused"
    assert code in str(excinfo.value) and message in str(excinfo.value)
    assert [call.verb for call in later] == ["query", "export-item", "append-many"]
    assert kinds(later[-1].batch) == ["session.closed", "item.state"]
    assert fake.store().read()[0]["pass"] is None


@pytest.mark.parametrize("code", ["not_found", "missing_db", "integrity"])
def test_H29_exit_one_and_four_refusals_are_permanent(fake, code):
    excinfo, _, later = _refused_review(fake, code)
    assert refusal(excinfo) == "ledger_refused"
    assert code in str(excinfo.value)
    # The release's fresh read (query, export-item) comes first: no batch-key lookup follows the refused answer.
    assert [call.verb for call in later] == ["query", "export-item", "append-many"]


def test_H30_a_usage_answer_is_incompatible(fake):
    excinfo, _, later = _refused_review(fake, "usage")
    assert refusal(excinfo) == "ledger_incompatible"
    assert [call.verb for call in later] == ["query", "export-item", "append-many"]


@pytest.mark.parametrize(
    "answer", ["stale_revision with exit 3", "conflict with exit 5", "ok true with exit 1"],
)
def test_H31_the_code_is_read_from_the_envelope_not_the_exit_status(fake, answer):
    session = build_ledger(fake, Source(), "S2", 0)
    if answer == "ok true with exit 1":
        fake.script("append-many", commit=True, status=1)
        changes(session)
        assert fake.appends()[-1].status == 1
        assert session.review_round == 1
        session.record_tried("Run the suite.")
        assert kinds(fake.events())[-1] == "item.tried"
        assert session.live is True
        return
    code, status = ("stale_revision", 3) if answer.startswith("stale") else ("conflict", 5)
    fake.answer("append-many", code, status=status)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        changes(session)
    assert refusal(excinfo) == "stale"


@pytest.mark.parametrize("answer", ["error landed", "unusable landed", "unusable absent"])
def test_H32_error_and_unusable_are_resolved_by_the_batch_key(fake, answer):
    session = build_ledger(fake, Source(), "S2", 0)
    code, outcome = answer.split()
    fake.answer("append-many", code, commit=outcome == "landed", message=f"unusable database: {code} detail")
    if outcome == "landed":
        changes(session)
        assert session.review_round == 1
        session.record_tried("Run the suite.")
        assert kinds(fake.events())[-1] == "item.tried"
        assert session.live is True
        return
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        changes(session)
    assert refusal(excinfo) == "ledger_refused"
    assert f"unusable database: {code} detail" in str(excinfo.value)
    assert session.live is False


# --- The join, the store's own used-handle guard, and the folds of an outsider's events ---


class _LockExits:
    """Delegates to a handle's real lock and records the handle's binding at every release."""

    def __init__(self, handle):
        self.handle, self.lock, self.seen = handle, handle._threads, []

    def __enter__(self):
        return self.lock.__enter__()

    def __exit__(self, *exc):
        self.seen.append((self.handle.pass_id, self.handle.ended))
        return self.lock.__exit__(*exc)


def test_H33_a_join_appends_nothing_and_binds_under_the_store_lock(fake):
    first = open_ledger(fake, Source())
    handle = fake.store()
    _, token = handle.read()
    exits = handle._threads = _LockExits(handle)
    events, calls = fake.events(), fake.calls()
    assert handle.apply(token, [("join", {"id": first.pass_id})]) == token
    assert exits.seen == [(first.pass_id, None)]
    assert fake.events() == events
    assert fake.calls() == calls
    assert (handle.pass_id, handle.ended) == (first.pass_id, None)
    landed = handle.apply(token, [("tried", {"what": "A write after the join.", "phase": "build"})])
    assert landed == str(fake.events()[-1]["id"])
    assert fake.events()[-1]["kind"] == "item.tried"
    assert len(claims(fake)) == 1


def test_a_claim_binds_the_handle_before_the_store_lock_is_released(fake):
    handle = fake.store()
    _, token = handle.read()
    exits = handle._threads = _LockExits(handle)
    handle.apply(token, [("claim", _CLAIM)])
    assert exits.seen == [(_CLAIM["id"], None)]
    assert (handle.pass_id, handle.ended) == (_CLAIM["id"], None)


def test_a_second_process_joins_a_live_pass_by_its_id_and_continues(fake):
    src = Source()
    first = build_ledger(fake, src, "S2", 0)
    opened = len(claims(fake))
    joined = open_ledger(fake, src, pass_id=first.pass_id)
    assert joined.live is True and joined.pass_id == first.pass_id
    assert len(claims(fake)) == opened
    joined.record_tried("Run the suite.")
    assert kinds(fake.events())[-1] == "item.tried"
    assert fake.store().read()[0]["pass"]["id"] == first.pass_id


def test_a_join_naming_another_id_is_refused_held_and_writes_nothing(fake):
    src = Source()
    first = open_ledger(fake, src)
    before = fake.events()
    with pytest.raises(ticket.TicketHeld) as excinfo:
        open_ledger(fake, src, pass_id="0000000000000000")
    assert excinfo.value.holder["id"] == first.pass_id
    assert fake.events() == before


_CLAIM = {"id": "0123456789abcdef", "host": "example-host", "started_at": "2026-01-01T00:00:00Z"}


@pytest.mark.parametrize("event", ["claim", "join"])
@pytest.mark.parametrize("used", ["bound", "ended"])
def test_a_binding_apply_on_a_used_handle_is_refused_before_any_cli_call(fake, used, event):
    handle = fake.store()
    session = open_ledger(fake, Source(), handle=handle)
    if used == "ended":
        session.end()
    value = _CLAIM if event == "claim" else {"id": session.pass_id}
    token = str(fake.events()[-1]["id"])
    events, calls = fake.events(), fake.calls()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        handle.apply(token, [(event, value)])
    assert refusal(excinfo) == "handle_used"
    assert session.pass_id in str(excinfo.value)
    assert fake.events() == events
    assert fake.calls() == calls


def test_a_claim_on_a_handle_bound_during_the_open_is_refused_by_the_store(fake):
    handle = fake.store()
    box = {}

    class Racing(Source):
        """A source whose pull opens pass A on the same handle before it returns."""

        def pull(self, key):
            if "a" not in box:
                box["a"] = open_ledger(fake, Source(), handle=handle)
                box["events"], box["appends"] = fake.events(), len(fake.appends())
            return super().pull(key)

    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_ledger(fake, Racing(), handle=handle)
    a = box["a"]
    assert refusal(excinfo) == "handle_used"
    assert a.pass_id in str(excinfo.value) and "fresh store handle" in str(excinfo.value)
    assert fake.events() == box["events"]
    assert len(fake.appends()) == box["appends"]
    assert fake.store().read()[0]["pass"]["id"] == a.pass_id
    a.record_tried("A writes after the refused open.")
    assert fake.events()[-1]["kind"] == "item.tried"
    assert a.live is True


def test_a_second_join_on_a_handle_bound_during_the_open_is_refused_by_the_store(fake):
    first = open_ledger(fake, Source())
    handle = fake.store()
    box = {}

    class Racing(Source):
        """A source whose pull joins the pass on the same handle before it returns."""

        def pull(self, key):
            if "j1" not in box:
                box["j1"] = open_ledger(fake, Source(), pass_id=first.pass_id, handle=handle)
            return super().pull(key)

    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_ledger(fake, Racing(), pass_id=first.pass_id, handle=handle)
    assert refusal(excinfo) == "handle_used"
    assert first.pass_id in str(excinfo.value)
    j1 = box["j1"]
    assert j1.live is True
    j1.record_tried("The first join writes.")
    assert fake.events()[-1]["kind"] == "item.tried"
    assert fake.store().read()[0]["pass"]["id"] == first.pass_id


def _outside_close(fake, phase="close", stop_source=WRITER, state_source=WRITER):
    """An outsider's stop and state under this store's own ``ticket-close`` keys, with the named phase and sources."""

    stem = f"ticket-close/{KEY}/0123456789abcdef/fedcba9876543210"
    fake.outside("item.stopped", {"point": "Closed by hand.", "phase": phase, "skills": [], "refs": [], "source": stop_source},
                 key=f"{stem}/stop")
    fake.outside("item.state", {"status": "closed", "source": state_source}, key=f"{stem}/state")


@pytest.mark.parametrize(
    "variant, trusted",
    [("control", True), ("phase", False), ("source", False)],
    ids=["right keys, close phase, same source", "a stop that is not a close phase", "a stop of another source"],
)
def test_an_outside_close_under_the_close_keys_is_trusted_only_with_a_close_phase_and_one_source(fake, variant, trusted):
    src = Source()
    build_ledger(fake, src, "S4", 1).end()
    if variant == "control":
        _outside_close(fake)
    elif variant == "phase":
        _outside_close(fake, phase="build")
    else:
        _outside_close(fake, state_source="another-writer")
    before = fake.events()
    # The fold marks the last stamp as the close only for a trusted close. The resume refuses a close stamp
    # that is not at the close phase on its own, so the mark is the observable guard for the phase and the source.
    card, _ = fake.store().read()
    assert card["status"] == "closed"
    assert card["stamps"][-1]["close"] is trusted
    if trusted:
        assert open_ledger(fake, src).phase == "closed"
    else:
        with pytest.raises(ticket.StoreUnavailable) as excinfo:
            open_ledger(fake, src)
        assert refusal(excinfo) == "close_invalid"
    assert fake.events() == before


def test_a_release_naming_another_pass_leaves_the_live_pass_holding(fake):
    src = Source()
    live = open_ledger(fake, src)
    fake.outside("session.closed", {"outcome": "released by hand"}, key=f"ticket-pass/{KEY}/0123456789abcdef/released")
    holder = fake.store().read()[0]["pass"]
    assert (holder or {}).get("id") == live.pass_id
    before = fake.events()
    with pytest.raises(ticket.TicketHeld) as excinfo:
        open_ledger(fake, src)
    assert excinfo.value.holder["id"] == live.pass_id
    assert fake.events() == before
    fake.outside("session.closed", {"outcome": "released by hand"}, key=f"ticket-pass/{KEY}/{live.pass_id}/released")
    assert fake.store().read()[0]["pass"] is None


# --- A pinned ledger CLI: nothing is looked up on PATH, and a moved one is refused by name ----------------


def _pin_topic_cli(cli):
    """Write the topic skill's override file under this test's HOME, as the setup step does."""

    import json as _json

    path = Path(os.environ["HOME"]) / ".claude" / "skill-config" / "topic.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({"ledger_cli": str(cli)}), encoding="utf-8")


def _planted_ahead_on_path(tmp_path, monkeypatch):
    """A `ledger` first on PATH that writes a marker file whenever it is run with any argument."""

    marker = tmp_path / "planted.marker"
    folder = tmp_path / "evil"
    folder.mkdir()
    plant = folder / "ledger"
    plant.write_text(f'#!/bin/sh\necho "ran: $*" >> {marker}\nexit 1\n', encoding="utf-8")
    plant.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    return marker


def test_a_pinned_cli_runs_every_ticket_call_and_a_planted_ledger_on_path_never_runs(tmp_path, monkeypatch, capsys):
    from ledger_support import machine

    pinned = FakeLedger(tmp_path / "pinned")
    machine(monkeypatch, tmp_path)
    _pin_topic_cli(pinned.path)
    marker = _planted_ahead_on_path(tmp_path, monkeypatch)
    # The store selection, the opened pass and the item-source read all run the pinned CLI.
    chosen = ticket.choose_store(tmp_path, KEY, TITLE)
    assert isinstance(chosen, ticket.LedgerStore) and chosen.cli == str(pinned.path)
    assert chosen.selection == "ledger (explicit setting: ledger_cli in the topic config names it)"
    session = ticket.TicketPass.open(KEY, Source(), chosen)
    assert session.reconciliation[0] == f"store: {chosen.selection}"
    answer = ticket._ledger_read(chosen.cli, "query", "--item", KEY)
    assert answer is not None
    assert pinned.calls(), "the pinned CLI was run"
    assert not marker.exists(), marker.read_text() if marker.exists() else ""


def test_a_moved_pinned_cli_is_refused_by_name_before_any_ticket_call_runs(tmp_path, monkeypatch):
    from ledger_support import machine

    pinned = FakeLedger(tmp_path / "pinned")
    machine(monkeypatch, tmp_path)
    _pin_topic_cli(pinned.path)
    marker = _planted_ahead_on_path(tmp_path, monkeypatch)
    live = ticket.choose_store(tmp_path, KEY, TITLE)
    calls = len(pinned.calls())
    pinned.path.unlink()
    # The store selection refuses (not `ledger_incompatible`): the probe is never reached.
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.choose_store(tmp_path, KEY, TITLE)
    assert refusal(excinfo) == "ledger_cli_moved"
    line = str(excinfo.value)
    assert line.startswith("ledger_cli_moved: ") and str(pinned.path) in line and " setup" in line and "\n" not in line
    # A handle chosen before the move refuses on its first call, through the store's own runner.
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.TicketPass.open(KEY, Source(), live)
    assert refusal(excinfo) == "ledger_cli_moved"
    # And the item-source read refuses the same way.
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket._ledger_read(str(pinned.path), "query", "--item", KEY)
    assert refusal(excinfo) == "ledger_cli_moved"
    assert len(pinned.calls()) == calls
    assert not marker.exists()


def test_a_moved_pinned_cli_stops_import_cards_by_name(tmp_path, monkeypatch, capsys):
    from ledger_support import machine

    pinned = FakeLedger(tmp_path / "pinned")
    machine(monkeypatch, tmp_path)
    _pin_topic_cli(pinned.path)
    marker = _planted_ahead_on_path(tmp_path, monkeypatch)
    pinned.path.unlink()
    assert ticket.main(["import-cards", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("ticket.py: ledger_cli_moved: ") and len(err.splitlines()) == 1
    assert not marker.exists()


GO_WORDS = "Go with this plan."


def _prompt(session_id, text=GO_WORDS, origin="person", **extra):
    payload = {"origin": origin, "text": text, "session_id": session_id}
    payload.update(extra)
    return payload


def _capture(fake, payload, key=None, recorded_at=None):
    """An item-less utterance.captured event, as ``ledger capture --kind prompt`` writes."""

    if recorded_at is None:
        recorded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    fake._append(
        None, "utterance.captured", payload, key or f"capture:{len(fake.events())}",
        recorded_at=recorded_at,
    )


def _binder_kinds(fake):
    return [event["kind"] for event in fake.events() if event["kind"] in ("decision.elected", "decision.recorded")]


def test_H39_bind_picks_this_sessions_newest_utterance(fake, monkeypatch):
    monkeypatch.setenv("LEDGER_SESSION", "sess-this")
    session = open_ledger(fake, Source())
    present(session)
    _capture(fake, _prompt("sess-this"))
    _capture(fake, _prompt("sess-other"))
    this_id, other_id = [event["id"] for event in fake.events() if event["kind"] == "utterance.captured"]
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    assert result["bound"] is True
    assert result["utterance_id"] == this_id != other_id
    elected = [event for event in fake.events() if event["kind"] == "decision.elected"]
    recorded = [event for event in fake.events() if event["kind"] == "decision.recorded"]
    assert len(elected) == 1 and len(recorded) == 1
    assert elected[0]["payload"]["quote_ref"] == this_id
    assert elected[0]["payload"]["quotes"] == [GO_WORDS]
    assert elected[0]["payload"]["chosen"] == "go"
    assert elected[0]["payload"]["rejected"] == ["hold"]
    assert elected[0]["idempotency_key"] == f"ticket-bind/{KEY}/plan/1/{this_id}/elect"
    assert recorded[0]["payload"]["source_event_id"] == elected[0]["id"] == result["election_id"]
    assert recorded[0]["payload"]["who"] == "the person"
    assert recorded[0]["payload"]["who_kind"] == "person"
    assert recorded[0]["payload"]["standing"] == "settled"
    assert recorded[0]["payload"]["source"] == WRITER
    assert recorded[0]["payload"]["verbatim"] == GO_WORDS
    assert recorded[0]["idempotency_key"] == f"ticket-bind/{KEY}/plan/1/{this_id}/decision"
    assert fake.calls("elect") and fake.calls("append")
    assert all("decision.recorded" not in (call.stdin or "") for call in fake.appends())


@pytest.mark.parametrize(
    "case, reason",
    [
        ("unset", "LEDGER_SESSION unset"),
        ("no match", "no matching utterance"),
        ("other session", "no matching utterance"),
        ("other origin", "no matching utterance"),
        ("omitted", "no matching utterance"),
        ("wrong words", "no matching utterance"),
    ],
)
def test_H40_bind_fails_closed_with_no_write(fake, monkeypatch, case, reason):
    if case != "unset":
        monkeypatch.setenv("LEDGER_SESSION", "sess-this")
    else:
        monkeypatch.delenv("LEDGER_SESSION", raising=False)
    session = open_ledger(fake, Source())
    present(session)
    if case == "unset":
        _capture(fake, _prompt("sess-this"))
    elif case == "other session":
        _capture(fake, _prompt("sess-other"))
    elif case == "other origin":
        _capture(fake, {"text": GO_WORDS, "session_id": "sess-this"})
    elif case == "omitted":
        _capture(fake, _prompt("sess-this", omitted_bytes=12, omitted_sha256="a" * 64))
    elif case == "wrong words":
        _capture(fake, _prompt("sess-this", text="Ship it."))
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    assert result == {"bound": False, "reason": reason}
    assert _binder_kinds(fake) == []
    assert fake.calls("elect") == []
    assert fake.calls("append") == []
    if case == "unset":
        assert fake.calls("events") == []


def test_H40_bind_fails_closed_when_elect_is_refused(fake, monkeypatch):
    monkeypatch.setenv("LEDGER_SESSION", "sess-this")
    session = open_ledger(fake, Source())
    present(session)
    _capture(fake, _prompt("sess-this"))
    fake.answer("elect", "usage")
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    assert result["bound"] is False and result["reason"].startswith("elect refused:")
    assert _binder_kinds(fake) == []
    assert fake.calls("append") == []


def test_H40_bind_fails_closed_when_append_is_refused(fake, monkeypatch):
    monkeypatch.setenv("LEDGER_SESSION", "sess-this")
    session = open_ledger(fake, Source())
    present(session)
    _capture(fake, _prompt("sess-this"))
    fake.answer("append", "validation")
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    assert result["bound"] is False and result["reason"].startswith("append refused:")
    assert [event["kind"] for event in fake.events() if event["kind"] == "decision.elected"]
    assert [event["kind"] for event in fake.events() if event["kind"] == "decision.recorded"] == []


def test_a_markdown_go_is_not_bound(repo):
    from ticket_support import open_pass

    session = open_pass(repo, Source())
    present(session)
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    assert result == {"bound": False, "reason": "no ledger"}


def test_H42_a_bound_plan_go_leaves_begin_build_unblocked(fake, monkeypatch):
    monkeypatch.setenv("LEDGER_SESSION", "sess-this")
    session = open_ledger(fake, Source())
    present(session)
    _capture(fake, _prompt("sess-this"))
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    assert result["bound"] is True
    session.begin_build()
    assert session.phase == "build"


def test_H42_an_append_refused_bind_leaves_begin_build_unblocked(fake, monkeypatch):
    monkeypatch.setenv("LEDGER_SESSION", "sess-this")
    session = open_ledger(fake, Source())
    present(session)
    _capture(fake, _prompt("sess-this"))
    fake.answer("append", "validation")
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    assert result["bound"] is False and result["reason"].startswith("append refused:")
    session.begin_build()
    assert session.phase == "build"


def test_H42_a_foreign_write_after_the_note_still_makes_begin_build_stale(fake, monkeypatch):
    monkeypatch.setenv("LEDGER_SESSION", "sess-this")
    session = open_ledger(fake, Source())
    present(session)
    _capture(fake, _prompt("sess-this"))
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    assert result["bound"] is True
    fake.outside("item.note", note("see PR 12"))
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        session.begin_build()
    assert refusal(excinfo) == "stale"


@pytest.mark.parametrize("verb", ["elect", "append"])
def test_H43_bind_is_unbound_when_a_success_payload_has_no_event_id(fake, monkeypatch, verb):
    monkeypatch.setenv("LEDGER_SESSION", "sess-this")
    session = open_ledger(fake, Source())
    present(session)
    _capture(fake, _prompt("sess-this"))
    fake.script(
        verb, commit=True, status=0,
        stdout=json.dumps({"ok": True, "data": {}}, sort_keys=True) + "\n",
    )
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    assert result["bound"] is False
    assert _is_text_reason(result.get("reason"))
    if verb == "elect":
        assert result["reason"] == "elect refused: no event id"
        assert fake.calls("append") == []
        assert [event["kind"] for event in fake.events() if event["kind"] == "decision.recorded"] == []
    else:
        assert result["reason"] == "append refused: no event id"


def _is_text_reason(reason):
    return type(reason) is str and bool(reason.strip())


def test_H44_bind_ignores_a_prompt_captured_before_the_pass(fake, monkeypatch):
    monkeypatch.setenv("LEDGER_SESSION", "sess-this")
    _capture(fake, _prompt("sess-this"), recorded_at="2000-01-01T00:00:00Z")
    session = open_ledger(fake, Source())
    present(session)
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    assert result == {"bound": False, "reason": "no matching utterance"}
    assert _binder_kinds(fake) == []
    assert fake.calls("elect") == []
    assert fake.calls("append") == []


def _planted(kind, payload, key, item=KEY):
    return {"kind": kind, "payload": payload, "item_id": item, "key": key}


@pytest.mark.parametrize("window", ["outsider note before elect", "outsider note between elect and append",
                                    "this pass released between elect and append"])
def test_H45_a_foreign_write_in_the_bind_window_makes_begin_build_stale(fake, monkeypatch, window):
    monkeypatch.setenv("LEDGER_SESSION", "sess-this")
    session = open_ledger(fake, Source())
    present(session)
    _capture(fake, _prompt("sess-this"))
    if window == "outsider note before elect":
        fake.script("elect", commit=True, before=[_planted("item.note", note("see PR 12"), "outside-note-1")])
    elif window == "outsider note between elect and append":
        fake.script("append", commit=True, before=[_planted("item.note", note("see PR 12"), "outside-note-1")])
    else:
        release = {"outcome": f"ticket pass {session.pass_id} released", "source": WRITER}
        fake.script("append", commit=True, before=[
            _planted("session.closed", release, f"ticket-pass/{KEY}/{session.pass_id}/released"),
        ])
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    # The chain itself is sound, so the go is bound; only the token stays put.
    assert result["bound"] is True
    assert _binder_kinds(fake) == ["decision.elected", "decision.recorded"]
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        session.begin_build()
    assert refusal(excinfo) == "stale"


def test_H45_an_unreadable_range_after_the_bind_leaves_begin_build_stale(fake, monkeypatch):
    monkeypatch.setenv("LEDGER_SESSION", "sess-this")
    session = open_ledger(fake, Source())
    present(session)
    _capture(fake, _prompt("sess-this"))
    fake.script("export-item", commit=True)  # the claim read answers as the CLI does
    fake.script("export-item", stdout="not json\n", status=1)  # the range read after the bind is unreadable
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    assert result["bound"] is True
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        session.begin_build()
    assert refusal(excinfo) == "stale"


@pytest.mark.parametrize("when, bound", [("before the claim", False), ("after the claim", True)])
def test_H46_bind_takes_only_a_prompt_captured_after_the_claim_event(fake, monkeypatch, when, bound):
    second = "2026-11-16T10:00:00Z"
    monkeypatch.setattr(ticket, "_now", lambda: second)
    monkeypatch.setenv("LEDGER_SESSION", "sess-this")
    if when == "before the claim":
        _capture(fake, _prompt("sess-this"), recorded_at=second)
    session = open_ledger(fake, Source())
    present(session)
    if when == "after the claim":
        _capture(fake, _prompt("sess-this"), recorded_at=second)
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    if bound:
        assert result["bound"] is True
        assert _binder_kinds(fake) == ["decision.elected", "decision.recorded"]
    else:
        assert result == {"bound": False, "reason": "no matching utterance"}
        assert fake.calls("elect") == [] and fake.calls("append") == []
        assert _binder_kinds(fake) == []


def test_H46_bind_fails_closed_when_the_claim_cannot_be_read(fake, monkeypatch):
    monkeypatch.setenv("LEDGER_SESSION", "sess-this")
    session = open_ledger(fake, Source())
    present(session)
    _capture(fake, _prompt("sess-this"))
    fake.script("export-item", stdout="not json\n", status=1)
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    assert result == {"bound": False, "reason": "pass claim not readable"}
    assert fake.calls("events") == [] and fake.calls("elect") == []
    assert _binder_kinds(fake) == []


def test_H47_a_landed_election_with_no_envelope_leaves_begin_build_unblocked(fake, monkeypatch):
    monkeypatch.setenv("LEDGER_SESSION", "sess-this")
    session = open_ledger(fake, Source())
    present(session)
    _capture(fake, _prompt("sess-this"))
    fake.script("elect", commit=True, stdout="", status=0)
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    elected = [event for event in fake.events() if event["kind"] == "decision.elected"]
    assert len(elected) == 1
    assert elected[0]["idempotency_key"].endswith("/elect")
    assert result == {"bound": False, "reason": "elect refused: no envelope"}
    assert fake.calls("append") == []
    assert _binder_kinds(fake) == ["decision.elected"]
    session.begin_build()
    assert session.phase == "build"


@needs_real_cli
def test_H41_a_bound_go_chains_decision_to_election_to_utterance(tmp_path, monkeypatch):
    cli = LEDGER_REAL_CLI
    db = str(tmp_path / "ledger.db")
    session_id = "sess-real-bind"
    monkeypatch.setenv("LEDGER_DB", db)
    monkeypatch.setenv("LEDGER_HARNESS", "ticket-tests")
    monkeypatch.setenv("LEDGER_SESSION", session_id)
    other = '[{"kind": "item.created", "payload": {"type": "ticket", "title": "Another ticket"}}]'
    subprocess.run(
        [cli, "--json", "append-many", "EK-1"], input=other, text=True, check=True, capture_output=True,
    )
    session = open_ledger(cli, Source())
    present(session)
    hook = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": GO_WORDS,
        "session_id": session_id,
        "prompt_id": "prompt-bind-1",
    }
    captured = subprocess.run(
        [cli, "--json", "capture", "--kind", "prompt"],
        input=json.dumps(hook), text=True, check=True, capture_output=True,
    )
    utterance_id = json.loads(captured.stdout)["data"]["event"]["id"]
    result = session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)
    assert result["bound"] is True, result
    assert result["utterance_id"] == utterance_id
    # The real ledger's revision and export agree that the bind window holds
    # only the binder's two events, so the pass token advanced past them.
    session.begin_build()
    assert session.phase == "build"
    listed = subprocess.run(
        [cli, "--json", "decisions", "--item", KEY], text=True, check=True, capture_output=True,
    )
    rows = json.loads(listed.stdout)["data"]["decisions"]
    assert len(rows) == 1
    assert rows[0]["standing"] == "settled"
    assert rows[0]["source_event_id"] == result["election_id"]
    card = subprocess.run([cli, "card", "--full", KEY], text=True, check=True, capture_output=True)
    body = card.stdout
    decisions = body.split("## Decisions", 1)[1].split("\n## ", 1)[0]
    records = body.split("## Decision records", 1)[1].split("\n## ", 1)[0]
    assert f"quote_ref={utterance_id}" in decisions
    assert "-> go" in decisions
    assert GO_WORDS in records
    assert f"source event {result['election_id']}" in records

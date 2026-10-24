"""Matrix section H: the ledger store, against a fake ledger CLI, and against the real one where a row needs it.

H19 and H24 need the ledger's own code: they run when ``LEDGER_CLI`` names
the ledger CLI (H24 loads ``ledger/schema.py`` from beside it) and are
skipped otherwise. Every other row runs against the fake.
"""

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import ticket
from ledger_support import WRITER, FakeLedger, ReadHook, build_ledger, open_ledger
from ticket_support import A, B, KEY, TITLE, Source, changes, go_plan, passing, present, refusal

SKILL_DIR = Path(__file__).resolve().parent.parent
LEDGER_CLI = os.environ.get("LEDGER_CLI", "")
needs_ledger_cli = pytest.mark.skipif(not LEDGER_CLI, reason="LEDGER_CLI does not name the ledger CLI")


@pytest.fixture
def fake(tmp_path):
    return FakeLedger(tmp_path / "ledger")


def kinds(events):
    return [event["kind"] for event in events]


def claims(fake):
    return [event for event in fake.events() if event["kind"] == "session.opened"]


def note(text):
    return {"text": text}


def after(fake, before):
    """The events appended since ``before`` was taken."""

    return fake.events()[len(before):]


def released(session):
    return [("session.closed", f"ticket-pass/{KEY}/{session.pass_id}/released")]


def keyed(events):
    return [(event["kind"], event["idempotency_key"]) for event in events]


# --- Claims, races and keys ---------------------------------------------------


def test_H1_two_sessions_racing_to_claim_get_one_pass(fake):
    src = Source()
    open_ledger(fake, src).end()
    opened = len(claims(fake))
    winner = []
    loser = ReadHook(fake.path, KEY, TITLE, lambda: winner.append(open_ledger(fake, src)))
    with pytest.raises(ticket.TicketHeld) as excinfo:
        open_ledger(fake, src, handle=loser)
    assert fake.appends()[-1].code == "stale_revision"
    assert excinfo.value.holder["id"] == winner[0].pass_id
    assert len(claims(fake)) == opened + 1
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
    assert "item.state" not in kinds(fake.events())
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
    assert state == {
        "kind": "item.state", "item_id": KEY, "idempotency_key": f"ticket-close/{KEY}/{pass_id}/{token}/state",
        "payload": {"status": "closed", "owner": "someone", "claim": "claim-7", "blocked_by": ["EK-1"], "source": WRITER},
    }
    assert "--expect-revision" not in release.argv
    assert [(e["kind"], e["idempotency_key"]) for e in release.batch] == released(session)


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
    assert keyed(after(fake, before)) == released(session)
    assert [e["payload"] for e in fake.events() if e["kind"] == "item.state"] == [outside]


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


def test_H13_release_needs_no_revision_and_lands_once(fake):
    session = open_ledger(fake, Source())
    fake.outside("item.note", note("see PR 12"))
    session.end()
    first = fake.appends()[-1]
    assert "--expect-revision" not in first.argv
    assert keyed(fake.events()[-1:]) == released(session)
    count = len(fake.events())
    session.store.release()
    second = fake.appends()[-1]
    assert second.stdin == first.stdin and second.code is None and second.status == 0
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
            "SELECT count(*) FROM events WHERE kind = 'session.opened' AND idempotency_key = ?",
            (f"ticket-pass/{KEY}/{session.pass_id}/claimed",),
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


@pytest.mark.parametrize("title", ["same title", "other title"])
def test_H23_the_first_claim_creates_the_item(fake, title):
    src = Source()
    first = []
    second = ReadHook(fake.path, KEY, TITLE if title == "same title" else "Another title",
                      lambda: first.append(open_ledger(fake, src)))
    with pytest.raises(ticket.TicketHeld) as excinfo:
        open_ledger(fake, src, handle=second)
    claim = fake.appends()[0]
    assert claim.argv[-2:] == ["--expect-revision", "none"]
    purpose = f"ticket pass {first[0].pass_id} on {ticket._host()}"
    created, opened = claim.batch
    assert created == {"kind": "item.created", "item_id": KEY, "idempotency_key": f"ticket-item/{KEY}/created",
                       "payload": {"type": "ticket", "title": TITLE, "source": WRITER}}
    assert opened["kind"] == "session.opened" and opened["idempotency_key"] == f"ticket-pass/{KEY}/{first[0].pass_id}/claimed"
    assert opened["payload"]["purpose"].startswith(purpose + " at ") and opened["payload"]["source"] == WRITER
    assert fake.appends()[1].code == ("stale_revision" if title == "same title" else "conflict")
    assert excinfo.value.holder["id"] == first[0].pass_id
    assert kinds(fake.events()).count("item.created") == 1
    rows = subprocess.run([str(fake.path), "--json", "query", "--item", KEY], capture_output=True, text=True, check=True)
    assert '"type": "ticket"' in rows.stdout


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


@needs_ledger_cli
def test_H24_every_batch_the_store_builds_passes_the_toolkit_schema(tmp_path):
    validate_payload = _toolkit_validate_payload()
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
            # Every batch the fake received, whether or not the stage finished.
            for element in (element for call in fake.appends() for element in call.batch):
                assert set(element) == {"kind", "payload", "item_id", "idempotency_key"}
                validate_payload(element["kind"], element["payload"])
                assert element["payload"]["source"] == WRITER
                assert element["kind"] not in ("decision.recorded", "review.recorded", "outside.recorded")
                assert "source_event_id" not in element["payload"]
                if element["kind"] == "item.stopped":
                    assert "close" not in element["payload"]
                kinds_sent.add(element["kind"])
    assert kinds_sent == {
        "item.created", "session.opened", "session.closed", "item.note", "item.stopped", "item.tried", "item.state"}


def _toolkit_validate_payload():
    """The ledger's own ``validate_payload``, loaded from ``ledger/schema.py`` beside the CLI ``LEDGER_CLI`` names."""

    cli = Path(LEDGER_CLI).resolve()
    for root in (cli.parent, cli.parent.parent / "skills" / "_lib"):
        schema = root / "ledger" / "schema.py"
        if schema.is_file():
            spec = importlib.util.spec_from_file_location("ledger_schema_under_test", schema)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.validate_payload
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


@pytest.mark.parametrize("body", ["not json", "json list", "fails the validator"])
def test_H26_a_ticket_note_that_does_not_parse_is_unreadable(fake, body):
    src = Source()
    build_ledger(fake, src, "S2", 0).end()
    text = "ticket-review:v2 " + {"not json": "{", "json list": "[1]",
                                   "fails the validator": '{"round": "2", "verdict": "pass"}'}[body]
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
    assert [kinds(call.batch) for call in later] == [["session.closed"]]
    assert fake.store().read()[0]["pass"] is None


@pytest.mark.parametrize("code", ["not_found", "missing_db", "integrity"])
def test_H29_exit_one_and_four_refusals_are_permanent(fake, code):
    excinfo, _, later = _refused_review(fake, code)
    assert refusal(excinfo) == "ledger_refused"
    assert code in str(excinfo.value)
    assert [call.verb for call in later] == ["append-many"]


def test_H30_a_usage_answer_is_incompatible(fake):
    excinfo, _, later = _refused_review(fake, "usage")
    assert refusal(excinfo) == "ledger_incompatible"
    assert [call.verb for call in later] == ["append-many"]


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

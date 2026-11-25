"""Close-at-merge contract B1–B11. gh is an offline executable; ledger writes use the real store boundary."""

import json
import os
from pathlib import Path

import pytest

import ticket
from ledger_support import FakeLedger, build_ledger, ledger_library, open_ledger
from ticket_support import KEY, Source, build, changes, go_outward, passing
from test_ticket_lease import Fake, Real, BACKENDS

PR = "example/repo#14"


@pytest.fixture
def fake(tmp_path):
    return FakeLedger(tmp_path / "ledger")


def approved(fake, rounds=1):
    session = build_ledger(fake, Source(), "S3", rounds)
    go_outward(session)
    return session


def gh(monkeypatch, tmp_path, output='{"state":"OPEN"}', status=0, delay=0):
    directory = tmp_path / "gh-bin"
    directory.mkdir(exist_ok=True)
    command = directory / "gh"
    arguments = directory / "args.json"
    # Absolute interpreter: even the missing-gh test may replace PATH with an empty directory.
    import sys
    command.write_text(
        f"#!{sys.executable}\nimport json,sys,time\n"
        f"open({str(arguments)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        f"time.sleep({delay!r})\nprint({output!r})\nsys.exit({status})\n", encoding="utf-8",
    )
    command.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    return arguments


def unchanged(fake, session, operation, error, match):
    before, appends = fake.events(), len(fake.appends())
    with pytest.raises(error, match=match):
        operation()
    assert fake.events() == before, "a refused gate must write no events"
    assert len(fake.appends()) == appends, "a refused gate must send no append-many"
    assert session.live, "a gate refusal must leave the pass live"
    assert fake.store().read()[0]["status"] == "open"


def test_B1_link_requires_go_and_is_atomic_idempotent_and_conflict_checked(fake):
    session = build_ledger(fake, Source(), "S3")
    unchanged(fake, session, lambda: session.record_pull_request(PR), ticket.GateRequired, "outward go")
    go_outward(session)
    start = len(fake.appends())
    session.record_pull_request(PR)
    writes = fake.appends()[start:]
    assert len(writes) == 1
    assert [event["kind"] for event in writes[0].batch] == ["item.linked", "item.stopped"]
    assert writes[0].batch[0]["payload"] == {"rel": "pr", "target": PR, "source": "ticket"}
    assert writes[0].batch[1]["payload"]["phase"] == "review"
    assert fake.store().read()[0]["pr"] == PR
    before = fake.events(), len(fake.appends())
    session.record_pull_request(PR)
    assert (fake.events(), len(fake.appends())) == before, "same PR replay must write nothing"
    unchanged(fake, session, lambda: session.record_pull_request("other/repo#15"), ticket.TicketError, "pr_conflict")


@pytest.mark.parametrize("ref", ["#14", "example/repo#0", "example/repo#01", "example/repo#14 ",
                                    " example/repo#14", "- example/repo#14", "`example/repo#14`",
                                    "example/repo #14", "example/repo#14\n", "example/repo", "example/repo#x"])
def test_B1_reference_grammar_names_near_misses(fake, ref):
    session = approved(fake)
    unchanged(fake, session, lambda: session.record_pull_request(ref), ticket.TicketError, "pr_invalid")


def test_B2_close_reads_live_state_and_records_outcome(fake, monkeypatch, tmp_path):
    session = approved(fake)
    session.record_pull_request(PR)
    # A caller-written cache must not authorize closing an open PR.
    session._card["pr_state"] = "MERGED"
    args = gh(monkeypatch, tmp_path)
    unchanged(fake, session, lambda: session.close("Done."), ticket.TicketError, "pr_open")
    assert json.loads(args.read_text()) == ["pr", "view", "14", "--repo", "example/repo", "--json", "state"]
    gh(monkeypatch, tmp_path, '{"state":"MERGED"}')
    session.close("Done.")
    card = fake.store().read()[0]
    assert card["status"] == "closed"
    assert card["stamps"][-1]["point"] == f"Done. Pull request {PR}: merged."
    assert PR in card["stamps"][-1]["refs"]
    assert not session.live
    assert open_ledger(fake, Source()).phase == "closed", "rerun after close must not repeat the close"


def test_B2_closed_unmerged_is_distinct(fake, monkeypatch, tmp_path):
    session = approved(fake)
    session.record_pull_request(PR)
    gh(monkeypatch, tmp_path, '{"state":"CLOSED"}')
    session.close("Abandoned.")
    assert fake.store().read()[0]["stamps"][-1]["point"] == f"Abandoned. Pull request {PR}: closed unmerged."


@pytest.mark.parametrize("mode", ["missing", "nonzero", "bad-json", "array", "missing-state", "lowercase"])
def test_B3_unreadable_state_fails_closed(fake, monkeypatch, tmp_path, mode):
    session = approved(fake)
    session.record_pull_request(PR)
    if mode == "missing":
        empty = tmp_path / "empty-path"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
    else:
        output = {"nonzero": '{"state":"MERGED"}', "bad-json": "not json", "array": "[]",
                  "missing-state": "{}", "lowercase": '{"state":"merged"}'}[mode]
        gh(monkeypatch, tmp_path, output, status=1 if mode == "nonzero" else 0)
    unchanged(fake, session, lambda: session.close("Done."), ticket.TicketError, "pr_open")


@pytest.mark.parametrize("output", ["not json", '{"state":"merged"}', '{"state":"UNKNOWN"}', "{}"])
def test_B3_reader_normalizes_unreadable_and_noncanonical_state(monkeypatch, tmp_path, output):
    gh(monkeypatch, tmp_path, output)
    assert ticket.pull_request_state(PR) == "OPEN"


def test_B3_slow_reader_refuses_close_without_writing(fake, monkeypatch, tmp_path):
    session = approved(fake)
    session.record_pull_request(PR)
    monkeypatch.setattr(ticket, "REVIEW_RECORD_TIMEOUT_SECONDS", 0.1)
    gh(monkeypatch, tmp_path, '{"state":"MERGED"}', delay=0.5)
    unchanged(fake, session, lambda: session.close("Done."), ticket.TicketError, "pr_open")


def test_B2_close_independently_refuses_noncanonical_state(fake, monkeypatch):
    session = approved(fake)
    session.record_pull_request(PR)
    monkeypatch.setattr(ticket, "pull_request_state", lambda ref: "UNKNOWN")
    unchanged(fake, session, lambda: session.close("Done."), ticket.TicketError, "pr_open")


def test_B10_linked_pr_cannot_close_local_after_new_plan(fake):
    from ticket_support import go_plan, present
    source = Source()
    session = build_ledger(fake, source, "S4", 1)
    session.end()
    source.edit("text")
    session = open_ledger(fake, source)
    present(session)
    go_plan(session)
    session.begin_build()
    passing(session)
    session.record_outward_declined(evidence="conversation:no", verbatim="Keep this plan local.")
    unchanged(fake, session, session.close_local, ticket.TicketError, "pr_recorded")


def test_B6_only_outward_go_may_name_review_round(fake):
    session = approved(fake)
    decision = dict(fake.store().read()[0]["decisions"][-1], gate="plan")
    unchanged(fake, session, lambda: session._write([("decision", decision)]),
              ticket.TicketError, "only an outward go names a review round")


def test_B4_close_requires_recorded_pr(fake):
    session = approved(fake)
    assert fake.store().read()[0].get("pr") is None
    unchanged(fake, session, lambda: session.close("Done."), ticket.TicketError, "pr_required")


def test_B5_fix_round_spends_old_go_and_new_go_names_final_round(fake):
    session = approved(fake)
    session.record_pull_request(PR)
    changes(session)
    assert session.phase == "build" and session.review_round == 2
    assert "passes: true" not in session.plan.render(), "a changes review must clear the old passing evidence"
    passing(session)
    assert session.review_round == 3
    unchanged(fake, session, session.require_outward_go, ticket.GateRequired, "outward go")
    unchanged(fake, session, lambda: session.close("Done."), ticket.GateRequired, "outward go")
    go_outward(session)
    assert fake.store().read()[0]["decisions"][-1]["round"] == 3
    session.require_outward_go()
    session.close("Fixed.")
    assert fake.store().read()[0]["status"] == "closed"


@pytest.mark.parametrize("linked", [False, True])
def test_B6_legacy_roundless_go_only_before_pr(repo, linked):
    from ticket_support import edit_card, open_pass
    session = build(repo, Source(), "S3")
    go_outward(session)
    if linked:
        session.record_pull_request(PR)
    session.end()
    edit_card(repo, lambda card: card["decisions"][-1].pop("round"))
    resumed = open_pass(repo, Source())
    assert resumed.approved("outward") is (not linked)
    if linked:
        with pytest.raises(ticket.GateRequired, match="outward go"):
            resumed.require_outward_go()


@pytest.mark.parametrize("fault", ["no-pr", "late-pass", "already-third"])
def test_B7_late_review_refusals_and_cap(fake, fault):
    session = approved(fake, rounds=3 if fault == "already-third" else 1)
    if fault != "no-pr":
        session.record_pull_request(PR)
    operation = (lambda: passing(session)) if fault == "late-pass" else (lambda: changes(session))
    unchanged(fake, session, operation, ticket.TicketError, "late_review_refused")


def test_B7_late_third_changes_stops(fake):
    session = approved(fake, rounds=2)
    session.record_pull_request(PR)
    changes(session)
    assert (session.phase, session.review_round, session.live) == ("stopped", 3, False)
    assert fake.store().read()[0]["status"] == "open"


@pytest.fixture(params=BACKENDS)
def managed_backend(request, tmp_path, monkeypatch):
    backend = Fake(tmp_path) if request.param == "fake" else Real(tmp_path, monkeypatch)
    backend.new_item()
    if request.param == "fake":
        from ledger_support import plant_take
        plant_take(backend.fake, "loop-holder")
    else:
        loop, open_database = ledger_library(backend.path)
        with open_database(backend.db) as database:
            loop.take_item(database, KEY, "loop-holder", idempotency_key="managed", claim_key="loop-claim")
    return backend


@pytest.mark.parametrize("mode", ["close", "close-without-pr", "close-local"])
def test_B8_managed_close_is_a_live_write_free_stop(managed_backend, mode):
    local = mode == "close-local"
    backend = managed_backend
    session = open_ledger(backend.path, Source(), holder="loop-holder")
    from ticket_support import present, go_plan
    present(session)
    go_plan(session)
    session.begin_build()
    passing(session)
    if local:
        session.record_outward_declined(evidence="conversation:no", verbatim="Keep it local.")
    else:
        go_outward(session)
        if mode == "close":
            # The loop owns linking: seed its existing PR, not a pass-written generic link.
            if isinstance(backend, Fake):
                backend.write("item.linked", {"rel": "pr", "target": PR, "source": "loop"})
            if isinstance(backend, Real):
                loop, open_database = ledger_library(backend.path)
                with open_database(backend.db) as database:
                    loop.link_pull_request(database, KEY, "loop-holder", "example/repo", 14)
            session = open_ledger(backend.path, Source(), pass_id=session.pass_id, holder="loop-holder")
    before = backend.events()
    appends = len(backend.fake.appends()) if isinstance(backend, Fake) else None
    operation = session.close_local if local else lambda: session.close("Done.")
    with pytest.raises(ticket.TicketError, match=f"ticket {KEY}: this step belongs to the loop") as caught:
        operation()
    assert "\n" not in str(caught.value)
    assert backend.events() == before, "managed stop must write nothing"
    assert session.live, "managed stop must leave the pass live"
    if isinstance(backend, Fake):
        assert len(backend.fake.appends()) == appends
    assert session.store.read()[0]["status"] == "open"


def test_B9_managed_pr_link_is_a_live_write_free_stop(fake):
    session = approved(fake)
    fake.outside("loop.managed", {})
    session = open_ledger(fake, Source(), pass_id=session.pass_id)
    unchanged(fake, session, lambda: session.record_pull_request(PR), ticket.TicketError,
              f"ticket {KEY}: this step belongs to the loop")


def test_B1_a_concurrent_link_writer_cannot_land_half_a_step(fake):
    session = approved(fake)
    fake.script("append-many", commit=True, before=[{
        "kind": "item.note", "payload": {"text": "Concurrent writer."}, "item_id": KEY, "key": "concurrent",
    }])
    with pytest.raises(ticket.StoreUnavailable) as caught:
        session.record_pull_request(PR)
    assert caught.value.code == "stale"
    assert not any(event["kind"] == "item.linked" for event in fake.events())
    assert not any(event["kind"] == "item.stopped" and "recorded;" in event["payload"]["point"] for event in fake.events())
    resumed = open_ledger(fake, Source())
    resumed.record_pull_request(PR)
    before = fake.events()
    resumed.record_pull_request(PR)
    assert fake.events() == before


@pytest.mark.parametrize("operation", ["close", "record_pull_request"])
def test_fresh_read_refusal_names_latest_writer_without_attempting_write(fake, operation):
    session = approved(fake)
    held = session._token
    fake.outside("item.note", {"text": "Concurrent note."}, actor="new-agent", session="new-session")
    current = str(fake.events()[-1]["id"])
    appends = len(fake.appends())
    with pytest.raises(ticket.StoreUnavailable) as caught:
        if operation == "close":
            session.close("Done.")
        else:
            session.record_pull_request(PR)
    assert caught.value.code == "stale"
    assert f"a fresh read found revision {current}, expected {held} (" in str(caught.value)
    assert "actor=new-agent" in str(caught.value)
    assert "session=new-session" in str(caught.value)
    # Only failure cleanup may append; the refused step itself must not.
    assert all(event["kind"] in ("session.closed", "item.state")
               for call in fake.appends()[appends:] for event in call.batch)
    assert not any(event["kind"] == "item.linked" for event in fake.events())


def test_B11_skill_documents_close_gate_and_fix_round():
    text = (Path(ticket.__file__).parent / "SKILL.md").read_text()
    outward = text.split("## 6. Outward go: a hard stop\n", 1)[1].split("\n## ", 1)[0]
    live = text.split("## One live pass\n", 1)[1].split("\n## ", 1)[0]
    final = text.split("## The final message\n", 1)[1].split("\n## ", 1)[0]
    for phrase in ("record_pull_request", "owner/repo#N", "gh pr view", "closed unmerged", "fix round", "new outward go"):
        assert phrase in outward, f"section 6 must document {phrase}"
    assert "belongs to the loop" in live
    assert "still live" in final and "pull request" in final and "merged" in final

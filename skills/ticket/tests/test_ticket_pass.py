"""Matrix sections E and F: live-pass steps, gates, lifecycle, and close."""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import ticket
from ticket_support import (
    A, B, F5, KEY, TITLE, Source, build, build_released, card_bytes, changes, edit_card, go_outward,
    go_plan, open_pass, passing, present, read_card, refusal, store,
)

SKILL_DIR = Path(__file__).resolve().parent.parent


def _outsider_writes_tried(repo, src, session):
    """A second handle, joined to the same pass, lands a `tried` the pass has not read."""

    other = open_pass(repo, src, pass_id=session.pass_id)
    other.record_tried("An outside note on the ticket.")
    return read_card(repo)


# --- E1-E5: gates and what the steps store ----------------------------------


def test_E1_plan_and_outward_go_are_hard_stops(repo):
    session = build(repo, Source(), "S1")
    before = card_bytes(repo)
    with pytest.raises(ticket.GateRequired, match="plan go"):
        session.begin_build()
    assert card_bytes(repo) == before
    go_plan(session)
    session.begin_build()
    passing(session)
    before = card_bytes(repo)
    with pytest.raises(ticket.GateRequired, match="outward go"):
        session.require_outward_go()
    with pytest.raises(ticket.GateRequired, match="outward go"):
        session.close("Done.", refs=("example/repo#14",))
    assert card_bytes(repo) == before
    assert session.phase == "outward_go"


@pytest.mark.parametrize("stop", ["plan", "pass", "cap"])
def test_E2_each_stop_is_stamped_in_the_same_write_as_its_record(repo, monkeypatch, stop):
    src = Source()
    if stop == "plan":
        session = open_pass(repo, src)
        session.finish_orient()
        session.finish_research()
    else:
        session = build(repo, src, "S2", 0 if stop == "pass" else 2)
    before = read_card(repo)
    real_fold = ticket._fold

    def fails_after_the_first_event(card, events):
        folded = real_fold(card, events[:1])
        if len(events) > 1:
            raise RuntimeError("the store failed after folding the first event")
        return folded

    monkeypatch.setattr(ticket, "_fold", fails_after_the_first_event)
    with pytest.raises(RuntimeError):
        if stop == "plan":
            session.present_plan(ticket.PlanCard(session.ticket))
        elif stop == "pass":
            passing(session)
        else:
            changes(session)
    monkeypatch.setattr(ticket, "_fold", real_fold)
    after = read_card(repo)
    assert {**after, "pass": before["pass"]} == before
    assert session.live is False


def test_E3_a_plan_go_is_recorded_only_by_the_presenting_pass(repo):
    src = Source()
    build_released(repo, src, "S1")
    session = open_pass(repo, src)
    assert session.phase == "plan"
    before = card_bytes(repo)
    with pytest.raises(ticket.TicketError, match="expected phase plan_go"):
        go_plan(session)
    assert card_bytes(repo) == before


def test_E4_a_plan_go_stores_the_identity_it_approved(repo):
    session = build(repo, Source(), "S1")
    go_plan(session)
    card = read_card(repo)
    go = card["decisions"][-1]
    assert (go["gate"], go["plan_revision"], go["criteria"], go["pass"]) == ("plan", 1, card["plan"]["criteria"], session.pass_id)
    session.begin_build()
    assert session.phase == "build"


def test_E5_a_passing_review_must_list_every_criterion(repo):
    session = build(repo, Source(), "S2", 0)
    before = card_bytes(repo)
    with pytest.raises(ticket.TicketError, match="every acceptance criterion"):
        session.record_review("pass", evidence="reviews/round-1.md", passed=(0,))
    assert card_bytes(repo) == before
    session.record_review("pass", evidence="reviews/round-1.md", passed=(1, 0))
    card = read_card(repo)
    assert (card["reviews"][-1]["round"], card["reviews"][-1]["passed"]) == (1, [0, 1])
    assert card["stamps"][-1]["point"] == "Waiting at outward go after review round 1."


# --- E6-E12: tokens, refusals and lifecycle ---------------------------------

E6_STEPS = ["present_plan", "record_go plan", "record_go outward", "record_review", "record_tried", "close", "more_info"]


def _e6_live(repo, src, step):
    if step == "present_plan":
        session = open_pass(repo, src)
        session.finish_orient()
        session.finish_research()
        return session
    if step == "record_go plan":
        return build(repo, src, "S1")
    if step == "record_go outward":
        return build(repo, src, "S3", 1)
    if step == "close":
        return build(repo, src, "S4", 1)
    return build(repo, src, "S2", 0)


def _e6_run(session, step):
    if step == "present_plan":
        session.present_plan(ticket.PlanCard(session.ticket))
    elif step == "record_go plan":
        go_plan(session)
    elif step == "record_go outward":
        go_outward(session)
    elif step == "record_review":
        changes(session)
    elif step == "record_tried":
        session.record_tried("The pass's own note.")
    elif step == "close":
        session.close("Done.", refs=("example/repo#14",))
    else:
        session.more_info("person", "Which branch?")


@pytest.mark.parametrize("step", E6_STEPS)
def test_E6_a_stale_token_refuses_every_write(repo, step):
    src = Source()
    session = _e6_live(repo, src, step)
    outside = _outsider_writes_tried(repo, src, session)
    with pytest.raises(ticket.StoreUnavailable, match="the card on disk changed") as excinfo:
        _e6_run(session, step)
    assert refusal(excinfo) == "stale"
    assert session.live is False
    after = read_card(repo)
    assert after == {**outside, "pass": None}
    assert after["tried"][-1]["what"] == "An outside note on the ticket."


@pytest.mark.parametrize("fault", ["wrong phase", "bad evidence", "bad index", "bad ref"])
def test_E7_a_ticket_error_writes_nothing_and_keeps_the_pass_live(repo, fault):
    session = build(repo, Source(), "S2", 0)
    before = card_bytes(repo)
    with pytest.raises(ticket.TicketError):
        if fault == "wrong phase":
            session.begin_build()
        elif fault == "bad evidence":
            session.record_review("changes", evidence="not a reference at all")
        elif fault == "bad index":
            session.record_review("changes", evidence="reviews/round-1.md", passed=(5,))
        else:
            session.record_tried("Ran the suite.", evidence="logs/run.txt\nsecond line")
    assert card_bytes(repo) == before
    assert session.live is True
    changes(session)
    assert read_card(repo)["reviews"][-1]["round"] == 1


def test_E8_the_round_cap_counts_recorded_rounds_across_passes(repo):
    src = Source()
    build_released(repo, src, "S2", 2)
    session = open_pass(repo, src)
    assert session.review_round == 2
    writes = []
    real_apply = session.store.apply

    def counting(token, events):
        writes.append([kind for kind, _ in events])
        return real_apply(token, events)

    session.store.apply = counting
    changes(session)
    assert (session.phase, session.live) == ("stopped", False)
    assert writes == [["review", "stamp"]]
    card = read_card(repo)
    assert (len(card["reviews"]), card["stamps"][-1]["point"]) == (3, ticket.CAP_POINT)
    assert open_pass(repo, src).reconciliation == (F5(1),)


def test_E9_a_store_read_that_fails_inside_the_outward_check_ends_the_pass(repo):
    session = build(repo, Source(), "S4", 1)

    def broken_read():
        raise OSError("the disk went away")

    session.store.read = broken_read
    with pytest.raises(OSError):
        session.require_outward_go()
    assert session.live is False
    assert read_card(repo)["pass"] is None


def test_E10_an_exception_leaving_the_with_block_ends_the_pass(repo):
    with pytest.raises(RuntimeError):
        with ticket.TicketPass.open(KEY, Source(), store(repo)) as session:
            assert read_card(repo)["pass"]["id"] == session.pass_id
            raise RuntimeError("the caller failed")
    assert session.live is False
    assert read_card(repo)["pass"] is None


_CHILD = """
import json, sys
sys.path.insert(0, sys.argv[1])
import ticket
from ticket_support import Source, open_pass
try:
    open_pass(sys.argv[2], Source())
except ticket.StoreUnavailable as exc:
    print(json.dumps({"code": exc.code, "message": str(exc)}))
    sys.exit(3)
print(json.dumps({"code": None}))
"""


def _child_open(repo):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(SKILL_DIR), str(SKILL_DIR / "tests"))))
    return subprocess.run(
        [sys.executable, "-c", _CHILD, str(SKILL_DIR), str(repo)], capture_output=True, text=True, env=env, check=False
    )


def test_E11a_a_pass_holds_the_ticket_across_processes(repo):
    first = open_pass(repo, Source())
    before = card_bytes(repo)
    result = _child_open(repo)
    assert result.returncode == 3, result.stderr
    answer = json.loads(result.stdout)
    live = read_card(repo)["pass"]
    assert answer["code"] == "held"
    release = f"python3 {SKILL_DIR / 'ticket.py'} release {KEY} {first.pass_id}"
    for part in (live["id"], live["host"], live["started_at"], f"`{release}`"):
        assert part in answer["message"]
    assert card_bytes(repo) == before


def test_E11f_the_held_refusal_command_runs_from_the_target_repository(repo, tmp_path):
    first = open_pass(repo, Source())
    result = _child_open(repo)
    message = json.loads(result.stdout)["message"]
    command = shlex.split(re.search(r"`(python3 [^`]*release [^`]*)`", message).group(1))
    assert command[1] == str(SKILL_DIR / "ticket.py")
    elsewhere = tmp_path / "target"
    elsewhere.mkdir()
    assert not (elsewhere / "ticket.py").exists()
    released = subprocess.run(
        [sys.executable, *command[1:], "--repo", str(repo)], cwd=elsewhere, capture_output=True, text=True, check=False
    )
    assert released.returncode == 0, released.stderr
    assert read_card(repo)["pass"] is None
    assert first.pass_id in message


def test_E11b_join_refuses_an_id_that_is_not_the_live_pass(repo):
    first = open_pass(repo, Source())
    before = card_bytes(repo)
    with pytest.raises(ticket.TicketHeld) as excinfo:
        open_pass(repo, Source(), pass_id="0000000000000000")
    assert first.pass_id in str(excinfo.value)
    assert card_bytes(repo) == before


@pytest.mark.parametrize("path", ["close", "more_info", "cap", "end", "refusal", "with-block"])
def test_E11c_the_claim_is_released_on_every_exit_path(repo, path):
    src = Source()
    if path == "close":
        session = build(repo, src, "S6")
    elif path == "more_info":
        session = build(repo, src, "S2", 0)
        session.more_info("person", "Which branch?")
    elif path == "cap":
        session = build(repo, src, "S5")
    elif path == "end":
        session = build(repo, src, "S2", 0)
        session.end()
    elif path == "refusal":
        session = build(repo, src, "S2", 0)
        _outsider_writes_tried(repo, src, session)
        with pytest.raises(ticket.StoreUnavailable):
            session.record_tried("Late.")
    else:
        with pytest.raises(RuntimeError):
            with build(repo, src, "S2", 0) as session:
                raise RuntimeError("the caller failed")
    assert session.live is False
    assert read_card(repo)["pass"] is None
    if path not in ("close", "cap"):
        assert open_pass(repo, src).live is True


def _release_command(repo, pass_id):
    return subprocess.run(
        [sys.executable, str(SKILL_DIR / "ticket.py"), "release", KEY, pass_id, "--repo", str(repo)],
        capture_output=True, text=True, check=False,
    )


def test_E11d_a_person_clears_a_crashed_pass_with_the_documented_command(repo):
    crashed = open_pass(repo, Source())
    before = card_bytes(repo)
    wrong = _release_command(repo, "0000000000000000")
    assert wrong.returncode == 1
    assert card_bytes(repo) == before
    right = _release_command(repo, crashed.pass_id)
    assert right.returncode == 0, right.stderr
    assert read_card(repo)["pass"] is None
    assert open_pass(repo, Source()).live is True


def test_E11e_an_ended_pass_writes_nothing(repo):
    session = build(repo, Source(), "S4", 1)
    session.end()
    before = card_bytes(repo)
    steps = [
        lambda: session.finish_orient(),
        lambda: session.present_plan(ticket.PlanCard(session.ticket)),
        lambda: go_outward(session),
        lambda: session.begin_build(),
        lambda: changes(session),
        lambda: session.approved("outward"),
        lambda: session.require_outward_go(),
        lambda: session.record_tried("Late."),
        lambda: session.more_info("person", "Still there?"),
        lambda: session.close("Done.", refs=("example/repo#14",)),
    ]
    for step in steps:
        with pytest.raises(ticket.StoreUnavailable) as excinfo:
            step()
        assert refusal(excinfo) == "pass_ended"
    assert card_bytes(repo) == before


@pytest.mark.parametrize("destination", ["person", "research", "someone_else"])
def test_E12_three_more_info_exits(repo, destination):
    session = open_pass(repo, Source())
    session.finish_orient()
    session.finish_research()
    if destination == "someone_else":
        with pytest.raises(ticket.TicketError, match="role"):
            session.more_info(destination, "Can design sign off?")
        assert session.live is True
        result = session.more_info(destination, "Can design sign off?", role="design lead")
        assert result.waiting == "waiting on design lead: Can design sign off?"
        assert result.drafted_ask == f"Could you help with {KEY}? Can design sign off?"
    else:
        result = session.more_info(destination, "Which branch?")
        assert result.waiting == f"waiting on {destination}: Which branch?"
        assert result.drafted_ask is None
    stamp = read_card(repo)["stamps"][-1]
    assert (stamp["point"], stamp["phase"], stamp["close"]) == (result.waiting, "plan", False)
    assert session.live is False


# --- E13-E24: the held plan, the live phase and the held token -------------


def test_E13_open_questions_tried_and_steps_persist_and_resume_rebuilds_the_pass(repo):
    src = Source()
    session = open_pass(repo, src)
    session.finish_orient()
    session.finish_research()
    plan = ticket.PlanCard(session.ticket)
    plan.complete_step("orient")
    plan.skip_step("research", "The ticket already names the module.")
    plan.skip_step("plan", "The whole change fits in one sentence.", diff_statement="Add one missing guard.")
    plan.add_open_question("Does the guard need a flag?")
    session.present_plan(plan)
    session.record_tried("Read the module.", outcome="It has no guard.")
    session.end()
    resumed = open_pass(repo, src)
    rendered = resumed.plan.render()
    assert "- orient: complete" in rendered
    assert "- research: skip: The ticket already names the module." in rendered
    assert "- plan: skip: The whole change fits in one sentence." in rendered
    assert "- build: pending" in rendered
    assert "Diff: Add one missing guard." in rendered
    assert "- Does the guard need a flag?" in rendered
    assert read_card(repo)["tried"] == [{"what": "Read the module.", "phase": "plan", "outcome": "It has no guard."}]


def test_E14_resume_pulls_the_live_ticket_before_reading_the_card(repo):
    trace = []

    class TracingStore(ticket.MarkdownStore):
        def read(self):
            trace.append("read")
            return super().read()

    ticket.TicketPass.open(KEY, Source(trace=trace), TracingStore(repo, KEY, TITLE))
    assert trace[:2] == ["pull", "read"]


def test_E15_skips_require_reasons_and_plan_skip_requires_one_sentence_diff():
    plan = ticket.PlanCard(Source().pull(KEY))
    with pytest.raises(ticket.TicketError, match="skip: <reason>"):
        plan.skip_step("research", "")
    with pytest.raises(ticket.TicketError, match="one-sentence diff"):
        plan.skip_step("plan", "The change is small.", diff_statement="Add the guard. Add the store.")
    plan.skip_step("plan", "The change is small.", diff_statement="Add the guard.")
    assert plan.diff_statement == "Add the guard."


E16_EDITS = {
    "role": lambda go: go.update(who_kind="role", relayed_by="the lead"),
    "lean": lambda go: go.update(standing="lean"),
    "source": lambda go: go.update(source={"what": "reply", "ref": "conversation:plan-go"}),
    "tag": lambda go: go.update(tags=[{"type": "ticket", "id": "EK-99"}]),
    "other revision": lambda go: go.update(plan_revision=2),
    "other criteria": lambda go: go.update(criteria=[B, A]),
    "other pass": lambda go: go.update(**{"pass": "0123456789abcdef"}),
    # A hand edit can also leave a field of another type; it opens nothing.
    "source not an object": lambda go: go.update(source="ratification"),
    "tags not a list": lambda go: go.update(tags="EK-14"),
}


@pytest.mark.parametrize("field", list(E16_EDITS))
def test_E16_each_go_check_alone_keeps_every_gate_shut(repo, field):
    src = Source()
    first = build(repo, src, "S2", 0)
    edit_card(repo, lambda card: E16_EDITS[field](card["decisions"][0]))
    joined = open_pass(repo, src, pass_id=first.pass_id)
    assert joined.approved("plan") is False
    # Criteria that no longer match the card's plan are a card-side change
    # (design 4.3), so that pass is back at plan; the others wait at plan go.
    with pytest.raises(ticket.TicketError) as excinfo:
        joined.begin_build()
    if field == "other criteria":
        assert joined.phase == "plan"
    else:
        assert isinstance(excinfo.value, ticket.GateRequired)


def test_E17_orient_and_research_are_in_memory_phases(repo):
    src = Source()
    session = open_pass(repo, src)
    before = card_bytes(repo)
    assert session.approved("plan") is False
    session.finish_orient()
    assert session.phase == "research"
    session.finish_research()
    assert session.phase == "plan"
    assert card_bytes(repo) == before
    session.end()
    assert open_pass(repo, src).phase == "orient"


def test_E17_a_go_without_a_stored_plan_opens_nothing_at_orient(repo):
    # A hand edit can leave a go decision on a card that holds no plan.
    session = build(repo, Source(), "S1")
    go_plan(session)
    session.end()
    edit_card(repo, lambda card: card.update(plan=None))
    resumed = open_pass(repo, Source())
    assert resumed.phase == "orient"
    assert resumed.approved("plan") is False


@pytest.mark.parametrize("stage", ["S5", "S6"])
def test_E18_a_terminal_open_takes_no_claim_and_is_not_live(repo, stage):
    src = Source()
    build(repo, src, stage)
    before = card_bytes(repo)
    session = open_pass(repo, src)
    assert session.live is False
    assert read_card(repo)["pass"] is None
    session.end()
    assert card_bytes(repo) == before


def test_E19_the_pass_holds_the_stored_plan_after_a_changed_resume(repo):
    src = Source()
    build_released(repo, src, "S3", 1)
    src.edit("reorder")
    session = open_pass(repo, src)
    assert session.plan.acceptance_criteria == (A, B)
    with pytest.raises(ticket.TicketError, match="in order"):
        session.present_plan(session.plan)
    session.present_plan(ticket.PlanCard(session.ticket))
    assert read_card(repo)["plan"]["criteria"] == [B, A]


def test_E20_render_derives_passes_from_the_stored_rounds(repo):
    src = Source()
    build_released(repo, src, "S3", 1)
    rendered = open_pass(repo, src).plan.render()
    assert rendered.count("passes: true") == 2
    assert rendered.count("(review round 1: reviews/round-1.md)") == 2
    assert "passes" not in json.dumps(read_card(repo)["plan"])
    open_pass(repo, src, pass_id=read_card(repo)["pass"]["id"]).end()
    src.edit("reorder")
    rendered = open_pass(repo, src).plan.render()
    assert "passes: true" not in rendered
    assert rendered.count("passes: false") == 2


@pytest.mark.parametrize("step", ["finish_research", "begin_build"])
def test_E21_the_live_phase_is_held_between_writes(repo, step):
    src = Source()
    session = open_pass(repo, src)
    if step == "finish_research":
        session.finish_orient()
        before = card_bytes(repo)
        session.finish_research()
        assert card_bytes(repo) == before
        assert session.phase == "plan"
        assert read_card(repo)["plan"] is None
        session.present_plan(ticket.PlanCard(session.ticket))
        assert read_card(repo)["plan"]["revision"] == 1
    else:
        present(session)
        go_plan(session)
        before = card_bytes(repo)
        session.begin_build()
        assert card_bytes(repo) == before
        assert session.phase == "build"
        changes(session)
        assert read_card(repo)["reviews"][-1]["round"] == 1


def test_E22_a_recorded_plan_go_resumes_at_build_and_begin_build_is_live_only(repo):
    src = Source()
    session = build(repo, src, "S1")
    go_plan(session)
    assert session.phase == "plan_go"
    assert session.approved("plan") is True
    with pytest.raises(ticket.TicketError, match="expected phase build"):
        changes(session)
    session.end()
    resumed = open_pass(repo, src)
    assert resumed.phase == "build"
    with pytest.raises(ticket.TicketError, match="expected phase plan_go"):
        resumed.begin_build()
    changes(resumed)
    assert read_card(repo)["reviews"][-1]["round"] == 1


@pytest.mark.parametrize("step", ["begin_build", "require_outward_go", "approved"])
def test_E23_a_reading_step_refuses_a_moved_token_before_its_gate(repo, step):
    src = Source()
    if step == "begin_build":
        session = build(repo, src, "S1")
        go_plan(session)
    else:
        session = build(repo, src, "S4", 1)
    outside = _outsider_writes_tried(repo, src, session)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        if step == "begin_build":
            session.begin_build()
        elif step == "require_outward_go":
            session.require_outward_go()
        else:
            session.approved("outward")
    assert refusal(excinfo) == "stale"
    assert session.live is False
    assert read_card(repo) == {**outside, "pass": None}


def test_E24_the_held_token_advances_only_after_a_successful_write(repo):
    session = build(repo, Source(), "S2", 0)
    session.record_tried("First.")
    session.record_tried("Second.")
    held = session._token
    with pytest.raises(ticket.TicketError):
        session.record_tried("")
    assert session._token == held
    assert session.live is True
    session.record_tried("Fourth.")
    assert [item["what"] for item in read_card(repo)["tried"]] == ["First.", "Second.", "Fourth."]
    assert session.live is True


# --- F: close ----------------------------------------------------------------


def test_F1_close_writes_the_stamp_and_the_closed_status_in_one_write(repo):
    src = Source()
    session = build(repo, src, "S4", 1)
    writes = []
    real_apply = session.store.apply

    def counting(token, events):
        writes.append([kind for kind, _ in events])
        return real_apply(token, events)

    session.store.apply = counting
    session.close("Done.", refs=("example/repo#14", "branch:ek-14"), next_skills=("ticket", "review"))
    assert writes == [["stamp", "closed"]]
    card = read_card(repo)
    assert card["stamps"][-1] == {
        "point": "Done.", "phase": "close", "next_skills": ["ticket", "review"],
        "refs": ["example/repo#14", "branch:ek-14"], "close": True,
    }
    assert (card["status"], card["pass"]) == ("closed", None)
    assert open_pass(repo, src).phase == "closed"


@pytest.mark.parametrize("where", ["at build", "at plan_go", "outward go missing"])
def test_F2_close_is_refused_outside_outward_go_or_without_the_go(repo, where):
    src = Source()
    if where == "at build":
        session = build(repo, src, "S2", 0)
    elif where == "at plan_go":
        first = build(repo, src, "S1")
        session = open_pass(repo, src, pass_id=first.pass_id)
    else:
        session = build(repo, src, "S3", 1)
    before = card_bytes(repo)
    with pytest.raises(ticket.TicketError) as excinfo:
        session.close("Done.", refs=("example/repo#14",))
    if where == "outward go missing":
        assert isinstance(excinfo.value, ticket.GateRequired)
    else:
        assert "expected phase outward_go" in str(excinfo.value)
    assert card_bytes(repo) == before
    assert read_card(repo)["status"] == "open"
    assert session.live is True


def _no_plan_go(card):
    card["decisions"] = [d for d in card["decisions"] if d.get("gate") != "plan"]


def _no_outward_go(card):
    card["decisions"] = [d for d in card["decisions"] if d.get("gate") != "outward"]


def _final_changes(card):
    card["reviews"][-1]["verdict"] = "changes"


def _final_other_criteria(card):
    card["reviews"][-1].update(criteria=[B, A], plan_revision=2)


def _fourth_review(card):
    first = card["reviews"][0]
    card["reviews"][1:1] = [dict(first), dict(first)]
    for number, review in enumerate(card["reviews"], 1):
        review["round"] = number


def _last_stamp_not_close(card):
    card["stamps"][-1]["phase"] = "review"


def _no_close_stamp(card):
    card["stamps"][-1]["close"] = False


def _close_stamp_malformed(card):
    card["stamps"][-1]["point"] = ""


def _criteria_edited(card):
    card["plan"]["criteria"] = [B, A]
    card["reviews"][-1]["criteria"] = [B, A]
    for decision in card["decisions"]:
        decision["criteria"] = [B, A]


def _no_plan(card):
    card["plan"] = None


def _final_passes_fewer(card):
    card["reviews"][-1]["passed"] = [0]


def _final_without_evidence(card):
    card["reviews"][-1]["evidence"] = ""


def _no_stamps(card):
    card["stamps"] = []


# The nine checks of the row, then the further parts of a closed card the
# same checks read (a passing final review lists every criterion and carries
# its evidence; a close stamp exists; a plan exists).
F3_EDITS = {
    "plan go missing": _no_plan_go,
    "outward go missing": _no_outward_go,
    "final review changes": _final_changes,
    "final review other criteria": _final_other_criteria,
    "fourth review": _fourth_review,
    "last stamp not close": _last_stamp_not_close,
    "status closed without close stamp": _no_close_stamp,
    "close stamp malformed": _close_stamp_malformed,
    "criteria edited": _criteria_edited,
    "no plan": _no_plan,
    "final review passes fewer criteria": _final_passes_fewer,
    "final review without evidence": _final_without_evidence,
    "no stamps": _no_stamps,
}


@pytest.mark.parametrize("case", list(F3_EDITS))
def test_F3_each_closed_check_alone_refuses_a_closed_card(repo, case):
    src = Source()
    build(repo, src, "S6", 2)
    assert open_pass(repo, src).phase == "closed"
    edit_card(repo, F3_EDITS[case])
    before = card_bytes(repo)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_pass(repo, src)
    assert refusal(excinfo) == "close_invalid"
    assert card_bytes(repo) == before


def test_F4_a_closed_card_is_reported_closed_with_its_point_and_refs(repo):
    src = Source()
    build(repo, src, "S6")
    session = open_pass(repo, src)
    assert session.phase == "closed"
    stamp = session.resume_card["stamps"][-1]
    assert (stamp["point"], stamp["refs"], stamp["next_skills"]) == ("Ticket pass complete.", ["example/repo#14"], ["ticket"])


def test_F5_a_write_between_the_close_read_and_the_close_is_refused(repo):
    src = Source()
    session = build(repo, src, "S4", 1)
    outside = _outsider_writes_tried(repo, src, session)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        session.close("Done.", refs=("example/repo#14",))
    assert refusal(excinfo) == "stale"
    assert read_card(repo) == {**outside, "pass": None}
    assert read_card(repo)["status"] == "open"
    resumed = open_pass(repo, src)
    assert resumed.phase == "outward_go"
    assert resumed.approved("outward") is True


def test_F6_a_closed_ticket_never_reopens_on_a_later_resume(repo):
    src = Source()
    closer = build(repo, src, "S6")
    before = card_bytes(repo)
    first = open_pass(repo, src)
    second = open_pass(repo, src, pass_id=closer.pass_id)
    assert (first.phase, second.phase) == ("closed", "closed")
    assert (first.live, second.live) == (False, False)
    assert card_bytes(repo) == before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"point": ""},
        {"point": "x" * 601},
        {"refs": tuple(f"docs/note-{n}.md" for n in range(21))},
        {"refs": ("not a reference",)},
        {"next_skills": tuple(f"skill-{n}" for n in range(13))},
    ],
    ids=["empty point", "point over 600", "21 refs", "bad ref target", "13 next skills"],
)
def test_F7_close_validates_its_point_refs_and_next_skills(repo, kwargs):
    session = build(repo, Source(), "S4", 1)
    before = card_bytes(repo)
    arguments = {"point": "Done.", "refs": ("example/repo#14",), **kwargs}
    with pytest.raises(ticket.TicketError):
        session.close(arguments.pop("point"), **arguments)
    assert card_bytes(repo) == before
    assert session.live is True


def test_F8_a_closed_card_takes_no_claim_on_the_ledger_and_the_markdown_store(repo):
    # The ledger half of this row arrives with the ledger store; this is the markdown half.
    src = Source()
    build(repo, src, "S6")
    open_pass(repo, src)
    assert read_card(repo)["pass"] is None

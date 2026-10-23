"""Matrix section I: every validator refuses each fault alone, writes nothing, and keeps the pass live."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import ticket
from ticket_support import (
    A, B, F8, F9, F10, KEY, TITLE, Source, build, card_bytes, card_path, open_pass, read_card, refusal, store,
)

SKILL_DIR = Path(__file__).resolve().parent.parent


def _refused_by_store(repo, kind, value, message):
    """A live pass's handle refuses the event with ``message``; nothing is written and the pass stays live."""

    session = build(repo, Source(), "S2", 0)
    before = card_bytes(repo)
    with pytest.raises(ticket.TicketError, match=message):
        session.store.apply(session._token, [(kind, value)])
    assert card_bytes(repo) == before
    assert session.live is True
    return session


# --- I1: the ticket source ---------------------------------------------------

GOOD_TICKET = {
    "key": KEY, "title": TITLE, "status": "In Progress", "description": "d",
    "acceptance_criteria": [A, B], "links": [{"kind": "figma", "url": "https://example.com/f"}],
}

I1_FAULTS = {
    "missing field": ({"status": None}, "ticket is missing: status"),
    "criteria not a list": ({"acceptance_criteria": A}, "must be a list"),
    "empty criteria": ({"acceptance_criteria": []}, "must not be empty"),
    "non-text criterion": ({"acceptance_criteria": [A, 5]}, "must be text"),
    "blank criterion": ({"acceptance_criteria": [A, "  "]}, "may be blank"),
    "links not a list": ({"links": {"kind": "figma"}}, "links must be a list"),
    "link without kind": ({"links": [{"url": "https://example.com"}]}, "needs a kind"),
    "link without url": ({"links": [{"kind": "figma"}]}, "needs a url"),
    "empty title": ({"title": "  "}, "title must be non-empty"),
    "key mismatch": ({"key": "EK-99"}, "expected 'EK-14'"),
}


@pytest.mark.parametrize("fault", list(I1_FAULTS))
def test_I1_ticket_from_mapping_refuses_each_fault(tmp_path, fault):
    change, message = I1_FAULTS[fault]
    value = {**GOOD_TICKET, **change}
    if change.get("status", "") is None:
        del value["status"]
    path = tmp_path / "ticket.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ticket.TicketError, match=message):
        ticket.FileTicketSource(path).pull(KEY)


# --- I2-I8: the record validators -------------------------------------------

STAMP = {"point": "Paused.", "phase": "build", "next_skills": ["ticket"], "refs": ["docs/a.md"], "close": False}

I2_FAULTS = {
    "phase": ({"phase": "deploy"}, "unknown phase"),
    "empty point": ({"point": "  "}, "point must not be empty"),
    "non-text point": ({"point": 5}, "point must be text"),
    "point over 600": ({"point": "x" * 601}, "at most 600"),
    "13 next skills": ({"next_skills": [f"skill-{n}" for n in range(13)]}, "at most 12 skills"),
    "non-text skill": ({"next_skills": [5]}, "next skill must be text"),
    "skill over 80": ({"next_skills": ["s" * 81]}, "at most 80"),
    "skill regex": ({"next_skills": ["not a skill"]}, "must be a skill name"),
    "21 refs": ({"refs": [f"docs/{n}.md" for n in range(21)]}, "at most 20 references"),
    "ref not a target": ({"refs": ["not a reference"]}, "must be a path"),
    "ref over 300": ({"refs": ["docs/" + "a" * 300 + ".md"]}, "at most 300"),
    "ref with newline": ({"refs": ["docs/a.md\ndocs/b.md"]}, "one line"),
    # Further shape checks the same validator makes.
    "unknown field": ({"owner": "me"}, "exactly point, phase"),
    "close not bool": ({"close": "yes"}, "close must be true or false"),
}


@pytest.mark.parametrize("fault", list(I2_FAULTS))
def test_I2_stamp_validation_refuses_each_fault(repo, fault):
    change, message = I2_FAULTS[fault]
    _refused_by_store(repo, "stamp", {**STAMP, **change}, message)


def _decision():
    return {
        "who": "the person", "who_kind": "person", "standing": "settled",
        "source": {"what": "ratification", "ref": "conversation:plan-go"},
        "effect": "The plan gate is approved for plan revision 1.", "tags": [{"type": "ticket", "id": KEY}],
        "verbatim": "Go.", "phase": "plan", "gate": "plan", "plan_revision": 1, "criteria": [A, B],
        "pass": "0123456789abcdef",
    }


I3_FAULTS = {
    "who_kind": ({"who_kind": "robot"}, "who_kind must be person or role"),
    "role without relayed_by": ({"who_kind": "role"}, "requires relayed_by"),
    "standing": ({"standing": "maybe"}, "standing must be lean or settled"),
    "source what": ({"source": {"what": "rumor", "ref": "conversation:1"}}, "known outside event"),
    "empty tags": ({"tags": []}, "at least one tag"),
    "tag keys": ({"tags": [{"type": "ticket"}]}, "exactly a type and an id"),
    "tag type": ({"tags": [{"type": "epic", "id": "x"}]}, "unknown decision tag type"),
    "criteria invalid": ({"criteria": []}, "criteria must not be empty"),
    "gate unknown": ({"gate": "deploy"}, "unknown gate"),
    "plan_revision not int": ({"plan_revision": "1"}, "plan_revision must be a whole number"),
    # Further fields the same validator checks.
    "not an object": ("a decision", "decision must be an object"),
    "who": ({"who": ""}, "who must be non-empty"),
    "source ref": ({"source": {"what": "ratification", "ref": ""}}, "source's ref must be non-empty"),
    "effect": ({"effect": " "}, "effect must be non-empty"),
    "verbatim": ({"verbatim": ""}, "verbatim must be non-empty"),
    "phase": ({"phase": "deploy"}, "unknown phase"),
    "evidence": ({"evidence": "a\nb"}, "one line"),
    "tag id": ({"tags": [{"type": "ticket", "id": ""}]}, "tag's id must be non-empty"),
    "pass": ({"pass": ""}, "decision's pass must be non-empty"),
}


@pytest.mark.parametrize("fault", list(I3_FAULTS))
def test_I3_decision_validation_refuses_each_fault(repo, fault):
    change, message = I3_FAULTS[fault]
    value = change if isinstance(change, str) else {**_decision(), **change}
    _refused_by_store(repo, "decision", value, message)


def _review():
    return {"round": 1, "verdict": "changes", "plan_revision": 1, "criteria": [A, B], "passed": [],
            "findings": [], "reviewer": "reviewer", "evidence": "reviews/round-1.md"}


I4_FAULTS = {
    "round not int": ({"round": "1"}, "round must be a whole number"),
    "round 0": ({"round": 0}, "round must be a whole number"),
    "verdict": ({"verdict": "maybe"}, "verdict must be pass or changes"),
    "21 findings": ({"findings": [f"finding {n}" for n in range(21)]}, "at most 20 findings"),
    "non-text finding": ({"findings": [5]}, "review finding must be non-empty"),
    "reviewer non-text": ({"reviewer": 5}, "reviewer must be non-empty"),
    "evidence not a ref": ({"evidence": "not a reference"}, "must be a path"),
    "plan_revision 0": ({"plan_revision": 0}, "plan_revision must be a whole number"),
    "criteria invalid": ({"criteria": A}, "criteria must be a list"),
    "passed not a list": ({"passed": "0"}, "passed must be a list"),
    "passed index out of range": ({"passed": [2]}, "out of range"),
    "passed repeated": ({"passed": [0, 0]}, "repeats an index"),
    "not an object": ("a review", "review must be an object"),
}


@pytest.mark.parametrize("fault", list(I4_FAULTS))
def test_I4_review_validation_refuses_each_fault(repo, fault):
    change, message = I4_FAULTS[fault]
    value = change if isinstance(change, str) else {**_review(), **change}
    _refused_by_store(repo, "review", value, message)


def _plan_snapshot():
    return ticket.PlanCard(Source().pull(KEY)).snapshot(2, "0123456789abcdef", Source().pull(KEY))


I5_FAULTS = {
    "not a mapping": ([A, B], "must be an object"),
    "wrong schema": ({"schema": "ticket-plan-card/v1"}, "must carry schema"),
    "unknown field": ({"passes": [True, True]}, "unknown fields"),
    "other key": ({"key": "EK-99"}, "another ticket"),
    "empty title": ({"title": " "}, "title must be non-empty"),
    "revision": ({"revision": True}, "revision must be a whole number"),
    "status non-text": ({"ticket_status": 5}, "ticket_status must be text"),
    "updated non-text": ({"ticket_updated_at": 5}, "ticket_updated_at must be text"),
    "pass non-text": ({"pass": 5}, "pass must be non-empty"),
    "open_questions": ({"open_questions": [""]}, "open_questions must be a list"),
    "diff non-text": ({"diff_statement": 123}, "diff_statement must be non-empty"),
    "criteria not a list": ({"criteria": A}, "criteria must be a list"),
    "criterion non-text": ({"criteria": [A, 5]}, "criteria must hold only non-empty text"),
}


@pytest.mark.parametrize("fault", list(I5_FAULTS))
def test_I5_plan_snapshot_validation_refuses_each_fault(repo, fault):
    change, message = I5_FAULTS[fault]
    value = change if isinstance(change, list) else {**_plan_snapshot(), **change}
    _refused_by_store(repo, "plan", value, message)


@pytest.mark.parametrize(
    "shape, message",
    [((A, B), None), ({A, B}, "must be a list"), ([], "must not be empty"), ([A, 5], "only non-empty text")],
    ids=["tuple ok", "set", "empty", "non-text entry"],
)
def test_I6_criteria_texts_helper_refuses_each_shape(shape, message):
    if message is None:
        ticket._criteria_texts(shape, "criteria")
    else:
        with pytest.raises(ticket.TicketError, match=message):
            ticket._criteria_texts(shape, "criteria")


I7_FAULTS = {
    "missing step": (lambda p: p["steps"].pop(), "each once and in order"),
    "extra step": (lambda p: p["steps"].append({"name": "deploy", "status": "pending"}), "each once and in order"),
    "out of order": (lambda p: p["steps"].reverse(), "each once and in order"),
    "unknown status": (lambda p: p["steps"][0].update(status="done"), "unknown status"),
    "skipped without reason": (lambda p: p["steps"][1].update(status="skipped"), "requires skip: <reason>"),
    "reason on an unskipped step": (lambda p: p["steps"][1].update(skip="Known."), "carries no skip reason"),
    "plan skipped without diff": (lambda p: p["steps"][2].update(status="skipped", skip="Small."), "one-sentence diff"),
    "diff of two sentences": (
        lambda p: (p["steps"][2].update(status="skipped", skip="Small."), p.update(diff_statement="Add it. Test it.")),
        "one-sentence diff",
    ),
    "diff numeric": (
        lambda p: (p["steps"][2].update(status="skipped", skip="Small."), p.update(diff_statement=123)),
        "diff_statement must be non-empty",
    ),
    "step unknown field": (lambda p: p["steps"][0].update(owner="me"), "unknown fields"),
    "steps not a list": (lambda p: p.update(steps="orient"), "requires its steps"),
}


@pytest.mark.parametrize("fault", list(I7_FAULTS))
def test_I7_a_plan_card_that_breaks_the_step_rules_is_not_persisted(repo, fault):
    change, message = I7_FAULTS[fault]
    session = open_pass(repo, Source())
    session.finish_orient()
    session.finish_research()
    plan = ticket.PlanCard(session.ticket)
    real_snapshot = plan.snapshot

    def broken_snapshot(*args):
        value = real_snapshot(*args)
        change(value)
        return value

    plan.snapshot = broken_snapshot
    before = card_bytes(repo)
    with pytest.raises(ticket.TicketError, match=message):
        session.present_plan(plan)
    assert card_bytes(repo) == before
    assert session.live is True
    assert read_card(repo)["plan"] is None


def test_I25_completing_a_skipped_plan_step_clears_its_diff(repo):
    session = open_pass(repo, Source())
    session.finish_orient()
    session.finish_research()
    plan = ticket.PlanCard(session.ticket)
    plan.skip_step("plan", "One sentence covers it.", diff_statement="Add the guard.")
    plan.complete_step("orient")
    assert plan.diff_statement == "Add the guard."
    plan.complete_step("plan")
    assert plan.diff_statement is None
    assert "diff_statement" not in plan.snapshot(1, "0123456789abcdef", session.ticket)
    assert "Diff:" not in plan.render()
    session.present_plan(plan)
    stored = read_card(repo)["plan"]
    assert "diff_statement" not in stored
    assert {step["name"]: step["status"] for step in stored["steps"]}["plan"] == "complete"


@pytest.mark.parametrize("status", ["pending", "complete"])
def test_I26_a_diff_statement_on_a_plan_step_that_is_not_skipped_is_not_persisted(repo, status):
    session = open_pass(repo, Source())
    session.finish_orient()
    session.finish_research()
    plan = ticket.PlanCard(session.ticket)
    real_snapshot = plan.snapshot

    def broken_snapshot(*args):
        value = real_snapshot(*args)
        value["diff_statement"] = "Add the guard."
        value["steps"][2]["status"] = status
        return value

    plan.snapshot = broken_snapshot
    before = card_bytes(repo)
    with pytest.raises(ticket.TicketError, match="diff_statement only while the plan step is skipped"):
        session.present_plan(plan)
    assert card_bytes(repo) == before
    assert session.live is True


TRIED = {"what": "Ran the suite.", "phase": "build"}

I8_FAULTS = {
    "empty what": ({"what": " "}, "what must be non-empty"),
    "phase": ({"phase": "deploy"}, "unknown phase"),
    "outcome non-text": ({"outcome": 5}, "outcome must be non-empty"),
    "evidence not a ref": ({"evidence": "logs/a.txt\nlogs/b.txt"}, "one line"),
    "unknown field": ({"owner": "me"}, "a tried record has what"),
}


@pytest.mark.parametrize("fault", list(I8_FAULTS))
def test_I8_tried_validation_refuses_each_fault(repo, fault):
    change, message = I8_FAULTS[fault]
    _refused_by_store(repo, "tried", {**TRIED, **change}, message)


@pytest.mark.parametrize(
    "kind, value, message",
    [("note", {"text": "x"}, "unknown event"),
     ("claim", {"id": "a1", "host": "h"}, "exactly its id, host and started_at"),
     ("claim", {"id": "", "host": "h", "started_at": "t"}, "claim's id must be non-empty"),
     ("claim", {"id": "a1; b2", "host": "h", "started_at": "t"}, "claim's id must be lowercase hex")],
    ids=["unknown event", "claim fields", "claim id", "claim id not hex"],
)
def test_I_extra_the_store_refuses_an_unknown_event_or_a_malformed_claim(repo, kind, value, message):
    # Not a matrix row: the event validators beyond section I's records.
    handle = store(repo)
    _, token = handle.read()
    with pytest.raises(ticket.TicketError, match=message):
        handle.apply(token, [(kind, value)])
    assert card_bytes(repo) is None


# --- I9-I11: the reader, the lock, the claim ---------------------------------


@pytest.mark.parametrize("missing", ["no start", "no end"])
def test_I9_the_card_reader_refuses_a_block_without_its_markers(repo, missing):
    build(repo, Source(), "S1").end()
    body = card_path(repo).read_text(encoding="utf-8")
    if missing == "no start":
        body = body.replace("<!-- ticket-card:v2\n", "<!-- ticket-card\n")
    else:
        body = body.rstrip("\n")[: -len("\n-->")] + "\n"
    card_path(repo).write_text(body, encoding="utf-8")
    with pytest.raises(ticket.StoreUnavailable, match="no complete ticket-card block") as excinfo:
        open_pass(repo, Source())
    assert refusal(excinfo) == "card_unreadable"


def test_I10_a_lock_that_is_not_a_regular_file_is_refused(repo):
    lock = card_path(repo).with_name(f"{KEY}.lock")
    lock.parent.mkdir()
    os.mkfifo(lock)
    with pytest.raises(ticket.StoreUnavailable, match="not a regular file"):
        open_pass(repo, Source())
    assert card_bytes(repo) is None


@pytest.mark.parametrize("case", ["unbound with live", "bound other live", "bound none live", "ended"])
def test_I11_check_pass_refuses_each_case(repo, case):
    tried = [("tried", {"what": "A note.", "phase": "orient"})]
    if case == "bound none live":
        handle = store(repo)
        handle.pass_id = "0000000000000000"
        expected = "pass_ended"
    elif case == "ended":
        session = open_pass(repo, Source())
        handle = store(repo)
        handle.pass_id = session.pass_id
        handle.release()
        expected = "pass_ended"
    else:
        open_pass(repo, Source())
        handle = store(repo)
        if case == "bound other live":
            handle.pass_id = "0000000000000000"
        expected = "held"
    _, token = handle.read()
    before = card_bytes(repo)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        handle.apply(token, tried)
    assert refusal(excinfo) == expected
    assert card_bytes(repo) == before


def test_I12_identity_reads_the_revision_strictly():
    plan = {"revision": 1, "criteria": [A, B], "pass": "0123456789abcdef"}
    assert ticket._identity({"plan": plan}) == (1, (A, B))
    for revision in (True, 1.0, "1", 0, "absent"):
        stored = dict(plan, revision=revision)
        if revision == "absent":
            del stored["revision"]
        with pytest.raises(ticket.StoreUnavailable) as excinfo:
            ticket._identity({"plan": stored})
        assert refusal(excinfo) == "plan_unidentifiable", revision


# --- I13-I17: the steps' own checks -----------------------------------------


@pytest.mark.parametrize(
    "gate, evidence, verbatim, message",
    [("deploy", "conversation:go", "Go.", "unknown gate"),
     ("plan", "", "Go.", "requires evidence"),
     ("plan", "conversation:go", "  ", "requires evidence")],
    ids=["gate", "evidence", "verbatim"],
)
def test_I13_record_go_refuses_a_bad_gate_or_empty_words(repo, gate, evidence, verbatim, message):
    session = build(repo, Source(), "S1")
    before = card_bytes(repo)
    with pytest.raises(ticket.TicketError, match=message):
        session.record_go(gate, evidence=evidence, verbatim=verbatim)
    assert card_bytes(repo) == before
    assert session.live is True


@pytest.mark.parametrize(
    "verdict, passed, message",
    [("maybe", (), "verdict must be pass or changes"),
     ("changes", ("0",), "out of range: '0'"),
     ("changes", (2,), "out of range: 2"),
     ("changes", (0, 0), "repeats an index"),
     ("pass", (0,), "every acceptance criterion"),
     ("wrong phase", (), "expected phase build")],
    ids=["verdict", "index type", "index range", "index repeated", "pass not listing all", "wrong phase"],
)
def test_I14_record_review_refuses_each_fault(repo, verdict, passed, message):
    session = build(repo, Source(), "S1" if verdict == "wrong phase" else "S2", 0)
    before = card_bytes(repo)
    with pytest.raises(ticket.TicketError, match=message):
        session.record_review("changes" if verdict == "wrong phase" else verdict, evidence="reviews/round-1.md", passed=passed)
    assert card_bytes(repo) == before
    assert session.live is True


@pytest.mark.parametrize(
    "destination, question, role, message",
    [("boss", "Which branch?", None, "unknown more-information exit"),
     ("person", "  ", None, "exact question"),
     ("someone_else", "Which branch?", None, "requires a role")],
    ids=["destination", "empty question", "someone_else without role"],
)
def test_I15_more_info_refuses_each_fault(repo, destination, question, role, message):
    session = build(repo, Source(), "S2", 0)
    before = card_bytes(repo)
    with pytest.raises(ticket.TicketError, match=message):
        session.more_info(destination, question, role=role)
    assert card_bytes(repo) == before
    assert session.live is True


@pytest.mark.parametrize("branch", ["skipped step", "complete step", "pending step", "diff", "updated_at", "status"])
def test_I16_resume_restores_steps_diff_and_questions_and_flags_status(repo, branch):
    src = Source()
    session = open_pass(repo, src)
    session.finish_orient()
    session.finish_research()
    plan = ticket.PlanCard(session.ticket)
    plan.complete_step("orient")
    plan.skip_step("plan", "One sentence covers it.", diff_statement="Add the guard.")
    plan.add_open_question("Which flag?")
    session.present_plan(plan)
    session.end()
    if branch == "updated_at":
        src.updated_at = "2026-10-18T09:00:00Z"
    if branch == "status":
        src.status = "Blocked"
    resumed = open_pass(repo, src)
    steps = {step.name: (step.status, step.skip) for step in resumed.plan.steps}
    if branch == "skipped step":
        assert steps["plan"] == ("skipped", "One sentence covers it.")
    elif branch == "complete step":
        assert steps["orient"] == ("complete", None)
    elif branch == "pending step":
        assert steps["research"] == ("pending", None)
    elif branch == "diff":
        assert (resumed.plan.diff_statement, resumed.plan.open_questions) == ("Add the guard.", ["Which flag?"])
    elif branch == "updated_at":
        assert resumed.reconciliation == (F8(1), F10)
    else:
        assert resumed.reconciliation == (F8(1), F9("In Progress", "Blocked"))


def test_I17_an_open_question_must_be_text():
    plan = ticket.PlanCard(Source().pull(KEY))
    for question in ("", "  "):
        with pytest.raises(ticket.TicketError, match="open question"):
            plan.add_open_question(question)
    assert plan.open_questions == []


# --- I20, I22: store selection and present_plan -----------------------------


def _release(repo, env):
    return subprocess.run(
        [sys.executable, str(SKILL_DIR / "ticket.py"), "release", KEY, "0123456789abcdef", "--repo", str(repo)],
        capture_output=True, text=True, env=env, check=False,
    )


@pytest.mark.parametrize(
    "case",
    ["empty selector is unset", "selector names the ledger", "release command exits 2 on an incompatible ledger",
     "release exits 1 on a bad card"],
)
def test_I20_store_selection(repo, monkeypatch, case):
    env = {name: value for name, value in os.environ.items() if name != "TICKET_LEDGER_CLI"}
    if case == "empty selector is unset":
        monkeypatch.setenv("TICKET_LEDGER_CLI", "")
        assert isinstance(ticket.choose_store(repo, KEY, TITLE), ticket.MarkdownStore)
    elif case == "selector names the ledger":
        monkeypatch.setenv("TICKET_LEDGER_CLI", sys.executable)
        with pytest.raises(ticket.StoreUnavailable) as excinfo:
            ticket.choose_store(repo, KEY, TITLE)
        assert refusal(excinfo) == "ledger_store_unavailable"
        assert not (repo / ".ticket").exists()
    elif case == "release command exits 2 on an incompatible ledger":
        result = _release(repo, dict(env, TICKET_LEDGER_CLI=sys.executable))
        assert result.returncode == 2
        assert len(result.stderr.strip().splitlines()) == 1
        assert not (repo / ".ticket").exists()
    else:
        card_path(repo).parent.mkdir()
        card_path(repo).write_text("# not a card\n", encoding="utf-8")
        before = card_bytes(repo)
        result = _release(repo, env)
        assert result.returncode == 1
        assert len(result.stderr.strip().splitlines()) == 1
        assert card_bytes(repo) == before


@pytest.mark.parametrize("fault", ["other key", "other order"])
def test_I22_present_plan_refuses_each_fault(repo, fault):
    session = open_pass(repo, Source())
    session.finish_orient()
    session.finish_research()
    plan = ticket.PlanCard(session.ticket)
    if fault == "other key":
        plan.key = "EK-99"
        message = "another ticket"
    else:
        plan._criteria = (B, A)
        message = "in order"
    before = card_bytes(repo)
    with pytest.raises(ticket.TicketError, match=message):
        session.present_plan(plan)
    assert card_bytes(repo) == before
    assert session.live is True

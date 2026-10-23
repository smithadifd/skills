from __future__ import annotations

import contextlib
import copy
import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import replace as dataclasses_replace
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
FIXTURE = SKILL_DIR / "tests" / "fixtures" / "ticket.json"
SCHEMA = SKILL_DIR / "plan-card.schema.json"
KEY = "EK-14"
TITLE = "Add guarded ticket pass"

_spec = importlib.util.spec_from_file_location("ticket_skill", SKILL_DIR / "ticket.py")
ticket = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ticket
_spec.loader.exec_module(ticket)


class MemoryStore(ticket.MarkdownStore):
    """The markdown adapter's card logic, held in memory instead of a file."""

    def __init__(self, trace=None):
        super().__init__(SKILL_DIR, KEY, TITLE)
        self.trace = trace
        self.card = None

    def _load(self):
        if self.card is None:
            return ticket._blank_card(self.key, self.title)
        return copy.deepcopy(self.card)

    def _save(self, card):
        self.card = copy.deepcopy(card)

    def _writing(self):
        # One in-memory card in one process: there is no second writer to lock out.
        return contextlib.nullcontext()

    def read_card(self):
        if self.trace is not None:
            self.trace.append("read-card")
        return super().read_card()


class StaticSource:
    def __init__(self, value):
        self.value = value

    def pull(self, key):
        return self.value


def fixture_ticket():
    return ticket.FileTicketSource(FIXTURE).pull(KEY)


def source():
    return ticket.FileTicketSource(FIXTURE)


def to_plan_go(store):
    session = ticket.TicketPass.open(KEY, source(), store)
    session.finish_orient()
    session.finish_research()
    session.present_plan(ticket.PlanCard(session.ticket))
    return session


def to_build(store):
    session = to_plan_go(store)
    session.record_go("plan", evidence="conversation:plan-go", verbatim="Go with this plan.")
    session.begin_build()
    return session


def resume(store, value=None, pass_id=None):
    """Continue the live pass ``pass_id``, or start a new pass from the card when it is None."""

    src = StaticSource(value) if value is not None else source()
    return ticket.TicketPass.open(KEY, src, store, resume=True, pass_id=pass_id)


def plan_go_effect(revision, pass_id):
    return f"The plan gate is approved for plan revision {revision} presented by pass {pass_id}."


def test_fixture_source_exposes_live_ticket_and_design_links():
    value = fixture_ticket()
    assert value.status == "In Progress"
    assert value.updated_at == "2026-10-17T14:30:00Z"
    assert {link["kind"] for link in value.links} == {"figma", "confluence"}


def test_plan_card_schema_defaults_and_no_public_pass_flip():
    card = ticket.PlanCard(fixture_ticket())
    data = card.as_dict()
    assert data["schema"] == "ticket-plan-card/v1"
    assert [item["passes"] for item in data["acceptance_criteria"]] == [False, False]
    assert [item["review_evidence"] for item in data["acceptance_criteria"]] == [None, None]
    assert not hasattr(card, "mark_passes")
    assert isinstance(card.acceptance_criteria, tuple)
    with pytest.raises(AttributeError):
        card.acceptance_criteria = (ticket.Criterion("x", True, "x", 1),)


def test_skips_require_reasons_and_plan_skip_requires_one_sentence_diff():
    card = ticket.PlanCard(fixture_ticket())
    with pytest.raises(ticket.TicketError, match="skip: <reason>"):
        card.skip_step("research", "")
    card.skip_step("research", "The fixture already supplies the answer.")

    with pytest.raises(ticket.TicketError, match="one-sentence diff"):
        card.skip_step(
            "plan",
            "The change is fully described.",
            diff_statement="Add the guard. Add the adapter.",
        )
    card.skip_step(
        "plan",
        "The complete change fits in the diff statement.",
        diff_statement="Add one missing error message.",
    )
    rendered = card.render()
    assert "research: skip: The fixture already supplies the answer." in rendered
    assert "plan: skip: The complete change fits in the diff statement." in rendered
    assert "Diff: Add one missing error message." in rendered


def test_schema_encodes_pass_evidence_skip_reason_and_plan_diff_rules():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    criterion = schema["properties"]["acceptance_criteria"]["items"]
    step = schema["properties"]["steps"]["items"]
    assert criterion["allOf"][0]["then"]["required"] == ["review_evidence", "review_round"]
    assert step["allOf"][0]["then"]["required"] == ["skip"]
    assert schema["allOf"][0]["then"]["required"] == ["diff_statement"]
    assert "revision" in schema["properties"]
    assert "open_questions" in schema["properties"]


def test_resume_pulls_live_ticket_before_reading_card():
    trace = []

    class Source:
        def pull(self, key):
            trace.append("pull-live")
            return fixture_ticket()

    session = ticket.TicketPass.open(KEY, Source(), MemoryStore(trace), resume=True)
    assert session.resume_card["key"] == KEY
    assert trace == ["pull-live", "read-card"]


def test_plan_and_outward_go_are_hard_stops():
    store = MemoryStore()
    session = to_plan_go(store)

    with pytest.raises(ticket.GateRequired, match="plan go"):
        session.begin_build()
    assert session.phase == "plan_go"

    session.record_go("plan", evidence="conversation:plan-go", verbatim="Go with this plan.")
    session.begin_build()
    session.record_review("pass", evidence="reviews/round-1.md", passed_criteria=(0, 1))
    assert session.phase == "outward_go"

    with pytest.raises(ticket.GateRequired, match="outward go"):
        session.require_outward_go()
    with pytest.raises(ticket.GateRequired, match="outward go"):
        session.close("Done", refs=("example/repo#14",))
    assert session.phase == "outward_go"

    session.record_go("outward", evidence="conversation:outward-go", verbatim="Open the PR.")
    session.require_outward_go()
    session.close("Ticket pass complete.", refs=("example/repo#14",))
    assert session.phase == "closed"
    assert [item["effect"] for item in store.card["decisions"]] == [
        plan_go_effect(1, session.pass_id),
        "The outward gate is approved for plan revision 1.",
    ]
    assert store.card["stamps"][-1]["close"] is True


def test_both_hard_stops_are_stamped_before_the_stop_is_exposed():
    store = MemoryStore()
    session = to_plan_go(store)
    plan_ref = store.card["plan_ref"]
    assert plan_ref
    assert store.card["stamps"][-1] == {
        "point": "Waiting at plan go for plan revision 1.",
        "phase": "plan",
        "next_skills": ["ticket"],
        "refs": [plan_ref],
        "close": False,
    }
    assert resume(store, pass_id=session.pass_id).phase == "plan_go"

    session.record_go("plan", evidence="conversation:plan-go", verbatim="Go.")
    session.begin_build()
    session.record_review("pass", evidence="reviews/round-1.md", passed_criteria=(0, 1))
    assert store.card["stamps"][-1] == {
        "point": "Waiting at outward go after review round 1.",
        "phase": "review",
        "next_skills": ["ticket"],
        "refs": [store.card["plan_ref"], "reviews/round-1.md"],
        "close": False,
    }
    assert resume(store, pass_id=session.pass_id).phase == "outward_go"


def test_a_failed_stop_stamp_does_not_expose_the_stop():
    class NoStamps(MemoryStore):
        def write_stamp(self, *args, **kwargs):
            raise ticket.StoreUnavailable("stamp refused")

    session = ticket.TicketPass.open(KEY, source(), NoStamps())
    session.finish_orient()
    session.finish_research()
    with pytest.raises(ticket.StoreUnavailable):
        session.present_plan(ticket.PlanCard(session.ticket))
    assert session.phase == "plan"


def test_resume_enters_a_hard_stop_only_when_its_stamp_is_stored():
    # Probe: the plan snapshot is stored but the plan go stamp is refused.
    class FailPlanStamp(MemoryStore):
        def write_stamp(self, *args, **kwargs):
            raise ticket.StoreUnavailable("stamp refused")

    store = FailPlanStamp()
    session = ticket.TicketPass.open(KEY, source(), store)
    session.finish_orient()
    session.finish_research()
    with pytest.raises(ticket.StoreUnavailable):
        session.present_plan(ticket.PlanCard(session.ticket))
    assert store.card["plan"]["revision"] == 1
    assert store.card["stamps"] == []
    resumed = resume(store)
    assert (session.phase, resumed.phase) == ("plan", "plan")
    assert "no plan go stop stamp for plan revision 1; resuming at plan" in resumed.reconciliation

    # Probe: a passing review is stored but the outward go stamp is refused.
    class FailOutwardStamp(MemoryStore):
        def write_stamp(self, point, phase, next_skills=(), refs=()):
            if point.startswith("Waiting at outward go"):
                raise ticket.StoreUnavailable("stamp refused")
            super().write_stamp(point, phase, next_skills, refs)

    store = FailOutwardStamp()
    session = to_build(store)
    with pytest.raises(ticket.StoreUnavailable):
        session.record_review("pass", evidence="reviews/1", passed_criteria=(0, 1))
    assert len(store.card["reviews"]) == 1
    resumed = resume(store)
    # The passing round stays recorded; the pass resumes to write its stop, not to build again.
    assert (session.phase, resumed.phase) == ("build", "outward_stop")
    assert any(line.startswith("outward stop not recorded: review round 1") for line in resumed.reconciliation)
    with pytest.raises(ticket.TicketError):
        resumed.record_go("outward", evidence="conversation:outward", verbatim="Ship it.")


def test_review_changes_continue_without_person_until_round_cap():
    store = MemoryStore()
    session = to_build(store)

    session.record_review("changes", evidence="reviews/1", findings=("first",))
    session.record_review("changes", evidence="reviews/2", findings=("second",))
    assert session.phase == "build"
    session.record_review("changes", evidence="reviews/3", findings=("third",))
    assert session.phase == "stopped"
    assert len(store.card["reviews"]) == 3
    assert store.card["stamps"][-1]["point"] == "Review did not converge after round three."


@pytest.mark.parametrize(
    "destination, role, expected",
    [
        ("person", None, "waiting on person: Which API should own retries?"),
        ("research", None, "waiting on research: Which API should own retries?"),
        ("someone_else", "API owner", "waiting on API owner: Which API should own retries?"),
    ],
)
def test_three_more_info_exits(destination, role, expected):
    store = MemoryStore()
    session = ticket.TicketPass.open(KEY, source(), store)
    result = session.more_info(destination, "Which API should own retries?", role=role)
    assert result.waiting == expected
    assert store.card["stamps"][-1]["point"] == expected
    assert session.phase == "stopped"
    if destination == "someone_else":
        assert result.drafted_ask == "Could you help with EK-14? Which API should own retries?"
    else:
        assert result.drafted_ask is None


def test_plan_card_open_questions_and_tried_persist_and_resume_rebuilds_the_pass():
    store = MemoryStore()
    session = ticket.TicketPass.open(KEY, source(), store)
    session.finish_orient()
    session.finish_research()
    plan = ticket.PlanCard(session.ticket)
    plan.skip_step("research", "The fixture answers it.")
    plan.add_open_question("Which API owns retries?")
    session.present_plan(plan)
    session.record_go("plan", evidence="conversation:plan-go", verbatim="Go.")
    session.begin_build()
    session.record_tried("Retry in the client.", outcome="Too chatty.", evidence="notes/retry.md")
    session.record_review("changes", evidence="reviews/1", passed_criteria=(0,))

    resumed = resume(store, pass_id=session.pass_id)
    assert resumed.phase == "build"
    assert resumed.review_round == 1
    assert resumed.plan_revision == 1
    assert resumed.approved("plan") is True
    assert resumed.approved("outward") is False
    assert resumed.reconciliation == ()
    first, second = resumed.plan.acceptance_criteria
    assert (first.passes, first.review_evidence, first.review_round) == (True, "reviews/1", 1)
    assert second.passes is False
    assert resumed.plan.open_questions == ["Which API owns retries?"]
    assert resumed.plan.steps[1].skip == "The fixture answers it."
    assert resumed.resume_card["tried"] == [
        {"what": "Retry in the client.", "phase": "build", "outcome": "Too chatty.", "evidence": "notes/retry.md"}
    ]

    resumed.record_review("pass", evidence="reviews/2", passed_criteria=(0, 1))
    assert resumed.phase == "outward_go"
    assert [review["round"] for review in store.card["reviews"]] == [1, 2]


def test_resume_flags_changed_acceptance_criteria_and_never_keeps_them_passing():
    store = MemoryStore()
    session = to_build(store)
    session.record_review("changes", evidence="reviews/1", passed_criteria=(0, 1))

    live = fixture_ticket()
    changed = ticket.Ticket(
        live.key,
        live.title,
        "In Review",
        live.description,
        (live.acceptance_criteria[0], "Plan and outward work require two approvals."),
        live.links,
        "2026-10-18T09:00:00Z",
    )
    resumed = resume(store, changed, pass_id=session.pass_id)
    assert resumed.phase == "plan"
    assert [item.passes for item in resumed.plan.acceptance_criteria] == [True, False]
    flags = "\n".join(resumed.reconciliation)
    assert "new or changed on the ticket: Plan and outward work require two approvals." in flags
    assert "removed from the ticket: Plan and outward work require explicit approval." in flags
    assert "ticket status changed: In Progress -> In Review" in flags

    resumed.present_plan(ticket.PlanCard(resumed.ticket))
    assert resumed.plan_revision == 2
    with pytest.raises(ticket.GateRequired):
        resumed.begin_build()


def test_round_cap_counts_recorded_rounds_across_resumes():
    store = MemoryStore()
    session = to_build(store)
    session.record_review("changes", evidence="reviews/1")
    session.record_review("changes", evidence="reviews/2")

    after_two = resume(store, pass_id=session.pass_id)
    assert after_two.phase == "build"
    assert after_two.review_round == 2
    after_two.record_review("changes", evidence="reviews/3")
    assert after_two.phase == "stopped"

    after_three = resume(store)
    assert after_three.review_round == 3
    assert after_three.phase == "stopped"
    with pytest.raises(ticket.TicketError):
        after_three.record_review("pass", evidence="reviews/4", passed_criteria=(0, 1))
    assert len(store.card["reviews"]) == 3


def test_forged_passes_in_the_store_are_not_trusted_on_resume():
    store = MemoryStore()
    session = to_build(store)
    session.record_review("changes", evidence="reviews/1")
    forged = store.card["plan"]
    forged["acceptance_criteria"][0].update(passes=True, review_evidence="x", review_round=9)
    forged["acceptance_criteria"][1].update(passes=True, review_evidence="x", review_round=1)

    resumed = resume(store, pass_id=session.pass_id)
    assert [item.passes for item in resumed.plan.acceptance_criteria] == [False, False]
    assert sum("not backed by a recorded review round" in flag for flag in resumed.reconciliation) == 2
    assert resumed.phase == "build"


def test_public_api_and_attribute_assignment_cannot_forge_gates_or_passes():
    store = MemoryStore()
    session = to_plan_go(store)

    # Probe: a caller-supplied go record and a direct phase assignment.
    session.go_records = {"plan": "invented"}
    with pytest.raises(ticket.GateRequired):
        session.begin_build()
    with pytest.raises(AttributeError):
        session.phase = "build"
    with pytest.raises(AttributeError):
        session.review_round = 0
    assert session.phase == "plan_go"

    # A decision that is not the person's settled ratification opens nothing.
    store.record_decision(
        ticket.DecisionRecord(
            who="API owner",
            who_kind="role",
            relayed_by="the person",
            standing="lean",
            source=ticket.DecisionSource("reply", "thread:1"),
            effect="The plan gate is approved for plan revision 1.",
            tags=({"type": "ticket", "id": KEY},),
            verbatim="Looks fine.",
            phase="plan",
        )
    )
    with pytest.raises(ticket.GateRequired):
        session.begin_build()

    session.record_go("plan", evidence="conversation:plan-go", verbatim="Go.")
    session.begin_build()

    # Claude probes A and C: out-of-range, negative and boolean indices, and a
    # pass with no per-criterion evidence, are refused before anything is written.
    for bad in ((-1,), (2,), (True,), (0, 0)):
        with pytest.raises(ticket.TicketError):
            session.record_review("changes", evidence="reviews/1", passed_criteria=bad)
    with pytest.raises(ticket.TicketError, match="every acceptance criterion"):
        session.record_review("pass", evidence="reviews/1")
    with pytest.raises(ticket.TicketError, match="must be a path"):
        session.record_review("changes", evidence="x")
    assert store.card["reviews"] == []
    assert session.review_round == 0
    assert [item.passes for item in session.plan.acceptance_criteria] == [False, False]

    # Resume probe: an edited card with an invented approval rebuilds nothing.
    class Edited(MemoryStore):
        def read_card(self):
            return {"edited": True, "decisions": [{"effect": "pretend plan approval"}]}

    resumed = ticket.TicketPass.open(KEY, source(), Edited(), resume=True)
    assert (resumed.phase, resumed.plan, resumed.review_round) == ("orient", None, 0)


def at_plan_revision_two(store):
    """A pass whose current plan is revision 2, so revision 1 is a real older plan."""

    live = fixture_ticket()
    earlier = ticket.Ticket(
        live.key,
        live.title,
        live.status,
        live.description,
        (live.acceptance_criteria[0], "An earlier wording of the second criterion."),
        live.links,
        live.updated_at,
    )
    first = ticket.TicketPass.open(KEY, StaticSource(earlier), store)
    first.finish_orient()
    first.finish_research()
    first.present_plan(ticket.PlanCard(earlier))
    session = resume(store, pass_id=first.pass_id)
    assert session.phase == "plan"
    session.present_plan(ticket.PlanCard(session.ticket))
    assert (session.plan_revision, session.phase) == (2, "plan_go")
    return session


def go_effect(gate, revision, pass_id):
    if gate == "plan":
        return plan_go_effect(revision, pass_id)
    return f"The {gate} gate is approved for plan revision {revision}."


def one_fault_go(gate, fault, pass_id):
    """The person's settled ratification of a gate at revision 2, wrong in one field only."""

    fields = dict(
        who="the person",
        who_kind="person",
        standing="settled",
        source=ticket.DecisionSource("ratification", "conversation:go"),
        effect=go_effect(gate, 2, pass_id),
        tags=({"type": "ticket", "id": KEY},),
        verbatim="Go.",
        phase="plan" if gate == "plan" else "review",
    )
    if fault == "role":
        fields.update(who="API owner", who_kind="role", relayed_by="the person")
    elif fault == "lean":
        fields.update(standing="lean")
    elif fault == "source":
        fields.update(source=ticket.DecisionSource("reply", "conversation:go"))
    elif fault == "tag":
        fields.update(tags=({"type": "ticket", "id": "EK-OTHER"},))
    elif fault == "older_revision":
        fields.update(effect=go_effect(gate, 1, pass_id))
    else:
        assert fault == "none"
    return ticket.DecisionRecord(**fields)


GO_FAULTS = ("role", "lean", "source", "tag", "older_revision")


@pytest.mark.parametrize("fault", GO_FAULTS + ("none",))
def test_each_go_check_alone_keeps_every_gate_shut(fault):
    def refused(call):
        if fault == "none":
            call()
        else:
            with pytest.raises(ticket.GateRequired):
                call()

    store = MemoryStore()
    session = at_plan_revision_two(store)
    store.record_decision(one_fault_go("plan", fault, session.pass_id))
    refused(session.begin_build)

    store = MemoryStore()
    session = at_plan_revision_two(store)
    session.record_go("plan", evidence="conversation:plan-go", verbatim="Go.")
    session.begin_build()
    session.record_review("pass", evidence="reviews/1", passed_criteria=(0, 1))
    store.record_decision(one_fault_go("outward", fault, session.pass_id))
    refused(session.require_outward_go)
    refused(lambda: session.close("Done.", refs=("example/repo#14",)))
    assert session.phase == ("closed" if fault == "none" else "outward_go")


def test_review_that_the_store_does_not_record_flips_nothing():
    class DropsReviews(MemoryStore):
        def record_review(self, review):
            ticket._validate_review(review)

    store = DropsReviews()
    session = to_build(store)
    with pytest.raises(ticket.StoreUnavailable, match="not recorded"):
        session.record_review("pass", evidence="reviews/1", passed_criteria=(0, 1))
    assert [item.passes for item in session.plan.acceptance_criteria] == [False, False]
    assert session.phase == "build"


def test_invalid_pass_is_refused_before_anything_is_written():
    store = MemoryStore()
    session = to_build(store)
    with pytest.raises(ticket.TicketError, match="every acceptance criterion"):
        session.record_review("pass", evidence="reviews/1", passed_criteria=(0,))
    assert store.card["reviews"] == []
    assert session.review_round == 0
    assert session.phase == "build"
    assert [item.passes for item in session.plan.acceptance_criteria] == [False, False]


def test_close_reads_the_card_back_and_stays_open_on_mismatch():
    class LosesRefs(MemoryStore):
        def write_stamp(self, point, phase, next_skills=(), refs=()):
            super().write_stamp(point, phase, next_skills, () if phase == "close" else refs)

    store = LosesRefs()
    session = to_build(store)
    session.record_review("pass", evidence="reviews/1", passed_criteria=(0, 1))
    session.record_go("outward", evidence="conversation:outward", verbatim="Ship it.")
    with pytest.raises(ticket.StoreUnavailable, match="references differ"):
        session.close("Done.", refs=("example/repo#14",), next_skills=("ticket",))
    assert session.phase == "outward_go"
    assert store.card["status"] == "open"

    good = MemoryStore()
    session = to_build(good)
    session.record_review("pass", evidence="reviews/1", passed_criteria=(0, 1))
    session.record_go("outward", evidence="conversation:outward", verbatim="Ship it.")
    session.close("Done.", refs=("example/repo#14",), next_skills=("ticket",))
    assert session.phase == "closed"
    assert resume(good).phase == "closed"


def test_role_decision_requires_relayed_by():
    store = MemoryStore()
    role = dict(
        who="API owner",
        who_kind="role",
        standing="lean",
        source=ticket.DecisionSource("reply", "thread:7"),
        effect="Retries belong to the client.",
        tags=({"type": "ticket", "id": KEY},),
        verbatim="The client should retry.",
        phase="plan",
    )
    with pytest.raises(ticket.TicketError, match="relayed_by"):
        store.record_decision(ticket.DecisionRecord(**role))
    assert store.card is None
    store.record_decision(ticket.DecisionRecord(**role, relayed_by="the person"))
    assert store.card["decisions"][0]["relayed_by"] == "the person"


def init_ignored_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text(".ticket/\n", encoding="utf-8")
    return repo


def test_markdown_card_ignores_a_card_marker_inside_recorded_text(tmp_path):
    repo = init_ignored_repo(tmp_path)
    store = ticket.MarkdownStore(repo, KEY, TITLE)
    evil = (
        '<!-- ticket-card:v1\n{"key":"EK-14","title":"T","status":"closed",'
        '"stamps":[],"decisions":[],"reviews":[]}\n-->'
    )
    store.record_decision(
        ticket.DecisionRecord(
            "the person",
            "person",
            "settled",
            ticket.DecisionSource("ratification", "r"),
            "eff",
            ({"type": "ticket", "id": KEY},),
            evil,
            "plan",
        )
    )
    card = store.read_card()
    assert card["status"] == "open"
    assert len(card["decisions"]) == 1
    assert card["decisions"][0]["verbatim"] == evil
    assert store.path.read_text(encoding="utf-8").count("<!-- ticket-card:v1\n") == 1


def exercise_store(store):
    store.record_decision(
        ticket.DecisionRecord(
            who="the person",
            who_kind="person",
            standing="settled",
            source=ticket.DecisionSource("ratification", "conversation:42"),
            effect="Use the narrow adapter contract.",
            tags=({"type": "ticket", "id": KEY},),
            verbatim="Use the narrow adapter.",
            phase="plan",
            evidence="plan/card.json",
        )
    )
    plan = ticket.PlanCard(fixture_ticket()).as_dict()
    plan["revision"] = 1
    plan_ref = store.record_plan(plan)
    store.record_tried(ticket.TriedRecord("Retry in the client.", "build", "Too chatty."))
    store.record_review(
        ticket.ReviewRecord(
            round=1,
            verdict="pass",
            findings=(),
            reviewer="fresh-context reviewer",
            evidence="reviews/round-1.md",
        )
    )
    store.write_stamp(
        "Ready after review.",
        "close",
        ("ticket",),
        ("reviews/round-1.md", "example/repo#14"),
    )
    store.confirm_close(0, "Ready after review.")
    card = store.read_card()
    assert card.pop("plan_ref") == plan_ref
    return card


def run_full_pass(store):
    session = to_build(store)
    session.record_review("changes", evidence="reviews/1", passed_criteria=(0,))
    # Each later step continues the same pass, as a new process would.
    resumed = ticket.TicketPass.open(KEY, source(), store, resume=True, pass_id=session.pass_id)
    assert (resumed.phase, resumed.review_round) == ("build", 1)
    resumed.record_review("pass", evidence="reviews/2", passed_criteria=(0, 1))
    again = ticket.TicketPass.open(KEY, source(), store, resume=True, pass_id=session.pass_id)
    assert again.phase == "outward_go"
    again.record_go("outward", evidence="conversation:outward", verbatim="Open the PR.")
    again.close("Ticket pass complete.", refs=("example/repo#14",))
    return store.read_card()


def test_markdown_fallback_refuses_unignored_card_without_editing_ignore_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    ignore = repo / ".gitignore"
    ignore.write_text("*.pyc\n", encoding="utf-8")
    store = ticket.MarkdownStore(repo, KEY, TITLE)

    with pytest.raises(ticket.StoreUnavailable, match="add `.ticket/`"):
        store.write_stamp("Stopped.", "orient")
    assert ignore.read_text(encoding="utf-8") == "*.pyc\n"
    assert not store.path.exists()


def test_markdown_adapter_records_every_card_field(tmp_path):
    repo = init_ignored_repo(tmp_path)
    markdown = ticket.MarkdownStore(repo, KEY, TITLE)
    card = exercise_store(markdown)
    assert markdown.path == repo / ".ticket" / "EK-14.md"
    assert "# EK-14 — Add guarded ticket pass" in markdown.path.read_text(encoding="utf-8")
    assert card["status"] == "closed"
    assert card["plan"]["revision"] == 1
    assert card["tried"] == [{"what": "Retry in the client.", "phase": "build", "outcome": "Too chatty."}]
    assert [item["source"] for item in card["decisions"]] == [{"what": "ratification", "ref": "conversation:42"}]
    assert [(review["round"], review["verdict"]) for review in card["reviews"]] == [(1, "pass")]
    assert [(stamp["point"], stamp["close"]) for stamp in card["stamps"]] == [("Ready after review.", True)]


def test_full_pass_through_the_markdown_store(tmp_path):
    card = run_full_pass(ticket.MarkdownStore(init_ignored_repo(tmp_path), KEY, TITLE))
    pass_id = card["plan"]["pass"]
    assert card["decisions"][0]["effect"] == plan_go_effect(1, pass_id)
    assert [review["round"] for review in card["reviews"]] == [1, 2]
    assert card["plan"]["acceptance_criteria"][1]["review_round"] == 2
    assert card["stamps"][-1]["close"] is True
    assert card["status"] == "closed"
    assert card["pass"] is None


def test_a_failed_close_is_not_closed_in_the_store_or_on_resume(tmp_path):
    # Probe: the store drops the close stamp's references.
    class Lossy(ticket.MarkdownStore):
        def write_stamp(self, point, phase, next_skills=(), refs=()):
            super().write_stamp(point, phase, next_skills, () if phase == "close" else refs)

    store = Lossy(init_ignored_repo(tmp_path), KEY, TITLE)
    session = to_build(store)
    session.record_review("pass", evidence="reviews/1", passed_criteria=(0, 1))
    session.record_go("outward", evidence="conversation:outward", verbatim="Ship it.")
    with pytest.raises(ticket.StoreUnavailable, match="references differ"):
        session.close("Done.", refs=("example/repo#14",), next_skills=("ticket",))
    assert session.phase == "outward_go"
    card = store.read_card()
    assert card["status"] == "open"
    assert card["stamps"][-1]["close"] is False

    resumed = resume(store)
    assert resumed.phase == "outward_go"
    assert "a close stamp was written but never confirmed; the ticket is not closed" in resumed.reconciliation


def concrete_store(tmp_path, *mixins):
    cls = type("Store", (*mixins, ticket.MarkdownStore), {})
    return cls(init_ignored_repo(tmp_path), KEY, TITLE)


def fresh_handle(store):
    """Another handle on the same ticket and store, as another process would open."""

    return ticket.MarkdownStore(store.repo, KEY, TITLE)


def approved_for_close(store):
    session = to_build(store)
    session.record_review("pass", evidence="reviews/1", passed_criteria=(0, 1))
    session.record_go("outward", evidence="conversation:outward", verbatim="Ship it.")
    return session


def last_two(store):
    return [(stamp["point"], stamp["close"]) for stamp in store.read_card()["stamps"][-2:]]


UNCONFIRMED = "a close stamp was written but never confirmed; the ticket is not closed"
CLOSED_INVALID = "the store is marked closed but the close does not validate: its final stamp is not the confirmed close"


class Interleaves:
    """Probe: a stop lands between the close read-back and the confirm."""

    def confirm_close(self, position, point):
        self.write_stamp("Concurrent stop.", "review")
        super().confirm_close(position, point)


def test_a_stop_between_read_back_and_confirm_is_refused(tmp_path):
    store = concrete_store(tmp_path, Interleaves)
    session = approved_for_close(store)
    with pytest.raises(ticket.StoreUnavailable, match="no longer the pending close"):
        session.close("Done.", refs=("example/repo#14",))
    assert session.phase == "outward_go"
    assert store.read_card()["status"] == "open"
    assert last_two(store) == [("Done.", False), ("Concurrent stop.", False)]

    resumed = resume(store)
    assert resumed.phase == "outward_go"
    assert UNCONFIRMED in resumed.reconciliation


class MarkdownClosesWithoutMark:
    """Confirm returns cleanly but stores only the closed status."""

    def confirm_close(self, position, point):
        card = self._load()
        card["status"] = "closed"
        self._save(card)


def test_a_confirm_whose_read_back_fails_does_not_resume_as_closed(tmp_path):
    store = concrete_store(tmp_path, MarkdownClosesWithoutMark)
    session = approved_for_close(store)
    with pytest.raises(ticket.StoreUnavailable, match="not marked closed"):
        session.close("Done.", refs=("example/repo#14",))
    assert session.phase == "outward_go"
    assert store.read_card()["status"] == "closed"

    resumed = resume(store)
    assert resumed.phase == "outward_go"
    assert CLOSED_INVALID + "; the ticket is not closed" in resumed.reconciliation
    assert UNCONFIRMED in resumed.reconciliation


def test_a_closed_store_with_a_later_stop_does_not_resume_as_closed(tmp_path):
    store = concrete_store(tmp_path)
    session = approved_for_close(store)
    session.close("Done.", refs=("example/repo#14",))
    assert resume(store).phase == "closed"

    # A handle whose pass ended writes nothing more, so the later stop
    # comes from another handle with no pass of its own.
    with pytest.raises(ticket.StoreUnavailable, match="has ended") as ended:
        store.write_stamp("Later stop.", "review", ("ticket",))
    assert ended.value.code == "pass_ended"
    store = fresh_handle(store)
    store.write_stamp("Later stop.", "review", ("ticket",))
    assert store.read_card()["status"] == "closed"
    assert last_two(store) == [("Done.", True), ("Later stop.", False)]
    resumed = resume(store)
    assert resumed.phase == "outward_go"
    assert CLOSED_INVALID + "; the ticket is not closed" in resumed.reconciliation


# The pending-close check, the close confirm's position, and the closed-resume
# checks each get a case where only that one fact is wrong.


def test_confirm_refuses_a_last_stamp_that_is_not_that_pending_close(tmp_path):
    store = concrete_store(tmp_path)
    store.write_stamp("Done.", "close", ("ticket",))
    with pytest.raises(ticket.StoreUnavailable, match="no longer the pending close"):
        store.confirm_close(0, "Another point.")
    assert store.read_card()["status"] == "open"

    store.write_stamp("Ready.", "review", ("ticket",))
    with pytest.raises(ticket.StoreUnavailable, match="no longer the pending close"):
        store.confirm_close(1, "Ready.")
    card = store.read_card()
    assert card["status"] == "open"
    assert [stamp["close"] for stamp in card["stamps"]] == [False, False]


class ConfirmsThenRepeatsTheClose:
    """Confirm marks the close, then the store holds a second confirmed copy after it."""

    def confirm_close(self, position, point):
        super().confirm_close(position, point)
        with self._writing():
            card = self._load()
            card["stamps"].append(dict(card["stamps"][position]))
            self._save(card)


def test_close_refuses_when_the_confirmed_stamp_is_no_longer_at_its_position(tmp_path):
    store = concrete_store(tmp_path, ConfirmsThenRepeatsTheClose)
    session = approved_for_close(store)
    with pytest.raises(ticket.StoreUnavailable, match="not marked closed"):
        session.close("Done.", refs=("example/repo#14",))
    assert session.phase == "outward_go"


def closed_markdown_store(tmp_path):
    store = concrete_store(tmp_path)
    approved_for_close(store).close("Done.", refs=("example/repo#14",))
    assert resume(store).phase == "closed"
    return store


def edit_card(store, change):
    with store._writing():
        card = store._load()
        change(card)
        store._save(card)


def test_a_closed_card_whose_last_review_did_not_pass_does_not_resume_as_closed(tmp_path):
    store = closed_markdown_store(tmp_path)
    edit_card(store, lambda card: card["reviews"][-1].update(verdict="changes"))
    resumed = resume(store)
    assert resumed.phase != "closed"
    flags = "\n".join(resumed.reconciliation)
    assert "the last review round did not pass every criterion to an outward go stop" in flags


def test_a_closed_card_without_its_outward_go_does_not_resume_as_closed(tmp_path):
    store = closed_markdown_store(tmp_path)
    outward = "The outward gate is approved for plan revision 1."
    edit_card(
        store,
        lambda card: card.update(decisions=[item for item in card["decisions"] if item["effect"] != outward]),
    )
    resumed = resume(store)
    assert resumed.phase == "outward_go"
    flags = "\n".join(resumed.reconciliation)
    assert "the outward go decision is missing" in flags


# A separate process holds the ticket's lock file and this process is the
# writer that has to wait.

FIRST_WRITER = r"""
import importlib.util
import os
import select
import sys

spec = importlib.util.spec_from_file_location("ticket_first_writer", sys.argv[1])
ticket = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ticket
spec.loader.exec_module(ticket)
mode, key, title, where = sys.argv[2:6]
store = ticket.MarkdownStore(where, key, title)
if mode == "hold":
    with store._writing():
        print("locked", flush=True)
        # Hold until told to release, and never longer than 20 seconds.
        select.select([sys.stdin], [], [], 20)
elif mode == "burst":
    # Land ten stamps; each write waits for the lock when another writer has it.
    for landed in range(10):
        store.write_stamp(f"Burst stamp {landed} from {os.getpid()}.", "build")
    print("burst", flush=True)
else:
    store.write_stamp("Written by the first writer.", "build", ("ticket",))
    print("written", flush=True)
"""


def first_writer(mode, where):
    return subprocess.Popen(
        [sys.executable, "-c", FIRST_WRITER, str(SKILL_DIR / "ticket.py"), mode, KEY, TITLE, str(where)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def leftover_temp_files(repo):
    return sorted(path.name for path in (repo / ".ticket").iterdir() if path.name.endswith(".tmp"))


def test_markdown_write_waits_for_the_lock_file_and_lands_after_release(tmp_path):
    repo = init_ignored_repo(tmp_path)
    store = ticket.MarkdownStore(repo, KEY, TITLE)
    store.write_stamp("Written before the lock was held.", "build", ("ticket",))
    assert store.lock_path == repo / ".ticket" / f"{KEY}.lock"
    card_before = store.path.read_bytes()
    lock = os.open(store.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    writer = first_writer("write", repo)
    try:
        # The writer is running and blocked: it has not written while the lock is held.
        time.sleep(1.0)
        assert writer.poll() is None
        assert store.path.read_bytes() == card_before
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
    out, err = writer.communicate(timeout=30)
    assert (writer.returncode, out.splitlines()[-1:]) == (0, ["written"]), err
    assert [stamp["point"] for stamp in store.read_card()["stamps"]] == [
        "Written before the lock was held.",
        "Written by the first writer.",
    ]
    assert leftover_temp_files(repo) == []


def test_markdown_close_confirm_waits_for_the_lock_and_rechecks_the_last_stamp(tmp_path):
    repo = init_ignored_repo(tmp_path)
    store = ticket.MarkdownStore(repo, KEY, TITLE)
    store.write_stamp("Done.", "close", ("ticket",))
    lock = os.open(store.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    # A stop lands from another process while this confirm is waiting its turn.
    other = first_writer("write", repo)
    time.sleep(0.5)
    fcntl.flock(lock, fcntl.LOCK_UN)
    os.close(lock)
    out, err = other.communicate(timeout=30)
    assert other.returncode == 0, err
    with pytest.raises(ticket.StoreUnavailable, match="no longer the pending close"):
        store.confirm_close(0, "Done.")
    card = store.read_card()
    assert (card["status"], [stamp["close"] for stamp in card["stamps"]]) == ("open", [False, False])


def test_markdown_store_refuses_a_lock_path_that_is_a_symlink(tmp_path):
    repo = init_ignored_repo(tmp_path)
    store = ticket.MarkdownStore(repo, KEY, TITLE)
    (repo / ".ticket").mkdir()
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("must survive\n", encoding="utf-8")
    os.symlink(sentinel, store.lock_path)

    with pytest.raises(ticket.StoreUnavailable, match="cannot open the write lock"):
        store.write_stamp("Never lands.", "build")
    assert sentinel.read_text(encoding="utf-8") == "must survive\n"
    assert not store.path.exists()


def test_markdown_writers_in_parallel_lose_no_write(tmp_path):
    repo = init_ignored_repo(tmp_path)
    writers = [first_writer("burst", repo) for _ in range(6)]
    results = [writer.communicate(timeout=60) for writer in writers]
    assert [writer.returncode for writer in writers] == [0] * 6, [err for _, err in results]
    stamps = ticket.MarkdownStore(repo, KEY, TITLE).read_card()["stamps"]
    assert len(stamps) == 60
    assert len({stamp["point"] for stamp in stamps}) == 60
    assert [out.strip() for out, _ in results] == ["burst"] * 6
    assert leftover_temp_files(repo) == []


def test_markdown_lock_held_by_a_dead_writer_is_released_by_the_system(tmp_path):
    repo = init_ignored_repo(tmp_path)
    holder = first_writer("hold", repo)
    assert holder.stdout.readline().strip() == "locked", holder.stderr.read()
    holder.kill()
    holder.wait(timeout=30)
    store = ticket.MarkdownStore(repo, KEY, TITLE)

    store.write_stamp("Written after the dead writer.", "build")
    assert [stamp["point"] for stamp in store.read_card()["stamps"]] == ["Written after the dead writer."]


class FailsMidWrite:
    """The write raises after the store has taken the ticket."""

    def _save(self, card):
        raise RuntimeError("disk went away mid-write")


def test_markdown_lock_is_released_when_a_write_raises(tmp_path):
    repo = init_ignored_repo(tmp_path)
    failing = type("Store", (FailsMidWrite, ticket.MarkdownStore), {})(repo, KEY, TITLE)
    with pytest.raises(RuntimeError, match="mid-write"):
        failing.write_stamp("Never lands.", "build")
    assert leftover_temp_files(repo) == []

    other = first_writer("write", repo)
    out, err = other.communicate(timeout=30)
    assert (other.returncode, out.splitlines()[-1:]) == (0, ["written"]), err
    store = ticket.MarkdownStore(repo, KEY, TITLE)
    assert [stamp["point"] for stamp in store.read_card()["stamps"]] == ["Written by the first writer."]


# ``make_store`` opens a new store handle on the same card, as a new process
# would; its label names the handle in the test only.


CLAIM_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def claim_of(card, pass_id):
    """The live-session claim on a card: session id, host and started-at, nothing else."""

    claim = card["pass"]
    assert set(claim) == {"id", "host", "started_at"}
    assert claim["id"] == pass_id
    assert claim["host"] and not any(ch.isspace() for ch in claim["host"])
    assert CLAIM_TIME.fullmatch(claim["started_at"])
    return claim


def named(claim):
    return f"session {claim['id']}, host {claim['host']}, started {claim['started_at']}"


def check_a_pass_holds_the_ticket_across_processes(make_store):
    """The reviewers' window: pass A stops at plan go, its process exits, pass B opens.

    Every ``make_store`` call is a new handle, as a new process opens one.
    """

    a_store = make_store("pass-a")
    first = ticket.TicketPass.open(KEY, source(), a_store)
    first.finish_orient()
    first.finish_research()
    first.present_plan(ticket.PlanCard(first.ticket))
    pass_a = first.pass_id
    assert pass_a
    # Process 1 of pass A ends at the plan go stop; its pass record stays.
    del first
    claim = claim_of(make_store("reader").read_card(), pass_a)

    # Pass B opens a fresh handle while A waits for the person's go.
    b_store = make_store("pass-b")
    with pytest.raises(ticket.TicketHeld) as refused:
        ticket.TicketPass.open(KEY, source(), b_store, resume=True)
    assert (refused.value.key, refused.value.holder) == (KEY, named(claim))
    assert f"ticket {KEY} already has a live writer: {named(claim)}" in str(refused.value)
    assert f"python3 ticket.py release {KEY} {pass_a}" in str(refused.value)
    # B's handle, outside any pass, is refused by A's name too.
    with pytest.raises(ticket.TicketHeld) as refused:
        b_store.write_stamp("Plan B, never shown to the person.", "plan")
    assert refused.value.holder == named(claim)
    # So is a pass id that is not the live pass's.
    with pytest.raises(ticket.TicketHeld) as refused:
        ticket.TicketPass.open(KEY, source(), make_store("pass-b"), pass_id="0123456789abcdef")
    assert refused.value.holder == named(claim)
    card = make_store("reader").read_card()
    assert card["pass"] == claim
    assert [stamp["point"] for stamp in card["stamps"]] == ["Waiting at plan go for plan revision 1."]

    # Process 2 of pass A: the person said go, and A continues with its pass id.
    second = ticket.TicketPass.open(KEY, source(), make_store("pass-a"), resume=True, pass_id=pass_a)
    assert second.phase == "plan_go"
    second.record_go("plan", evidence="conversation:plan-go", verbatim="Go with this plan.")
    second.begin_build()
    card = make_store("reader").read_card()
    assert card["plan"]["open_questions"] == []
    assert card["plan"]["pass"] == pass_a
    assert [stamp["point"] for stamp in card["stamps"]] == ["Waiting at plan go for plan revision 1."]
    assert [(item["source"]["ref"], item["effect"]) for item in card["decisions"]] == [
        ("conversation:plan-go", plan_go_effect(1, pass_a))
    ]

    # Only after A ends may B start; it then reads A's approved plan.
    second.end("the test is done with pass A")
    assert make_store("reader").read_card()["pass"] is None
    later = ticket.TicketPass.open(KEY, source(), make_store("pass-b"), resume=True)
    assert later.phase == "build"
    later.end()


def check_the_pass_record_is_released_on_every_exit_path(make_store):
    def live():
        return make_store("reader").read_card()["pass"]

    # Normal exit: a more-information stop ends the pass.
    session = ticket.TicketPass.open(KEY, source(), make_store("asks"))
    session.more_info("person", "Which API owns retries?")
    assert (session.live, live()) == (False, None)

    # A validation refusal changes nothing, so the pass stays live.
    session = ticket.TicketPass.open(KEY, source(), make_store("validates"))
    with pytest.raises(ticket.TicketError):
        session.record_review("pass", evidence="reviews/1")
    assert session.live
    claim_of({"pass": live()}, session.pass_id)
    session.end()
    assert live() is None

    # Error exit: a store call fails mid-pass.
    failing = make_store("fails")
    session = ticket.TicketPass.open(KEY, source(), failing)

    def disk_went_away(*args, **kwargs):
        raise RuntimeError("disk went away mid-write")

    failing.write_stamp = disk_went_away
    with pytest.raises(RuntimeError, match="mid-write"):
        session.more_info("person", "Which API owns retries?")
    assert (session.live, live()) == (False, None)
    del failing.write_stamp
    with pytest.raises(ticket.StoreUnavailable, match="has ended"):
        failing.write_stamp("Written after the pass ended.", "orient")

    # Error exit: an exception leaves a with block over the pass.
    with pytest.raises(RuntimeError, match="left the block"):
        with ticket.TicketPass.open(KEY, source(), make_store("with-block")) as session:
            claim_of({"pass": live()}, session.pass_id)
            raise RuntimeError("left the block")
    assert (session.live, live()) == (False, None)

    # Refusal exit: pass A's record was cleared by a person, as after a
    # crash, and pass B started. A's next write is refused by B's name, A
    # ends, and A's ending leaves B's record alone.
    a = ticket.TicketPass.open(KEY, source(), make_store("pass-a"))
    make_store("person").clear_pass(a.pass_id)
    b = ticket.TicketPass.open(KEY, source(), make_store("pass-b"))
    b_claim = claim_of({"pass": live()}, b.pass_id)
    with pytest.raises(ticket.TicketHeld) as refused:
        a.record_tried("Retry in the client.")
    assert refused.value.holder == named(b_claim)
    assert not a.live
    assert live() == b_claim
    with pytest.raises(ticket.TicketHeld):
        make_store("person").clear_pass(a.pass_id)
    b.end()
    assert live() is None
    assert [item["what"] for item in make_store("reader").read_card()["tried"]] == []


def markdown_harness(tmp_path):
    repo = init_ignored_repo(tmp_path)

    def make_store(writer=None):
        return ticket.MarkdownStore(repo, KEY, TITLE)

    return make_store


def test_markdown_pass_holds_the_ticket_across_processes(tmp_path):
    check_a_pass_holds_the_ticket_across_processes(markdown_harness(tmp_path))


def test_markdown_pass_record_is_released_on_every_exit_path(tmp_path):
    check_the_pass_record_is_released_on_every_exit_path(markdown_harness(tmp_path))


def test_a_plan_go_counts_only_for_the_pass_that_presented_the_plan():
    store = MemoryStore()
    session = to_plan_go(store)
    pass_a = session.pass_id
    assert store.card["plan"]["pass"] == pass_a

    # The person's settled go, naming another pass's plan, opens nothing.
    go = one_fault_go("plan", "none", "0123456789abcdef")
    store.record_decision(dataclasses_replace(go, effect=plan_go_effect(1, "0123456789abcdef")))
    with pytest.raises(ticket.GateRequired):
        session.begin_build()
    assert session.live

    # A go is not recorded against a stored plan this pass did not present.
    store.card["plan"]["pass"] = "0123456789abcdef"
    with pytest.raises(ticket.StoreUnavailable, match="no plan go was recorded") as refused:
        session.record_go("plan", evidence="conversation:plan-go", verbatim="Go.")
    assert refused.value.code == "plan_changed"
    assert not session.live
    assert [item["source"]["ref"] for item in store.card["decisions"]] == ["conversation:go"]
    store.card["plan"]["pass"] = pass_a

    # Pass A crashed at plan go and a person cleared it. Pass C resumes at
    # plan, not at plan go, so the person's go always follows the plan the
    # pass that asked for it presented.
    store.card["pass"] = {"id": pass_a, "host": "crashed-host", "started_at": "2026-10-18T00:00:00Z"}
    store.clear_pass(pass_a)
    resumed = resume(store)
    assert resumed.phase == "plan"
    assert (
        "plan revision 1 was presented by another pass and has no plan go; resuming at plan to present it again"
        in resumed.reconciliation
    )
    resumed.present_plan(ticket.PlanCard(resumed.ticket))
    resumed.record_go("plan", evidence="conversation:plan-go", verbatim="Go.")
    resumed.begin_build()
    assert store.card["decisions"][-1]["effect"] == plan_go_effect(2, resumed.pass_id)


def release_command(key, pass_id, *extra, env=None):
    # The caller's own environment must not select a store for the test.
    base = {name: value for name, value in os.environ.items() if name != ticket.LEDGER_SELECTOR}
    return subprocess.run(
        [sys.executable, str(SKILL_DIR / "ticket.py"), "release", key, pass_id, *extra],
        capture_output=True,
        text=True,
        env={**base, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})},
    )


def test_a_person_clears_a_crashed_pass_with_the_documented_command(tmp_path):
    make_store = markdown_harness(tmp_path)
    extra, env = ("--repo", str(make_store().repo)), {}
    crashed = ticket.TicketPass.open(KEY, source(), make_store("crashed-pass"))
    pass_id = crashed.pass_id
    del crashed  # its process died without ending the pass
    claim = claim_of(make_store().read_card(), pass_id)

    with pytest.raises(ticket.TicketHeld) as refused:
        ticket.TicketPass.open(KEY, source(), make_store("next-pass"), resume=True)
    assert refused.value.holder == named(claim)

    wrong = release_command(KEY, "0123456789abcdef", *extra, env=env)
    assert wrong.returncode == 1
    assert named(claim) in wrong.stderr
    assert make_store().read_card()["pass"] == claim

    cleared = release_command(KEY, pass_id, *extra, env=env)
    assert (cleared.returncode, cleared.stdout.strip()) == (0, f"released session {pass_id} on ticket {KEY}"), cleared.stderr
    assert make_store().read_card()["pass"] is None
    nxt = ticket.TicketPass.open(KEY, source(), make_store("next-pass"), resume=True)
    assert nxt.live
    nxt.end()


def test_choose_store_returns_the_markdown_store(tmp_path, monkeypatch):
    monkeypatch.delenv(ticket.LEDGER_SELECTOR, raising=False)
    repo = init_ignored_repo(tmp_path)
    store = ticket.choose_store(repo, KEY, TITLE)
    assert isinstance(store, ticket.MarkdownStore)
    assert store.path == repo.resolve() / ".ticket" / f"{KEY}.md"


def test_choose_store_refuses_a_ledger_selection_and_writes_nothing(tmp_path, monkeypatch):
    repo = init_ignored_repo(tmp_path)
    monkeypatch.setenv(ticket.LEDGER_SELECTOR, str(tmp_path / "ledger"))
    with pytest.raises(ticket.LedgerUnsupported) as refused:
        ticket.choose_store(repo, KEY, TITLE)
    assert str(refused.value) == "the ledger store is not supported yet; use the markdown store"
    assert refused.value.code == "ledger_unsupported"
    assert sorted(path.name for path in repo.iterdir()) == [".git", ".gitignore"]


def test_release_with_the_ledger_selected_exits_2_with_one_line_and_writes_nothing(tmp_path):
    repo = init_ignored_repo(tmp_path)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    selected = release_command(
        KEY, "0123456789abcdef", "--repo", str(repo), env={ticket.LEDGER_SELECTOR: str(tmp_path / "ledger")}
    )
    assert selected.returncode == 2, selected.stderr
    assert selected.stderr == "ticket: the ledger store is not supported yet; use the markdown store\n"
    assert selected.stdout == ""
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before


# Two shapes that hide a literal from a text search: an upper-case environment
# name glued from quoted pieces, and a filesystem path assembled from parts.
_UPPER_CONCAT = re.compile(r"""(["'])[A-Z][A-Z0-9_]*\1\s*\+\s*(["'])[A-Z0-9_]+\2""")
_PATH_ASSEMBLY = re.compile(r"""Path\(\s*(["'])/\1\s*\)|(["'])/\2\s*(\+|\.join\()|\+\s*(["'])/\4""")


def disguised_lines(text):
    return [line for line in text.splitlines() if _UPPER_CONCAT.search(line) or _PATH_ASSEMBLY.search(line)]


def test_detector_catches_both_shapes():
    # \x22 is a double quote; written escaped so this file does not match itself.
    assert disguised_lines("env[\x22FOO_\x22 + \x22BAR\x22] = 1")
    assert disguised_lines("Path(\x22/\x22).joinpath(\x22home\x22, \x22me\x22)")
    assert disguised_lines("root = \x22/\x22 + \x22home\x22")
    assert disguised_lines("\x22/\x22.join((\x22\x22, \x22home\x22))")
    assert not disguised_lines("\x22 / \x22.join(lines)")


def test_skill_tree_assembles_no_hidden_paths_or_environment_names():
    offenders = []
    for path in sorted(SKILL_DIR.rglob("*")):
        if "__pycache__" in path.parts or path.suffix not in (".py", ".md", ".json", ".sh"):
            continue
        for line in disguised_lines(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(SKILL_DIR)}: {line.strip()}")
    assert offenders == []


# Comment markers and entities in recorded text stay inside the card's one
# machine comment, and read back exactly as they were recorded.

MARKERS = "ends here --> starts here <!-- and & keeps its ampersand &amp; <b>"


def test_recorded_comment_markers_leave_one_comment_block_and_round_trip(tmp_path):
    repo = init_ignored_repo(tmp_path)
    store = ticket.MarkdownStore(repo, KEY, TITLE)
    store.record_decision(
        ticket.DecisionRecord(
            "the person",
            "person",
            "settled",
            ticket.DecisionSource("ratification", "conversation:7"),
            MARKERS,
            ({"type": "ticket", "id": KEY},),
            MARKERS,
            "plan",
        )
    )
    store.record_tried(ticket.TriedRecord(MARKERS, "build", MARKERS))
    body = store.path.read_text(encoding="utf-8")
    assert body.count("<!--") == 1
    assert body.count("-->") == 1
    assert body.rstrip("\n").endswith("-->")
    card = ticket.MarkdownStore(repo, KEY, TITLE).read_card()
    assert (card["decisions"][0]["verbatim"], card["decisions"][0]["effect"]) == (MARKERS, MARKERS)
    assert card["tried"] == [{"what": MARKERS, "phase": "build", "outcome": MARKERS}]


# One validator decides whether a card may close: before the close stamp is
# written, after it is read back, and after the close is confirmed.

NOT_PASSED = "the last review round did not pass every criterion to an outward go stop"


def final_review_to_changes(card):
    card["reviews"][-1]["verdict"] = "changes"


def test_close_refuses_a_card_whose_final_review_is_changes_and_writes_no_close(tmp_path):
    store = concrete_store(tmp_path)
    session = approved_for_close(store)
    edit_card(store, final_review_to_changes)
    before = store.read_card()
    with pytest.raises(ticket.StoreUnavailable, match=NOT_PASSED):
        session.close("Done.", refs=("example/repo#14",))
    after = store.read_card()
    # The refusal ends the pass, which releases its claim; nothing else changes.
    assert after == {**before, "pass": None}
    assert after["status"] == "open"
    assert [stamp["phase"] for stamp in after["stamps"]] == ["plan", "review"]


class FlipsTheFinalReviewWithTheCloseStamp:
    """The store changes the final review while it writes the pending close stamp."""

    def write_stamp(self, point, phase, next_skills=(), refs=()):
        super().write_stamp(point, phase, next_skills, refs)
        if phase == "close":
            edit_card(self, final_review_to_changes)


class FlipsTheFinalReviewWithTheConfirm:
    """The store changes the final review while it confirms the close."""

    def confirm_close(self, position, point):
        super().confirm_close(position, point)
        edit_card(self, final_review_to_changes)


def test_close_read_back_refuses_a_final_review_that_changed_before_the_confirm(tmp_path):
    store = concrete_store(tmp_path, FlipsTheFinalReviewWithTheCloseStamp)
    session = approved_for_close(store)
    with pytest.raises(ticket.StoreUnavailable, match="close read-back failed: .*" + NOT_PASSED):
        session.close("Done.", refs=("example/repo#14",))
    card = store.read_card()
    assert card["status"] == "open"
    assert card["stamps"][-1]["close"] is False


def test_close_final_read_back_refuses_a_final_review_that_changed_with_the_confirm(tmp_path):
    store = concrete_store(tmp_path, FlipsTheFinalReviewWithTheConfirm)
    session = approved_for_close(store)
    with pytest.raises(ticket.StoreUnavailable, match="close read-back failed: .*" + NOT_PASSED):
        session.close("Done.", refs=("example/repo#14",))
    assert session.phase == "outward_go"
    assert resume(store).phase != "closed"


# Three recorded reviews stop the pass only once the cap stop stamp is stored.

CAP_POINT = "Review did not converge after round three."


class LosesTheCapStamp:
    """The process dies after round three is stored and before its cap stop stamp."""

    lose = True

    def write_stamp(self, point, phase, next_skills=(), refs=()):
        if self.lose and point == CAP_POINT:
            raise RuntimeError("the process died before the cap stop stamp")
        super().write_stamp(point, phase, next_skills, refs)


def test_a_lost_cap_stop_stamp_resumes_retryable_until_the_stamp_is_written(tmp_path):
    store = concrete_store(tmp_path, LosesTheCapStamp)
    session = to_build(store)
    session.record_review("changes", evidence="reviews/1")
    session.record_review("changes", evidence="reviews/2")
    with pytest.raises(RuntimeError, match="before the cap stop stamp"):
        session.record_review("changes", evidence="reviews/3")
    card = store.read_card()
    assert len(card["reviews"]) == 3
    assert CAP_POINT not in [stamp["point"] for stamp in card["stamps"]]
    assert card["pass"] is None

    # Resume reports the missing stop and keeps the pass live to write it.
    store.lose = False
    resumed = resume(store)
    assert resumed.phase == "cap_stop"
    assert resumed.live
    assert any("cap stop not recorded" in line for line in resumed.reconciliation)
    # Writing that stamp is the only action: a fourth review and other stops are refused.
    with pytest.raises(ticket.TicketError):
        resumed.record_review("changes", evidence="reviews/4")
    with pytest.raises(ticket.TicketError):
        resumed.more_info("person", "Which API owns retries?")
    with pytest.raises(ticket.TicketError):
        resumed.record_tried("Retry in the client.")
    assert len(store.read_card()["reviews"]) == 3

    resumed.record_cap_stop()
    assert (resumed.phase, resumed.live) == ("stopped", False)
    stamps = store.read_card()["stamps"]
    assert (stamps[-1]["point"], stamps[-1]["phase"]) == (CAP_POINT, "review")
    assert stamps[-1]["next_skills"] == ["ticket", "review"]
    assert stamps[-1]["refs"] == [".ticket/EK-14.md", "reviews/3"]
    again = resume(store)
    assert (again.phase, again.reconciliation) == ("stopped", ())


def test_record_cap_stop_is_refused_outside_the_cap_stop_state():
    session = to_build(MemoryStore())
    with pytest.raises(ticket.TicketError, match="expected phase cap_stop"):
        session.record_cap_stop()


# A passing review reaches outward go only once its outward stop stamp is
# stored. Each crash window inside a passing review resumes to write it.

OUTWARD_POINT = "Waiting at outward go after review round {}."


class LosesTheOutwardStamp:
    """The process dies after a passing round is stored and before its outward stop stamp."""

    lose = True

    def write_stamp(self, point, phase, next_skills=(), refs=()):
        if self.lose and point.startswith("Waiting at outward go"):
            raise RuntimeError("the process died before the outward stop stamp")
        super().write_stamp(point, phase, next_skills, refs)


class LosesThePlanSnapshotAfterAPassingReview:
    """The process dies after a passing round is stored and before the plan snapshot that marks it."""

    lose = False

    def record_plan(self, plan):
        if self.lose:
            raise RuntimeError("the process died before the plan snapshot")
        return super().record_plan(plan)


def check_the_outward_stop_retry(store, resumed, review_round, evidence):
    """The resumed pass may only write the outward stop, then continues to close."""

    assert resumed.phase == "outward_stop"
    assert resumed.live
    assert any(
        line.startswith(f"outward stop not recorded: review round {review_round}") for line in resumed.reconciliation
    )
    with pytest.raises(ticket.TicketError):
        resumed.record_review("pass", evidence="reviews/again", passed_criteria=(0, 1))
    with pytest.raises(ticket.TicketError):
        resumed.record_go("outward", evidence="conversation:outward", verbatim="Ship it.")
    with pytest.raises(ticket.TicketError):
        resumed.more_info("person", "Which API owns retries?")
    with pytest.raises(ticket.TicketError):
        resumed.record_tried("Retry in the client.")
    with pytest.raises(ticket.TicketError):
        resumed.record_cap_stop()
    assert len(store.read_card()["reviews"]) == review_round

    resumed.record_outward_stop()
    assert (resumed.phase, resumed.live) == ("outward_go", True)
    card = store.read_card()
    stamp = card["stamps"][-1]
    assert (stamp["point"], stamp["phase"], stamp["next_skills"]) == (OUTWARD_POINT.format(review_round), "review", ["ticket"])
    assert stamp["refs"] == [".ticket/EK-14.md", evidence]
    assert [(item["passes"], item["review_round"], item["review_evidence"]) for item in card["plan"]["acceptance_criteria"]] == [
        (True, review_round, evidence)
    ] * 2
    resumed.record_go("outward", evidence="conversation:outward", verbatim="Ship it.")
    resumed.close("Done.", refs=("example/repo#14",))
    assert resume(store).phase == "closed"


def test_a_lost_outward_stamp_after_a_passing_round_three_resumes_to_write_it(tmp_path):
    store = concrete_store(tmp_path, LosesTheOutwardStamp)
    session = to_build(store)
    session.record_review("changes", evidence="reviews/1")
    session.record_review("changes", evidence="reviews/2")
    with pytest.raises(RuntimeError, match="before the outward stop stamp"):
        session.record_review("pass", evidence="reviews/3", passed_criteria=(0, 1))
    assert store.read_card()["pass"] is None
    store.lose = False
    check_the_outward_stop_retry(store, resume(store), 3, "reviews/3")


def test_a_lost_plan_snapshot_after_a_passing_review_resumes_to_write_the_outward_stop(tmp_path):
    store = concrete_store(tmp_path, LosesThePlanSnapshotAfterAPassingReview)
    session = to_build(store)
    store.lose = True
    with pytest.raises(RuntimeError, match="before the plan snapshot"):
        session.record_review("pass", evidence="reviews/1", passed_criteria=(0, 1))
    card = store.read_card()
    assert [review["verdict"] for review in card["reviews"]] == ["pass"]
    assert [item["passes"] for item in card["plan"]["acceptance_criteria"]] == [False, False]
    store.lose = False
    check_the_outward_stop_retry(store, resume(store), 1, "reviews/1")


def test_a_stamped_passing_round_three_the_plan_card_lost_is_not_a_cap_stop():
    store = MemoryStore()
    session = to_build(store)
    session.record_review("changes", evidence="reviews/1")
    session.record_review("changes", evidence="reviews/2")
    session.record_review("pass", evidence="reviews/3", passed_criteria=(0, 1))
    session.end()
    # Only an edit can leave the stamped passing round off the plan card.
    for item in store.card["plan"]["acceptance_criteria"]:
        item.update(passes=False, review_evidence=None, review_round=None)
    resumed = resume(store)
    assert resumed.phase == "stopped"
    assert any("a person reconciles the card" in line for line in resumed.reconciliation)
    assert CAP_POINT not in [stamp["point"] for stamp in store.card["stamps"]]


def test_record_outward_stop_is_refused_outside_the_outward_stop_state():
    session = to_build(MemoryStore())
    with pytest.raises(ticket.TicketError, match="expected phase outward_stop"):
        session.record_outward_stop()


def test_an_old_plans_passing_round_does_not_finish_a_new_plan(tmp_path):
    # Round 1 passes on plan revision 1 and its outward stop stamp is lost.
    store = concrete_store(tmp_path, LosesTheOutwardStamp)
    session = to_build(store)
    with pytest.raises(RuntimeError, match="before the outward stop stamp"):
        session.record_review("pass", evidence="reviews/1", passed_criteria=(0, 1))
    assert store.read_card()["reviews"][0]["plan_revision"] == 1
    store.lose = False

    # The ticket's criteria change, so the next pass plans again and gets a plan go.
    changed = dataclasses_replace(
        fixture_ticket(), acceptance_criteria=(*fixture_ticket().acceptance_criteria[:1], "Retries are logged.")
    )
    replanned = resume(store, changed)
    assert replanned.phase == "plan"
    replanned.present_plan(ticket.PlanCard(replanned.ticket))
    replanned.record_go("plan", evidence="conversation:plan-go-2", verbatim="Go with plan two.")

    # A later process of that pass resumes at build: round 1 reviewed plan revision 1, not 2.
    later = resume(store, changed, pass_id=replanned.pass_id)
    assert (later.plan_revision, later.phase) == (2, "build")
    with pytest.raises(ticket.TicketError, match="expected phase outward_stop"):
        later.record_outward_stop()
    assert [item.passes for item in later.plan.acceptance_criteria] == [False, False]
    later.record_review("pass", evidence="reviews/2", passed_criteria=(0, 1))
    assert later.phase == "outward_go"
    assert store.read_card()["reviews"][-1]["plan_revision"] == 2


def test_record_outward_stop_refuses_a_stored_round_whose_evidence_changed(tmp_path):
    store = concrete_store(tmp_path, LosesThePlanSnapshotAfterAPassingReview)
    session = to_build(store)
    store.lose = True
    with pytest.raises(RuntimeError, match="before the plan snapshot"):
        session.record_review("pass", evidence="reviews/1", passed_criteria=(0, 1))
    store.lose = False
    resumed = resume(store)
    assert resumed.phase == "outward_stop"
    # The stored round no longer carries the evidence the resume read.
    edit_card(store, lambda card: card["reviews"][-1].update(evidence="reviews/other"))
    with pytest.raises(ticket.StoreUnavailable, match="does not carry this evidence"):
        resumed.record_outward_stop()
    card = store.read_card()
    assert not any(stamp["point"].startswith("Waiting at outward go") for stamp in card["stamps"])
    assert [item["passes"] for item in card["plan"]["acceptance_criteria"]] == [False, False]


def drop_outward_stamp(card):
    card["stamps"] = [stamp for stamp in card["stamps"] if not stamp["point"].startswith("Waiting at outward go")]


def test_a_card_without_its_outward_stop_stamp_neither_closes_nor_resumes_closed(tmp_path):
    # Close refuses, writing no close stamp.
    store = concrete_store(tmp_path)
    session = approved_for_close(store)
    edit_card(store, drop_outward_stamp)
    with pytest.raises(ticket.StoreUnavailable, match=NOT_PASSED):
        session.close("Done.", refs=("example/repo#14",))
    assert [stamp["phase"] for stamp in store.read_card()["stamps"]] == ["plan"]

    # A closed card whose outward stop stamp was edited out does not resume as closed.
    (tmp_path / "closed").mkdir()
    closed = closed_markdown_store(tmp_path / "closed")
    edit_card(closed, drop_outward_stamp)
    resumed = resume(closed)
    assert resumed.phase != "closed"
    assert any(NOT_PASSED in line for line in resumed.reconciliation)


def unreasoned_skip(plan):
    plan.steps[1].status = "skipped"


def plan_skip_without_diff(plan):
    plan.steps[2].status, plan.steps[2].skip = "skipped", "one line"


def plan_skip_with_two_sentence_diff(plan):
    plan.steps[2].status, plan.steps[2].skip = "skipped", "one line"
    plan.diff_statement = "Change one line. Then another."


def unknown_status(plan):
    plan.steps[0].status = "done"


def missing_step(plan):
    plan.steps.pop()


def reordered_steps(plan):
    plan.steps.reverse()


def reason_on_a_step_not_skipped(plan):
    plan.steps[3].skip = "not skipped"


@pytest.mark.parametrize(
    "fault",
    (
        unreasoned_skip,
        plan_skip_without_diff,
        plan_skip_with_two_sentence_diff,
        unknown_status,
        missing_step,
        reordered_steps,
        reason_on_a_step_not_skipped,
    ),
)
def test_a_plan_card_that_breaks_the_step_rules_is_not_persisted(fault):
    store = MemoryStore()
    session = ticket.TicketPass.open(KEY, source(), store)
    session.finish_orient()
    session.finish_research()
    plan = ticket.PlanCard(session.ticket)
    fault(plan)
    with pytest.raises(ticket.TicketError):
        session.present_plan(plan)
    assert (store.card["plan"], store.card["stamps"], session.phase, session.live) == (None, [], "plan", True)


def test_the_store_refuses_a_plan_snapshot_step_with_an_unknown_field():
    store = MemoryStore()
    plan = ticket.PlanCard(fixture_ticket()).as_dict()
    plan["revision"] = 1
    plan["steps"][0]["owner"] = "someone"
    with pytest.raises(ticket.TicketError, match="unknown fields"):
        store.record_plan(plan)
    assert store.card is None


def test_a_plan_card_that_keeps_the_step_rules_is_persisted():
    store = MemoryStore()
    session = ticket.TicketPass.open(KEY, source(), store)
    session.finish_orient()
    session.finish_research()
    plan = ticket.PlanCard(session.ticket)
    plan.skip_step("research", "the ticket names the file")
    plan.skip_step("plan", "a one-line change", diff_statement="Rename the retry flag.")
    session.present_plan(plan)
    stored = {step["name"]: step for step in store.card["plan"]["steps"]}
    assert stored["plan"] == {"name": "plan", "status": "skipped", "skip": "a one-line change"}
    assert store.card["plan"]["diff_statement"] == "Rename the retry flag."


class PausesInsideTheFirstSave:
    """The first save on this handle waits, holding the write, while another thread writes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inside = threading.Event()
        self.paused = False

    def _save(self, card):
        if not self.paused:
            self.paused = True
            self.inside.set()
            time.sleep(0.5)
        super()._save(card)


def test_two_threads_on_one_store_handle_lose_no_write(tmp_path):
    store = concrete_store(tmp_path, PausesInsideTheFirstSave)
    errors = []

    def write(point):
        try:
            store.write_stamp(point, "build")
        except Exception as exc:  # reported below
            errors.append(exc)

    first = threading.Thread(target=write, args=("Written by thread one.",))
    first.start()
    assert store.inside.wait(10)
    second = threading.Thread(target=write, args=("Written by thread two.",))
    second.start()
    first.join(10)
    second.join(10)
    assert errors == []
    assert sorted(stamp["point"] for stamp in store.read_card()["stamps"]) == [
        "Written by thread one.",
        "Written by thread two.",
    ]

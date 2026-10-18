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
import time
from dataclasses import replace as dataclasses_replace
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
FIXTURE = SKILL_DIR / "tests" / "fixtures" / "ticket.json"
FAKE_LEDGER = SKILL_DIR / "tests" / "fixtures" / "fake_ledger.py"
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
    # Sol probe: the plan snapshot is stored but the plan go stamp is refused.
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

    # Sol probe: a passing review is stored but the outward go stamp is refused.
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
    assert (session.phase, resumed.phase) == ("build", "build")
    assert "no outward go stop stamp for review round 1; resuming at build" in resumed.reconciliation
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

    # Sol probe: a caller-supplied go record and a direct phase assignment.
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

    # Sol resume probe: an edited card with an invented approval rebuilds nothing.
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


def fake_ledger(tmp_path, monkeypatch, **env):
    fake = tmp_path / "fake-ledger"
    fake.write_bytes(FAKE_LEDGER.read_bytes())
    fake.chmod(0o755)
    monkeypatch.setenv("FAKE_LEDGER_LOG", str(tmp_path / "argv.jsonl"))
    monkeypatch.setenv("FAKE_LEDGER_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("FAKE_LEDGER_KEY", KEY)
    monkeypatch.setenv("FAKE_LEDGER_TITLE", TITLE)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return fake


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


def test_markdown_and_fake_ledger_adapters_produce_equivalent_cards(tmp_path, monkeypatch):
    repo = init_ignored_repo(tmp_path)
    markdown = ticket.MarkdownStore(repo, KEY, TITLE)
    markdown_card = exercise_store(markdown)
    assert markdown.path == repo / ".ticket" / "EK-14.md"
    assert "# EK-14 — Add guarded ticket pass" in markdown.path.read_text(encoding="utf-8")

    fake = fake_ledger(tmp_path, monkeypatch)
    ledger = ticket.LedgerStore(KEY, TITLE, cli=fake, db=tmp_path / "ledger.db")
    assert ledger.is_available()
    ledger_card = exercise_store(ledger)
    assert ledger_card == markdown_card
    assert ledger_card["plan"]["revision"] == 1
    assert ledger_card["tried"] == [{"what": "Retry in the client.", "phase": "build", "outcome": "Too chatty."}]

    argv = [json.loads(line) for line in (tmp_path / "argv.jsonl").read_text(encoding="utf-8").splitlines()]
    for expected in ("doctor", "item.stopped", "decision.recorded", "review.recorded", "item.note", "item.tried", "card", "export-item"):
        assert any(expected in call for call in argv), expected


def test_full_pass_through_markdown_and_fake_ledger_matches(tmp_path, monkeypatch):
    markdown_card = run_full_pass(ticket.MarkdownStore(init_ignored_repo(tmp_path), KEY, TITLE))
    fake = fake_ledger(tmp_path, monkeypatch)
    ledger_card = run_full_pass(ticket.LedgerStore(KEY, TITLE, cli=fake, db=tmp_path / "ledger.db"))
    for card in (markdown_card, ledger_card):
        card.pop("plan_ref")
        for stamp in card["stamps"]:
            stamp["refs"] = [ref for ref in stamp["refs"] if not ref.startswith((".ticket/", "event:"))]
        # Each run has its own pass id; the plan and its plan go both name it.
        pass_id = card["plan"].pop("pass")
        assert card["decisions"][0]["effect"] == plan_go_effect(1, pass_id)
        card["decisions"][0]["effect"] = "plan go"
    assert ledger_card == markdown_card
    assert markdown_card["status"] == "closed"
    assert markdown_card["pass"] is None


@pytest.mark.parametrize("kind", ("markdown", "ledger"))
def test_a_failed_close_is_not_closed_in_the_store_or_on_resume(kind, tmp_path, monkeypatch):
    # Sol probe: the store drops the close stamp's references.
    if kind == "markdown":

        class Lossy(ticket.MarkdownStore):
            def write_stamp(self, point, phase, next_skills=(), refs=()):
                super().write_stamp(point, phase, next_skills, () if phase == "close" else refs)

        store = Lossy(init_ignored_repo(tmp_path), KEY, TITLE)
    else:

        class Lossy(ticket.LedgerStore):
            def write_stamp(self, point, phase, next_skills=(), refs=()):
                super().write_stamp(point, phase, next_skills, () if phase == "close" else refs)

        store = Lossy(KEY, TITLE, cli=fake_ledger(tmp_path, monkeypatch), db=tmp_path / "ledger.db")
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


def concrete_store(kind, tmp_path, monkeypatch, *mixins):
    if kind == "markdown":
        cls = type("Store", (*mixins, ticket.MarkdownStore), {})
        return cls(init_ignored_repo(tmp_path), KEY, TITLE)
    cls = type("Store", (*mixins, ticket.LedgerStore), {})
    return cls(KEY, TITLE, cli=fake_ledger(tmp_path, monkeypatch), db=tmp_path / "ledger.db")


def fresh_handle(store, writer=None):
    """Another handle on the same ticket and store, as another process would open."""

    if isinstance(store, ticket.MarkdownStore):
        return ticket.MarkdownStore(store.repo, KEY, TITLE, writer=writer)
    return ticket.LedgerStore(KEY, TITLE, cli=store.cli, db=store.db, writer=writer)


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
    """Sol probe: a stop lands between the close read-back and the confirm."""

    def confirm_close(self, position, point):
        self.write_stamp("Concurrent stop.", "review")
        super().confirm_close(position, point)


@pytest.mark.parametrize("kind", ("markdown", "ledger"))
def test_a_stop_between_read_back_and_confirm_is_refused(kind, tmp_path, monkeypatch):
    store = concrete_store(kind, tmp_path, monkeypatch, Interleaves)
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


class LedgerClosesWithoutMark:
    """Confirm returns cleanly but never names the stop it closed."""

    def confirm_close(self, position, point):
        self._run("state", self.key, "--status", "closed")


@pytest.mark.parametrize(
    "kind, mixin", (("markdown", MarkdownClosesWithoutMark), ("ledger", LedgerClosesWithoutMark))
)
def test_a_confirm_whose_read_back_fails_does_not_resume_as_closed(kind, mixin, tmp_path, monkeypatch):
    store = concrete_store(kind, tmp_path, monkeypatch, mixin)
    session = approved_for_close(store)
    with pytest.raises(ticket.StoreUnavailable, match="not marked closed"):
        session.close("Done.", refs=("example/repo#14",))
    assert session.phase == "outward_go"
    assert store.read_card()["status"] == "closed"

    resumed = resume(store)
    assert resumed.phase == "outward_go"
    assert CLOSED_INVALID + "; the ticket is not closed" in resumed.reconciliation
    assert UNCONFIRMED in resumed.reconciliation


@pytest.mark.parametrize("kind", ("markdown", "ledger"))
def test_a_closed_store_with_a_later_stop_does_not_resume_as_closed(kind, tmp_path, monkeypatch):
    store = concrete_store(kind, tmp_path, monkeypatch)
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


@pytest.mark.parametrize("kind", ("markdown", "ledger"))
def test_confirm_refuses_a_last_stamp_that_is_not_that_pending_close(kind, tmp_path, monkeypatch):
    store = concrete_store(kind, tmp_path, monkeypatch)
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


def test_close_refuses_when_the_confirmed_stamp_is_no_longer_at_its_position(tmp_path, monkeypatch):
    store = concrete_store("markdown", tmp_path, monkeypatch, ConfirmsThenRepeatsTheClose)
    session = approved_for_close(store)
    with pytest.raises(ticket.StoreUnavailable, match="not marked closed"):
        session.close("Done.", refs=("example/repo#14",))
    assert session.phase == "outward_go"


def closed_markdown_store(tmp_path, monkeypatch):
    store = concrete_store("markdown", tmp_path, monkeypatch)
    approved_for_close(store).close("Done.", refs=("example/repo#14",))
    assert resume(store).phase == "closed"
    return store


def edit_card(store, change):
    with store._writing():
        card = store._load()
        change(card)
        store._save(card)


def test_a_closed_card_whose_last_review_did_not_pass_does_not_resume_as_closed(tmp_path, monkeypatch):
    store = closed_markdown_store(tmp_path, monkeypatch)
    edit_card(store, lambda card: card["reviews"][-1].update(verdict="changes"))
    resumed = resume(store)
    assert resumed.phase != "closed"
    flags = "\n".join(resumed.reconciliation)
    assert "the last review round did not pass every criterion to an outward go stop" in flags


def test_a_closed_card_without_its_outward_go_does_not_resume_as_closed(tmp_path, monkeypatch):
    store = closed_markdown_store(tmp_path, monkeypatch)
    outward = "The outward gate is approved for plan revision 1."
    edit_card(
        store,
        lambda card: card.update(decisions=[item for item in card["decisions"] if item["effect"] != outward]),
    )
    resumed = resume(store)
    assert resumed.phase == "outward_go"
    flags = "\n".join(resumed.reconciliation)
    assert "the outward go decision is missing" in flags


# One live writer per ticket. On the markdown store a separate process holds
# the ticket through the store's own write lock, and this process is the
# second writer. On the ledger store two passes pin the same revision, and
# the one that writes second is refused by the first one's name.

FIRST_WRITER = r"""
import importlib.util
import select
import sys

spec = importlib.util.spec_from_file_location("ticket_first_writer", sys.argv[1])
ticket = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ticket
spec.loader.exec_module(ticket)
mode, kind, key, title, where, db = sys.argv[2:8]
if kind == "markdown":
    store = ticket.MarkdownStore(where, key, title, writer="first-writer")
else:
    store = ticket.LedgerStore(key, title, cli=where, db=db, writer="first-writer")
if mode == "hold":
    with store._writing():
        print(store.holder, flush=True)
        # Hold until told to release, and never longer than 20 seconds.
        select.select([sys.stdin], [], [], 20)
elif mode == "burst":
    # Land ten stamps, retrying each one that another writer refuses.
    landed = refused = 0
    while landed < 10:
        try:
            store.write_stamp(f"Burst stamp {landed} from {store.holder}.", "build")
        except ticket.TicketHeld as held:
            assert held.holder.startswith("first-writer (pid "), held.holder
            refused += 1
        else:
            landed += 1
    print(refused, flush=True)
else:
    store.write_stamp("Written by the first writer.", "build", ("ticket",))
    print(store.holder, flush=True)
    print("written", flush=True)
"""


def first_writer(mode, kind, where, db=""):
    return subprocess.Popen(
        [sys.executable, "-c", FIRST_WRITER, str(SKILL_DIR / "ticket.py"), mode, kind, KEY, TITLE, str(where), str(db)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def lock_dir_entries(repo):
    return sorted(path.name for path in (repo / ".ticket").iterdir() if path.name.endswith((".lock", ".tmp")))


def test_markdown_store_refuses_a_second_live_writer_by_name(tmp_path):
    repo = init_ignored_repo(tmp_path)
    store = ticket.MarkdownStore(repo, KEY, TITLE, writer="second-writer")
    holder = first_writer("hold", "markdown", repo)
    try:
        held_by = holder.stdout.readline().strip()
        assert held_by.startswith("first-writer (pid "), holder.stderr.read()
        started = time.monotonic()
        with pytest.raises(ticket.TicketHeld) as refused:
            store.write_stamp("Written by the second writer.", "build", ("ticket",))
        assert time.monotonic() - started < 10
        assert (refused.value.key, refused.value.holder) == (KEY, held_by)
        assert f"ticket {KEY} already has a live writer: {held_by}" in str(refused.value)
        assert refused.value.code == "held"
        assert store.read_card()["stamps"] == []
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
    assert holder.returncode == 0, holder.stderr.read()

    store.write_stamp("Written by the second writer.", "build", ("ticket",))
    assert [stamp["point"] for stamp in store.read_card()["stamps"]] == ["Written by the second writer."]
    assert lock_dir_entries(repo) == []


def test_a_markdown_close_confirm_is_refused_while_another_writer_holds_the_ticket(tmp_path):
    repo = init_ignored_repo(tmp_path)
    store = ticket.MarkdownStore(repo, KEY, TITLE, writer="second-writer")
    store.write_stamp("Done.", "close", ("ticket",))
    holder = first_writer("hold", "markdown", repo)
    try:
        held_by = holder.stdout.readline().strip()
        assert held_by.startswith("first-writer (pid "), holder.stderr.read()
        with pytest.raises(ticket.TicketHeld) as refused:
            store.confirm_close(0, "Done.")
        assert refused.value.holder == held_by
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
    card = store.read_card()
    assert (card["status"], card["stamps"][-1]["close"]) == ("open", False)
    store.confirm_close(0, "Done.")
    card = store.read_card()
    assert (card["status"], card["stamps"][-1]["close"]) == ("closed", True)


def test_markdown_lock_names_its_holder_from_the_moment_it_is_taken_until_it_is_released(tmp_path, monkeypatch):
    # A contender writes at the two edges of the first writer's hold: right
    # after the lock node is linked into place, and right before the first
    # writer removes it. Both refusals must name the first writer.
    repo = init_ignored_repo(tmp_path)
    first = ticket.MarkdownStore(repo, KEY, TITLE, writer="first-writer")
    contender = ticket.MarkdownStore(repo, KEY, TITLE, writer="contender")
    lock_name = first.lock_path.name
    real_link, real_unlink = os.link, os.unlink
    refusals = {}

    def contend(edge):
        if edge in refusals:
            return
        refusals[edge] = None
        try:
            contender.write_stamp("Written by the contender.", "build")
        except ticket.TicketHeld as refused:
            refusals[edge] = refused.holder

    def link(source, target, **kwargs):
        real_link(source, target, **kwargs)
        if target == lock_name:
            contend("taken")

    def unlink(path, **kwargs):
        if path == lock_name:
            contend("releasing")
        real_unlink(path, **kwargs)

    monkeypatch.setattr(ticket.os, "link", link)
    monkeypatch.setattr(ticket.os, "unlink", unlink)
    first.write_stamp("Written by the first writer.", "build")
    monkeypatch.undo()

    assert refusals == {"taken": first.holder, "releasing": first.holder}
    assert [stamp["point"] for stamp in first.read_card()["stamps"]] == ["Written by the first writer."]
    assert lock_dir_entries(repo) == []


@pytest.mark.parametrize("planted", ("symlink", "hardlink"))
def test_markdown_store_refuses_a_lock_node_it_did_not_make(planted, tmp_path):
    repo = init_ignored_repo(tmp_path)
    store = ticket.MarkdownStore(repo, KEY, TITLE)
    (repo / ".ticket").mkdir()
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("must survive\n", encoding="utf-8")
    if planted == "symlink":
        os.symlink(sentinel, store.lock_path)
    else:
        os.link(sentinel, store.lock_path)

    with pytest.raises(ticket.StoreUnavailable, match="it was not used as a lock and nothing was written") as refused:
        store.write_stamp("Never lands.", "build")
    assert refused.value.code == "unsafe_lock"
    assert sentinel.read_text(encoding="utf-8") == "must survive\n"
    assert not store.path.exists()
    assert os.path.samefile(store.lock_path, sentinel)
    assert lock_dir_entries(repo) == [store.lock_path.name]


def test_markdown_writers_in_parallel_lose_no_write(tmp_path):
    repo = init_ignored_repo(tmp_path)
    writers = [first_writer("burst", "markdown", repo) for _ in range(6)]
    results = [writer.communicate(timeout=60) for writer in writers]
    assert [writer.returncode for writer in writers] == [0] * 6, [err for _, err in results]
    stamps = ticket.MarkdownStore(repo, KEY, TITLE).read_card()["stamps"]
    assert len(stamps) == 60
    assert len({stamp["point"] for stamp in stamps}) == 60
    # The writers did contend: some writes were refused by name and retried.
    assert sum(int(out) for out, _ in results) > 0
    assert lock_dir_entries(repo) == []


def test_markdown_lock_left_by_a_killed_writer_is_replaced(tmp_path):
    repo = init_ignored_repo(tmp_path)
    holder = first_writer("hold", "markdown", repo)
    held_by = holder.stdout.readline().strip()
    assert held_by.startswith("first-writer (pid "), holder.stderr.read()
    holder.kill()
    holder.wait(timeout=30)
    store = ticket.MarkdownStore(repo, KEY, TITLE)
    assert store.lock_path.read_text(encoding="utf-8") == held_by

    store.write_stamp("Written after the killed writer.", "build")
    assert [stamp["point"] for stamp in store.read_card()["stamps"]] == ["Written after the killed writer."]
    assert lock_dir_entries(repo) == []


class FailsMidWrite:
    """The write raises after the store has taken the ticket."""

    def _save(self, card):
        raise RuntimeError("disk went away mid-write")

    def _append(self, kind, payload):
        raise RuntimeError("disk went away mid-write")


def test_markdown_lock_is_released_when_a_write_raises(tmp_path):
    repo = init_ignored_repo(tmp_path)
    failing = type("Store", (FailsMidWrite, ticket.MarkdownStore), {})(repo, KEY, TITLE, writer="failing-writer")
    with pytest.raises(RuntimeError, match="mid-write"):
        failing.write_stamp("Never lands.", "build")
    assert lock_dir_entries(repo) == []

    other = first_writer("write", "markdown", repo)
    out, err = other.communicate(timeout=30)
    assert (other.returncode, out.splitlines()[-1:]) == (0, ["written"]), err
    store = ticket.MarkdownStore(repo, KEY, TITLE)
    assert [stamp["point"] for stamp in store.read_card()["stamps"]] == ["Written by the first writer."]


# The ledger arm runs the same checks on the fake CLI here and on the real CLI
# in the env-gated test below. ``cli`` runs one CLI verb against the database;
# ``make_store`` opens a new store handle with a writer label.


def fake_ledger_harness(tmp_path, monkeypatch):
    fake = fake_ledger(tmp_path, monkeypatch)
    db = tmp_path / "ledger.db"

    def cli(*args):
        result = subprocess.run([str(fake), "--json", "--db", str(db), *args], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)["data"]

    def make_store(writer=None):
        return ticket.LedgerStore(KEY, TITLE, cli=fake, db=db, writer=writer)

    return cli, make_store


def latest_state(cli):
    states = [event["payload"] for event in cli("export-item", KEY)["events"] if event["kind"] == "item.state"]
    return {name: value for name, value in states[-1].items() if name != "source"} if states else None


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
    assert make_store("reader").read_card()["pass"] == {"id": pass_a, "holder": a_store.holder}

    # Pass B opens a fresh handle while A waits for the person's go.
    b_store = make_store("pass-b")
    with pytest.raises(ticket.TicketHeld) as refused:
        ticket.TicketPass.open(KEY, source(), b_store, resume=True)
    assert (refused.value.key, refused.value.holder) == (KEY, a_store.holder)
    assert f"ticket {KEY} already has a live writer: {a_store.holder}" in str(refused.value)
    assert f"Its pass {pass_a} is live" in str(refused.value)
    assert f"python3 ticket.py release-pass {KEY} {pass_a}" in str(refused.value)
    # B's handle, outside any pass, is refused by A's name too.
    with pytest.raises(ticket.TicketHeld) as refused:
        b_store.write_stamp("Plan B, never shown to the person.", "plan")
    assert refused.value.holder == a_store.holder
    # So is a pass id that is not the live pass's.
    with pytest.raises(ticket.TicketHeld) as refused:
        ticket.TicketPass.open(KEY, source(), make_store("pass-b"), pass_id="0123456789abcdef")
    assert refused.value.holder == a_store.holder
    card = make_store("reader").read_card()
    assert card["pass"] == {"id": pass_a, "holder": a_store.holder}
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
    assert live() == {"id": session.pass_id, "holder": session.store.holder}
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
            assert live() == {"id": session.pass_id, "holder": session.store.holder}
            raise RuntimeError("left the block")
    assert (session.live, live()) == (False, None)

    # Refusal exit: pass A's record was cleared by a person, as after a
    # crash, and pass B started. A's next write is refused by B's name, A
    # ends, and A's ending leaves B's record alone.
    a = ticket.TicketPass.open(KEY, source(), make_store("pass-a"))
    make_store("person").clear_pass(a.pass_id)
    b_store = make_store("pass-b")
    b = ticket.TicketPass.open(KEY, source(), b_store)
    with pytest.raises(ticket.TicketHeld) as refused:
        a.record_tried("Retry in the client.")
    assert refused.value.holder == b_store.holder
    assert not a.live
    assert live() == {"id": b.pass_id, "holder": b_store.holder}
    with pytest.raises(ticket.TicketHeld):
        make_store("person").clear_pass(a.pass_id)
    b.end()
    assert live() is None
    assert [item["what"] for item in make_store("reader").read_card()["tried"]] == []


def markdown_harness(tmp_path):
    repo = init_ignored_repo(tmp_path)

    def make_store(writer=None):
        return ticket.MarkdownStore(repo, KEY, TITLE, writer=writer)

    return make_store


def test_markdown_pass_holds_the_ticket_across_processes(tmp_path):
    check_a_pass_holds_the_ticket_across_processes(markdown_harness(tmp_path))


def test_markdown_pass_record_is_released_on_every_exit_path(tmp_path):
    check_the_pass_record_is_released_on_every_exit_path(markdown_harness(tmp_path))


def test_markdown_pass_is_released_when_its_write_is_refused_by_the_lock(tmp_path):
    make_store = markdown_harness(tmp_path)

    class ContendedOnce(ticket.MarkdownStore):
        contend = False

        def _acquire(self, directory):
            if self.contend:
                self.contend = False
                raise ticket.TicketHeld(KEY, "a writer mid-write (pid 1, handle abcdef)")
            return super()._acquire(directory)

    store = ContendedOnce(make_store().repo, KEY, TITLE, writer="contended")
    session = ticket.TicketPass.open(KEY, source(), store)
    store.contend = True
    with pytest.raises(ticket.TicketHeld, match="a writer mid-write"):
        session.more_info("person", "Which API owns retries?")
    assert not session.live
    assert make_store().read_card()["pass"] is None
    assert make_store().read_card()["stamps"] == []


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
    store.card["pass"] = {"id": pass_a, "holder": "crashed"}
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


STALE_DIRECTORY_WRITER = r"""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("ticket_stale_writer", sys.argv[1])
ticket = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ticket
spec.loader.exec_module(ticket)
try:
    ticket.MarkdownStore(sys.argv[2], sys.argv[3], "T", writer="child").write_stamp("x", "build")
except ticket.TicketHeld as held:
    print(held.holder, flush=True)
else:
    print("written", flush=True)
"""


def test_stale_lock_replacement_refuses_while_another_process_holds_the_directory_lock(tmp_path):
    repo = init_ignored_repo(tmp_path)
    store = ticket.MarkdownStore(repo, KEY, TITLE, writer="contender")
    store._ensure_directory()
    store.lock_path.write_text("dead-writer (pid 1, handle abcdef)", encoding="utf-8")
    directory = os.open(store.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fcntl.flock(directory, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        child = subprocess.Popen(
            [sys.executable, "-c", STALE_DIRECTORY_WRITER, str(SKILL_DIR / "ticket.py"), str(repo), KEY],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            out, err = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=10)
            pytest.fail("the writer blocked on the directory lock instead of refusing")
        elapsed = time.monotonic() - started
    finally:
        fcntl.flock(directory, fcntl.LOCK_UN)
        os.close(directory)
    assert child.returncode == 0, err
    assert out.strip() == "another writer replacing the stale lock left by dead-writer (pid 1, handle abcdef)"
    assert elapsed < 5
    assert store.read_card()["stamps"] == []
    store.write_stamp("Written once the directory lock is free.", "build")
    assert [stamp["point"] for stamp in store.read_card()["stamps"]] == ["Written once the directory lock is free."]


def release_command(key, pass_id, *extra, env):
    return subprocess.run(
        [sys.executable, str(SKILL_DIR / "ticket.py"), "release-pass", key, pass_id, *extra],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **env},
    )


@pytest.mark.parametrize("kind", ("markdown", "ledger"))
def test_a_person_clears_a_crashed_pass_with_the_documented_command(kind, tmp_path, monkeypatch):
    if kind == "markdown":
        make_store = markdown_harness(tmp_path)
        extra, env = ("--repo", str(make_store().repo)), {"TICKET_LEDGER_CLI": str(tmp_path / "no-ledger")}
    else:
        fake = fake_ledger(tmp_path, monkeypatch)
        db = tmp_path / "ledger.db"

        def make_store(writer=None):
            return ticket.LedgerStore(KEY, TITLE, cli=fake, db=db, writer=writer)

        extra, env = (), {"TICKET_LEDGER_CLI": str(fake), "LEDGER_DB": str(db)}
    crashed_store = make_store("crashed-pass")
    crashed = ticket.TicketPass.open(KEY, source(), crashed_store)
    pass_id = crashed.pass_id
    del crashed  # its process died without ending the pass

    with pytest.raises(ticket.TicketHeld) as refused:
        ticket.TicketPass.open(KEY, source(), make_store("next-pass"), resume=True)
    assert refused.value.holder == crashed_store.holder

    wrong = release_command(KEY, "0123456789abcdef", *extra, env=env)
    assert wrong.returncode == 1
    assert crashed_store.holder in wrong.stderr
    assert make_store().read_card()["pass"] == {"id": pass_id, "holder": crashed_store.holder}

    cleared = release_command(KEY, pass_id, *extra, env=env)
    assert (cleared.returncode, cleared.stdout.strip()) == (0, f"released pass {pass_id} on ticket {KEY}"), cleared.stderr
    assert make_store().read_card()["pass"] is None
    nxt = ticket.TicketPass.open(KEY, source(), make_store("next-pass"), resume=True)
    assert nxt.live
    nxt.end()


def check_a_writer_that_passed_the_revision_check_is_refused(make_store):
    class RivalWritesAfterTheCheck(ticket.LedgerStore):
        rival = None

        def _run(self, *args):
            if args[0] == "append" and self.rival is not None:
                rival, self.rival = self.rival, None
                rival.write_stamp("Written by the rival.", "plan", ("ticket",))
            return super()._run(*args)

    template = make_store("late-writer")
    late = RivalWritesAfterTheCheck(KEY, TITLE, cli=template.cli, db=template.db, writer="late-writer")
    rival = make_store("rival")
    late.read_card()
    rival.read_card()
    late.rival = rival
    with pytest.raises(ticket.TicketHeld) as refused:
        late.write_stamp("Written by the late writer.", "plan", ("ticket",))
    assert refused.value.holder == rival.holder
    assert [stamp["point"] for stamp in make_store().read_card()["stamps"]] == ["Written by the rival."]


def check_a_stale_view_is_refused_by_the_cli_writer_name(cli, make_store):
    store = make_store("pass-a")
    store.read_card()
    changed = cli("state", KEY, "--status", "blocked", "--blocked-by", "EK-1")
    with pytest.raises(ticket.TicketHeld, match=f"wrote ledger event {changed['event']['id']} after this pass") as refused:
        store.write_stamp("Written from a stale view.", "build")
    assert refused.value.holder.endswith(f"(ledger event {changed['event']['id']})")
    assert make_store().read_card()["stamps"] == []


def check_writes_keep_owner_claim_and_blocked_by(cli, make_store):
    cli("state", KEY, "--status", "blocked", "--owner", "ticket-owner", "--claim", "pr-123", "--blocked-by", "EK-1")
    before = latest_state(cli)
    assert before == {"status": "blocked", "owner": "ticket-owner", "claim": "pr-123", "blocked_by": ["EK-1"]}
    store = make_store("pass-a")
    store.write_stamp("Done.", "close", ("ticket",))
    store.record_tried(ticket.TriedRecord("Retry in the client.", "build"))
    assert latest_state(cli) == before
    store.confirm_close(0, "Done.")
    assert latest_state(cli) == {**before, "status": "closed"}
    card = cli("card", KEY)
    assert (card["status"], card["owner"], card["claim"]) == ("closed", "ticket-owner", "pr-123")
    assert store.read_card()["stamps"][-1]["close"] is True


def check_a_ticket_claimed_for_work_stays_claimed_and_writable(cli, make_store):
    cli("claim", KEY, "--owner", "ticket-owner")
    session = to_build(make_store("ticket-owner"))
    session.record_review("pass", evidence="reviews/1", passed_criteria=(0, 1))
    card = cli("card", KEY)
    assert (card["status"], card["owner"]) == ("claimed", "ticket-owner")
    assert latest_state(cli)["status"] == "claimed"
    assert make_store().read_card()["status"] == "claimed"


def check_a_write_between_the_check_and_the_append_is_refused_by_name(cli, make_store):
    """A ledger state event lands between a write's revision check and its append.

    The CLI cannot make the two one step, so the skill's event still lands;
    the read after the append finds the interleaved event and refuses by name.
    """

    class ExternalRace(ticket.LedgerStore):
        inject = None

        def _run(self, *args):
            if args[0] == "append" and self.inject is not None:
                inject, self.inject = self.inject, None
                inject()
            return super()._run(*args)

    template = make_store()
    store = ExternalRace(KEY, TITLE, cli=template.cli, db=template.db, writer="skill-pass")
    session = ticket.TicketPass.open(KEY, source(), store)
    interleaved = {}
    store.inject = lambda: interleaved.update(cli("state", KEY, "--status", "blocked", "--blocked-by", "EK-99"))
    with pytest.raises(ticket.TicketHeld) as refused:
        session.record_tried("Retry in the client.", outcome="Too chatty.")
    event_id = interleaved["event"]["id"]
    assert refused.value.holder.endswith(f"(ledger event {event_id})")
    message = str(refused.value)
    assert f"wrote ledger event {event_id} between this write's revision check and its append" in message
    assert "still landed after it" in message
    # The refusal ended the pass and released its record; the handle writes nothing more.
    assert not session.live
    assert make_store("reader").read_card()["pass"] is None
    with pytest.raises(ticket.StoreUnavailable, match="has ended"):
        store.write_stamp("Written after the refusal.", "build")
    events = cli("export-item", KEY)["events"]
    tried = [event["id"] for event in events if event["kind"] == "item.tried"]
    assert len(tried) == 1 and tried[0] > event_id
    assert latest_state(cli) == {"status": "blocked", "blocked_by": ["EK-99"]}

    # A handle outside any pass is refused the same way.
    bare = ExternalRace(KEY, TITLE, cli=template.cli, db=template.db, writer="bare")
    bare.read_card()
    bare.inject = lambda: interleaved.update(cli("state", KEY, "--status", "open"))
    with pytest.raises(ticket.TicketHeld, match="between this write's revision check and its append"):
        bare.write_stamp("Must not land silently after the state write.", "build")
    with pytest.raises(ticket.TicketHeld):
        bare.write_stamp("Nor may the next write.", "build")


def test_ledger_pass_holds_the_ticket_across_processes(tmp_path, monkeypatch):
    check_a_pass_holds_the_ticket_across_processes(fake_ledger_harness(tmp_path, monkeypatch)[1])


def test_ledger_pass_record_is_released_on_every_exit_path(tmp_path, monkeypatch):
    check_the_pass_record_is_released_on_every_exit_path(fake_ledger_harness(tmp_path, monkeypatch)[1])


def test_ledger_write_between_the_check_and_the_append_is_refused_by_name(tmp_path, monkeypatch):
    check_a_write_between_the_check_and_the_append_is_refused_by_name(*fake_ledger_harness(tmp_path, monkeypatch))


def test_ledger_writer_that_passed_the_revision_check_is_refused_by_the_append_key(tmp_path, monkeypatch):
    check_a_writer_that_passed_the_revision_check_is_refused(fake_ledger_harness(tmp_path, monkeypatch)[1])


def test_ledger_write_from_a_stale_view_is_refused_by_the_cli_writer_name(tmp_path, monkeypatch):
    check_a_stale_view_is_refused_by_the_cli_writer_name(*fake_ledger_harness(tmp_path, monkeypatch))


def test_ledger_writes_keep_the_tickets_owner_claim_and_blocked_by(tmp_path, monkeypatch):
    check_writes_keep_owner_claim_and_blocked_by(*fake_ledger_harness(tmp_path, monkeypatch))


def test_ledger_ticket_claimed_for_work_stays_claimed_and_writable(tmp_path, monkeypatch):
    check_a_ticket_claimed_for_work_stays_claimed_and_writable(*fake_ledger_harness(tmp_path, monkeypatch))


def test_a_ledger_close_confirm_is_refused_after_another_process_wrote(tmp_path, monkeypatch):
    fake = fake_ledger(tmp_path, monkeypatch)
    db = tmp_path / "ledger.db"
    store = ticket.LedgerStore(KEY, TITLE, cli=fake, db=db, writer="second-writer")
    store.write_stamp("Done.", "close", ("ticket",))
    other = first_writer("write", "ledger", fake, db)
    out, err = other.communicate(timeout=30)
    assert other.returncode == 0, err
    written_by = out.splitlines()[0]
    assert written_by.startswith("first-writer (pid ")
    with pytest.raises(ticket.TicketHeld) as refused:
        store.confirm_close(0, "Done.")
    assert refused.value.holder == written_by
    card = store.read_card()
    assert card["status"] == "open"
    assert [(stamp["point"], stamp["close"]) for stamp in card["stamps"]] == [
        ("Done.", False),
        ("Written by the first writer.", False),
    ]


def test_a_failed_ledger_write_leaves_the_ticket_writable(tmp_path, monkeypatch):
    fake = fake_ledger(tmp_path, monkeypatch)
    db = tmp_path / "ledger.db"
    failing = type("Store", (FailsMidWrite, ticket.LedgerStore), {})(KEY, TITLE, cli=fake, db=db, writer="failing-writer")
    with pytest.raises(RuntimeError, match="mid-write"):
        failing.write_stamp("Never lands.", "build")
    store = ticket.LedgerStore(KEY, TITLE, cli=fake, db=db)
    assert store._card_envelope()["status"] == "open"
    store.write_stamp("Written after the failure.", "build")
    assert [stamp["point"] for stamp in store.read_card()["stamps"]] == ["Written after the failure."]


def test_choose_store_uses_markdown_when_no_ledger_cli_is_installed(tmp_path):
    repo = init_ignored_repo(tmp_path)
    store = ticket.choose_store(repo, KEY, TITLE, cli=tmp_path / "missing-ledger", db=tmp_path / "ledger.db")
    assert isinstance(store, ticket.MarkdownStore)


def test_choose_store_reports_a_missing_ledger_database_instead_of_falling_back(tmp_path, monkeypatch):
    repo = init_ignored_repo(tmp_path)
    fake = fake_ledger(tmp_path, monkeypatch, FAKE_LEDGER_MISSING_DB="1")
    with pytest.raises(ticket.StoreUnavailable, match="Initialise it") as raised:
        ticket.choose_store(repo, KEY, TITLE, cli=fake, db=tmp_path / "ledger.db")
    assert raised.value.code == "missing_db"
    assert not (repo / ".ticket").exists()


def test_ledger_write_refusal_surfaces_the_cli_message(tmp_path, monkeypatch):
    repo = init_ignored_repo(tmp_path)
    message = "writes need a writer identity in the environment"
    fake = fake_ledger(tmp_path, monkeypatch, FAKE_LEDGER_REFUSE_WRITES=message)
    store = ticket.choose_store(repo, KEY, TITLE, cli=fake, db=tmp_path / "ledger.db")
    assert isinstance(store, ticket.LedgerStore)
    with pytest.raises(ticket.StoreUnavailable, match=message) as raised:
        store.write_stamp("Stopped.", "orient")
    assert raised.value.code == "refused"


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


def test_real_ledger_cli_adapter_when_configured(tmp_path):
    configured = os.environ.get("TICKET_LEDGER_CLI")
    if not configured or not Path(configured).is_file():
        pytest.skip("set TICKET_LEDGER_CLI to a ledger CLI executable to run this test")

    db = tmp_path / "real-ledger.db"
    with pytest.raises(ticket.StoreUnavailable) as missing:
        ticket.choose_store(init_ignored_repo(tmp_path), KEY, TITLE, cli=configured, db=db)
    assert missing.value.code == "missing_db"

    # Initialise the database the way SKILL.md says. The CLI's own writer
    # identity comes from the environment of whoever runs this test.
    created = subprocess.run(
        [configured, "--json", "--db", str(db), "new", "--id", KEY, "--type", "ticket", "--title", TITLE],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, "the ledger CLI refused to initialise: " + created.stdout + created.stderr

    store = ticket.choose_store(tmp_path / "repo", KEY, TITLE, cli=configured, db=db)
    assert isinstance(store, ticket.LedgerStore)
    card = run_full_pass(store)
    assert card["status"] == "closed"
    assert [review["round"] for review in card["reviews"]] == [1, 2]
    assert card["plan"]["acceptance_criteria"][1]["review_round"] == 2
    assert card["decisions"][0]["source"] == {"what": "ratification", "ref": "conversation:plan-go"}
    assert card["stamps"][-1]["close"] is True

    # The one-writer checks the fake CLI runs above, each on its own database.
    def harness(name):
        path = tmp_path / f"{name}.db"

        def cli(*args):
            result = subprocess.run([configured, "--json", "--db", str(path), *args], capture_output=True, text=True)
            assert result.returncode == 0, result.stdout + result.stderr
            return json.loads(result.stdout)["data"]

        def make_store(writer=None):
            return ticket.LedgerStore(KEY, TITLE, cli=configured, db=path, writer=writer)

        cli("new", "--id", KEY, "--type", "ticket", "--title", TITLE)
        return cli, make_store

    check_a_pass_holds_the_ticket_across_processes(harness("two-passes")[1])
    check_the_pass_record_is_released_on_every_exit_path(harness("exit-paths")[1])
    check_a_write_between_the_check_and_the_append_is_refused_by_name(*harness("interleaved"))
    check_a_writer_that_passed_the_revision_check_is_refused(harness("append-key")[1])
    check_a_stale_view_is_refused_by_the_cli_writer_name(*harness("stale-view"))
    check_writes_keep_owner_claim_and_blocked_by(*harness("carried-state"))
    check_a_ticket_claimed_for_work_stays_claimed_and_writable(*harness("work-claim"))

    # On an initialised database the adapter creates a new ticket's item itself.
    other = ticket.LedgerStore("EK-15", "Second ticket", cli=configured, db=db)
    other.write_stamp("waiting on person: Which API owns retries?", "orient", ("ticket",))
    assert other.read_card()["stamps"][0]["point"] == "waiting on person: Which API owns retries?"

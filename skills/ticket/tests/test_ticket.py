from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
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


def resume(store, value=None):
    src = StaticSource(value) if value is not None else source()
    return ticket.TicketPass.open(KEY, src, store, resume=True)


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
        "The plan gate is approved for plan revision 1.",
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
    assert resume(store).phase == "plan_go"

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
    assert resume(store).phase == "outward_go"


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

    resumed = resume(store)
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
    resumed = resume(store, changed)
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

    after_two = resume(store)
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

    resumed = resume(store)
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
    session = resume(store)
    assert session.phase == "plan"
    session.present_plan(ticket.PlanCard(session.ticket))
    assert (session.plan_revision, session.phase) == (2, "plan_go")
    return session


def one_fault_go(gate, fault):
    """The person's settled ratification of a gate at revision 2, wrong in one field only."""

    fields = dict(
        who="the person",
        who_kind="person",
        standing="settled",
        source=ticket.DecisionSource("ratification", "conversation:go"),
        effect=f"The {gate} gate is approved for plan revision 2.",
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
        fields.update(effect=f"The {gate} gate is approved for plan revision 1.")
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
    store.record_decision(one_fault_go("plan", fault))
    refused(session.begin_build)

    store = MemoryStore()
    session = at_plan_revision_two(store)
    session.record_go("plan", evidence="conversation:plan-go", verbatim="Go.")
    session.begin_build()
    session.record_review("pass", evidence="reviews/1", passed_criteria=(0, 1))
    store.record_decision(one_fault_go("outward", fault))
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
    resumed = ticket.TicketPass.open(KEY, source(), store, resume=True)
    assert (resumed.phase, resumed.review_round) == ("build", 1)
    resumed.record_review("pass", evidence="reviews/2", passed_criteria=(0, 1))
    again = ticket.TicketPass.open(KEY, source(), store, resume=True)
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
    assert ledger_card == markdown_card
    assert markdown_card["status"] == "closed"


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

    store.write_stamp("Later stop.", "review", ("ticket",))
    assert store.read_card()["status"] == "closed"
    assert last_two(store) == [("Done.", True), ("Later stop.", False)]
    resumed = resume(store)
    assert resumed.phase == "outward_go"
    assert CLOSED_INVALID + "; the ticket is not closed" in resumed.reconciliation


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

    # On an initialised database the adapter creates a new ticket's item itself.
    other = ticket.LedgerStore("EK-15", "Second ticket", cli=configured, db=db)
    other.write_stamp("waiting on person: Which API owns retries?", "orient", ("ticket",))
    assert other.read_card()["stamps"][0]["point"] == "waiting on person: Which API owns retries?"

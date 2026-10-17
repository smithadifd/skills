from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
FIXTURE = SKILL_DIR / "tests" / "fixtures" / "ticket.json"
FAKE_LEDGER = SKILL_DIR / "tests" / "fixtures" / "fake_ledger.py"
SCHEMA = SKILL_DIR / "plan-card.schema.json"

_spec = importlib.util.spec_from_file_location("ticket_skill", SKILL_DIR / "ticket.py")
ticket = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ticket
_spec.loader.exec_module(ticket)


class RecordingStore:
    def __init__(self, trace=None):
        self.trace = trace
        self.stamps = []
        self.decisions = []
        self.reviews = []

    def write_stamp(self, point, phase, next_skills=(), refs=(), *, close=False):
        self.stamps.append(
            {
                "point": point,
                "phase": phase,
                "next_skills": list(next_skills),
                "refs": list(refs),
                "close": close,
            }
        )

    def record_decision(self, decision):
        self.decisions.append(decision.as_dict())

    def record_review(self, review):
        self.reviews.append(review.as_dict())

    def read_card(self):
        if self.trace is not None:
            self.trace.append("read-card")
        return {"prior": True}


def fixture_ticket():
    return ticket.FileTicketSource(FIXTURE).pull("EK-14")


def test_fixture_source_exposes_live_ticket_and_design_links():
    value = fixture_ticket()
    assert value.status == "In Progress"
    assert value.updated_at == "2026-10-17T14:30:00Z"
    assert {link["kind"] for link in value.links} == {"figma", "confluence"}


def test_plan_card_schema_defaults_and_review_evidence_guard():
    card = ticket.PlanCard(fixture_ticket())
    data = card.as_dict()
    assert data["schema"] == "ticket-plan-card/v1"
    assert [item["passes"] for item in data["acceptance_criteria"]] == [False, False]
    assert [item["review_evidence"] for item in data["acceptance_criteria"]] == [None, None]

    with pytest.raises(ticket.TicketError, match="review-evidence"):
        card.mark_passes(0, None)
    assert card.acceptance_criteria[0].passes is False

    card.mark_passes(0, "reviews/round-1.md#criterion-1")
    assert card.acceptance_criteria[0].passes is True
    assert card.acceptance_criteria[0].review_evidence == "reviews/round-1.md#criterion-1"


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
    assert "review_evidence" in criterion["allOf"][0]["then"]["properties"]
    assert step["allOf"][0]["then"]["required"] == ["skip"]
    assert schema["allOf"][0]["then"]["required"] == ["diff_statement"]


def test_resume_pulls_live_ticket_before_reading_card():
    trace = []

    class Source:
        def pull(self, key):
            trace.append("pull-live")
            return fixture_ticket()

    session = ticket.TicketPass.open("EK-14", Source(), RecordingStore(trace), resume=True)
    assert session.resume_card == {"prior": True}
    assert trace == ["pull-live", "read-card"]


def test_plan_and_outward_go_are_hard_stops():
    store = RecordingStore()
    session = ticket.TicketPass.open("EK-14", ticket.FileTicketSource(FIXTURE), store)
    session.finish_orient()
    session.finish_research()
    plan = ticket.PlanCard(session.ticket)
    session.present_plan(plan)

    with pytest.raises(ticket.GateRequired, match="plan go"):
        session.begin_build()
    assert session.phase == "plan_go"

    session.record_go("plan", evidence="conversation:plan-go", verbatim="Go with this plan.")
    session.begin_build()
    session.record_review(
        "pass",
        evidence="reviews/round-1.md",
        passed_criteria=(0, 1),
    )
    assert session.phase == "outward_go"

    with pytest.raises(ticket.GateRequired, match="outward go"):
        session.close("Done", refs=("example/repo#14",))
    assert session.phase == "outward_go"

    session.record_go("outward", evidence="conversation:outward-go", verbatim="Open the PR.")
    session.close("Ticket pass complete.", refs=("example/repo#14",))
    assert session.phase == "closed"
    assert [item["effect"] for item in store.decisions] == [
        "The plan gate is approved for this pass.",
        "The outward gate is approved for this pass.",
    ]
    assert store.stamps[-1]["close"] is True


def test_review_changes_continue_without_person_until_round_cap():
    store = RecordingStore()
    session = ticket.TicketPass.open("EK-14", ticket.FileTicketSource(FIXTURE), store)
    session.finish_orient()
    session.finish_research()
    session.present_plan(ticket.PlanCard(session.ticket))
    session.record_go("plan", evidence="conversation:1", verbatim="Go.")
    session.begin_build()

    session.record_review("changes", evidence="reviews/1", findings=("first",))
    session.record_review("changes", evidence="reviews/2", findings=("second",))
    assert session.phase == "build"
    session.record_review("changes", evidence="reviews/3", findings=("third",))
    assert session.phase == "stopped"
    assert len(store.reviews) == 3
    assert store.stamps[-1]["point"] == "Review did not converge after round three."


@pytest.mark.parametrize(
    "destination, role, expected",
    [
        ("person", None, "waiting on person: Which API should own retries?"),
        ("research", None, "waiting on research: Which API should own retries?"),
        ("someone_else", "API owner", "waiting on API owner: Which API should own retries?"),
    ],
)
def test_three_more_info_exits(destination, role, expected):
    store = RecordingStore()
    session = ticket.TicketPass.open("EK-14", ticket.FileTicketSource(FIXTURE), store)
    result = session.more_info(destination, "Which API should own retries?", role=role)
    assert result.waiting == expected
    assert store.stamps[-1]["point"] == expected
    assert session.phase == "stopped"
    if destination == "someone_else":
        assert result.drafted_ask == "Could you help with EK-14? Which API should own retries?"
    else:
        assert result.drafted_ask is None


def init_ignored_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text(".ticket/\n", encoding="utf-8")
    return repo


def exercise_store(store):
    store.record_decision(
        ticket.DecisionRecord(
            who="the person",
            who_kind="person",
            standing="settled",
            source=ticket.DecisionSource("ratification", "conversation:42"),
            effect="Use the narrow adapter contract.",
            tags=({"type": "ticket", "id": "EK-14"},),
            verbatim="Use the narrow adapter.",
            phase="plan",
            evidence="plan/card.json",
        )
    )
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
        close=True,
    )
    return store.read_card()


def test_markdown_fallback_refuses_unignored_card_without_editing_ignore_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    ignore = repo / ".gitignore"
    ignore.write_text("*.pyc\n", encoding="utf-8")
    store = ticket.MarkdownStore(repo, "EK-14", "Add guarded ticket pass")

    with pytest.raises(ticket.StoreUnavailable, match="add `.ticket/`"):
        store.write_stamp("Stopped.", "orient")
    assert ignore.read_text(encoding="utf-8") == "*.pyc\n"
    assert not store.path.exists()


def test_markdown_and_fake_ledger_adapters_produce_equivalent_cards(tmp_path, monkeypatch):
    repo = init_ignored_repo(tmp_path)
    markdown = ticket.MarkdownStore(repo, "EK-14", "Add guarded ticket pass")
    markdown_card = exercise_store(markdown)
    assert markdown.path == repo / ".ticket" / "EK-14.md"
    assert "# EK-14 — Add guarded ticket pass" in markdown.path.read_text(encoding="utf-8")

    fake = tmp_path / "fake-ledger"
    fake.write_bytes(FAKE_LEDGER.read_bytes())
    fake.chmod(0o755)
    log = tmp_path / "argv.jsonl"
    state = tmp_path / "state.json"
    monkeypatch.setenv("TICKET_LEDGER_CLI", str(fake))
    monkeypatch.setenv("FAKE_LEDGER_LOG", str(log))
    monkeypatch.setenv("FAKE_LEDGER_STATE", str(state))
    monkeypatch.setenv("FAKE_LEDGER_KEY", "EK-14")
    monkeypatch.setenv("FAKE_LEDGER_TITLE", "Add guarded ticket pass")

    ledger = ticket.LedgerStore("EK-14", "Add guarded ticket pass", db=tmp_path / "ledger.db")
    assert ledger.is_available()
    ledger_card = exercise_store(ledger)
    assert ledger_card == markdown_card

    argv = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert any("doctor" in call for call in argv)
    assert any("item.stopped" in call for call in argv)
    assert any("decision.recorded" in call for call in argv)
    assert any("review.recorded" in call for call in argv)
    assert any("card" in call for call in argv)
    assert any("export-item" in call for call in argv)


def test_choose_store_uses_markdown_when_doctor_fails(tmp_path):
    repo = init_ignored_repo(tmp_path)
    store = ticket.choose_store(
        repo,
        "EK-14",
        "Add guarded ticket pass",
        cli=tmp_path / "missing-ledger",
        db=tmp_path / "ledger.db",
    )
    assert isinstance(store, ticket.MarkdownStore)


def test_real_ledger_cli_adapter_when_available(tmp_path, monkeypatch):
    default = Path("/").joinpath(
        "Users", "andrew", "work", "linear-kit", "wt", "EK12-rev", "scripts", "ledger"
    )
    configured = os.environ.get("TICKET_LEDGER_CLI")
    cli = Path(configured) if configured else default
    if not cli.exists():
        pytest.skip("real ledger CLI is not available; set TICKET_LEDGER_CLI to run this test")

    db = tmp_path / "real-ledger.db"
    env = os.environ.copy()
    env["LEDGER_" + "HAR" + "NESS"] = "ticket-test"
    created = subprocess.run(
        [
            str(cli),
            "--json",
            "--db",
            str(db),
            "new",
            "--id",
            "EK-14",
            "--type",
            "ticket",
            "--title",
            "Add guarded ticket pass",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert created.returncode == 0, created.stderr + created.stdout
    monkeypatch.setenv("TICKET_LEDGER_CLI", str(cli))
    store = ticket.LedgerStore("EK-14", "Add guarded ticket pass", db=db)
    assert store.is_available()
    card = exercise_store(store)
    assert card["status"] == "closed"
    assert card["decisions"][0]["source"] == {
        "what": "ratification",
        "ref": "conversation:42",
    }
    assert card["stamps"][-1]["close"] is True

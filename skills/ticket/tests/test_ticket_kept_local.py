"""Matrix section K: a declined outward go, a repository with no remote, and the builder's worktree at close."""

import json
import subprocess
import time
from pathlib import Path

import pytest

import ticket
from ledger_support import FakeLedger, open_ledger
from ticket_support import (
    KEY, Source, build, card_bytes, edit_card, go_outward, open_pass, passing, present, read_card, refusal,
)

SKILL_DIR = Path(__file__).resolve().parent.parent
GO_WORDS = "Go with this plan."
NO_WORDS = "No, do not push or open a pull request. Keep it local."
KEPT_LOCAL = ticket.KEPT_LOCAL_POINT.format(revision=1)
ANSWERED = (
    "the outward gate of plan revision 1 already holds the person's answer, so nothing more is "
    "recorded: close() after a go, close_local() after a declined go"
)


@pytest.fixture
def fake(tmp_path):
    return FakeLedger(tmp_path / "ledger")


def _capture(fake, text, session_id="sess-this"):
    """An item-less person prompt, as ``ledger capture --kind prompt`` writes it."""

    fake._append(
        None, "utterance.captured", {"origin": "person", "text": text, "session_id": session_id},
        f"capture:{len(fake.events())}", recorded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    return fake.events()[-1]["id"]


def _of_kind(fake, kind):
    return [event for event in fake.events() if event["kind"] == kind]


def _ticket_decisions(fake):
    prefix = "ticket-decision:v"
    notes = [event["payload"]["text"] for event in _of_kind(fake, "item.note")]
    return [json.loads(text.split(" ", 1)[1]) for text in notes if text.startswith(prefix)]


def decline(session, verbatim=NO_WORDS):
    return session.record_outward_declined(evidence="conversation:outward-no", verbatim=verbatim)


def _at_outward_go_on_the_ledger(fake, monkeypatch, src):
    """A ledger pass through a bound plan go and a passing round, waiting at the outward go."""

    monkeypatch.setenv("LEDGER_SESSION", "sess-this")
    session = open_ledger(fake, src)
    present(session)
    go_id = _capture(fake, f"Looks right. {GO_WORDS}")
    assert session.record_go("plan", evidence="conversation:plan-go", verbatim=GO_WORDS)["utterance_id"] == go_id
    session.begin_build()
    passing(session)
    assert session.phase == "outward_go"
    return session


def _no_outward_action(session):
    with pytest.raises(ticket.GateRequired, match="outward go is required"):
        session.require_outward_go()
    assert session.approved("outward") is False


# --- K1-K2: a declined outward go is a decision bound to the person's words, and closes kept local ----


def test_K1_a_declined_outward_go_is_bound_to_the_captured_utterance_and_closes_kept_local(fake, monkeypatch):
    src = Source()
    session = _at_outward_go_on_the_ledger(fake, monkeypatch, src)
    no_id = _capture(fake, NO_WORDS)
    result = decline(session)
    assert result["bound"] is True and result["utterance_id"] == no_id
    elected = _of_kind(fake, "decision.elected")
    assert [event["payload"]["chosen"] for event in elected] == ["go", "no go"]
    assert elected[1]["payload"]["rejected"] == ["go"]
    assert elected[1]["payload"]["quotes"] == [NO_WORDS] and elected[1]["payload"]["quote_ref"] == no_id
    assert elected[1]["idempotency_key"] == f"ticket-bind/{KEY}/outward-declined/1/{no_id}/elect"
    recorded = _of_kind(fake, "decision.recorded")
    assert [event["payload"]["verbatim"] for event in recorded] == [GO_WORDS, NO_WORDS]
    assert recorded[1]["payload"]["source_event_id"] == elected[1]["id"] == result["election_id"]
    assert recorded[1]["idempotency_key"] == f"ticket-bind/{KEY}/outward-declined/1/{no_id}/decision"
    declined = _ticket_decisions(fake)[1]
    assert declined["declined"] == "outward" and "gate" not in declined
    assert (declined["verbatim"], declined["plan_revision"]) == (NO_WORDS, 1)
    # Declined, not yet closed: nothing may leave the machine, and the pass is still live.
    _no_outward_action(session)
    assert session.live is True
    assert session.close_local() is None
    assert (session.phase, session.live) == ("closed", False)
    stops = _of_kind(fake, "item.stopped")
    assert (stops[-1]["payload"]["point"], stops[-1]["payload"]["phase"]) == (KEPT_LOCAL, "close")
    assert [event["payload"]["status"] for event in _of_kind(fake, "item.state")] == ["closed"]
    with pytest.raises(ticket.StoreUnavailable):
        session.require_outward_go()
    resumed = open_ledger(fake, src)
    assert (resumed.phase, resumed.live) == ("closed", False)
    assert resumed.resume_card["stamps"][-1]["point"] == KEPT_LOCAL
    with pytest.raises((ticket.TicketError, ticket.StoreUnavailable)):
        resumed.require_outward_go()


def test_K2_a_declined_go_with_no_matching_utterance_closes_and_reports_not_bound(fake, monkeypatch):
    src = Source()
    session = _at_outward_go_on_the_ledger(fake, monkeypatch, src)
    _capture(fake, "Something else entirely.")
    elections = len(_of_kind(fake, "decision.elected"))
    assert decline(session) == {"bound": False, "reason": "no matching utterance"}
    assert len(_of_kind(fake, "decision.elected")) == elections
    assert _ticket_decisions(fake)[1]["declined"] == "outward"
    session.close_local()
    assert open_ledger(fake, src).phase == "closed"


def test_K2_a_markdown_decline_is_not_bound_and_closes_kept_local(repo):
    src = Source()
    session = build(repo, src, "S3", 1)
    assert decline(session) == {"bound": False, "reason": "no ledger"}
    session.close_local(refs=("branch:ek-14",))
    card = read_card(repo)
    assert card["status"] == "closed"
    assert card["stamps"][-1] == {"point": KEPT_LOCAL, "phase": "close", "next_skills": [], "refs": ["branch:ek-14"], "close": True}
    assert open_pass(repo, src).phase == "closed"


# --- K3: what the guards refuse --------------------------------------------------------------------


def _decline_of(go, standing="settled"):
    declined = {name: value for name, value in go.items() if name != "gate"}
    declined.update(declined="outward", standing=standing, effect="The outward gate is declined.")
    return declined


def _add_decline(standing):
    """A card edit: a decline of the plan beside the outward go already on it, as a hand edit could leave."""

    def add(card):
        go = next(decision for decision in card["decisions"] if decision.get("gate") == "outward")
        card["decisions"].append(_decline_of(go, standing))

    return add



@pytest.mark.parametrize("answer", ["none yet", "a go beside the decline"])
def test_K3_a_kept_local_close_needs_the_persons_decline_and_no_go(repo, answer):
    src = Source()
    if answer == "none yet":
        session = build(repo, src, "S3", 1)
    else:
        build(repo, src, "S4", 1).end()
        edit_card(repo, _add_decline("settled"))
        session = open_pass(repo, src)
    before = card_bytes(repo)
    with pytest.raises(
        ticket.GateRequired, match="a kept-local close requires the person's declined outward go and no outward go",
    ):
        session.close_local()
    assert card_bytes(repo) == before
    assert session.live is True


@pytest.mark.parametrize("answer", ["an outward go", "a decline"])
def test_K3_a_decline_is_refused_once_the_outward_gate_holds_an_answer(repo, answer):
    session = build(repo, Source(), "S4" if answer == "an outward go" else "S3", 1)
    if answer == "a decline":
        decline(session)
    before = card_bytes(repo)
    with pytest.raises(ticket.TicketError, match=ANSWERED.replace("(", r"\(").replace(")", r"\)")):
        decline(session)
    assert card_bytes(repo) == before
    assert session.live is True


def test_K3_no_outward_go_is_recorded_after_a_decline(repo):
    session = build(repo, Source(), "S3", 1)
    decline(session)
    before = card_bytes(repo)
    with pytest.raises(ticket.TicketError, match=r"already holds the person's answer"):
        go_outward(session)
    assert card_bytes(repo) == before
    _no_outward_action(session)


@pytest.mark.parametrize("words", [{"evidence": "", "verbatim": NO_WORDS}, {"evidence": "conversation:no", "verbatim": " "}])
def test_K3_a_decline_needs_evidence_and_the_persons_words(repo, words):
    session = build(repo, Source(), "S3", 1)
    before = card_bytes(repo)
    with pytest.raises(ticket.TicketError, match="a declined go record requires evidence and the person's verbatim words"):
        session.record_outward_declined(**words)
    assert card_bytes(repo) == before


@pytest.mark.parametrize("standing", ["settled", "lean"])
def test_K3_a_decline_on_the_card_shuts_the_outward_gate_even_beside_a_go(repo, standing):
    src = Source()
    build(repo, src, "S4", 1).end()
    edit_card(repo, _add_decline(standing))
    session = open_pass(repo, src)
    assert session.phase == "outward_go"
    _no_outward_action(session)
    with pytest.raises(ticket.GateRequired, match="an outward go is required before close"):
        session.close("Done.", refs=("example/repo#14",))
    assert read_card(repo)["status"] == "open"


@pytest.mark.parametrize(
    "fault, message",
    [
        ({"declined": "plan"}, "a declined decision names only the outward gate"),
        ({"gate": "outward"}, "a decision is either a go or a declined go, not both"),
        ({"plan_revision": 0}, "a declined decision's plan_revision must be a whole number of at least 1"),
        ({"criteria": []}, "a declined decision's criteria"),
    ],
    ids=["other gate", "both", "revision", "criteria"],
)
def test_K3_the_decision_validator_reads_a_decline_strictly(repo, fault, message):
    session = build(repo, Source(), "S3", 1)
    decline(session)
    declined = {**read_card(repo)["decisions"][-1], **fault}
    with pytest.raises(ticket.TicketError, match=message):
        ticket._validate_decision(declined)


# --- K4: a closed kept-local card is checked as strictly as any close -------------------------------


def _kept_local_card(repo, src):
    session = build(repo, src, "S3", 1)
    decline(session)
    session.close_local()
    assert open_pass(repo, src).phase == "closed"


def _point_changed(card):
    card["stamps"][-1]["point"] = "Ticket pass complete."


def _go_added(card):
    declined = card["decisions"][-1]
    go = {name: value for name, value in declined.items() if name != "declined"}
    card["decisions"].append({**go, "gate": "outward"})


def _decline_dropped(card):
    card["decisions"] = [decision for decision in card["decisions"] if "declined" not in decision]


def _decline_lean(card):
    card["decisions"][-1]["standing"] = "lean"


K4_EDITS = {
    "point changed": _point_changed,
    "an outward go added": _go_added,
    "decline dropped": _decline_dropped,
    "decline only a lean": _decline_lean,
}


@pytest.mark.parametrize("case", list(K4_EDITS))
def test_K4_each_kept_local_check_alone_refuses_a_closed_card(repo, case):
    src = Source()
    _kept_local_card(repo, src)
    edit_card(repo, K4_EDITS[case])
    before = card_bytes(repo)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_pass(repo, src)
    assert refusal(excinfo) == "close_invalid"
    assert card_bytes(repo) == before


# --- K5: no remote, no push to propose ----------------------------------------------------------------


def test_K5_a_repository_with_no_remote_offers_the_local_close_and_closes_through_it(repo):
    route = ticket.outward_route(repo)
    assert route.remotes == () and route.propose_push is False
    assert route.line == ticket.NO_REMOTE_LINE
    assert "no remote" in route.line and "kept local" in route.line
    src = Source()
    session = build(repo, src, "S3", 1)
    decline(session)
    session.close_local()
    card = read_card(repo)
    assert card["status"] == "closed"
    assert [decision.get("gate") or decision.get("declined") for decision in card["decisions"]] == ["plan", "outward"]
    assert [decision["verbatim"] for decision in card["decisions"]] == [GO_WORDS, NO_WORDS]


def test_K5_a_repository_with_a_remote_proposes_the_push(repo):
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/repo.git"], check=True)
    route = ticket.outward_route(repo)
    assert route.remotes == ("origin",) and route.propose_push is True
    assert route.line == "remotes: origin"


def test_K5_an_unreadable_remote_list_counts_as_a_remote(tmp_path):
    route = ticket.outward_route(tmp_path / "not-a-repository")
    assert route.remotes is None and route.propose_push is True
    assert route.line.startswith("the repository's remotes could not be read (")
    assert route.line.endswith("so it counts as having one and the outward go stops as usual")


# --- K6: the builder's worktree at close ------------------------------------------------------------------


def _git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def _builder_worktree(repo, name="agent-1", *, detached=False):
    """A worktree where `builder-core` gets one: under the repository's `.claude/worktrees`."""

    if not _git("-C", str(repo), "rev-list", "--all"):
        _git("-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    path = repo / ".claude" / "worktrees" / name
    branch = ["--detach"] if detached else ["-b", f"ticket/{name}"]
    _git("-C", str(repo), "worktree", "add", "-q", *branch, str(path))
    return path


def test_K6_a_clean_worktree_is_removed_its_branch_kept_and_the_empty_claude_folder_removed(repo):
    path = _builder_worktree(repo)
    line = ticket.tidy_worktree(path)
    assert line == (
        f"builder worktree {path} was removed; its branch ticket/agent-1 is kept; "
        f"the empty {repo / '.claude' / 'worktrees'} and {repo / '.claude'} were removed"
    )
    assert not path.exists() and not (repo / ".claude").exists()
    assert _git("-C", str(repo), "branch", "--list", "ticket/agent-1").strip() == "ticket/agent-1"
    assert ".claude" not in _git("-C", str(repo), "status", "--porcelain")


def test_K6_a_claude_folder_that_holds_more_is_named_and_left(repo):
    path = _builder_worktree(repo)
    (repo / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    line = ticket.tidy_worktree(path)
    assert line.endswith(f"; {repo / '.claude'} is left in place: it is not empty")
    assert not path.exists() and (repo / ".claude" / "settings.json").exists()


def test_K6_a_dirty_worktree_is_named_and_kept(repo):
    path = _builder_worktree(repo)
    (path / "notes.txt").write_text("unsaved\n", encoding="utf-8")
    line = ticket.tidy_worktree(path)
    assert line == (
        f"builder worktree {path} (under the repository's .claude/worktrees) was kept: "
        "it has uncommitted or untracked changes; nothing was removed"
    )
    assert (path / "notes.txt").exists()


def test_K6_a_worktree_not_on_a_branch_is_named_and_kept(repo):
    path = _builder_worktree(repo, detached=True)
    line = ticket.tidy_worktree(path)
    assert "was kept: it is not on a branch, so removing it could lose its commits" in line
    assert path.exists()


def test_K6_a_worktree_whose_state_cannot_be_read_is_named_and_kept(tmp_path):
    path = tmp_path / "loose" / "agent-9"
    path.mkdir(parents=True)
    (path / "work.txt").write_text("x\n", encoding="utf-8")
    line = ticket.tidy_worktree(path)
    assert line.startswith(f"builder worktree {path} was kept: its state could not be read (")
    assert (path / "work.txt").exists()


def test_K6_a_worktree_git_will_not_remove_is_named_and_kept(repo):
    path = _builder_worktree(repo)
    _git("-C", str(repo), "worktree", "lock", str(path))
    line = ticket.tidy_worktree(path)
    assert line.startswith(
        f"builder worktree {path} (under the repository's .claude/worktrees) was kept: git refused to remove it ("
    )
    assert path.exists()


def test_K6_no_worktree_says_nothing_and_a_missing_one_is_named(tmp_path):
    assert ticket.tidy_worktree(None) is None
    gone = tmp_path / "gone"
    assert ticket.tidy_worktree(gone) == f"builder worktree {gone} was not found; nothing was removed"


def test_K6_both_closes_tidy_the_builders_worktree(repo, tmp_path):
    path = _builder_worktree(repo)
    session = build(repo, Source(), "S3", 1)
    decline(session)
    assert session.close_local(worktree=path).startswith(f"builder worktree {path} was removed")
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    (other / ".gitignore").write_text(".ticket/\n", encoding="utf-8")
    dirty = _builder_worktree(other)
    (dirty / "notes.txt").write_text("unsaved\n", encoding="utf-8")
    session = build(other, Source(), "S4", 1)
    assert session.close("Done.", refs=("example/repo#14",), worktree=dirty).startswith(f"builder worktree {dirty} (under")
    assert read_card(other)["status"] == "closed"


# --- K7: SKILL.md carries the declined path, the no-remote path and the worktree line --------------------


def _skill_section(heading):
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    start = text.index(f"\n## {heading}\n") + 1
    end = text.find("\n## ", start + 1)
    return " ".join(text[start:end if end != -1 else len(text)].split())


def test_K7_skill_md_describes_the_declined_path_the_no_remote_path_and_the_worktree():
    outward = _skill_section("6. Outward go: a hard stop")
    for needle in (
        "ticket.outward_route(", "session.record_outward_declined(evidence=",
        "`declined go recorded, not bound to a captured utterance`", "session.close_local(",
        "worktree=", "never forced",
    ):
        assert needle in outward, needle
    final = _skill_section("The final message")
    assert "kept local" in final and "worktree" in final

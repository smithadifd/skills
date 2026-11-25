"""Phase-boundary estimates are advisory, durable per pass, and shared by phase name."""

import pytest

import ticket
from ledger_support import FakeLedger
from ticket_support import KEY, Source, go_outward, go_plan, passing, store


@pytest.fixture(params=["markdown", "ledger"])
def handles(request, repo, tmp_path):
    if request.param == "ledger":
        fake = FakeLedger(tmp_path / "context-ledger")
        return fake.store
    return lambda: store(repo)


def opened(handles, reading, **kwargs):
    return ticket.TicketPass.open(KEY, Source(), handles(), context_reading=reading, **kwargs)


def reach(session, boundary):
    session.finish_orient()
    if boundary == "orient":
        return
    session.finish_research()
    if boundary == "research":
        return
    session.present_plan(ticket.PlanCard(session.ticket))
    if boundary == "plan":
        return
    go_plan(session)
    session.begin_build()
    session.record_build("test-model", harness="test")
    if boundary == "build":
        return
    passing(session)


@pytest.mark.parametrize("boundary,remaining", [
    ("orient", 575_000), ("research", 500_000), ("plan", 450_000),
    ("build", 250_000), ("review", 125_000),
])
def test_over_floor_prints_exactly_one_line_at_each_boundary(handles, capsys, boundary, remaining):
    available = False
    session = opened(handles, lambda: ticket.ContextReading(700_000, 1_000_000) if available else None)
    # Earlier boundaries are unavailable; only the requested boundary can print.
    action = {
        "orient": session.finish_orient,
        "research": session.finish_research,
        "plan": lambda: session.present_plan(ticket.PlanCard(session.ticket)),
        "build": lambda: session.record_build("test-model", harness="test"),
        "review": lambda: passing(session),
    }[boundary]
    previous = {"research": "orient", "plan": "research", "build": "plan", "review": "build"}
    if boundary in previous:
        reach(session, previous[boundary])
    if boundary == "build":
        go_plan(session)
        session.begin_build()
    available = True
    action()
    expected = (
        f"Context bound after {boundary} (pass {session.pass_id}): used 700000 + remaining {remaining} "
        f"= {700_000 + remaining} tokens > floor 800000 (0.8 of 1000000); "
        f"recommend handoff to a fresh session on {KEY} from its resume card {session.store.ref}.\n"
    )
    assert capsys.readouterr().out == expected, "over-floor boundary must print exactly one advisory line"
    assert session.live, "a context recommendation must not end the pass"
    if boundary == "review":
        assert session.phase == "outward_go"
        assert not session.approved("outward"), "a context recommendation must not grant the outward go"
    session.end()


@pytest.mark.parametrize("used", [0, 225_000])
def test_under_or_equal_floor_prints_nothing(handles, capsys, used):
    session = opened(handles, lambda: ticket.ContextReading(used, 1_000_000))
    session.finish_orient()
    assert capsys.readouterr().out == "", "at or below the floor must stay silent"
    assert session.phase == "research" and session.live
    session.end()


def test_floor_fraction_scales_with_window(handles, capsys):
    session = opened(handles, lambda: ticket.ContextReading(0, 2_000_000), context_floor_fraction=0.25)
    session.finish_orient()
    assert "remaining 575000 = 575000 tokens > floor 500000 (0.25 of 2000000)" in capsys.readouterr().out
    session.end()


def test_large_window_floor_prints_integer_tokens(handles, capsys):
    session = opened(handles, lambda: ticket.ContextReading(1_500_000, 2_000_000))
    session.finish_orient()
    assert "tokens > floor 1600000 (0.8 of 2000000)" in capsys.readouterr().out, "floor must print integer tokens, not exponent notation"
    session.end()


def test_same_boundary_on_resumed_pass_does_not_print_again(handles, capsys):
    reading = lambda: ticket.ContextReading(700_000, 1_000_000)
    session = opened(handles, reading)
    session.finish_orient()
    assert capsys.readouterr().out.startswith("Context bound after orient"), "first crossing must print"
    resumed = opened(handles, reading, pass_id=session.pass_id)
    assert resumed.phase == "orient"
    resumed.finish_orient()
    assert capsys.readouterr().out == "", "resuming a pass must not repeat an already printed boundary"
    resumed.finish_research()
    assert capsys.readouterr().out.startswith("Context bound after research"), "other boundaries still print"
    resumed.end()
    new_pass = opened(handles, reading)
    new_pass.finish_orient()
    assert capsys.readouterr().out.startswith("Context bound after orient"), "a new pass has its own boundary record"
    new_pass.end()


@pytest.mark.parametrize("kind", ["absent", "none", "raises"])
def test_unavailable_reading_is_silent_and_pass_continues(handles, capsys, kind):
    def unavailable():
        if kind == "raises":
            raise OSError("context reader unavailable")
        return None

    kwargs = {} if kind == "absent" else {"context_reading": unavailable}
    session = ticket.TicketPass.open(KEY, Source(), handles(), **kwargs)
    reach(session, "review")
    assert capsys.readouterr().out == "", "unavailable context must print nothing"
    assert session.phase == "outward_go" and session.live, "unavailable context must not block the pass"
    go_outward(session)
    session.record_pull_request("example/repo#14")
    session.close("Published after the person's outward go.")
    assert session.phase == "closed"


@pytest.mark.parametrize("reading", [
    ticket.ContextReading(True, 10**6),
    ticket.ContextReading(-5, 10**6),
    ticket.ContextReading(7*10**5, 0),
], ids=["bool-used", "negative-used", "zero-window"])
def test_malformed_reading_is_silent_and_unstamped(handles, capsys, reading):
    # Remaining orient costs alone exceed this floor, even with a negative count.
    session = opened(handles, lambda: reading, context_floor_fraction=0.5)
    before = session.store.read()[0]["stamps"]
    session.finish_orient()
    assert capsys.readouterr().out == "", "malformed context reading must print nothing"
    assert session.store.read()[0]["stamps"] == before, "malformed context reading must write no stamp"
    assert session.phase == "research" and session.live
    session.end()


@pytest.mark.parametrize("verdict,last_point", [
    ("pass", "Waiting at outward go after review round 1."),
    ("changes", "Waiting at plan go for plan revision 1."),
])
def test_over_floor_review_keeps_stop_stamp_last(handles, capsys, verdict, last_point):
    available = False
    session = opened(handles, lambda: ticket.ContextReading(700_000, 1_000_000) if available else None)
    reach(session, "build")
    available = True
    if verdict == "pass":
        passing(session)
    else:
        session.record_review("changes", evidence="reviews/r1.md", findings=["Fix the behavior."])
    line = capsys.readouterr().out.strip()
    assert line.startswith("Context bound after review"), "review must cross the context floor"
    stamps = session.store.read()[0]["stamps"]
    assert stamps[-1]["point"] == last_point, "context advisory must not replace the last stop or outward-go stamp"
    session.end()


def test_estimates_load_by_phase_name():
    costs = ticket.load_phase_costs()
    assert costs == {
        "orient": 25_000, "research": 75_000, "plan": 50_000, "build": 200_000,
        "review": 125_000, "outward": 25_000, "close": 100_000,
    }, "named phases must expose the shared seed estimates"
    assert sum(costs[phase] for phase in ("orient", "research", "plan", "build")) == 350_000
    assert sum(costs[phase] for phase in ("review", "outward", "close")) == 250_000


def test_review_changes_budget_includes_another_build(handles, capsys):
    available = False
    session = opened(handles, lambda: ticket.ContextReading(400_000, 1_000_000) if available else None)
    reach(session, "build")
    available = True
    session.record_review("changes", evidence="reviews/r1.md", findings=["Fix the behavior."])
    assert "remaining 450000 = 850000 tokens > floor 800000" in capsys.readouterr().out
    assert session.phase == "build" and session.live
    passing(session)
    assert capsys.readouterr().out == "", "later rounds must not repeat the review boundary"
    session.end()


def test_reader_runs_only_at_boundaries(handles, capsys):
    calls = []

    def reading():
        calls.append(True)
        return ticket.ContextReading(700_000, 1_000_000)

    session = opened(handles, reading)
    assert calls == [], "opening a pass is not a phase boundary"
    session.record_tried("Read target conventions.")
    assert calls == [], "ordinary progress records are not phase boundaries"
    reach(session, "review")
    assert len(calls) == 5, "only the five completed phase boundaries read context"
    capsys.readouterr()
    go_outward(session)
    session.record_pull_request("example/repo#14")
    session.require_outward_go()
    session.close("Published after approval.")
    assert len(calls) == 5, "approval, outward actions and close add no boundary polling"
    assert capsys.readouterr().out == ""

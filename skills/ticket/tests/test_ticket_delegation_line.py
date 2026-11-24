"""Plan parts drive advice only; context and durable stamps share the boundary path."""

import json

import pytest

import ticket
from ledger_support import FakeLedger
from ticket_support import KEY, Source, go_plan, passing, store


PARTS = ("Change the consumer", "Migrate the stored records")


@pytest.mark.parametrize(
    "items", ["abc", {"part": "Change the consumer"}, {"Change the consumer"}, 7],
    ids=["bare-string", "mapping", "set", "integer"],
)
def test_plan_items_refuse_non_list_or_tuple(items):
    with pytest.raises(ticket.TicketError, match="a plan card's items must be a list"):
        ticket.PlanCard(Source().pull(KEY), items=items)


@pytest.fixture(params=["markdown", "ledger"])
def handles(request, repo, tmp_path):
    if request.param == "ledger":
        return FakeLedger(tmp_path / "delegation-ledger").store
    return lambda: store(repo)


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    executable = tmp_path / "loop-adapter"
    # Detect presence, never execute: running this adapter would fail the test.
    executable.write_text("#!/bin/sh\nexit 91\n")
    executable.chmod(0o755)
    shipped = tmp_path / "shipped-topic.json"
    shipped.write_text("{}")
    override = tmp_path / "override-topic.json"
    override.write_text("{}")
    monkeypatch.setattr(ticket._LEDGER_RULE, "CONFIG_PATH", shipped)
    monkeypatch.setattr(ticket._LEDGER_RULE, "OVERRIDE_PATH", str(override))
    monkeypatch.delenv("TICKET_LOOP_ADAPTER", raising=False)
    return executable, shipped, override


def at_plan(handles, reading, **kwargs):
    # Earlier boundaries cannot consume the plan-card reading or print warnings.
    available = False

    def host_reading():
        return reading() if available and reading is not None else None

    session = ticket.TicketPass.open(KEY, Source(), handles(), context_reading=host_reading, **kwargs)
    session.finish_orient()
    session.finish_research()
    available = True
    return session


def delegation_lines(capsys):
    return [line for line in capsys.readouterr().out.splitlines() if line.startswith("Delegation at plan card")]


def test_over_bound_with_loop_names_parts_estimate_bound_and_child_of(handles, adapter, monkeypatch, capsys):
    monkeypatch.setenv("TICKET_LOOP_ADAPTER", str(adapter[0]))
    session = at_plan(handles, lambda: ticket.ContextReading(200_000, 1_000_000))
    plan = ticket.PlanCard(session.ticket, items=PARTS)
    session.present_plan(plan)
    assert capsys.readouterr().out == (
        f"Delegation at plan card (pass {session.pass_id}): 2 items implied; used 200000 + estimate 650000 "
        "= 850000 tokens > bound 800000 (0.8 of 1000000); recommend a plan to file the parts as child-of "
        "items and hand them downstream to the loop, only after the plan go.\n"
    ), "over-bound parts must print one line naming the count, estimate, bound and child-of plan"
    card = session.store.read()[0]
    assert card["plan"]["items"] == list(PARTS), "the count must come from the stored plan's own parts"
    assert all(part in plan.render() for part in PARTS), "the person must see the counted parts on the card"
    assert session.phase == "plan_go" and session.live and not session.approved("plan")
    with pytest.raises(ticket.GateRequired):
        session.begin_build()
    go_plan(session)
    assert session.store.read()[0]["stamps"][-1]["point"] == "Waiting at plan go for plan revision 1.", "delegation advice must leave the plan stop last even after its go"
    session.begin_build()
    passing(session)
    assert session.phase == "outward_go" and not session.approved("outward"), "advice cannot grant either go"
    assert session.store.read()[0]["stamps"][-1]["point"] == "Waiting at outward go after review round 1."
    session.end()


def test_over_bound_without_loop_names_the_addon(handles, adapter, capsys):
    session = at_plan(handles, lambda: ticket.ContextReading(200_000, 1_000_000))
    session.present_plan(ticket.PlanCard(session.ticket, items=PARTS))
    lines = delegation_lines(capsys)
    assert len(lines) == 1 and "2 items implied" in lines[0] and "estimate 650000" in lines[0] and "bound 800000" in lines[0]
    assert "loop is the add-on that would take these parts" in lines[0], "absent loop must name the add-on, not promise a hand-off"
    assert "recommend a plan to file" not in lines[0]
    session.end()


@pytest.mark.parametrize("used", [0, 150_000])
def test_under_or_equal_bound_is_silent(handles, adapter, capsys, used):
    session = at_plan(handles, lambda: ticket.ContextReading(used, 1_000_000))
    session.present_plan(ticket.PlanCard(session.ticket, items=PARTS))
    assert delegation_lines(capsys) == [], "at or below the delegation bound must stay silent"
    assert not any(stamp["point"].startswith("Delegation") for stamp in session.store.read()[0]["stamps"])
    session.end()


def test_one_item_is_silent_even_when_context_is_over_bound(handles, adapter, capsys):
    session = at_plan(handles, lambda: ticket.ContextReading(900_000, 1_000_000))
    session.present_plan(ticket.PlanCard(session.ticket))
    assert delegation_lines(capsys) == [], "a one-item plan must never recommend delegation from its count"
    session.end()


@pytest.mark.parametrize("kind", ["absent", "none", "raises", "bool-used", "negative-used", "zero-window"])
def test_unusable_reading_is_silent_unstamped_and_continues(handles, adapter, capsys, kind):
    def reading():
        if kind == "raises":
            raise OSError("context unavailable")
        return {
            "none": None,
            "bool-used": ticket.ContextReading(True, 1_000_000),
            "negative-used": ticket.ContextReading(-5, 1_000_000),
            "zero-window": ticket.ContextReading(700_000, 0),
        }[kind]

    session = at_plan(handles, None if kind == "absent" else reading, context_floor_fraction=0.5)
    session.present_plan(ticket.PlanCard(session.ticket, items=PARTS))
    assert capsys.readouterr().out == "", "unusable context must print no advice"
    assert [stamp["point"] for stamp in session.store.read()[0]["stamps"]] == [
        "Waiting at plan go for plan revision 1."
    ], "unusable context must leave only the ordinary stop stamp"
    go_plan(session)
    session.begin_build()
    passing(session)
    assert session.phase == "outward_go" and session.live, "unusable context must not block the pass"
    session.end()


def test_resume_replan_does_not_repeat_and_keeps_stop_last(handles, adapter, capsys):
    reading = lambda: ticket.ContextReading(200_000, 1_000_000)
    session = at_plan(handles, reading)
    session.present_plan(ticket.PlanCard(session.ticket, items=PARTS))
    assert len(delegation_lines(capsys)) == 1
    resumed = ticket.TicketPass.open(KEY, Source(), handles(), pass_id=session.pass_id, context_reading=reading)
    assert resumed.plan.items == PARTS, "a resume must restore the named parts, not infer count from criteria"
    assert delegation_lines(capsys) == [], "resume must not replay the delegation line"
    # A changed plan in the same pass reaches the card again, but prints no second line.
    changed_source = Source(criteria=(*session.ticket.acceptance_criteria, "Verify the migrated records."))
    resumed = ticket.TicketPass.open(
        KEY, changed_source, handles(), pass_id=session.pass_id, context_reading=reading,
    )
    assert resumed.phase == "plan"
    resumed.present_plan(ticket.PlanCard(resumed.ticket, items=PARTS))
    assert delegation_lines(capsys) == [], "re-presenting a plan after resume must not repeat delegation advice"
    assert resumed.store.read()[0]["stamps"][-1]["point"] == "Waiting at plan go for plan revision 2."
    go_plan(resumed)
    resumed.begin_build()
    passing(resumed)
    assert resumed.store.read()[0]["stamps"][-1]["point"] == "Waiting at outward go after review round 1."
    resumed.end()


def test_estimate_uses_named_shared_costs_and_caller_floor(handles, adapter, monkeypatch, capsys):
    # A reordered table with tiny costs distinguishes name lookup from seed copies.
    monkeypatch.setattr(ticket, "load_phase_costs", lambda: dict(
        close=900, outward=800, review=30, build=70, plan=600, research=500, orient=400,
    ))
    session = at_plan(handles, lambda: ticket.ContextReading(101, 1_000), context_floor_fraction=0.3)
    session.present_plan(ticket.PlanCard(session.ticket, items=PARTS))
    lines = delegation_lines(capsys)
    assert len(lines) == 1 and "estimate 200 = 301 tokens > bound 300 (0.3 of 1000)" in lines[0], "budget must use build and review by name and the caller's floor"
    session.end()


@pytest.mark.parametrize("source", ["shipped", "override", "environment"])
def test_loop_discovery_follows_optional_addon_precedence(handles, adapter, monkeypatch, capsys, source):
    executable, shipped, override = adapter
    if source == "shipped":
        shipped.write_text(json.dumps({"loop_adapter": str(executable)}))
    elif source == "override":
        shipped.write_text(json.dumps({"loop_adapter": "none"}))
        override.write_text(json.dumps({"loop_adapter": str(executable)}))
    else:
        override.write_text(json.dumps({"loop_adapter": "none"}))
        monkeypatch.setenv("TICKET_LOOP_ADAPTER", str(executable))
    session = at_plan(handles, lambda: ticket.ContextReading(200_000, 1_000_000))
    session.present_plan(ticket.PlanCard(session.ticket, items=PARTS))
    assert "child-of" in delegation_lines(capsys)[0], "adapter discovery must honor env, override, then shipped config"
    session.end()


@pytest.mark.parametrize("problem", ["config", "relative", "missing", "not-executable", "costs"])
def test_unreadable_advisory_inputs_do_not_block(handles, adapter, monkeypatch, capsys, problem):
    if problem == "config":
        adapter[2].write_text("not json")
    elif problem == "costs":
        def unreadable_costs():
            raise OSError("cost table unavailable")
        monkeypatch.setattr(ticket, "load_phase_costs", unreadable_costs)
    else:
        setting = {"relative": "relative/adapter", "missing": str(adapter[0].with_name("missing")),
                   "not-executable": str(adapter[1])}[problem]
        monkeypatch.setenv("TICKET_LOOP_ADAPTER", setting)
    session = at_plan(handles, lambda: ticket.ContextReading(200_000, 1_000_000))
    session.present_plan(ticket.PlanCard(session.ticket, items=PARTS))
    assert delegation_lines(capsys) == [], "unreadable advice inputs must be silent"
    assert session.phase == "plan_go" and session.live
    assert session.store.read()[0]["stamps"][-1]["point"] == "Waiting at plan go for plan revision 1."
    session.end()


def test_older_plan_without_parts_resumes_as_one_item(handles, capsys):
    session = at_plan(handles, None)
    plan = ticket.PlanCard(session.ticket)
    snapshot = plan.snapshot(1, session.pass_id, session.ticket)
    snapshot.pop("items", None)
    restored = ticket.PlanCard.from_snapshot(snapshot)
    assert restored.items == (session.ticket.title,), "old cards name one ticket, not one item per criterion"
    assert capsys.readouterr().out == ""
    session.end()

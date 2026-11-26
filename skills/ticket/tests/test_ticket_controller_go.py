"""Matrix P: the open hand-off's controller, never a relaying seat, may give a go.

The fake always runs; LEDGER_REAL_CLI adds the same real-ledger rows using
section O's reserved-writer seed and isolated databases. P15 is the unchanged
A–O suite, including E16[role], L2's digests and G2's call order.
"""

import copy
import json
from pathlib import Path

import pytest

import ticket
from test_ticket_handoff import (
    DECLARED, HOLDER, Fake, backend, fake, fresh_card, handed_off, intent_id,
    open_handoff, open_ledger_id, record_of, refused,
)
from ticket_support import KEY, Source, passing, present, refusal


def at_stop(repo, backend, gate="plan"):
    session = open_handoff(repo, declared=DECLARED)
    present(session)
    if gate == "outward":
        session.record_go("plan", evidence="conversation:plan", verbatim=f"Go with the plan, {KEY}.")
        session.begin_build()
        passing(session)
    return session


def decision(session, gate="plan", **changes):
    revision, criteria = session._identity
    answer = {
        "who": "director", "who_kind": "role", "relayed_by": "relay-seat-1",
        "standing": "settled", "source": {"what": "ratification", "ref": "conversation:go"},
        "effect": f"The {gate} gate is approved for plan revision {revision}.",
        "tags": [{"type": "ticket", "id": KEY}], "verbatim": "Go with this plan.",
        "phase": "plan" if gate == "plan" else "review", "evidence": "conversation:go",
        "gate": gate, "plan_revision": revision, "criteria": list(criteria),
    }
    if gate == "plan":
        answer["pass"] = session.pass_id
    elif session.review_round:
        answer["round"] = session.review_round
    return {**answer, **changes}


def plant_go(session, gate="plan", **changes):
    answer = decision(session, gate, **changes)
    session._write([("decision", answer)])
    return answer


def test_P1_the_controllers_plan_go_opens_the_gate_on_both_handles(repo, backend):
    handed_off(backend)
    session = at_stop(repo, backend)
    plant_go(session)
    assert session.approved("plan"), "the open hand-off controller's plan go must count"
    resumed = open_handoff(repo, declared=DECLARED, pass_id=session.pass_id)
    assert resumed.approved("plan")
    session.begin_build()
    assert session.phase == resumed.phase == "build"


def test_P2_the_controllers_outward_go_opens_the_gate(repo, backend):
    handed_off(backend)
    session = at_stop(repo, backend, "outward")
    plant_go(session, "outward")
    assert session.approved("outward"), "the open hand-off controller's outward go must count"
    session.require_outward_go()


def test_P3_a_seat_roll_under_the_same_controller_keeps_the_go(repo, backend):
    handed_off(backend)
    session = at_stop(repo, backend)
    plant_go(session)
    session.end()
    backend.done()
    backend.hand_off(operation_id="build-2")
    resumed = open_handoff(repo, declared=DECLARED)
    assert resumed.phase == "build", "a seat roll must keep the same controller's plan go"
    assert not any("was presented by another pass and has no plan go" in line for line in resumed.reconciliation)


def record_role(session, gate="plan", **changes):
    arguments = {
        "who": "director", "relayed_by": "relay-seat-1", "plan_revision": session._identity[0],
        "evidence": "conversation:go", "verbatim": "Go with this plan.",
    }
    return session.record_role_go(gate, **{**arguments, **changes})


def role_refused(backend, session, call, code):
    before = backend.events() if backend else session.store.path.read_bytes()
    sent = len(backend.fake.appends()) if isinstance(backend, Fake) else None
    phase = session.phase
    with pytest.raises(ticket.GateRequired, match=code) as caught:
        call()
    assert KEY in str(caught.value)
    assert (backend.events() if backend else session.store.path.read_bytes()) == before, "a refusal must write nothing"
    if sent is not None:
        assert len(backend.fake.appends()) == sent, "a refusal must send no append-many"
    assert session._live and session.phase == phase, "a refused role go must keep the pass live at its stop"
    return str(caught.value)


@pytest.mark.parametrize("gate", ["plan", "outward"])
def test_P4_the_entry_writes_one_unbound_decision_under_the_fresh_revision(repo, backend, gate, monkeypatch):
    handed_off(backend)
    session = at_stop(repo, backend, gate)
    before = backend.events()
    token = session._token
    calls = []
    run = session.store._run

    def observe(*args, **kwargs):
        calls.append(args)
        return run(*args, **kwargs)

    def no_binding(*args, **kwargs):
        pytest.fail("a role go must never enter the person binder")

    monkeypatch.setattr(session.store, "_run", observe)
    monkeypatch.setattr(session, "_bind_recorded_go", no_binding)
    result = record_role(session, gate)
    assert result["bound"] is False, "a role go reports no election binding"
    landed = backend.events()[len(before):]
    assert [event["kind"] for event in landed] == ["item.note", "item.note"], (
        "a role go writes one decision note and the decided note beside it"
    )
    assert landed[1]["payload"]["text"].startswith("decided: "), "the second note is the decided note"
    assert landed[0]["payload"]["text"].startswith("ticket-decision:v2 ")
    assert record_of(landed[0]) == decision(session, gate), "the role note has the person's keys plus relayed_by only"
    assert [args[0] for args in calls] == ["query", "export-item", "append-many"], "the entry reads fresh and writes once"
    batch = calls[-1]
    assert batch[batch.index("--expect-revision") + 1] == token
    assert session.approved(gate)
    if gate == "plan":
        session.begin_build()
    else:
        session.require_outward_go()


def test_P4_a_lost_batch_answer_resumes_the_single_landed_go(repo, fake):
    handed_off(fake)
    session = at_stop(repo, fake)
    mark = len(fake.events())
    fake.fake.script("append-many", commit=True, stdout="", status=0)
    record_role(session)
    assert [event["kind"] for event in fake.events()[mark:]] == ["item.note", "item.note"]
    assert fake.events()[mark:][1]["payload"]["text"].startswith("decided: "), "the second note is the decided note"
    resumed = open_handoff(repo, declared=DECLARED, pass_id=session.pass_id)
    assert resumed.phase == "build"
    assert [event["kind"] for event in fake.events()[mark:]] == ["item.note", "item.note"], (
        "resume must not repeat the decision"
    )
    assert fake.events()[mark:][1]["payload"]["text"].startswith("decided: "), "the second note is the decided note"


def test_P4_another_writer_between_read_and_batch_is_stale_then_a_new_pass_reads_again(repo, fake):
    from test_ticket_ledger import _planted

    handed_off(fake)
    session = at_stop(repo, fake)
    mark = len(fake.events())
    fake.fake.script("append-many", commit=True, before=[
        _planted("item.note", {"text": "another writer"}, "other-writer"),
    ])
    with pytest.raises(ticket.StoreUnavailable) as caught:
        record_role(session)
    assert refusal(caught) == "stale", "the role go must bind to the fresh read's revision"
    assert not any(event["kind"] == "item.note" and event["payload"]["text"].startswith("ticket-decision:")
                   for event in fake.events()[mark:]), "a raced answer must not land"
    resumed = open_handoff(repo, declared=DECLARED)
    present(resumed)
    record_role(resumed)
    assert resumed.approved("plan")


@pytest.mark.parametrize("case", ["default", "v1"])
def test_P5a_without_a_hand_off_the_entry_refuses_and_keeps_the_pass_live(repo, backend, case):
    backend.file_row()
    if case == "v1":
        backend.take()
        backend.intent_v1()
    session = open_ledger_id(repo, holder=HOLDER)
    present(session)
    role_refused(backend, session, lambda: record_role(session), "role_go_no_handoff")


def test_P5a_markdown_has_no_controller(repo):
    from ticket_support import open_pass

    session = open_pass(repo, Source())
    present(session)
    role_refused(None, session, lambda: record_role(session), "role_go_no_handoff")


def test_P5b_after_a_handback_the_entry_names_the_closed_operation(repo, backend):
    handed_off(backend)
    backend.done()
    session = open_ledger_id(repo, holder=HOLDER)
    present(session)
    message = role_refused(backend, session, lambda: record_role(session), "role_go_handoff_closed")
    assert "build-1" in message


WRONG_ROLES = ["other", "Director", "director ", "dire", "dіrector", "", None, 7]


@pytest.mark.parametrize("who", WRONG_ROLES)
def test_P5c_only_the_exact_controller_may_be_recorded(repo, fake, who):
    handed_off(fake)
    session = at_stop(repo, fake)
    message = role_refused(fake, session, lambda: record_role(session, who=who), "role_go_not_controller")
    assert repr(who) in message and "director" in message


def test_P5c_a_previous_controller_cannot_record_on_the_next_hand_off(repo, backend):
    handed_off(backend, controller="previous")
    backend.done()
    backend.hand_off(operation_id="build-2")
    session = at_stop(repo, backend)
    role_refused(backend, session, lambda: record_role(session, who="previous"), "role_go_not_controller")


def break_handoff(fake, case):
    def change(state):
        intent = next(event for event in state["events"] if event["kind"] == "loop.intent")
        if case == "controller":
            intent["payload"]["controller"] = "not a token"
        elif case == "version":
            intent["payload_version"] = 3
        else:
            # Replace only the stop, keeping the revision fixed to expose a held-card decision.
            stop = state["events"][-1]
            assert stop["kind"] == "item.stopped"
            stop.update(kind="loop.intent", payload_version=2,
                        payload={**copy.deepcopy(intent["payload"]), "operation_id": "build-2"})
    fake.fake._change(change)


@pytest.mark.parametrize("case", ["controller", "version", "doubled"])
def test_P5d_the_entry_and_gate_read_the_live_hand_off_not_the_held_card(repo, fake, case):
    handed_off(fake)
    session = at_stop(repo, fake)
    # The held card has a controller and a go; the next fresh read must disprove it.
    plant_go(session)
    session._write([session._stamp("Waiting at plan go", ("ticket",), ())])
    break_handoff(fake, case)
    assert not session.approved("plan"), "an unreadable or doubled hand-off must open no gate"
    message = role_refused(fake, session, lambda: record_role(session), "role_go_controller_unreadable")
    assert ("handoff_ambiguous" if case == "doubled" else "handoff_unreadable") in message


@pytest.mark.parametrize("revision", [0, 2, "1", True, None])
def test_P5e_an_answer_for_another_revision_is_not_recorded(repo, fake, revision):
    handed_off(fake)
    session = at_stop(repo, fake)
    message = role_refused(fake, session, lambda: record_role(session, plan_revision=revision), "role_go_other_revision")
    assert repr(revision) in message and "1" in message


@pytest.mark.parametrize("case", ["unknown gate", "wrong plan phase", "wrong outward phase", "evidence", "words", "relay", "declined"])
def test_P5f_invalid_calls_write_nothing_and_do_not_read(repo, fake, case):
    handed_off(fake)
    session = at_stop(repo, fake, "outward" if case in ("wrong plan phase", "declined") else "plan")
    gate, arguments = "plan", {}
    if case == "unknown gate":
        gate = "Plan"
    elif case in ("wrong outward phase", "declined"):
        gate = "outward"
    if case == "declined":
        session.record_outward_declined(evidence="conversation:no", verbatim="Keep it local.")
    elif case in ("evidence", "words", "relay"):
        arguments[{"evidence": "evidence", "words": "verbatim", "relay": "relayed_by"}[case]] = " "
    before, sent = fake.events(), len(fake.fake.calls())
    with pytest.raises(ticket.TicketError):
        record_role(session, gate, **arguments)
    assert fake.events() == before
    assert len(fake.fake.calls()) == sent
    assert session._live


FAULTS = {
    **{f"who-{index}": {"who": who, "relayed_by": "director"} for index, who in enumerate(WRONG_ROLES)},
    "standing": {"standing": "lean"}, "source": {"source": {"what": "reply", "ref": "conversation:go"}},
    "tag": {"tags": []}, "revision": {"plan_revision": 9}, "criteria": {"criteria": ["Other criterion."]},
}


@pytest.mark.parametrize("gate", ["plan", "outward"])
@pytest.mark.parametrize("fault", list(FAULTS))
def test_P6_each_go_condition_alone_keeps_the_gate_shut(repo, fake, gate, fault):
    handed_off(fake)
    session = at_stop(repo, fake, gate)
    answer = decision(session, gate, **FAULTS[fault])
    # Invalid record shapes cannot be planted; the predicate still must fail closed on them.
    card = fresh_card(fake)
    card["decisions"] = [answer]
    assert not ticket._has_go(card, gate, session._identity, KEY), f"{gate} must reject {fault}"


def test_P6_the_presenting_pass_and_relay_are_required(repo, fake):
    handed_off(fake)
    session = at_stop(repo, fake)
    card = fresh_card(fake)
    card["decisions"] = [decision(session, **{"pass": "another-pass"})]
    assert not ticket._has_go(card, "plan", session._identity, KEY), "a plan go must name its presenting pass"
    for relay in (None, "", " "):
        answer = decision(session, relayed_by=relay)
        assert not ticket._is_go(answer, "plan", KEY, card), "a role go must have non-empty relayed_by"
    for handoff in (None, [], "director", {"controller": "not a token"}, {"controller": 7}):
        card["handoff"] = handoff
        answer = decision(session)
        if isinstance(handoff, dict) and isinstance(handoff.get("controller"), str):
            answer["who"] = handoff["controller"]
        assert not ticket._is_go(answer, "plan", KEY, card), "an unreadable handoff field is no controller"


def test_P7_a_handback_invalidates_the_waiting_pass_and_the_controllers_go(repo, backend):
    handed_off(backend)
    session = at_stop(repo, backend)
    plant_go(session)
    backend.done()
    with pytest.raises(ticket.StoreUnavailable) as caught:
        session.begin_build()
    assert refusal(caught) == "stale"
    refused(backend, lambda: open_handoff(repo, declared=DECLARED), "handoff_no_intent")
    resumed = open_ledger_id(repo, holder=HOLDER)
    assert resumed.phase == "plan"
    assert not resumed.approved("plan")
    card = fresh_card(backend)
    assert not ticket._has_go(card, "plan", session._identity, KEY), "a closed hand-off's controller is nobody"
    assert not ticket._is_go(decision(session, "outward"), "outward", KEY, card)


def test_P8_a_different_controller_invalidates_the_earlier_gos_at_both_gates(repo, backend):
    handed_off(backend)
    session = at_stop(repo, backend)
    plant_go(session)
    plant_go(session, "outward")
    session.end()
    backend.done()
    backend.hand_off(operation_id="build-2", controller="next-director")
    resumed = open_handoff(repo, declared=DECLARED)
    assert resumed.phase == "plan"
    card = fresh_card(backend)
    for gate in ("plan", "outward"):
        assert not ticket._has_go(card, gate, session._identity, KEY), "only the current controller's go may count"
    card["decisions"] = [decision(session, who="next-director")]
    assert ticket._has_go(card, "plan", session._identity, KEY), "the controller is not a constant role"


@pytest.mark.parametrize("version_one", [False, True], ids=["P10-no-handoff", "P9-version-one"])
def test_P9_P10_a_role_go_without_a_hand_off_opens_nothing(repo, backend, version_one):
    backend.file_row()
    if version_one:
        backend.take()
        backend.intent_v1()
    session = open_ledger_id(repo, holder=HOLDER)
    present(session)
    plant_go(session)
    assert not session.approved("plan"), "a role go without an open hand-off must not count"
    with pytest.raises(ticket.GateRequired):
        session.begin_build()
    session.record_go("plan", evidence="conversation:person", verbatim=f"Go, {KEY}.")
    session.begin_build()
    passing(session)
    plant_go(session, "outward")
    assert not session.approved("outward")
    with pytest.raises(ticket.GateRequired):
        session.require_outward_go()


def test_P11_a_note_target_and_closed_hand_off_are_not_the_controller(repo, fake):
    handed_off(fake, controller="other")
    fake.done()
    fake.write("item.note", {"text": "controller: other"})
    fake.hand_off(operation_id="build-2")
    def target(state):
        state["events"][-2]["payload"]["target"] = "other"
    fake.fake._change(target)
    session = at_stop(repo, fake)
    plant_go(session, who="other")
    assert not session.approved("plan"), "notes, targets and closed hand-offs confer no authority"
    plant_go(session)
    assert session.approved("plan")


@pytest.mark.parametrize("holder", [HOLDER, None])
def test_P12_the_controller_go_cannot_bypass_the_hand_off_source(repo, backend, holder, monkeypatch):
    handed_off(backend)
    session = at_stop(repo, backend)
    plant_go(session)
    plant_go(session, "outward")
    session.end()
    def no_derivation(*args, **kwargs):
        pytest.fail("another source must refuse before deriving pass state")
    monkeypatch.setattr(ticket, "_derive", no_derivation)
    refused(backend, lambda: open_ledger_id(repo, holder=holder), "handoff_source_required")


@pytest.mark.parametrize("hand_off", [False, True])
def test_P13_a_persons_go_is_unchanged_and_binds_even_on_a_hand_off(repo, backend, hand_off, monkeypatch):
    from test_ticket_ledger import _capture, _prompt
    import subprocess

    monkeypatch.setenv("LEDGER_SESSION", "controller-person-test")
    if hand_off:
        handed_off(backend)
        session = at_stop(repo, backend)
    else:
        backend.file_row()
        session = open_ledger_id(repo)
        present(session)
    words = f"Go with this plan, {KEY}."
    if isinstance(backend, Fake):
        _capture(backend.fake, _prompt("controller-person-test", text=words))
    else:
        hook = {"hook_event_name": "UserPromptSubmit", "prompt": words,
                "session_id": "controller-person-test", "prompt_id": "person-go-1"}
        subprocess.run([backend.path, "--json", "capture", "--kind", "prompt"], input=json.dumps(hook),
                       text=True, check=True, capture_output=True)
    result = session.record_go("plan", evidence="conversation:go", verbatim=words)
    assert result["bound"] is True, "a person's captured words still bind"
    notes = [event for event in backend.events() if event["kind"] == "item.note"
             and event["payload"]["text"].startswith("ticket-decision:")]
    person = record_of(notes[-1])
    expected = decision(session)
    expected.pop("relayed_by")
    expected.update(who="the person", who_kind="person", verbatim=words)
    assert person == expected, "record_go's person shape must be unchanged"
    assert session.approved("plan"), "a person's go never depends on a hand-off"
    session.begin_build()
    passing(session)
    session.record_go("outward", evidence="conversation:outward", verbatim=f"Send it, {KEY}.")
    assert session.approved("outward")


def test_P14_a_role_decline_shuts_outward_but_cannot_authorize_a_local_close(repo, fake):
    handed_off(fake)
    session = at_stop(repo, fake, "outward")
    decline = decision(session, "outward")
    decline.pop("gate")
    decline["declined"] = "outward"
    session._write([("decision", decline)])
    assert not ticket._has_declined(session._card, session._identity, KEY), "a role decline is not the person's no"
    with pytest.raises(ticket.GateRequired):
        session.close_local()
    plant_go(session, "outward")
    assert not session.approved("outward"), "any decline shuts outward even beside the controller's go"
    with pytest.raises(ticket.GateRequired):
        session.require_outward_go()


def test_P16_the_skill_explains_controller_answers_and_no_answer():
    text = (Path(ticket.__file__).parent / "SKILL.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    for phrase in (
        "record_role_go", "role_go_no_handoff", "role_go_handoff_closed", "role_go_controller_unreadable",
        "role_go_not_controller", "role_go_other_revision",
        "An answer that did not come from the person typing here is a role go",
        "No answer writes nothing",
    ):
        assert phrase in flat, phrase

"""Matrix sections C and D: a stored identity that cannot be read, and records
that no longer cover the card's plan."""

import os

import pytest

import ticket
from ticket_support import (
    A, B, B_EDITED, F8, F11_go, F11_review, Source, assert_only_a_claim, build, build_released, card_bytes,
    card_path, changes, edit_card, go_plan, open_pass, present, read_card, refusal, stage_of, write_card,
)

STAGES = ["S1", "S2[1]", "S3[1]", "S4[1]", "S5", "S6"]
REVIEWED_STAGES = ["S2[1]", "S3[1]", "S4[1]", "S5", "S6"]


def _refused_at(repo, stage, change, code="plan_unidentifiable"):
    """Build the stage, end its pass, apply ``change`` to the card, and require the open to refuse."""

    src = Source()
    base, rounds = stage_of(stage)
    build_released(repo, src, base, rounds)
    edit_card(repo, change)
    before = card_bytes(repo)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_pass(repo, src)
    assert refusal(excinfo) == code
    # Equal bytes: no claim and nothing else was written.
    assert card_bytes(repo) == before


def _set_plan(value):
    def change(card):
        card["plan"] = value
    return change


def _set_field(name, value):
    def change(card):
        card["plan"][name] = value
    return change


def _drop_field(name):
    def change(card):
        del card["plan"][name]
    return change


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("shape", [{}, [], ""], ids=["{}", "[]", "empty text"])
def test_C1_an_empty_stored_plan_is_refused(repo, stage, shape):
    _refused_at(repo, stage, _set_plan(shape))


@pytest.mark.parametrize("stage", STAGES)
def test_C2_a_boolean_revision_is_unidentifiable(repo, stage):
    _refused_at(repo, stage, _set_field("revision", True))


@pytest.mark.parametrize("stage", STAGES)
def test_C3_a_float_revision_is_unidentifiable(repo, stage):
    _refused_at(repo, stage, _set_field("revision", 1.0))


@pytest.mark.parametrize("stage", STAGES)
def test_C4_a_text_revision_is_unidentifiable(repo, stage):
    _refused_at(repo, stage, _set_field("revision", "1"))


@pytest.mark.parametrize("stage", STAGES)
def test_C5_a_zero_revision_is_unidentifiable(repo, stage):
    _refused_at(repo, stage, _set_field("revision", 0))


@pytest.mark.parametrize("stage", STAGES)
def test_C6_a_missing_revision_is_unidentifiable(repo, stage):
    _refused_at(repo, stage, _drop_field("revision"))


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("criteria", [{"0": A}, A, 5], ids=["mapping", "text", "number"])
def test_C7_criteria_that_are_not_a_list_are_unidentifiable(repo, stage, criteria):
    _refused_at(repo, stage, _set_field("criteria", criteria))


@pytest.mark.parametrize("stage", STAGES)
def test_C8_an_empty_criteria_list_is_unidentifiable(repo, stage):
    _refused_at(repo, stage, _set_field("criteria", []))


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("entry", [5, {"text": B}, None], ids=["number", "object", "null"])
def test_C9_a_criterion_that_is_not_text_is_unidentifiable(repo, stage, entry):
    _refused_at(repo, stage, _set_field("criteria", [A, entry]))


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("entry", ["", "   "], ids=["empty", "blank"])
def test_C10_an_empty_criterion_text_is_unidentifiable(repo, stage, entry):
    _refused_at(repo, stage, _set_field("criteria", [A, entry]))


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("value", ["absent", "", 7], ids=["absent", "empty", "number"])
def test_C11_a_plan_without_its_presenting_pass_is_unidentifiable(repo, stage, value):
    _refused_at(repo, stage, _drop_field("pass") if value == "absent" else _set_field("pass", value))


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("shape", [[A, B], 3, "x"], ids=["list", "number", "text"])
def test_C12_a_plan_that_is_not_an_object_is_unidentifiable(repo, stage, shape):
    _refused_at(repo, stage, _set_plan(shape))


@pytest.mark.parametrize("stage", REVIEWED_STAGES)
@pytest.mark.parametrize("value", ["missing", "2", 2.0], ids=["missing", "text", "float"])
def test_C13_a_review_without_a_whole_number_round_is_unreadable(repo, stage, value):
    def change(card):
        if value == "missing":
            del card["reviews"][-1]["round"]
        else:
            card["reviews"][-1]["round"] = value

    _refused_at(repo, stage, change, code="card_unreadable")


def test_C14_a_live_pass_whose_plan_is_corrupted_refuses_its_next_write(repo):
    session = build(repo, Source(), "S2", 0)
    stat = os.stat(card_path(repo))
    card = read_card(repo)
    card["plan"] = {}
    write_card(repo, card)
    os.utime(card_path(repo), ns=(stat.st_atime_ns, stat.st_mtime_ns))
    before = read_card(repo)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        session.record_review("changes", evidence="reviews/round-1.md")
    assert refusal(excinfo) == "stale"
    assert read_card(repo) == {**before, "pass": None}
    assert session.live is False


def test_C15_an_unidentifiable_plan_wins_over_a_valid_close(repo):
    _refused_at(repo, "S6", _set_field("revision", True))


@pytest.mark.parametrize("stage", STAGES)
def test_C16_a_plan_with_an_unknown_schema_is_unreadable(repo, stage):
    _refused_at(repo, stage, _set_field("schema", "ticket-plan-card/v999"), code="card_unreadable")


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("name", ["schema", "key", "title", "steps", "open_questions"])
def test_C17_a_plan_missing_a_required_field_is_unreadable(repo, stage, name):
    _refused_at(repo, stage, _drop_field(name), code="card_unreadable")


C18_FAULTS = {
    "other key": ("key", "EK-99"),
    "blank title": ("title", "  "),
    "status non-text": ("ticket_status", 5),
    "updated non-text": ("ticket_updated_at", 5),
    "open_questions not text": ("open_questions", [""]),
    "unknown field": ("passes", [True, True]),
    "steps not a list": ("steps", "orient"),
    "diff without a skipped plan step": ("diff_statement", "Add the guard."),
}


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("fault", list(C18_FAULTS))
def test_C18_a_plan_with_a_malformed_field_is_unreadable(repo, stage, fault):
    name, value = C18_FAULTS[fault]
    _refused_at(repo, stage, _set_field(name, value), code="card_unreadable")


@pytest.mark.parametrize("stage", STAGES)
def test_C19_an_identity_fault_wins_over_a_non_identity_fault(repo, stage):
    def change(card):
        card["plan"].update(schema="ticket-plan-card/v999", revision=True)

    _refused_at(repo, stage, change)


# --- D: card-side changes ----------------------------------------------------


def test_D1_a_plan_go_is_not_reused_for_criteria_edited_on_the_card_and_the_source(repo):
    src = Source()
    build_released(repo, src, "S2", 0)
    src.edit("reorder")
    before = edit_card(repo, lambda card: card["plan"].update(criteria=[B, A]))
    session = open_pass(repo, src)
    assert session.phase == "plan"
    assert session.reconciliation == (F11_go(1),)
    assert session.approved("plan") is False
    assert_only_a_claim(repo, before)


def test_D2_a_plan_go_is_not_reused_when_only_an_earlier_revision_was_reviewed(repo):
    src = Source()
    build_released(repo, src, "S2", 1)
    src.edit("text")
    session = open_pass(repo, src)
    present(session)
    go_plan(session)
    session.end()
    src.edit("reorder")
    edit_card(repo, lambda card: card["plan"].update(criteria=[B_EDITED, A]))
    session = open_pass(repo, src)
    assert session.phase == "plan"
    assert session.reconciliation == (F11_go(2),)
    assert session.approved("plan") is False


def test_D3_a_card_and_source_reordered_together_after_a_passing_review_resume_at_plan(repo):
    src = Source()
    build_released(repo, src, "S3", 1)
    src.edit("reorder")
    edit_card(repo, lambda card: card["plan"].update(criteria=[B, A]))
    session = open_pass(repo, src)
    assert session.phase == "plan"
    assert session.reconciliation == (F11_review(1, 1), F11_go(1))
    session.end()
    again = open_pass(repo, src)
    assert again.phase == "plan"
    assert again.reconciliation == (F11_review(1, 1), F11_go(1))
    present(again)
    go_plan(again)
    again.begin_build()
    assert again.phase == "build"


def test_D4_a_review_round_covers_nothing_once_the_card_plan_differs_from_its_criteria(repo):
    src = Source()
    build_released(repo, src, "S3", 1)
    src.edit("reorder")

    def reorder_plan_and_go(card):
        card["plan"]["criteria"] = [B, A]
        card["decisions"][0]["criteria"] = [B, A]

    edit_card(repo, reorder_plan_and_go)
    session = open_pass(repo, src)
    assert session.phase == "plan"
    assert session.reconciliation == (F11_review(1, 1),)


def _live_at(repo, step):
    src = Source()
    if step == "begin_build":
        session = build(repo, src, "S1")
        go_plan(session)
    elif step == "record_review":
        session = build(repo, src, "S2", 0)
    elif step == "record_go outward":
        session = build(repo, src, "S3", 1)
    else:
        session = build(repo, src, "S4", 1)
    return session


def _run_step(session, step):
    if step == "begin_build":
        session.begin_build()
    elif step == "record_review":
        session.record_review("changes", evidence="reviews/round-1.md")
    elif step == "record_go outward":
        session.record_go("outward", evidence="conversation:outward-go", verbatim="Open it.")
    elif step == "require_outward_go":
        session.require_outward_go()
    else:
        session.close("Done.", refs=("example/repo#14",))


@pytest.mark.parametrize("step", ["begin_build", "record_review", "record_go outward", "require_outward_go", "close"])
def test_D5_a_card_plan_edited_under_a_live_pass_refuses_the_next_step(repo, step):
    session = _live_at(repo, step)
    edited = edit_card(repo, lambda card: card["plan"].update(criteria=[B, A]))
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        _run_step(session, step)
    assert refusal(excinfo) == "stale"
    assert session.live is False
    assert read_card(repo) == {**edited, "pass": None}


def test_D6_an_outward_go_for_other_criteria_authorizes_nothing(repo):
    src = Source()
    build_released(repo, src, "S4", 1)
    edit_card(repo, lambda card: card["decisions"][-1].update(criteria=[B, A]))
    session = open_pass(repo, src)
    assert session.phase == "outward_go"
    assert session.approved("outward") is False
    with pytest.raises(ticket.GateRequired):
        session.require_outward_go()
    with pytest.raises(ticket.GateRequired):
        session.close("Done.", refs=("example/repo#14",))


def test_D7_a_plan_go_naming_another_pass_opens_nothing(repo):
    src = Source()
    first = build(repo, src, "S1")
    plan = read_card(repo)["plan"]
    go = {
        "who": "the person", "who_kind": "person", "standing": "settled",
        "source": {"what": "ratification", "ref": "conversation:plan-go"},
        "effect": "The plan gate is approved for plan revision 1.",
        "tags": [{"type": "ticket", "id": "EK-14"}], "verbatim": "Go.", "phase": "plan",
        "gate": "plan", "plan_revision": 1, "criteria": plan["criteria"], "pass": "0123456789abcdef",
    }
    edit_card(repo, lambda card: card["decisions"].append(go))
    joined = open_pass(repo, src, pass_id=first.pass_id)
    assert joined.phase == "plan_go"
    with pytest.raises(ticket.GateRequired):
        joined.begin_build()
    joined.end()
    session = open_pass(repo, src)
    assert session.phase == "plan"
    assert session.reconciliation == (F8(1),)


def test_D8_a_review_of_an_earlier_revision_is_history_not_a_flag(repo):
    src = Source()
    build_released(repo, src, "S2", 1)
    src.edit("text")
    session = open_pass(repo, src)
    present(session)
    go_plan(session)
    session.end()
    session = open_pass(repo, src)
    assert session.phase == "build"
    assert session.reconciliation == ()
    assert session.review_round == 1

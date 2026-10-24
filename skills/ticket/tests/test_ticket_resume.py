"""Matrix sections A and B: resume at every card state under every source edit.

"Nothing written" on a resume that takes a new claim means the card is equal
but for that claim; a terminal resume takes no claim, so its card bytes are
equal before and after.
"""

import pytest

import ticket
from ticket_support import (
    A, B, B_EDITED, C, EDIT_WHAT, F1, F2, F3, F4, F5, F6, F7, F8, F9, F10, F12, KEY, REORDERED, STATUS, TITLE, UPDATED, Source,
    assert_only_a_claim, build, build_released, card_bytes, changes, edit_card, go_plan,
    new_text, open_pass, passing, present, read_card, refusal, removed_text,
)


# --- A0, A1: a blank card and a presented plan -------------------------------


def test_A0_0_a_blank_card_resumes_at_orient(repo):
    session = open_pass(repo, Source())
    assert session.phase == "orient"
    assert session.reconciliation == ()
    card = read_card(repo)
    assert card["pass"]["id"] == session.pass_id
    assert {**card, "pass": None} == ticket._blank_card(KEY, card["title"])


def test_A1_0a_a_presented_plan_resumes_at_plan_go_for_its_own_pass(repo):
    src = Source()
    first = build(repo, src, "S1")
    before = card_bytes(repo)
    joined = open_pass(repo, src, pass_id=first.pass_id)
    assert joined.phase == "plan_go"
    assert joined.reconciliation == ()
    with pytest.raises(ticket.GateRequired):
        joined.begin_build()
    assert card_bytes(repo) == before


def test_A1_0b_a_presented_plan_resumes_at_plan_for_another_pass(repo):
    src = Source()
    build_released(repo, src, "S1")
    session = open_pass(repo, src)
    assert session.phase == "plan"
    assert session.reconciliation == (F8(1),)
    present(session)
    plan = read_card(repo)["plan"]
    assert (plan["revision"], plan["pass"]) == (2, session.pass_id)


def _resume_after_edit(repo, stage, rounds, kind):
    src = Source()
    build_released(repo, src, stage, rounds)
    src.edit(kind)
    before = read_card(repo)
    session = open_pass(repo, src)
    return src, session, before


def test_A1_1_a_source_change_before_the_plan_go_resumes_at_plan_reorder(repo):
    src, session, before = _resume_after_edit(repo, "S1", 0, "reorder")
    assert session.phase == "plan"
    assert session.reconciliation == (F1(1, REORDERED),)
    assert_only_a_claim(repo, before)
    assert session.plan.acceptance_criteria == (A, B)
    present(session)
    plan = read_card(repo)["plan"]
    assert (plan["revision"], plan["criteria"]) == (2, [B, A])


def test_A1_2_a_source_change_before_the_plan_go_resumes_at_plan_text(repo):
    src, session, before = _resume_after_edit(repo, "S1", 0, "text")
    assert session.phase == "plan"
    assert session.reconciliation == (F1(1, f"{new_text(B_EDITED)}; {removed_text(B)}"),)
    assert_only_a_claim(repo, before)


def test_A1_3_a_source_change_before_the_plan_go_resumes_at_plan_added(repo):
    src, session, before = _resume_after_edit(repo, "S1", 0, "added")
    assert session.phase == "plan"
    assert session.reconciliation == (F1(1, new_text(C)),)
    assert_only_a_claim(repo, before)


def test_A1_4_a_source_change_before_the_plan_go_resumes_at_plan_removed(repo):
    src, session, before = _resume_after_edit(repo, "S1", 0, "removed")
    assert session.phase == "plan"
    assert session.reconciliation == (F1(1, removed_text(B)),)
    assert_only_a_claim(repo, before)


# --- A2: building ------------------------------------------------------------


def test_A2_0a_building_resumes_at_build_with_rounds_counted(repo):
    src = Source()
    build_released(repo, src, "S2", 2)
    session = open_pass(repo, src)
    assert session.phase == "build"
    assert session.review_round == 2
    assert session.reconciliation == ()
    with pytest.raises(ticket.TicketError, match="expected phase plan_go") as excinfo:
        session.begin_build()
    assert not isinstance(excinfo.value, ticket.GateRequired)
    changes(session)
    assert (session.phase, session.live) == ("stopped", False)


def test_A2_0b_rounds_of_an_earlier_revision_count_toward_the_cap(repo):
    src = Source()
    build_released(repo, src, "S2", 2)
    src.edit("text")
    session = open_pass(repo, src)
    present(session)
    go_plan(session)
    session.begin_build()
    session.end()
    session = open_pass(repo, src)
    assert session.phase == "build"
    assert session.review_round == 2
    changes(session)
    assert (session.phase, session.live) == ("stopped", False)
    assert len(read_card(repo)["reviews"]) == 3
    again = open_pass(repo, src)
    assert again.phase == "stopped"
    assert again.reconciliation == (F5(2),)


def test_A2_1_a_source_change_while_building_resumes_at_plan_reorder(repo):
    src, session, before = _resume_after_edit(repo, "S2", 1, "reorder")
    assert session.phase == "plan"
    assert session.reconciliation == (F1(1, REORDERED),)
    assert_only_a_claim(repo, before)
    present(session)
    assert session.approved("plan") is False


def test_A2_2_a_source_change_while_building_resumes_at_plan_text(repo):
    src, session, before = _resume_after_edit(repo, "S2", 1, "text")
    assert session.phase == "plan"
    assert session.reconciliation == (F1(1, EDIT_WHAT["text"]),)


def test_A2_3_a_source_change_while_building_resumes_at_plan_added(repo):
    src, session, before = _resume_after_edit(repo, "S2", 0, "added")
    assert session.phase == "plan"
    assert session.reconciliation == (F1(1, new_text(C)),)


def test_A2_4_a_source_change_while_building_resumes_at_plan_removed(repo):
    src, session, before = _resume_after_edit(repo, "S2", 2, "removed")
    assert session.phase == "plan"
    assert session.reconciliation == (F1(1, removed_text(B)),)
    assert session.review_round == 2


# --- A3, A4: a passing round, and its outward go ----------------------------


def test_A3_0_a_passing_round_resumes_at_outward_go(repo):
    src = Source()
    build_released(repo, src, "S3", 1)
    before = read_card(repo)
    session = open_pass(repo, src)
    assert session.phase == "outward_go"
    assert session.approved("outward") is False
    with pytest.raises(ticket.GateRequired):
        session.require_outward_go()
    assert_only_a_claim(repo, before)


def test_A3_1_a_source_change_after_a_passing_round_resumes_at_plan_reorder(repo):
    src, session, before = _resume_after_edit(repo, "S3", 1, "reorder")
    assert session.phase == "plan"
    assert session.reconciliation == (F2(1, 1, REORDERED),)
    assert_only_a_claim(repo, before)
    assert session.approved("outward") is False
    present(session)
    go_plan(session)
    session.begin_build()
    assert session.phase == "build"
    passing(session)
    assert read_card(repo)["reviews"][-1]["round"] == 2


def test_A3_2_a_source_change_after_a_passing_round_resumes_at_plan_text(repo):
    src, session, before = _resume_after_edit(repo, "S3", 2, "text")
    assert session.phase == "plan"
    assert session.reconciliation == (F2(2, 1, EDIT_WHAT["text"]),)


def test_A3_3_a_source_change_after_a_passing_round_resumes_at_plan_added(repo):
    src, session, before = _resume_after_edit(repo, "S3", 1, "added")
    assert session.phase == "plan"
    assert session.reconciliation == (F2(1, 1, new_text(C)),)


def test_A3_4_a_source_change_after_a_passing_round_resumes_at_plan_removed(repo):
    src, session, before = _resume_after_edit(repo, "S3", 1, "removed")
    assert session.phase == "plan"
    assert session.reconciliation == (F2(1, 1, removed_text(B)),)


def test_A4_0_an_approved_pass_resumes_at_outward_go_with_the_go(repo):
    src = Source()
    build_released(repo, src, "S4", 1)
    before = read_card(repo)
    session = open_pass(repo, src)
    assert session.phase == "outward_go"
    assert session.approved("outward") is True
    session.require_outward_go()
    assert_only_a_claim(repo, before)
    session.end()

    def other_criteria(card):
        card["decisions"][-1]["criteria"] = [B, A]

    edit_card(repo, other_criteria)
    session = open_pass(repo, src)
    assert session.phase == "outward_go"
    assert session.approved("outward") is False


def test_A4_1_a_source_change_after_the_outward_go_spends_it_reorder(repo):
    src, session, before = _resume_after_edit(repo, "S4", 1, "reorder")
    assert session.phase == "plan"
    assert session.reconciliation == (F2(1, 1, REORDERED), F4(1))
    assert_only_a_claim(repo, before)
    assert session.approved("outward") is False


def test_A4_2_a_source_change_after_the_outward_go_spends_it_text(repo):
    src, session, before = _resume_after_edit(repo, "S4", 1, "text")
    assert session.phase == "plan"
    assert session.reconciliation == (F2(1, 1, EDIT_WHAT["text"]), F4(1))


def test_A4_3_a_source_change_after_the_outward_go_spends_it_added(repo):
    src, session, before = _resume_after_edit(repo, "S4", 2, "added")
    assert session.phase == "plan"
    assert session.reconciliation == (F2(2, 1, new_text(C)), F4(1))


def test_A4_4_a_source_change_after_the_outward_go_spends_it_removed(repo):
    src, session, before = _resume_after_edit(repo, "S4", 1, "removed")
    assert session.phase == "plan"
    assert session.reconciliation == (F2(1, 1, removed_text(B)), F4(1))


# --- A5: the review cap ------------------------------------------------------


@pytest.mark.parametrize("cap_stamp", ["present", "deleted"])
def test_A5_0_three_rounds_without_a_pass_resume_stopped_and_take_no_claim(repo, cap_stamp):
    src = Source()
    build(repo, src, "S5")
    if cap_stamp == "deleted":
        edit_card(repo, lambda card: card["stamps"].pop())
        assert all(stamp["point"] != ticket.CAP_POINT for stamp in read_card(repo)["stamps"])
    before = card_bytes(repo)
    session = open_pass(repo, src)
    assert (session.phase, session.live) == ("stopped", False)
    assert session.reconciliation == (F5(1),)
    assert read_card(repo)["pass"] is None
    assert card_bytes(repo) == before


def test_A5_1_a_round_three_pass_of_an_earlier_revision_is_the_cap(repo):
    src = Source()
    build_released(repo, src, "S3", 3)

    def revision_two_with_its_go(card):
        card["plan"]["revision"] = 2
        go = dict(card["decisions"][0], plan_revision=2)
        card["decisions"].append(go)

    edit_card(repo, revision_two_with_its_go)
    before = card_bytes(repo)
    session = open_pass(repo, src)
    assert (session.phase, session.live) == ("stopped", False)
    assert session.reconciliation == (F5(2),)
    assert not any("passed plan revision 2" in flag for flag in session.reconciliation)
    assert card_bytes(repo) == before


def _resume_at_cap(repo, kind):
    src = Source()
    build(repo, src, "S5")
    src.edit(kind)
    before = card_bytes(repo)
    session = open_pass(repo, src)
    return session, before


def test_A5_2_a_source_change_at_the_cap_stays_stopped_reorder(repo):
    session, before = _resume_at_cap(repo, "reorder")
    assert (session.phase, session.live) == ("stopped", False)
    assert session.reconciliation == (F5(1), F6(1, REORDERED))
    assert card_bytes(repo) == before
    assert read_card(repo)["pass"] is None


def test_A5_3_a_source_change_at_the_cap_stays_stopped_text(repo):
    session, before = _resume_at_cap(repo, "text")
    assert session.phase == "stopped"
    assert session.reconciliation == (F5(1), F6(1, EDIT_WHAT["text"]))


def test_A5_4_a_source_change_at_the_cap_stays_stopped_added(repo):
    session, before = _resume_at_cap(repo, "added")
    assert session.phase == "stopped"
    assert session.reconciliation == (F5(1), F6(1, new_text(C)))


def test_A5_5_a_source_change_at_the_cap_stays_stopped_removed(repo):
    session, before = _resume_at_cap(repo, "removed")
    assert session.phase == "stopped"
    assert session.reconciliation == (F5(1), F6(1, removed_text(B)))
    assert card_bytes(repo) == before


# --- A6: closed --------------------------------------------------------------


def test_A6_0_a_closed_card_resumes_closed_and_takes_no_claim(repo):
    src = Source()
    build(repo, src, "S6")
    before = card_bytes(repo)
    session = open_pass(repo, src)
    assert (session.phase, session.live) == ("closed", False)
    assert session.reconciliation == ()
    assert card_bytes(repo) == before


def _resume_closed(repo, kind):
    src = Source()
    build(repo, src, "S6")
    src.edit(kind)
    before = card_bytes(repo)
    session = open_pass(repo, src)
    assert card_bytes(repo) == before
    return session


def test_A6_1_a_source_change_after_close_keeps_the_ticket_closed_reorder(repo):
    session = _resume_closed(repo, "reorder")
    assert session.phase == "closed"
    assert session.reconciliation == (F7(REORDERED),)


def test_A6_2_a_source_change_after_close_keeps_the_ticket_closed_text(repo):
    session = _resume_closed(repo, "text")
    assert session.phase == "closed"
    assert session.reconciliation == (F7(EDIT_WHAT["text"]),)


def test_A6_3_a_source_change_after_close_keeps_the_ticket_closed_added(repo):
    session = _resume_closed(repo, "added")
    assert session.phase == "closed"
    assert session.reconciliation == (F7(new_text(C)),)


def test_A6_4_a_source_change_after_close_keeps_the_ticket_closed_removed(repo):
    session = _resume_closed(repo, "removed")
    assert session.phase == "closed"
    assert session.reconciliation == (F7(removed_text(B)),)


# --- A7, A8: flags that are not changes; duplicate criteria -----------------


def test_A7_status_title_and_updated_at_are_flags_not_changes(repo):
    src = Source()
    build_released(repo, src, "S3", 1)
    src.status, src.title, src.updated_at = "In Review", "A retitled ticket", "2026-10-18T09:00:00Z"
    session = open_pass(repo, src)
    assert session.phase == "outward_go"
    assert session.reconciliation == (F9("In Progress", "In Review"), F10, F12(TITLE, "A retitled ticket"))
    assert session.plan.review["round"] == 1
    assert "passes: true" in session.plan.render()


def test_A9_a_title_only_change_is_a_flag(repo):
    src = Source()
    build_released(repo, src, "S3", 1)
    src.title = "A retitled ticket"
    before = read_card(repo)
    session = open_pass(repo, src)
    assert session.phase == "outward_go"
    assert session.reconciliation == (F12(TITLE, "A retitled ticket"),)
    assert_only_a_claim(repo, before)


@pytest.mark.parametrize("direction", ["appears", "disappears"])
def test_A10_an_update_time_that_appears_or_disappears_is_a_flag(repo, direction):
    src = Source()
    if direction == "appears":
        src.updated_at = None
    build_released(repo, src, "S3", 1)
    assert ("ticket_updated_at" in read_card(repo)["plan"]) is (direction == "disappears")
    src.updated_at = UPDATED if direction == "appears" else None
    before = read_card(repo)
    session = open_pass(repo, src)
    assert session.phase == "outward_go"
    assert session.reconciliation == (F10,)
    assert_only_a_claim(repo, before)


@pytest.mark.parametrize("change", ["status", "title", "updated_at", "all three", "criteria and all three"])
def test_A11_metadata_changes_on_a_closed_card_are_flags(repo, change):
    src = Source()
    build_released(repo, src, "S6")
    flags = []
    if change == "criteria and all three":
        src.edit("reorder")
        flags.append(F7(REORDERED))
    if change in ("status", "all three", "criteria and all three"):
        src.status = "Done"
        flags.append(F9(STATUS, "Done"))
    if change in ("updated_at", "all three", "criteria and all three"):
        src.updated_at = "2026-10-18T09:00:00Z"
        flags.append(F10)
    if change in ("title", "all three", "criteria and all three"):
        src.title = "A retitled ticket"
        flags.append(F12(TITLE, "A retitled ticket"))
    before = card_bytes(repo)
    session = open_pass(repo, src)
    assert session.phase == "closed"
    assert session.reconciliation == tuple(flags)
    assert card_bytes(repo) == before


def _duplicates(repo, built_with, source_now):
    src = Source(built_with)
    build_released(repo, src, "S1")
    src.criteria = list(source_now)
    before = read_card(repo)
    session = open_pass(repo, src)
    return session, before


def test_A8_1_a_duplicate_criterion_added_is_a_change(repo):
    session, before = _duplicates(repo, ["A"], ["A", "A"])
    assert session.phase == "plan"
    assert session.reconciliation == (F1(1, new_text("A")),)
    assert_only_a_claim(repo, before)
    present(session)
    plan = read_card(repo)["plan"]
    assert (plan["revision"], plan["criteria"]) == (2, ["A", "A"])


def test_A8_2_a_duplicate_criterion_removed_is_a_change(repo):
    session, before = _duplicates(repo, ["A", "A"], ["A"])
    assert session.phase == "plan"
    assert session.reconciliation == (F1(1, removed_text("A")),)
    assert_only_a_claim(repo, before)


def test_A8_3_a_duplicate_criterion_moved_is_a_reorder(repo):
    session, before = _duplicates(repo, ["A", "A", "B"], ["A", "B", "A"])
    assert session.phase == "plan"
    assert session.reconciliation == (F1(1, REORDERED),)
    assert_only_a_claim(repo, before)


@pytest.mark.parametrize(
    "built_with, source_now, what",
    [
        (["A"], ["A", "A", "A"], f"{new_text('A')}; {new_text('A')}"),
        (["A", "A", "A"], ["A"], f"{removed_text('A')}; {removed_text('A')}"),
    ],
    ids=["added twice", "removed twice"],
)
def test_A8_4_each_extra_or_missing_occurrence_is_named_once(repo, built_with, source_now, what):
    session, before = _duplicates(repo, built_with, source_now)
    assert session.reconciliation == (F1(1, what),)


# --- B: a second edit after a re-presented plan; an edit at the cap ---------


def _re_presented(repo):
    """S3[1], then edit(text), then revision 2 presented by a new pass."""

    src = Source()
    build_released(repo, src, "S3", 1)
    src.edit("text")
    session = open_pass(repo, src)
    assert session.phase == "plan"
    present(session)
    return src, session


def test_B1_a_second_source_change_after_a_re_presented_plan_names_the_new_revision(repo):
    src, session = _re_presented(repo)
    session.end()
    src.edit("added")
    before = read_card(repo)
    again = open_pass(repo, src)
    assert again.phase == "plan"
    assert again.reconciliation == (F1(2, new_text(C)),)
    assert_only_a_claim(repo, before)


def test_B2_a_second_source_change_after_a_re_presented_plan_with_its_go(repo):
    src, session = _re_presented(repo)
    go_plan(session)
    session.begin_build()
    changes(session)
    session.end()
    src.edit("removed")
    again = open_pass(repo, src)
    assert again.phase == "plan"
    assert again.reconciliation == (F1(2, removed_text(B_EDITED)),)
    assert again.review_round == 2


def _capped_on_revision_two(repo):
    src = Source()
    build_released(repo, src, "S2", 2)
    src.edit("text")
    session = open_pass(repo, src)
    present(session)
    go_plan(session)
    session.begin_build()
    changes(session)
    assert (session.phase, session.live) == ("stopped", False)
    assert read_card(repo)["stamps"][-1]["point"] == ticket.CAP_POINT
    return src, session


def test_B3_a_second_source_change_at_the_cap_stops_without_a_round_claim(repo):
    src, _ = _capped_on_revision_two(repo)
    src.edit("reorder")
    before = card_bytes(repo)
    again = open_pass(repo, src)
    assert (again.phase, again.live) == ("stopped", False)
    assert again.reconciliation == (F5(2), F6(2, REORDERED))
    assert card_bytes(repo) == before


def test_B4_a_source_change_at_a_passing_round_three_stops(repo):
    src = Source()
    build_released(repo, src, "S3", 3)
    src.edit("text")
    before = card_bytes(repo)
    session = open_pass(repo, src)
    assert (session.phase, session.live) == ("stopped", False)
    assert session.reconciliation == (F3(3, 1, EDIT_WHAT["text"]),)
    assert not any("lacks" in flag or "without" in flag for flag in session.reconciliation)
    assert card_bytes(repo) == before


def test_B5_a_source_change_at_an_approved_round_three_stops_and_spends_the_go(repo):
    src = Source()
    build_released(repo, src, "S4", 3)
    src.edit("removed")
    before = card_bytes(repo)
    session = open_pass(repo, src)
    assert session.phase == "stopped"
    assert session.reconciliation == (F3(3, 1, removed_text(B)), F4(1))
    assert card_bytes(repo) == before


def test_B6_a_change_at_the_cap_reaches_no_review_step(repo):
    src, stopped = _capped_on_revision_two(repo)
    src.edit("reorder")
    joined = open_pass(repo, src, pass_id=stopped.pass_id)
    assert (joined.phase, joined.live) == ("stopped", False)
    before = card_bytes(repo)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        joined.record_review("changes", evidence="reviews/round-4.md")
    assert refusal(excinfo) == "pass_ended"
    assert card_bytes(repo) == before

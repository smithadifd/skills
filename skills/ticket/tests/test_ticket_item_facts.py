"""Matrix section P: the item facts a ticket pass writes on the ledger store.

A recorded decision also becomes one ``decided:`` item note, and a build result that names a served model
becomes one builder note, in the grammar the toolkit's ``row-item.sh facts`` counts. Neither is a ticket
record, so the card and every reader of it are unchanged. Every row runs on the fake ledger CLI.
"""

import json
import re
import subprocess

import pytest

import ticket
from ledger_support import FakeLedger, build_ledger, open_ledger
from ticket_support import KEY, Source, build, card_bytes, changes, go_plan, present

# The grammar below is copied from the toolkit's scripts/row_item.py (BUILT_BY_RE, BUILDER_KEY_REST_RE, MODEL_RE
# and ``enc``) and the ledger_import locator (LABEL_RE). It is a fixture, not an import: this skill never loads
# the toolkit, and a drift between the two shows here as a red test.
BUILT_BY_RE = re.compile(
    r"built-by: harness=(\S+) model=(\S+) requested=(\S+) source=(envelope|seat) "
    r"round=([1-9][0-9]{0,8})"
)
BUILDER_KEY_REST_RE = re.compile(r"r([1-9][0-9]{0,8}):([A-Za-z0-9._@+%-]+)")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,199}")
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}")
KEY_SAFE = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@+-")
DECIDED_PREFIX = "decided:"


def enc(component):
    """Rule KE: every byte outside letters, digits and ._@+- becomes %XX."""
    return "".join(chr(byte) if byte in KEY_SAFE else f"%{byte:02X}" for byte in component.encode("utf-8"))


def builder_fact_valid(key_rest, kind, text):
    """The toolkit's ``builder_fact_valid``, as ``facts`` counts a note under the builder key prefix."""
    if kind != "item.note" or type(text) is not str:
        return False
    in_key, in_text = BUILDER_KEY_REST_RE.fullmatch(key_rest), BUILT_BY_RE.fullmatch(text)
    if in_key is None or in_text is None:
        return False
    harness, model, requested, _source, round_text = in_text.groups()
    return bool(
        LABEL_RE.fullmatch(harness)
        and MODEL_RE.fullmatch(model)
        and (requested == "-" or MODEL_RE.fullmatch(requested))
        and round_text == in_key[1]
        and enc(model) == in_key[2]
    )


def facts(events, item=KEY):
    """The counts of the toolkit's ``item_facts``: ``(builder, builder_malformed, decided)``."""
    prefix = f"row-item:builder:{enc(item)}:"
    builder = malformed = decided = 0
    for event in events:
        text = event["payload"].get("text") if event["kind"] == "item.note" else None
        if event["idempotency_key"].startswith(prefix):
            if builder_fact_valid(event["idempotency_key"][len(prefix):], event["kind"], text):
                builder += 1
            else:
                malformed += 1
        if type(text) is str and text.lstrip().startswith(DECIDED_PREFIX):
            decided += 1
    return builder, malformed, decided


@pytest.fixture
def fake(tmp_path):
    return FakeLedger(tmp_path / "ledger")


def _notes(fake, prefix):
    return [e for e in fake.events() if e["kind"] == "item.note" and e["payload"]["text"].startswith(prefix)]


def _builders(fake):
    return [e for e in fake.events() if e["idempotency_key"].startswith("row-item:builder:")]


def _at_build(fake):
    """A ledger pass with the plan go recorded and the build begun (S2[0])."""
    return build_ledger(fake, Source(), "S2")


def test_P1_a_recorded_decision_writes_one_decided_note_beside_the_unchanged_decision_note(fake):
    session = _at_build(fake)
    events = fake.events()
    decisions = _notes(fake, "ticket-decision:v2 ")
    decided = _notes(fake, DECIDED_PREFIX)
    assert len(decisions) == 1 and len(decided) == 1
    assert decided[0]["payload"]["text"] == "decided: The plan gate is approved for plan revision 1. (by the person)"
    assert "\n" not in decided[0]["payload"]["text"]
    # Beside it, in the same batch: the very next event, keyed by the decision's ordinary key.
    assert decided[0]["id"] == decisions[0]["id"] + 1
    assert decided[0]["idempotency_key"] == decisions[0]["idempotency_key"] + "/decided"
    assert decided[0]["idempotency_key"] != decisions[0]["idempotency_key"]
    # The decision note and the card read from it are the ones the pass wrote before this row.
    record = json.loads(decisions[0]["payload"]["text"].split(" ", 1)[1])
    assert record == session.store.read()[0]["decisions"][0]
    batch = [c for c in fake.appends() if "ticket-decision:v2" in c.stdin][0].batch
    assert [element["kind"] for element in batch] == ["item.note", "item.note"]
    assert facts(events)[2] == 1


def test_P1b_the_decided_note_is_one_line_and_names_who_relayed_a_role_decision(fake):
    held = fake.store()
    decision = {"who": "the\nrelease\tmanager", "effect": "Ship it.\nAlso tag it.", "relayed_by": "a director"}
    batch = held._batch("0", [("decision", decision)])
    texts = [element["payload"]["text"] for element in batch]
    assert texts[1] == "decided: Ship it. Also tag it. (by the release manager, relayed by a director)"
    assert "\n" not in texts[1]


def test_P2_replaying_the_same_batch_writes_no_second_decided_note(fake):
    _at_build(fake)
    call = [c for c in fake.appends() if "ticket-decision:v2" in c.stdin][0]
    before = fake.events()
    done = subprocess.run([str(fake.path), *call.argv], input=call.stdin, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert fake.events() == before
    assert len(_notes(fake, DECIDED_PREFIX)) == 1


def test_P3_a_recorded_build_writes_one_builder_note_in_the_toolkits_grammar(fake):
    session = _at_build(fake)
    answer = session.record_build("claude-sonnet-5-5", harness="claude-code", requested="sonnet", source="seat")
    assert answer["written"] is True
    (event,) = _builders(fake)
    assert event["kind"] == "item.note"
    assert event["idempotency_key"] == f"row-item:builder:{KEY}:r1:claude-sonnet-5-5"
    assert event["payload"]["text"] == (
        "built-by: harness=claude-code model=claude-sonnet-5-5 requested=sonnet source=seat round=1"
    )
    assert builder_fact_valid(event["idempotency_key"][len(f"row-item:builder:{KEY}:"):], event["kind"], event["payload"]["text"])
    # What ``row-item.sh facts`` would count after a decision and a build on the item.
    builder, malformed, decided = facts(fake.events())
    assert builder == 1 and malformed == 0 and decided >= 1
    # The next build round is the next review round's: the key and the text carry it.
    changes(session)
    session.record_build("claude-sonnet-5-5", harness="claude-code", requested=None, source="envelope")
    second = _builders(fake)[-1]
    assert second["idempotency_key"] == f"row-item:builder:{KEY}:r2:claude-sonnet-5-5"
    assert second["payload"]["text"] == (
        "built-by: harness=claude-code model=claude-sonnet-5-5 requested=- source=envelope round=2"
    )
    assert facts(fake.events())[:2] == (2, 0)


def test_P4_a_model_outside_the_safe_key_set_is_encoded_in_the_key_and_verbatim_in_the_text(fake):
    session = _at_build(fake)
    model = "vendor/model:v1+x@2"
    assert session.record_build(model, harness="h1", requested=model)["written"] is True
    (event,) = _builders(fake)
    assert event["idempotency_key"] == f"row-item:builder:{KEY}:r1:vendor%2Fmodel%3Av1+x@2"
    assert f"model={model} " in event["payload"]["text"] and f"requested={model} " in event["payload"]["text"]
    assert facts(fake.events())[:2] == (1, 0)


@pytest.mark.parametrize("model", [None, "", "   ", "two words", "-leading-dash", "x" * 201, 7])
def test_P5_a_build_result_with_no_usable_model_writes_no_builder_note_and_raises_nothing(fake, model):
    session = _at_build(fake)
    before = fake.events()
    answer = session.record_build(model, harness="claude-code")
    assert answer["written"] is False and answer["reason"]
    assert fake.events() == before
    # The pass goes on: its next write lands.
    changes(session)


@pytest.mark.parametrize("fields", [
    {"harness": "has space"}, {"harness": ""}, {"harness": None}, {"requested": "two words"}, {"source": "lane"},
])
def test_P5b_a_value_outside_the_toolkits_grammar_writes_no_builder_note(fake, fields):
    session = _at_build(fake)
    before = fake.events()
    answer = session.record_build("claude-sonnet-5-5", **{"harness": "claude-code", **fields})
    assert answer["written"] is False and answer["reason"]
    assert fake.events() == before


def test_P5c_the_markdown_store_writes_no_builder_note_and_no_byte(repo):
    session = build(repo, Source(), "S2")
    before = card_bytes(repo)
    answer = session.record_build("claude-sonnet-5-5", harness="claude-code")
    assert answer == {"written": False, "reason": "no ledger"}
    assert card_bytes(repo) == before


def test_P6_no_decided_note_is_written_for_any_other_record_kind(fake):
    session = open_ledger(fake, Source())
    present(session)
    session.record_tried("looked at the code", outcome="nothing to change")
    assert _notes(fake, "ticket-plan:v2 ") and any(e["kind"] == "item.tried" for e in fake.events())
    go_plan(session)
    session.begin_build()
    changes(session)
    assert _notes(fake, "ticket-review:v2 ")
    # One plan, one tried, one review, one stamp or two: the only decided: note is the go's.
    assert len(_notes(fake, DECIDED_PREFIX)) == 1
    assert _notes(fake, DECIDED_PREFIX)[0]["idempotency_key"].endswith("/decided")


def test_P7_a_decision_missing_what_the_note_states_gets_no_decided_note_and_keeps_its_own(fake):
    held = fake.store()
    for missing in ("who", "effect"):
        decision = {"who": "the person", "effect": "Go.", missing: "  "}
        batch = held._batch("0", [("decision", decision)])
        assert [element["payload"]["text"].startswith("ticket-decision:v2 ") for element in batch] == [True]


def test_P8_a_second_fact_for_one_round_and_model_with_other_text_writes_nothing_and_raises_nothing(fake):
    session = _at_build(fake)
    assert session.record_build("claude-sonnet-5-5", harness="claude-code", requested="sonnet")["written"] is True
    before = fake.events()
    assert session.record_build("claude-sonnet-5-5", harness="claude-code", requested="sonnet")["written"] is True
    other = session.record_build("claude-sonnet-5-5", harness="claude-code", requested="opus")
    assert other["written"] is False and "already recorded" in other["reason"]
    assert fake.events() == before
    changes(session)


def test_P9_a_fact_another_handle_already_wrote_replays_without_moving_the_pass_back(fake):
    first = _at_build(fake)
    first.record_build("claude-sonnet-5-5", harness="claude-code", requested="sonnet")
    # A later event, so the replayed fact is no longer the item's newest.
    first.record_tried("ran the checks", outcome="green")
    second = open_ledger(fake, Source(), pass_id=first.pass_id)
    assert second.phase == "build"
    before = fake.events()
    assert second.record_build("claude-sonnet-5-5", harness="claude-code", requested="sonnet")["written"] is True
    assert fake.events() == before
    # A replay answers an old event id; the pass still holds the live revision, so its next write lands.
    changes(second)
    assert len(_notes(fake, "ticket-review:v2 ")) == 1


def _replaced_pass(fake, record):
    """A stale handle B on a pass A ended: A records the build (when ``record``), releases, another pass takes the claim.

    Returns B and what B's next review does: ``(handle, refusal)``, the refusal being the exception it raised.
    """

    first = _at_build(fake)
    stale = open_ledger(fake, Source(), pass_id=first.pass_id)
    assert stale.phase == "build"
    first.record_build("claude-sonnet-5-5", harness="claude-code", requested="sonnet")
    first.end()
    other = open_ledger(fake, Source())
    assert other.pass_id != first.pass_id
    other.record_tried("the next pass is working", outcome="green")
    if record:
        before = stale._token
        stale.record_build("claude-sonnet-5-5", harness="claude-code", requested="sonnet")
        assert stale._token == before
    with pytest.raises(ticket.StoreUnavailable) as caught:
        changes(stale)
    return other, caught.value


def test_P11_a_stale_handle_recording_a_fact_another_handle_wrote_is_refused_as_without_the_record(tmp_path):
    fake, control = FakeLedger(tmp_path / "ledger"), FakeLedger(tmp_path / "control")
    other, refusal = _replaced_pass(fake, record=True)
    other_control, plain = _replaced_pass(control, record=False)
    assert (type(refusal), refusal.code) == (type(plain), plain.code)
    assert str(refusal) == str(plain)
    # The fact is on the item once, nothing was appended for the stale handle, and the other pass still holds the claim.
    assert len(_builders(fake)) == 1
    assert [e["kind"] for e in fake.events()] == [e["kind"] for e in control.events()]
    assert other.pass_id is not None and other.phase == other_control.phase


def test_P12_a_resumed_handle_recording_other_text_for_one_round_and_model_returns_false_and_keeps_the_pass(fake):
    first = _at_build(fake)
    first.record_build("claude-sonnet-5-5", harness="claude-code", requested="sonnet")
    resumed = open_ledger(fake, Source(), pass_id=first.pass_id)
    before, sent = fake.events(), len(fake.appends())
    answer = resumed.record_build("claude-sonnet-5-5", harness="claude-code", requested="opus")
    assert answer["written"] is False and "already recorded" in answer["reason"]
    # Nothing was written, nothing was even sent, and the pass is not ended.
    assert fake.events() == before and len(fake.appends()) == sent
    assert resumed.phase == "build"
    changes(resumed)
    assert len(_notes(fake, "ticket-review:v2 ")) == 1
    assert len(_builders(fake)) == 1


def test_P12b_a_key_conflict_from_a_write_that_raced_the_read_returns_false_and_keeps_the_pass(fake, monkeypatch):
    first = _at_build(fake)
    first.record_build("claude-sonnet-5-5", harness="claude-code", requested="sonnet")
    resumed = open_ledger(fake, Source(), pass_id=first.pass_id)
    # The fact lands after the handle's read of the item, so only the ledger's answer says it is there.
    monkeypatch.setattr(resumed.store, "note_text", lambda key: None)
    before = fake.events()
    answer = resumed.record_build("claude-sonnet-5-5", harness="claude-code", requested="opus")
    assert answer["written"] is False and "already recorded" in answer["reason"]
    assert [call.code for call in fake.appends()][-1] == "conflict"
    assert fake.events() == before
    changes(resumed)
    assert len(_notes(fake, "ticket-review:v2 ")) == 1


def test_P10_a_build_is_recorded_only_while_building(fake):
    session = open_ledger(fake, Source())
    before = fake.events()
    with pytest.raises(ticket.TicketError):
        session.record_build("claude-sonnet-5-5", harness="claude-code")
    assert fake.events() == before

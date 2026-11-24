"""Opaque lane documents handed to the optional post-publication writer."""
import base64
import json
import shlex
import sys
from pathlib import Path

import pytest

import ticket
from ledger_support import FakeLedger, build_ledger
from ticket_support import Source, build, go_outward

PR = "https://example.invalid/project/pull/14"


@pytest.fixture
def writer(tmp_path, monkeypatch):
    state = tmp_path / "record-state.json"
    answers = [dict(status=0, stdout=f"written {PR} round=1 lane={lane}\n", stderr="")
               for lane in ("first", "second")]
    state.write_text(json.dumps(dict(calls=[], answers=answers)))
    executable = tmp_path / "review-gate"
    command = shlex.join([sys.executable, str(Path(__file__).with_name("fake_record_verb.py"))])
    executable.write_text(f'#!/bin/sh\nexec {command} "$@"\n')
    executable.chmod(0o755)
    monkeypatch.setenv("FAKE_RECORD_STATE", str(state))
    monkeypatch.setenv("TICKET_REVIEW_GATE", str(executable))
    # Never consult a machine-local topic config.
    config = tmp_path / "topic.json"
    config.write_text("{}")
    monkeypatch.setattr(ticket._LEDGER_RULE, "CONFIG_PATH", config)
    paths = [tmp_path / "first.json", tmp_path / "second.json"]
    paths[0].write_bytes(b'{ "lane": "first", "escaped": "\\u0061" }\r\n')
    paths[1].write_bytes(b'\n{ "lane": "second" }\n\n')
    return executable, state, paths, config


def calls(writer):
    return json.loads(writer[1].read_text())["calls"]


def session(tmp_path, stage="S4"):
    return build_ledger(FakeLedger(tmp_path / "ledger"), Source(), stage)


def test_R1_off_calls_nothing(tmp_path, monkeypatch, writer, capsys):
    monkeypatch.delenv("TICKET_REVIEW_GATE")
    live = session(tmp_path)
    live.record_lane_receipts(writer[2], published_pr=PR)
    assert capsys.readouterr().out == "review receipts not recorded: not configured\n"
    assert calls(writer) == []
    assert live.approved("outward")


def test_R2_bytes_order_argv_and_env_precedence(tmp_path, writer, capsys):
    writer[3].write_text(json.dumps({"review_gate": "/not/the/env/writer"}))
    live = session(tmp_path)
    before = live.store.read()
    live.record_lane_receipts(writer[2], published_pr=PR)
    assert calls(writer) == [dict(argv=["record"], stdin=base64.b64encode(p.read_bytes()).decode())
                             for p in writer[2]], "writer must receive unchanged bytes in handed order"
    assert capsys.readouterr().out == "".join(
        f"written {PR} round=1 lane={lane}\n" for lane in ("first", "second"))
    assert live.store.read() == before


def test_R3_refusal_continues_and_pass_stays_live(tmp_path, writer, capsys):
    state = json.loads(writer[1].read_text())
    state["answers"][0] = dict(status=2, stdout="", stderr="receipt refused\nignored detail\n")
    writer[1].write_text(json.dumps(state))
    live = session(tmp_path)
    live.record_lane_receipts(writer[2], published_pr=PR)
    assert len(calls(writer)) == 2, "a refusal must not stop the next document"
    assert capsys.readouterr().out == (
        f"review receipt not recorded: {writer[2][0]}: receipt refused\n"
        f"written {PR} round=1 lane=second\n")
    live.require_outward_go()
    live.record_tried("continued after recording refusal", outcome="pass live")


def test_R4_markdown_calls_nothing(repo, writer, capsys):
    live = build(repo, Source(), "S4")
    live.record_lane_receipts(writer[2], published_pr=PR)
    assert calls(writer) == [], "markdown must never call the writer"
    assert capsys.readouterr().out == "review receipts not recorded: markdown store\n"


def test_R5_no_documents_calls_nothing(tmp_path, writer, capsys):
    session(tmp_path).record_lane_receipts([], published_pr=PR)
    assert calls(writer) == []
    assert capsys.readouterr().out == "review receipts not recorded: no documents handed\n"


def test_R6_before_go_or_publication_noops(tmp_path, writer, capsys):
    live = session(tmp_path, "S3")
    live.record_lane_receipts(writer[2], published_pr=PR)
    assert calls(writer) == []
    assert capsys.readouterr().out.startswith("review receipts not recorded: an outward go is required")
    go_outward(live)
    live.record_lane_receipts(writer[2])
    assert calls(writer) == []
    assert capsys.readouterr().out == "review receipts not recorded: pull request not published\n"
    live.record_lane_receipts(writer[2], published_pr=PR)
    assert len(calls(writer)) == 2


def test_R7_store_read_failure_preserves_claim_and_close(tmp_path, monkeypatch, writer, capsys):
    fake = FakeLedger(tmp_path / "ledger")
    live = build_ledger(fake, Source(), "S4")
    before = fake.events()
    claim = live.plan.claim

    def unavailable():
        raise ticket.StoreUnavailable("receipt read unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(live.store, "read", unavailable)
        live.record_lane_receipts(writer[2], published_pr=PR)
    assert capsys.readouterr().out == "review receipts not recorded: receipt read unavailable\n"
    assert calls(writer) == []
    assert live.live, "a receipt store read failure must not end the pass"
    assert live.plan.claim == claim
    assert fake.events() == before, "a receipt read failure must not write or release the claim"
    assert live.store.read()[0]["pass"] == claim
    live.close("Ticket pass complete.")
    assert live.phase == "closed"


def test_R8_stale_card_preserves_claim_and_ordinary_stale_refusal(tmp_path, writer, capsys):
    refusals = []
    for receipts in (False, True):
        fake = FakeLedger(tmp_path / str(receipts))
        live = build_ledger(fake, Source(), "S4")
        claim = live.plan.claim
        fake.outside("item.note", {"text": "concurrent card write"})
        before = fake.events()
        if receipts:
            live.record_lane_receipts(writer[2], published_pr=PR)
            output = capsys.readouterr().out
            assert output.startswith("review receipts not recorded: ticket ")
            assert "changed since this pass last read or wrote it" in output
            assert len(output.splitlines()) == 1
            assert calls(writer) == []
            assert live.live, "a stale receipt check must not end the pass"
            assert live.plan.claim == claim
            assert fake.events() == before, "a stale receipt check must not write or release the claim"
            assert live.store.read()[0]["pass"] == claim
        with pytest.raises(ticket.StoreUnavailable) as excinfo:
            live.close("Ticket pass complete.")
        refusals.append(excinfo.value.code)
        assert not live.live
        assert not any(event["kind"] == "item.stopped" for event in fake.events()[len(before):])
    assert refusals == ["stale", "stale"]


def test_R9_timeout_continues_and_preserves_claim(tmp_path, monkeypatch, writer, capsys):
    state = json.loads(writer[1].read_text())
    state["answers"][0]["sleep"] = 1
    writer[1].write_text(json.dumps(state))
    monkeypatch.setattr(ticket, "REVIEW_RECORD_TIMEOUT_SECONDS", 0.25, raising=False)
    live = session(tmp_path)
    before = live.store.read()
    claim = live.plan.claim
    live.record_lane_receipts(writer[2], published_pr=PR)
    assert capsys.readouterr().out == (
        f"review receipt not recorded: {writer[2][0]}: timed out after 0.25s\n"
        f"written {PR} round=1 lane=second\n"), "a timeout must be reported and the next document recorded"
    assert len(calls(writer)) == 2
    assert live.live
    assert live.plan.claim == claim
    assert live.store.read() == before
    live.close("Ticket pass complete.")


def test_config_only_and_already_recorded(tmp_path, monkeypatch, writer, capsys):
    monkeypatch.delenv("TICKET_REVIEW_GATE")
    writer[3].write_text(json.dumps({"review_gate": str(writer[0])}))
    state = json.loads(writer[1].read_text())
    state["answers"][0]["stdout"] = f"already_recorded {PR} round=1 lane=first\n"
    writer[1].write_text(json.dumps(state))
    session(tmp_path).record_lane_receipts(writer[2][:1], published_pr=PR)
    assert len(calls(writer)) == 1
    assert capsys.readouterr().out == f"already_recorded {PR} round=1 lane=first\n"


def test_unreadable_document_calls_nothing(tmp_path, writer, capsys):
    missing = tmp_path / "missing.json"
    session(tmp_path).record_lane_receipts([missing], published_pr=PR)
    assert calls(writer) == []
    assert capsys.readouterr().out.startswith(f"review receipts not recorded: {missing}: ")


@pytest.mark.parametrize("setting", ["none", "relative/path", "/absent/writer"])
def test_unusable_setting_never_fails_pass(tmp_path, monkeypatch, writer, setting, capsys):
    monkeypatch.setenv("TICKET_REVIEW_GATE", setting)
    live = session(tmp_path)
    live.record_lane_receipts(writer[2], published_pr=PR)
    assert calls(writer) == []
    reason = ("not configured" if setting == "none"
              else "review-gate must be an absolute executable path")
    assert capsys.readouterr().out == f"review receipts not recorded: {reason}\n"
    live.require_outward_go()

"""Jev typed-decision fallback wiring through review_phase.run_phase.

Stub the resolver and the HTTP call. No network, no 1Password, no
machine-local paths.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SKILL_DIR))
import jev_client  # noqa: E402
import review_phase  # noqa: E402

GROK_FLAGGED = (FIXTURES / "grok-notools-flagged.md").read_text()
GROK_CLEARED = (FIXTURES / "grok-notools-cleared.md").read_text()
CLAUDE_REQUEST_CHANGES = (FIXTURES / "claude-fresh-request-changes.md").read_text()
NO_VERDICT = (FIXTURES / "no-verdict.md").read_text()

DRIFT_BLOCKING = GROK_FLAGGED.replace("flagged —", "BLOCKING —")
DRIFT_LGTM = GROK_CLEARED.replace("cleared —", "LGTM —")

FAKE_KEY = "REDACTION-SENTINEL-not-a-key-0002"


def _envelope(backend: str, return_text: str, *, jev: bool = False) -> dict:
    return {
        "repo/ref": "example/repo@abc123",
        "inputs": {
            "target": "example/repo#1",
            "backend": backend,
            "brief": "Review the pull request for correctness and safety.",
            "return_text": return_text,
        },
        "constraints": {"scope": "diff only"},
        "harness_root": None,
        "adapters": {"jev_fallback": True} if jev else {},
        "budget": {"tokens": 0},
    }


class _Counting:
    """Stand-in whose constructor call count is the assertion, not the output."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        raise AssertionError("JevClient must not be constructed")


class _Resp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, body=b""):
    return urllib.error.HTTPError(
        jev_client.ENDPOINT,
        code,
        "error",
        None,
        io.BytesIO(body),
    )


def _stub_secret(monkeypatch, key=FAKE_KEY):
    monkeypatch.setenv("OP_CONNECT_HOST", "http://connect.example")
    monkeypatch.setenv("OP_CONNECT_TOKEN", "connect-tok")
    monkeypatch.setattr(
        jev_client, "_default_resolve_ref", lambda ref, host, token: key
    )


def test_flag_off_unparseable_is_todays_failure_and_never_constructs(monkeypatch):
    counting = _Counting()
    monkeypatch.setattr(review_phase, "JevClient", counting)
    envelope = _envelope("claude-fresh", NO_VERDICT)
    assert envelope["adapters"] == {}
    result = review_phase.run_phase(envelope)
    with pytest.raises(review_phase.ParseError) as exc:
        review_phase.parse_claude_fresh(NO_VERDICT)
    expected = review_phase._result(
        "failure", f"claude-fresh: {exc.value}", NO_VERDICT, None
    )
    assert result == expected
    assert counting.calls == 0


def test_flag_absent_success_is_byte_identical_to_today():
    envelope = _envelope("grok-notools", GROK_FLAGGED)
    result = review_phase.run_phase(envelope)
    assert "verdict_source" not in result
    assert "confidence" not in result
    assert set(result) == {
        "phase_result",
        "status",
        "artifacts",
        "reason",
        "resume_token",
        "intervals",
    }
    assert result["intervals"] == []
    assert result["status"] == "success"
    assert result["artifacts"]["findings.json"]["verdict"] == "changes_requested"


def test_flag_on_other_adapter_parses_regex_source_jev_never_called(monkeypatch):
    counting = _Counting()
    monkeypatch.setattr(review_phase, "JevClient", counting)
    # grok-notools cannot parse a claude-fresh return; claude-fresh can.
    result = review_phase.run_phase(
        _envelope("grok-notools", CLAUDE_REQUEST_CHANGES, jev=True)
    )
    assert result["status"] == "success"
    assert result["verdict_source"] == "regex"
    assert "confidence" not in result
    assert result["artifacts"]["findings.json"]["verdict"] == "changes_requested"
    assert review_phase.validate_findings(result["artifacts"]["findings.json"]) == []
    assert counting.calls == 0


def test_flag_on_selected_adapter_parses_records_regex_never_calls_jev(monkeypatch):
    counting = _Counting()
    monkeypatch.setattr(review_phase, "JevClient", counting)
    result = review_phase.run_phase(_envelope("grok-notools", GROK_FLAGGED, jev=True))
    assert result["status"] == "success"
    assert result["verdict_source"] == "regex"
    assert counting.calls == 0


def test_flag_on_both_raise_jev_approve_records_source_and_confidence(monkeypatch):
    _stub_secret(monkeypatch)
    payload = {"answers": {"verdict": {"choice": "APPROVE", "confidence": 0.95}}}
    monkeypatch.setattr(jev_client, "urlopen", lambda *a, **k: _Resp(payload))
    result = review_phase.run_phase(_envelope("claude-fresh", NO_VERDICT, jev=True))
    assert result["status"] == "success"
    assert result["verdict_source"] == "jev"
    assert result["confidence"] == 0.95
    findings = result["artifacts"]["findings.json"]
    assert findings["verdict"] == "approved"
    assert review_phase.validate_findings(findings) == []


def test_flag_on_both_raise_low_confidence_fail_closed(monkeypatch):
    _stub_secret(monkeypatch)
    payload = {"answers": {"verdict": {"choice": "APPROVE", "confidence": 0.4}}}
    monkeypatch.setattr(jev_client, "urlopen", lambda *a, **k: _Resp(payload))
    result = review_phase.run_phase(_envelope("claude-fresh", NO_VERDICT, jev=True))
    assert result["status"] == "failure"
    assert "confidence" in result["reason"]
    assert "0.40" in result["reason"] or "0.4" in result["reason"]
    assert "below floor" in result["reason"]
    assert result["artifacts"]["findings.json"] is None
    assert "verdict_source" not in result


@pytest.mark.parametrize(
    ("confidence", "expected_reason"),
    [
        (float("nan"), "jev: non-finite JSON constant NaN"),
        (float("inf"), "jev: non-finite JSON constant Infinity"),
        (float("-inf"), "jev: non-finite JSON constant -Infinity"),
        (1.5, "jev: confidence 1.5 outside [0, 1]"),
        (-0.1, "jev: confidence -0.1 outside [0, 1]"),
    ],
)
def test_invalid_confidence_fail_closed(monkeypatch, confidence, expected_reason):
    _stub_secret(monkeypatch)
    payload = {
        "answers": {"verdict": {"choice": "APPROVE", "confidence": confidence}}
    }
    monkeypatch.setattr(jev_client, "urlopen", lambda *a, **k: _Resp(payload))
    result = review_phase.run_phase(_envelope("claude-fresh", NO_VERDICT, jev=True))
    assert result["status"] == "failure"
    assert result["reason"] == expected_reason
    assert result["artifacts"]["findings.json"] is None
    assert "verdict_source" not in result


def test_confidence_above_floor_still_succeeds(monkeypatch):
    _stub_secret(monkeypatch)
    payload = {"answers": {"verdict": {"choice": "APPROVE", "confidence": 0.85}}}
    monkeypatch.setattr(jev_client, "urlopen", lambda *a, **k: _Resp(payload))
    result = review_phase.run_phase(_envelope("claude-fresh", NO_VERDICT, jev=True))
    assert result["status"] == "success"
    assert result["artifacts"]["findings.json"]["verdict"] == "approved"
    assert result["confidence"] == 0.85


def test_confidence_just_below_floor_keeps_floor_failure(monkeypatch):
    _stub_secret(monkeypatch)
    payload = {"answers": {"verdict": {"choice": "APPROVE", "confidence": 0.69}}}
    monkeypatch.setattr(jev_client, "urlopen", lambda *a, **k: _Resp(payload))
    result = review_phase.run_phase(_envelope("claude-fresh", NO_VERDICT, jev=True))
    assert result["status"] == "failure"
    assert result["reason"] == "jev: confidence 0.69 below floor 0.70"
    assert result["artifacts"]["findings.json"] is None


def test_flag_on_both_raise_http_500_fail_closed_no_secret(monkeypatch):
    _stub_secret(monkeypatch)

    def boom(*a, **k):
        raise _http_error(500, f"denied {FAKE_KEY}".encode())

    monkeypatch.setattr(jev_client, "urlopen", boom)
    result = review_phase.run_phase(_envelope("claude-fresh", NO_VERDICT, jev=True))
    assert result["status"] == "failure"
    assert "500" in result["reason"]
    assert FAKE_KEY not in result["reason"]
    assert FAKE_KEY not in json.dumps(result)
    assert result["artifacts"]["findings.json"] is None


def test_flag_on_both_raise_resolve_error_fail_closed(monkeypatch):
    monkeypatch.setenv("OP_CONNECT_HOST", "http://connect.example")
    monkeypatch.setenv("OP_CONNECT_TOKEN", "connect-tok")

    class ResolveError(Exception):
        pass

    def boom(ref, host, token):
        raise ResolveError("item not found: 'typesafe-jev'")

    monkeypatch.setattr(jev_client, "_default_resolve_ref", boom)
    result = review_phase.run_phase(_envelope("claude-fresh", NO_VERDICT, jev=True))
    assert result["status"] == "failure"
    assert "jev:" in result["reason"]
    assert "typesafe-jev" in result["reason"] or "resolve" in result["reason"]
    assert result["artifacts"]["findings.json"] is None


@pytest.mark.parametrize(
    "text, choice, expected_verdict",
    [
        (DRIFT_BLOCKING, "REQUEST_CHANGES", "changes_requested"),
        (DRIFT_LGTM, "APPROVE", "approved"),
    ],
)
def test_vocabulary_drift_both_adapters_raise_jev_returns_typed(
    monkeypatch, text, choice, expected_verdict
):
    with pytest.raises(review_phase.ParseError):
        review_phase.parse_claude_fresh(text)
    with pytest.raises(review_phase.ParseError):
        review_phase.parse_grok_notools(text)

    _stub_secret(monkeypatch)
    payload = {"answers": {"verdict": {"choice": choice, "confidence": 0.88}}}
    monkeypatch.setattr(jev_client, "urlopen", lambda *a, **k: _Resp(payload))
    result = review_phase.run_phase(_envelope("grok-notools", text, jev=True))
    assert result["status"] == "success"
    assert result["verdict_source"] == "jev"
    assert result["artifacts"]["findings.json"]["verdict"] == expected_verdict
    assert result["confidence"] == 0.88
    assert review_phase.validate_findings(result["artifacts"]["findings.json"]) == []

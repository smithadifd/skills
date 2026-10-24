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


def _claude_two_verdicts(first: str, second: str) -> str:
    return (
        f"## Verdict: {first}\n\n"
        "### Issues\n"
        "- `src/report.py:14` — the total skips the last row; include it in the sum.\n\n"
        f"## Verdict: {second}\n\n"
        "INTERVAL: none\n"
    )


def _grok_two_verdicts(first: str, second: str) -> str:
    return (
        "## FINDINGS\n"
        "- `src/report.py:14` — the total skips the last row.\n\n"
        f"## VERDICT\n{first} — the first reading.\n\n"
        f"## VERDICT\n{second} — the second reading.\n\n"
        "INTERVAL: none\n"
    )


@pytest.mark.parametrize(
    "backend, text",
    [
        # The selected adapter raises the conflict itself.
        pytest.param(
            "claude-fresh", _claude_two_verdicts("REQUEST CHANGES", "APPROVE"),
            id="claude-fresh-request-changes-then-approve",
        ),
        pytest.param(
            "claude-fresh", _claude_two_verdicts("APPROVE", "REQUEST CHANGES"),
            id="claude-fresh-approve-then-request-changes",
        ),
        pytest.param(
            "grok-notools", _grok_two_verdicts("flagged", "cleared"),
            id="grok-notools-flagged-then-cleared",
        ),
        pytest.param(
            "grok-notools", _grok_two_verdicts("cleared", "flagged"),
            id="grok-notools-cleared-then-flagged",
        ),
        # The selected adapter cannot parse the shape; the conflict surfaces
        # in another adapter the fallback tries.
        pytest.param(
            "grok-notools", _claude_two_verdicts("REQUEST CHANGES", "APPROVE"),
            id="grok-notools-reads-claude-request-changes-then-approve",
        ),
        pytest.param(
            "grok-notools", _claude_two_verdicts("APPROVE", "REQUEST CHANGES"),
            id="grok-notools-reads-claude-approve-then-request-changes",
        ),
        pytest.param(
            "claude-fresh", _grok_two_verdicts("flagged", "cleared"),
            id="claude-fresh-reads-grok-flagged-then-cleared",
        ),
    ],
)
def test_flag_on_verdict_conflict_stays_a_failure_and_never_asks_jev(
    monkeypatch, backend, text
):
    _stub_secret(monkeypatch)
    calls = []
    payload = {"answers": {"verdict": {"choice": "APPROVE", "confidence": 0.95}}}

    def approve(*a, **k):
        calls.append(a)
        return _Resp(payload)

    monkeypatch.setattr(jev_client, "urlopen", approve)
    result = review_phase.run_phase(_envelope(backend, text, jev=True))
    assert result["status"] == "failure", result["reason"]
    assert result["reason"].startswith(f"{backend}: conflicting verdicts")
    assert result["artifacts"]["findings.json"] is None
    assert "verdict_source" not in result
    assert calls == []


def _claude_failure_with_grok_verdict(reverse: bool) -> str:
    blocks = [
        (
            "VERDICT: REQUEST CHANGES\n\n"
            "## ISSUES\n"
            "The total omits the last row.\n"
        ),
        (
            "## FINDINGS\n"
            "- none\n\n"
            "## VERDICT\n"
            "cleared — the total is complete.\n"
        ),
    ]
    if reverse:
        blocks.reverse()
    return "\n".join(blocks) + "\nINTERVAL: none\n"


def _grok_failure_with_claude_verdict(reverse: bool) -> str:
    labels = [
        "VERDICT: flagged — the total omits the last row.",
        "## Verdict: APPROVE",
    ]
    if reverse:
        labels.reverse()
    return "\n\n".join(labels) + "\n\nINTERVAL: none\n"


@pytest.mark.parametrize(
    "backend, text",
    [
        pytest.param(
            "claude-fresh",
            _claude_failure_with_grok_verdict(False),
            id="claude-fresh-changes-then-cleared",
        ),
        pytest.param(
            "claude-fresh",
            _claude_failure_with_grok_verdict(True),
            id="claude-fresh-cleared-then-changes",
        ),
        pytest.param(
            "grok-notools",
            _grok_failure_with_claude_verdict(False),
            id="grok-notools-flagged-then-approve",
        ),
        pytest.param(
            "grok-notools",
            _grok_failure_with_claude_verdict(True),
            id="grok-notools-approve-then-flagged",
        ),
    ],
)
def test_fallback_refuses_cross_vocabulary_conflict(monkeypatch, backend, text):
    consulted = []

    def consult(*args):
        consulted.append(args)
        raise AssertionError("Jev must not resolve a reply with conflicting verdicts")

    monkeypatch.setattr(review_phase, "_consult_jev", consult)
    result = review_phase.run_phase(_envelope(backend, text, jev=True))
    assert result["status"] == "failure", (
        f"{backend} fallback accepted cross-vocabulary verdicts: {result['reason']}"
    )
    assert result["reason"].startswith(f"{backend}: conflicting verdicts")
    assert result["artifacts"]["findings.json"] is None
    assert consulted == []


@pytest.mark.parametrize(
    "decorated",
    [
        pytest.param("## Verdict: **REQUEST CHANGES**", id="bold-value-heading"),
        pytest.param(
            "## **Verdict**: **REQUEST CHANGES**", id="bold-key-and-value-heading"
        ),
        pytest.param(
            "## **Verdict:** REQUEST CHANGES", id="bold-key-with-colon-heading"
        ),
        pytest.param("VERDICT: [REQUEST CHANGES]", id="bracketed-value-label"),
        pytest.param("VERDICT: `REQUEST CHANGES`", id="backticked-value-label"),
        pytest.param(
            "## `Verdict`: `REQUEST CHANGES`", id="backticked-key-and-value-heading"
        ),
        pytest.param(
            "## [Verdict]: [REQUEST CHANGES]", id="bracketed-key-and-value-heading"
        ),
        pytest.param(
            "## _Verdict_: _REQUEST CHANGES_", id="underscored-key-and-value-heading"
        ),
        pytest.param(
            "## **[Verdict]**: **[REQUEST CHANGES]**",
            id="composed-key-and-value-heading",
        ),
        pytest.param("**Verdict: REQUEST CHANGES**", id="bold-full-label"),
        pytest.param("## **Verdict: REQUEST CHANGES**", id="bold-full-heading"),
        pytest.param("*Verdict: REQUEST CHANGES*", id="italic-full-label"),
        pytest.param("`VERDICT: REQUEST CHANGES`", id="backticked-full-label"),
    ],
)
@pytest.mark.parametrize("reverse", [False, True], ids=["changes-first", "approve-first"])
def test_fallback_refuses_decorated_conflict(monkeypatch, decorated, reverse):
    labels = [decorated, "## Verdict: APPROVE"]
    if reverse:
        labels.reverse()
    text = "\n\n".join(labels) + "\n\nINTERVAL: none\n"
    monkeypatch.setattr(
        review_phase,
        "_consult_jev",
        lambda *args: pytest.fail("Jev must not resolve conflicting verdicts"),
    )
    result = review_phase.run_phase(_envelope("grok-notools", text, jev=True))
    assert result["status"] == "failure", (
        f"fallback accepted decorated conflicting verdicts: {result['reason']}"
    )
    assert result["reason"].startswith("grok-notools: conflicting verdicts")
    assert result["artifacts"]["findings.json"] is None
@pytest.mark.parametrize(
    "backend", ["claude-fresh", "grok-notools"]
)
@pytest.mark.parametrize("unknown_first", [True, False], ids=["unknown-first", "unknown-last"])
@pytest.mark.parametrize("reverse", [False, True], ids=["approve-first", "changes-first"])
def test_fallback_does_not_mask_a_conflict_with_an_unknown_verdict(
    monkeypatch, backend, unknown_first, reverse
):
    conflicting = ["## Verdict: APPROVE", "## Verdict: REQUEST CHANGES"]
    if reverse:
        conflicting.reverse()
    labels = ["VERDICT: MAYBE", *conflicting] if unknown_first else [
        *conflicting,
        "VERDICT: MAYBE",
    ]
    text = "\n\n".join(labels) + "\n\nINTERVAL: none\n"
    monkeypatch.setattr(
        review_phase,
        "_consult_jev",
        lambda *args: pytest.fail("Jev must not resolve conflicting verdicts"),
    )
    result = review_phase.run_phase(_envelope(backend, text, jev=True))
    assert result["status"] == "failure", result["reason"]
    assert result["reason"].startswith(f"{backend}: conflicting verdicts")
    assert result["artifacts"]["findings.json"] is None




@pytest.mark.parametrize("reverse", [False, True], ids=["flagged-first", "cleared-first"])
def test_fallback_refuses_inline_grok_heading_conflict(monkeypatch, reverse):
    labels = [
        "## VERDICT: flagged — the total omits the last row.",
        "## VERDICT\ncleared — the total is complete.",
    ]
    if reverse:
        labels.reverse()
    text = "## FINDINGS\n- none\n\n" + "\n\n".join(labels) + "\n\nINTERVAL: none\n"
    monkeypatch.setattr(
        review_phase,
        "_consult_jev",
        lambda *args: pytest.fail("Jev must not resolve conflicting verdicts"),
    )
    result = review_phase.run_phase(_envelope("claude-fresh", text, jev=True))
    assert result["status"] == "failure", (
        f"fallback accepted conflicting verdict headings: {result['reason']}"
    )
    assert result["reason"].startswith("claude-fresh: conflicting verdicts")
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

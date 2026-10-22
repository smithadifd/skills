"""Opt-in Jev shadow behavior on regex-parse-success review returns."""

from __future__ import annotations

import copy
import sys
import time
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SKILL_DIR))
import jev_client  # noqa: E402
import review_phase  # noqa: E402

GROK_FLAGGED = (FIXTURES / "grok-notools-flagged.md").read_text()


def _envelope(return_text: str = GROK_FLAGGED) -> dict:
    return {
        "repo/ref": "example/repo@abc123",
        "inputs": {
            "target": "example/repo#1",
            "backend": "grok-notools",
            "brief": "Review correctness and safety.",
            "return_text": return_text,
        },
        "constraints": {"scope": "diff only"},
        "harness_root": None,
        "adapters": {},
        "budget": {"tokens": 0},
    }


class StubClient:
    def __init__(self, outcome=("approved", 0.91)):
        self.outcome = outcome
        self.payloads: list[str] = []
        self.constructions = 0

    def factory(self):
        self.constructions += 1
        return self

    def decide(self, payload: str):
        self.payloads.append(payload)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def test_shadow_flag_off_never_resolves_jev_and_result_matches_base_byte_for_byte(
    monkeypatch,
):
    monkeypatch.delenv("REVIEW_PHASE_JEV_SHADOW", raising=False)
    envelope = _envelope()
    expected = review_phase.run_phase(copy.deepcopy(envelope))
    client = StubClient(AssertionError("Jev must remain untouched"))
    monkeypatch.setattr(review_phase, "JevClient", client.factory)

    actual = review_phase.run_phase(envelope)

    assert actual == expected
    assert actual["artifacts"]["review.md"] == GROK_FLAGGED
    assert client.constructions == 0
    assert client.payloads == []


def test_shadow_parse_success_sends_only_trimmed_payload_and_regex_stays_authoritative(
    monkeypatch,
):
    monkeypatch.setenv("REVIEW_PHASE_JEV_SHADOW", "1")
    client = StubClient(("approved", 0.91))
    monkeypatch.setattr(review_phase, "JevClient", client.factory)
    return_text = (
        "review preamble that must not leave the harness\n\n"
        + GROK_FLAGGED
        + "\n## NOTES\nprivate appendix that must not leave the harness\n"
    )

    result = review_phase.run_phase(_envelope(return_text))

    assert client.payloads == [jev_client.trim_payload(return_text)]
    assert "review preamble" not in client.payloads[0]
    assert "private appendix" not in client.payloads[0]
    assert result["artifacts"]["review.md"] == return_text
    assert result["artifacts"]["findings.json"]["verdict"] == "changes_requested"
    assert result["jev_verdict"] == "approved"
    assert result["confidence"] == 0.91
    assert result["agrees_with_regex"] is False
    assert result["status"] == "success"
    assert result["jev_reason"] is None


def test_shadow_agreement_is_recorded_without_replacing_regex_verdict(monkeypatch):
    monkeypatch.setenv("REVIEW_PHASE_JEV_SHADOW", "1")
    client = StubClient(("changes_requested", 0.88))
    monkeypatch.setattr(review_phase, "JevClient", client.factory)

    result = review_phase.run_phase(_envelope())

    assert result["artifacts"]["findings.json"]["verdict"] == "changes_requested"
    assert result["jev_verdict"] == "changes_requested"
    assert result["confidence"] == 0.88
    assert result["agrees_with_regex"] is True
    assert result["jev_reason"] is None


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        (TimeoutError("timed out"), "jev: timeout"),
        (
            review_phase.JevClosed("jev: missing OP_CONNECT_HOST"),
            "jev: OP_CONNECT_HOST unresolvable",
        ),
        (
            review_phase.JevClosed("jev: could not resolve secret"),
            "jev: could not resolve secret",
        ),
        (
            review_phase.JevClosed("jev: could not load op-resolve"),
            "jev: could not load op-resolve",
        ),
        (
            review_phase.JevClosed(
                "jev: Connect request to http://connect.example/v1/vaults "
                "failed: ConnectionRefusedError"
            ),
            "jev: Connect connection failed",
        ),
        (
            review_phase.JevClosed(
                "jev: Connect returned HTTP 401 for "
                "http://connect.example/v1/vaults"
            ),
            "jev: Connect HTTP 401",
        ),
        (
            review_phase.JevClosed("jev: vault not found: 'Automation'"),
            "jev: vault not found",
        ),
        (
            review_phase.JevClosed("jev: item not found: 'typesafe-jev'"),
            "jev: item not found",
        ),
        (
            review_phase.JevClosed("jev: malformed response"),
            "jev: malformed response",
        ),
        (
            review_phase.JevClosed("jev: request failed"),
            "jev: request failed",
        ),
        (
            review_phase.JevClosed(
                "jev: choice 'MAYBE' is not APPROVE or REQUEST_CHANGES"
            ),
            "jev: choice is not APPROVE or REQUEST_CHANGES",
        ),
        (
            review_phase.JevClosed("jev: confidence 0.40 below floor 0.70"),
            "jev: confidence below floor",
        ),
    ],
    ids=[
        "timeout",
        "missing-host",
        "secret-resolution",
        "op-resolve-loader",
        "connect-connection",
        "connect-http",
        "missing-vault",
        "missing-item",
        "malformed-response",
        "request-failed",
        "invalid-choice",
        "confidence-floor",
    ],
)
def test_shadow_operational_failure_records_classified_reason_and_preserves_review(
    monkeypatch, failure, expected_reason
):
    monkeypatch.setenv("REVIEW_PHASE_JEV_SHADOW", "1")
    client = StubClient(failure)
    monkeypatch.setattr(review_phase, "JevClient", client.factory)

    result = review_phase.run_phase(_envelope())

    assert result["status"] == "success"
    assert result["artifacts"]["review.md"] == GROK_FLAGGED
    assert result["artifacts"]["findings.json"]["verdict"] == "changes_requested"
    assert result["jev_verdict"] == "unavailable"
    assert result["confidence"] is None
    assert result["agrees_with_regex"] is None
    assert result["jev_reason"] == expected_reason


def test_shadow_missing_secret_records_keychain_reason_without_network(monkeypatch):
    class FakeOpResolve:
        @staticmethod
        def _keychain_connect_token():
            return "absent", None

    monkeypatch.setenv("REVIEW_PHASE_JEV_SHADOW", "1")
    monkeypatch.delenv("OP_CONNECT_HOST", raising=False)
    monkeypatch.delenv("OP_CONNECT_TOKEN", raising=False)
    monkeypatch.setattr(jev_client, "_load_op_resolve", lambda: FakeOpResolve)

    result = review_phase.run_phase(_envelope())

    assert result["status"] == "success"
    assert result["artifacts"]["findings.json"]["verdict"] == "changes_requested"
    assert result["jev_verdict"] == "unavailable"
    assert result["confidence"] is None
    assert result["agrees_with_regex"] is None
    assert result["jev_reason"] == "jev: OP_CONNECT_TOKEN unresolvable"


def test_shadow_unavailable_client_records_unavailable(monkeypatch):
    monkeypatch.setenv("REVIEW_PHASE_JEV_SHADOW", "1")
    monkeypatch.setattr(review_phase, "JevClient", None)

    result = review_phase.run_phase(_envelope())

    assert result["status"] == "success"
    assert result["artifacts"]["findings.json"]["verdict"] == "changes_requested"
    assert result["jev_verdict"] == "unavailable"
    assert result["confidence"] is None
    assert result["agrees_with_regex"] is None
    assert result["jev_reason"] == "jev: unexpected TypeError"


def test_shadow_client_parse_failure_records_unavailable(monkeypatch):
    monkeypatch.setenv("REVIEW_PHASE_JEV_SHADOW", "1")
    client = StubClient(ValueError("malformed typed response"))
    monkeypatch.setattr(review_phase, "JevClient", client.factory)

    result = review_phase.run_phase(_envelope())

    assert result["status"] == "success"
    assert result["artifacts"]["findings.json"]["verdict"] == "changes_requested"
    assert result["jev_verdict"] == "unavailable"
    assert result["confidence"] is None
    assert result["agrees_with_regex"] is None
    assert result["jev_reason"] == "jev: unexpected ValueError"


def test_shadow_reason_never_carries_secret_or_response_body(monkeypatch):
    secret = "shadow-secret-MUST-NOT-LEAK"
    monkeypatch.setenv("REVIEW_PHASE_JEV_SHADOW", "1")
    client = StubClient(review_phase.JevClosed(f"jev: HTTP 500: {secret}"))
    monkeypatch.setattr(review_phase, "JevClient", client.factory)

    result = review_phase.run_phase(_envelope())

    assert result["jev_reason"] == "jev: HTTP 500"
    assert secret not in result["jev_reason"]
    assert result["status"] == "success"
    assert result["artifacts"]["findings.json"]["verdict"] == "changes_requested"


# --- the shared heading matcher stays linear in shadow mode -----------------
#
# The Jev payload trim reads every line of the return with the same heading
# pattern as the regex adapters, after the regex parse has already succeeded.
# One line of `# a`, 3,000 spaces and `b` used to take over ten seconds there.

SHADOW_LIMIT_SECONDS = 1.0
THREE_THOUSAND_SPACE_LINE = "# a" + " " * 3000 + "b"


def test_trim_and_regex_adapters_share_one_heading_matcher():
    assert review_phase._HEADING is jev_client._ANY_HEADING


def test_shadow_run_on_a_return_holding_the_3000_space_heading_finishes_within_a_second(
    monkeypatch,
):
    monkeypatch.setenv("REVIEW_PHASE_JEV_SHADOW", "1")
    client = StubClient(("changes_requested", 0.88))
    monkeypatch.setattr(review_phase, "JevClient", client.factory)
    return_text = GROK_FLAGGED + "\n" + THREE_THOUSAND_SPACE_LINE + "\n"

    start = time.perf_counter()
    result = review_phase.run_phase(_envelope(return_text))
    elapsed = time.perf_counter() - start

    assert elapsed <= SHADOW_LIMIT_SECONDS, (
        f"run_phase in shadow mode took {elapsed:.2f}s on a return holding a "
        "line of `# a`, 3,000 spaces and `b`"
    )
    assert result["status"] == "success"
    assert result["artifacts"]["findings.json"]["verdict"] == "changes_requested"
    assert result["jev_verdict"] == "changes_requested"
    assert result["agrees_with_regex"] is True
    assert client.payloads == [jev_client.trim_payload(return_text)]
    assert THREE_THOUSAND_SPACE_LINE not in client.payloads[0]

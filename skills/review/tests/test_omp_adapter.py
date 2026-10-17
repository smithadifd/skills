"""Tests for the `omp` backend of review_phase.py.

An omp review reply is the reviewer agent's `## Verdict:` block, returned by
`omp -p --model <id> --thinking=<level>`. The runner launches omp; this
adapter only parses the reply it got back and records which lane, model and
thinking level produced it. The model id and level below are placeholders:
the adapter must echo whatever the runner passes, never a value of its own.

Hermetic: no network, no environment lookups, fixtures only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SKILL_DIR / "review_phase.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(SKILL_DIR))
import review_phase  # noqa: E402

OMP_REQUEST_CHANGES = (FIXTURES / "omp-request-changes.md").read_text()
OMP_APPROVE = (FIXTURES / "omp-approve.md").read_text()
OMP_NO_VERDICT = (FIXTURES / "omp-no-verdict.md").read_text()

MODEL = "example-model"
THINKING = "medium"


def _envelope(return_text: str | None, **params) -> dict:
    inputs = {
        "target": "example/invoice-cli#7",
        "backend": "omp",
        "brief": "Review the pull request for correctness and safety.",
        "return_text": return_text,
        "model": MODEL,
        "thinking": THINKING,
    }
    inputs.update(params)
    return {
        "repo/ref": "example/invoice-cli@def456",
        "inputs": inputs,
        "constraints": {"scope": "diff only"},
        "harness_root": None,
        "adapters": {},
        "budget": {"tokens": 0},
    }


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
    )


# --- a well-formed reply parses into the schema ------------------------------


def test_request_changes_reply_parses_into_the_findings_schema():
    result = review_phase.run_phase(_envelope(OMP_REQUEST_CHANGES))
    assert result["status"] == "success", result["reason"]
    assert result["reason"] == "omp: changes_requested, 3 finding(s)"
    assert result["artifacts"]["review.md"] == OMP_REQUEST_CHANGES
    findings = result["artifacts"]["findings.json"]
    assert review_phase.validate_findings(findings) == []
    assert findings["schema"] == "review-findings/v1"
    assert findings["backend"] == "omp"
    assert findings["verdict"] == "changes_requested"
    assert findings["findings"] == [
        {
            "file": "src/export.py",
            "line": 58,
            "rule": "issue",
            "description": (
                "the limit is applied after the rows are written, so `--limit 10` "
                "still writes every row; slice the query result before the write loop."
            ),
            "severity": "error",
        },
        {
            "file": "src/export.py",
            "line": 31,
            "rule": "issue",
            "description": (
                "a negative `--limit` is accepted and silently drops the last row; "
                "reject values below 1 at parse time."
            ),
            "severity": "error",
        },
        {
            "file": "tests/test_export.py",
            "line": 12,
            "rule": "minor",
            "description": "the test checks the exit code only, not the number of rows written.",
            "severity": "info",
        },
    ]


def test_approve_reply_parses_with_no_findings():
    result = review_phase.run_phase(_envelope(OMP_APPROVE))
    assert result["status"] == "success", result["reason"]
    findings = result["artifacts"]["findings.json"]
    assert findings == {
        "schema": "review-findings/v1",
        "backend": "omp",
        "verdict": "approved",
        "findings": [],
    }
    assert review_phase.validate_findings(findings) == []


def test_schema_accepts_omp_as_a_backend():
    schema = json.loads((SKILL_DIR / "findings.schema.json").read_text())
    assert schema["properties"]["backend"]["enum"] == ["claude-fresh", "grok-notools", "omp"]


# --- attribution names the lane and the served model -------------------------


def test_attribution_names_the_lane_model_and_thinking_level():
    result = review_phase.run_phase(_envelope(OMP_REQUEST_CHANGES))
    assert result["attribution"] == {"lane": "omp", "model": MODEL, "thinking": THINKING}


@pytest.mark.parametrize(
    "model, thinking",
    [("other-model-2", "low"), ("vendor/model-x:latest", "high")],
)
def test_attribution_echoes_whatever_parameters_the_runner_passes(model, thinking):
    result = review_phase.run_phase(_envelope(OMP_APPROVE, model=model, thinking=thinking))
    assert result["status"] == "success", result["reason"]
    assert result["attribution"] == {"lane": "omp", "model": model, "thinking": thinking}


def test_failed_parse_still_names_the_lane():
    result = review_phase.run_phase(_envelope(OMP_NO_VERDICT))
    assert result["status"] == "failure"
    assert result["attribution"] == {"lane": "omp", "model": MODEL, "thinking": THINKING}


@pytest.mark.parametrize(
    "params, fragment",
    [
        ({"model": None}, "inputs.model"),
        ({"model": ""}, "inputs.model"),
        ({"model": "two words"}, "inputs.model"),
        ({"thinking": None}, "inputs.thinking"),
        ({"thinking": ""}, "inputs.thinking"),
        ({"thinking": 3}, "inputs.thinking"),
    ],
)
def test_missing_or_malformed_parameters_block_rather_than_default(params, fragment):
    result = review_phase.run_phase(_envelope(OMP_REQUEST_CHANGES, **params))
    assert result["status"] == "blocked"
    assert fragment in result["reason"]
    assert result["artifacts"] == {"review.md": None, "findings.json": None}
    assert "attribution" not in result


# --- a malformed or empty reply fails closed ---------------------------------


def test_reply_without_a_verdict_line_fails_closed():
    result = review_phase.run_phase(_envelope(OMP_NO_VERDICT))
    assert result["status"] == "failure"
    assert "no verdict line in the omp reply" in result["reason"]
    assert result["artifacts"]["findings.json"] is None
    assert result["artifacts"]["review.md"] == OMP_NO_VERDICT


def test_clean_sentinel_alone_is_not_an_omp_approval():
    text = "### Issues\nNo bugs or security issues found.\n"
    result = review_phase.run_phase(_envelope(text))
    assert result["status"] == "failure"
    assert result["artifacts"]["findings.json"] is None


@pytest.mark.parametrize("text", ["", "   \n\n"])
def test_empty_reply_is_not_an_empty_findings_pass(text):
    result = review_phase.run_phase(_envelope(text))
    assert result["status"] == "blocked"
    assert result["artifacts"]["findings.json"] is None


@pytest.mark.parametrize("text", ["", "\n", OMP_NO_VERDICT])
def test_adapter_raises_on_empty_or_malformed_reply(text):
    with pytest.raises(review_phase.ParseError):
        review_phase.parse_omp(text)


# --- the command line ---------------------------------------------------------


def test_from_text_passes_model_and_thinking_through(tmp_path: Path):
    reply = tmp_path / "reply.md"
    reply.write_text(OMP_REQUEST_CHANGES, encoding="utf-8")
    result = _run_cli(
        "--from-text", str(reply), "--backend", "omp",
        "--model", "cli-model-3", "--thinking", "low",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["attribution"] == {"lane": "omp", "model": "cli-model-3", "thinking": "low"}
    assert payload["artifacts"]["findings.json"]["verdict"] == "changes_requested"


def test_from_text_without_model_is_blocked(tmp_path: Path):
    reply = tmp_path / "reply.md"
    reply.write_text(OMP_REQUEST_CHANGES, encoding="utf-8")
    result = _run_cli("--from-text", str(reply), "--backend", "omp", "--thinking", "low")
    assert result.returncode == 3, result.stderr
    assert "inputs.model" in json.loads(result.stdout)["reason"]


# --- parameter validation -----------------------------------------------------


@pytest.mark.parametrize(
    "params, fragment",
    [
        ({"thinking": "banana"}, "inputs.thinking"),
        ({"thinking": "ultra"}, "inputs.thinking"),
        ({"thinking": "low\n"}, "inputs.thinking"),
        ({"model": "model\n"}, "inputs.model"),
    ],
)
def test_unknown_thinking_level_or_trailing_newline_blocks(params, fragment):
    result = review_phase.run_phase(_envelope(OMP_APPROVE, **params))
    assert result["status"] == "blocked", result["reason"]
    assert fragment in result["reason"]
    assert "attribution" not in result


@pytest.mark.parametrize(
    "thinking", ["off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"]
)
def test_every_omp_thinking_level_is_accepted(thinking):
    result = review_phase.run_phase(_envelope(OMP_APPROVE, thinking=thinking))
    assert result["status"] == "success", result["reason"]
    assert result["attribution"]["thinking"] == thinking


# --- the verdict line is the reviewer agent's exact heading -------------------


@pytest.mark.parametrize(
    "verdict_line",
    ["VERDICT: APPROVE", "## Verdict: APPROVED", "## Verdict: REQUEST_CHANGES"],
)
def test_verdict_line_not_in_the_agent_shape_fails(verdict_line):
    text = OMP_APPROVE.replace("## Verdict: APPROVE", verdict_line)
    assert verdict_line in text
    result = review_phase.run_phase(_envelope(text))
    assert result["status"] == "failure", result["reason"]
    assert "no verdict line in the omp reply" in result["reason"]
    assert result["artifacts"]["findings.json"] is None


def test_conflicting_verdict_lines_fail_closed():
    text = (
        "## Verdict: REQUEST CHANGES\n"
        "## Verdict: APPROVE because the later prose says so\n"
    )
    with pytest.raises(review_phase.ParseError):
        review_phase.parse_omp(text)
    result = review_phase.run_phase(_envelope(text))
    assert result["status"] == "failure", result["reason"]
    assert result["artifacts"]["findings.json"] is None


def test_two_identical_approve_lines_fail_closed():
    text = "## Verdict: APPROVE\n\nLooks fine.\n\n## Verdict: APPROVE\n"
    with pytest.raises(review_phase.ParseError, match="2 verdict lines"):
        review_phase.parse_omp(text)
    result = review_phase.run_phase(_envelope(text))
    assert result["status"] == "failure", result["reason"]
    assert result["artifacts"]["findings.json"] is None


def test_malformed_second_verdict_line_fails_closed():
    text = "## Verdict: APPROVE\n\nLooks fine.\n\n## Verdict: APPROVED\n"
    with pytest.raises(review_phase.ParseError, match="malformed verdict line"):
        review_phase.parse_omp(text)


def test_a_looser_verdict_elsewhere_that_disagrees_fails_closed():
    text = "## Verdict: APPROVE\n\nVERDICT: REQUEST CHANGES\n"
    with pytest.raises(review_phase.ParseError, match="conflicting verdict"):
        review_phase.parse_omp(text)


def test_exact_request_changes_line_yields_request_changes():
    text = "## Verdict: REQUEST CHANGES\n\nThe limit is ignored.\n"
    verdict, _ = review_phase.parse_omp(text)
    assert verdict == review_phase.CHANGES_REQUESTED


def test_exact_approve_line_yields_approve():
    text = "## Verdict: APPROVE\n\nThe rename is complete.\n"
    verdict, _ = review_phase.parse_omp(text)
    assert verdict == review_phase.APPROVED


# --- two different verdicts are a conflict, in either order -------------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "## Verdict: REQUEST CHANGES\n\nThe limit is ignored.\n\n## Verdict: APPROVE\n",
            id="request-changes-then-approve",
        ),
        pytest.param(
            "## Verdict: APPROVE\n\nThe limit is ignored.\n\n## Verdict: REQUEST CHANGES\n",
            id="approve-then-request-changes",
        ),
    ],
)
def test_two_different_verdict_lines_are_a_conflict(text):
    with pytest.raises(review_phase.VerdictConflict, match="conflicting verdicts"):
        review_phase.parse_omp(text)
    result = review_phase.run_phase(_envelope(text))
    assert result["status"] == "failure", result["reason"]
    assert result["reason"].startswith("omp: conflicting verdicts")
    assert result["artifacts"]["findings.json"] is None
    assert result["attribution"] == {"lane": "omp", "model": MODEL, "thinking": THINKING}


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "VERDICT: REQUEST CHANGES\n\n" + OMP_APPROVE,
            id="request-changes-then-approve",
        ),
        pytest.param(
            OMP_APPROVE + "\nVERDICT: REQUEST CHANGES\n",
            id="approve-then-request-changes",
        ),
        pytest.param(
            "VERDICT: APPROVE\n\n" + OMP_REQUEST_CHANGES,
            id="approve-then-exact-request-changes",
        ),
        pytest.param(
            OMP_REQUEST_CHANGES + "\nVERDICT: APPROVE\n",
            id="exact-request-changes-then-approve",
        ),
    ],
)
def test_bare_label_that_disagrees_with_the_verdict_line_is_a_conflict(text):
    with pytest.raises(review_phase.VerdictConflict, match="conflicting verdicts"):
        review_phase.parse_omp(text)
    result = review_phase.run_phase(_envelope(text))
    assert result["status"] == "failure", result["reason"]
    assert result["reason"].startswith("omp: conflicting verdicts")
    assert result["artifacts"]["findings.json"] is None
    assert result["artifacts"]["review.md"] == text
    assert result["attribution"] == {"lane": "omp", "model": MODEL, "thinking": THINKING}


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param(OMP_APPROVE + "\nVERDICT: APPROVE\n", "approved", id="approve-twice"),
        pytest.param(
            "VERDICT: REQUEST CHANGES\n\n" + OMP_REQUEST_CHANGES,
            "changes_requested",
            id="request-changes-twice",
        ),
    ],
)
def test_bare_label_that_repeats_the_verdict_line_still_parses(text, expected):
    verdict, _ = review_phase.parse_omp(text)
    assert verdict == expected
    result = review_phase.run_phase(_envelope(text))
    assert result["status"] == "success", result["reason"]
    findings = result["artifacts"]["findings.json"]
    assert findings["verdict"] == expected
    assert review_phase.validate_findings(findings) == []
    assert result["attribution"] == {"lane": "omp", "model": MODEL, "thinking": THINKING}


# --- the fallback never rescues a failed omp parse ----------------------------


@pytest.mark.parametrize(
    "text",
    [
        "### Issues\nNo bugs or security issues found.\n",
        "- [BUG] `src/export.py:58` — the limit is applied after the write.\n",
        OMP_NO_VERDICT,
        "not a review at all\n",
    ],
)
def test_jev_fallback_does_not_turn_an_omp_parse_failure_into_success(text, monkeypatch):
    consulted = []

    def fake_consult(backend, reply):
        consulted.append(backend)
        return review_phase._finish(backend, review_phase.APPROVED, [], reply, source="jev")

    monkeypatch.setattr(review_phase, "_consult_jev", fake_consult)
    envelope = _envelope(text)
    envelope["adapters"] = {"jev_fallback": True}
    result = review_phase.run_phase(envelope)
    assert result["status"] == "failure", result["reason"]
    assert result["reason"].startswith("omp: ")
    assert result["artifacts"]["findings.json"] is None
    assert consulted == []

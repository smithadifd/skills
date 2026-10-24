"""Tests for review_phase.py — the one REVIEW entry point and its findings schema.

Seams under test, each observable from outside the module:
  * the phase result (`run_phase` / `run --envelope`): a `phase_result: v1`
    with `status` in success/failure/blocked, both artifacts, and a null
    resume token; the envelope is never mutated;
  * the schema validator (`validate_findings` / `--validate`): reads
    `findings.schema.json` and names the offending path;
  * each backend adapter's parse on fixture return text;
  * the standalone `review` text and a caller return both
    validate against the same schema file.

Expected values are literals taken from the fixtures and the schema, never
from the implementation. Hermetic: no network, no environment lookups.
"""

from __future__ import annotations

import copy
import itertools
import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SKILL_DIR / "review_phase.py"
SCHEMA = SKILL_DIR / "findings.schema.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(SKILL_DIR))
import review_phase  # noqa: E402

GROK_FLAGGED = (FIXTURES / "grok-notools-flagged.md").read_text()
GROK_CLEARED = (FIXTURES / "grok-notools-cleared.md").read_text()
CLAUDE_REQUEST_CHANGES = (FIXTURES / "claude-fresh-request-changes.md").read_text()
CLAUDE_APPROVE = (FIXTURES / "claude-fresh-approve.md").read_text()
REVIEW_SKILL_FINDINGS = (FIXTURES / "review-skill-findings.md").read_text()
REVIEW_SKILL_CLEAN = (FIXTURES / "review-skill-clean.md").read_text()
CLAUDE_ISSUES_CLEAN = (FIXTURES / "claude-fresh-issues-clean.md").read_text()
CLEAN_APPROVE = (FIXTURES / "clean-approve.md").read_text()
NO_VERDICT = (FIXTURES / "no-verdict.md").read_text()
CLAUDE_EVIDENCE = (FIXTURES / "claude-fresh-evidence.md").read_text()
REVIEWER_AGENT = SKILL_DIR.parent.parent / "agents" / "reviewer.md"

ENVELOPE_KEYS = ("repo/ref", "inputs", "constraints", "harness_root", "adapters", "budget")


def _envelope(backend: str, return_text: str, **inputs_extra) -> dict:
    inputs = {
        "target": "example/repo#1",
        "backend": backend,
        "brief": "Review the pull request for correctness and safety.",
        "return_text": return_text,
    }
    inputs.update(inputs_extra)
    return {
        "repo/ref": "example/repo@abc123",
        "inputs": inputs,
        "constraints": {"scope": "diff only"},
        "harness_root": None,
        "adapters": {},
        "budget": {"tokens": 0},
    }


def _valid_findings_doc() -> dict:
    return {
        "schema": "review-findings/v1",
        "backend": "grok-notools",
        "verdict": "changes_requested",
        "findings": [
            {
                "file": "src/report.sh",
                "line": 77,
                "rule": "finding",
                "description": "the report path is expanded unquoted",
                "severity": "error",
            }
        ],
    }


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
        cwd=cwd,
    )


# --- the schema file ---------------------------------------------------------


def test_schema_file_is_closed_at_every_object():
    schema = json.loads(SCHEMA.read_text())
    assert schema["$schema"].startswith("https://json-schema.org/")
    assert schema["$id"] == (
        "https://github.com/smithadifd/skills/blob/main/skills/review/findings.schema.json"
    ), "schema identifier must point to this repository"

    def walk(node: dict, path: str) -> None:
        if node.get("type") == "object" or "properties" in node:
            assert node.get("additionalProperties") is False, f"{path}: open object"
            props = set(node.get("properties", {}))
            assert set(node.get("required", [])) == props, f"{path}: required != properties"
            for name, child in node["properties"].items():
                if isinstance(child, dict):
                    walk(child, f"{path}.{name}")
        items = node.get("items")
        if isinstance(items, dict):
            walk(items, f"{path}[]")

    walk(schema, "$")
    finding = schema["properties"]["findings"]["items"]["properties"]
    assert set(finding) == {"file", "line", "rule", "description", "severity"}
    assert finding["severity"]["enum"] == ["error", "warning", "info"]
    assert schema["properties"]["verdict"]["enum"] == ["approved", "changes_requested"]


# --- the validator -----------------------------------------------------------


def test_validate_accepts_a_conforming_document():
    assert review_phase.validate_findings(_valid_findings_doc()) == []


def test_validate_names_a_missing_field():
    doc = _valid_findings_doc()
    del doc["findings"][0]["severity"]
    errors = review_phase.validate_findings(doc)
    assert len(errors) == 1
    assert "findings[0]" in errors[0] and "severity" in errors[0]


def test_validate_rejects_an_extra_field_at_both_levels():
    doc = _valid_findings_doc()
    doc["findings"][0]["fix"] = "quote it"
    errors = review_phase.validate_findings(doc)
    assert len(errors) == 1 and "fix" in errors[0]

    doc = _valid_findings_doc()
    doc["notes"] = "extra"
    errors = review_phase.validate_findings(doc)
    assert len(errors) == 1 and "notes" in errors[0]


def test_validate_rejects_a_bad_severity_and_a_bad_line_type():
    doc = _valid_findings_doc()
    doc["findings"][0]["severity"] = "critical"
    errors = review_phase.validate_findings(doc)
    assert len(errors) == 1 and "severity" in errors[0] and "critical" in errors[0]

    doc = _valid_findings_doc()
    doc["findings"][0]["line"] = "77"
    errors = review_phase.validate_findings(doc)
    assert len(errors) == 1 and "line" in errors[0]

    doc = _valid_findings_doc()
    doc["findings"][0]["line"] = 0
    assert len(review_phase.validate_findings(doc)) == 1


def test_validate_accepts_null_file_and_line():
    doc = _valid_findings_doc()
    doc["findings"][0]["file"] = None
    doc["findings"][0]["line"] = None
    assert review_phase.validate_findings(doc) == []


def test_validate_cli_exit_codes(tmp_path: Path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_valid_findings_doc()))
    result = _run_cli("--validate", str(good))
    assert result.returncode == 0, result.stderr

    bad_doc = _valid_findings_doc()
    bad_doc["verdict"] = "parked"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_doc))
    result = _run_cli("--validate", str(bad))
    assert result.returncode == 1
    assert "verdict" in result.stderr and "parked" in result.stderr


# --- the grok-notools adapter ------------------------------------------------


def test_grok_adapter_parses_the_flagged_return():
    verdict, findings = review_phase.parse_grok_notools(GROK_FLAGGED)
    assert verdict == "changes_requested"
    assert len(findings) == 6
    assert findings[1]["description"] == (
        "`_sm_default_worker_meter`: `tolower($0) ~ /off/` is an unanchored "
        "substring. A worker usage line containing `cutoff`/`offset`/`offline` "
        "is classified `off` and skips the fallback (`pool-off`) even when the "
        "pool is on."
    )
    assert findings[0]["file"] == "sample-monitor-run.sh"
    assert findings[0]["line"] is None
    assert {f["rule"] for f in findings} == {"finding"}
    assert {f["severity"] for f in findings} == {"error"}


def test_grok_adapter_parses_the_cleared_return():
    verdict, findings = review_phase.parse_grok_notools(GROK_CLEARED)
    assert verdict == "approved"
    assert findings == []
    document = {
        "schema": "review-findings/v1",
        "backend": "grok-notools",
        "verdict": verdict,
        "findings": findings,
    }
    assert review_phase.validate_findings(document) == []


def test_grok_adapter_without_a_verdict_section_raises():
    with pytest.raises(review_phase.ParseError):
        review_phase.parse_grok_notools("## FINDINGS\n- something\n")


def test_grok_adapter_parses_numbered_findings_under_a_heading():
    text = (FIXTURES / "grok-notools-numbered.md").read_text()
    verdict, findings = review_phase.parse_grok_notools(text)
    assert verdict == "changes_requested"
    assert len(findings) == 2
    assert findings[0]["file"] == "skills/example/queue.py"
    assert findings[0]["line"] == 19
    assert findings[0]["severity"] == "error"  # (blocker)
    assert findings[1]["severity"] == "info"  # (minor)


def test_grok_adapter_parses_the_bare_headless_numbered_verdict_and_findings():
    # No `##` at all: `FINDINGS:` / `VERDICT: flagged` on their own lines,
    # the verdict value inline with the label rather than on the next line.
    text = (FIXTURES / "grok-notools-headless-numbered.md").read_text()
    verdict, findings = review_phase.parse_grok_notools(text)
    assert verdict == "changes_requested"
    assert len(findings) == 2
    assert findings[0]["file"] == "skills/example/tool.py"
    assert findings[0]["line"] == 41
    assert findings[0]["severity"] == "warning"  # (major)
    assert findings[1]["severity"] == "info"  # (minor)


def test_grok_adapter_raises_when_the_findings_section_does_not_parse_as_bullets():
    text = (FIXTURES / "grok-notools-findings-unparsed.md").read_text()
    with pytest.raises(review_phase.ParseError, match="FINDINGS"):
        review_phase.parse_grok_notools(text)


# --- the claude-fresh adapter ------------------------------------------------


def test_claude_adapter_parses_the_reviewer_agent_block():
    verdict, findings = review_phase.parse_claude_fresh(CLAUDE_REQUEST_CHANGES)
    assert verdict == "changes_requested"
    assert len(findings) == 3
    first = findings[0]
    assert first["file"] == "skills/sample-monitor/sample-monitor-run.sh"
    assert first["line"] == 212
    assert first["rule"] == "issue"
    assert first["severity"] == "error"
    assert first["description"].startswith("the write probe runs in the wrapper process")
    assert findings[1]["line"] == 188
    minor = findings[2]
    assert minor["rule"] == "minor"
    assert minor["severity"] == "info"
    assert minor["line"] == 41


def test_claude_adapter_parses_an_approve_block():
    verdict, findings = review_phase.parse_claude_fresh(CLAUDE_APPROVE)
    assert verdict == "approved"
    assert findings == []


def test_claude_adapter_ignores_the_evidence_section():
    # Evidence bullets carry `path:line` locators too; they record what the
    # reviewer ran and must never be counted as findings.
    verdict, findings = review_phase.parse_claude_fresh(CLAUDE_EVIDENCE)
    assert verdict == "changes_requested"
    assert [(f["rule"], f["line"]) for f in findings] == [("issue", 58), ("minor", 22)]
    result = review_phase.run_phase(_envelope("claude-fresh", CLAUDE_EVIDENCE))
    assert result["status"] == "success"
    assert len(result["artifacts"]["findings.json"]["findings"]) == 2


def test_claude_adapter_parses_the_reviewer_agent_documented_format():
    if not REVIEWER_AGENT.is_file():
        pytest.skip("reviewer agent is not beside this skill (installed copy)")
    text = REVIEWER_AGENT.read_text()
    block = re.search(r"## Output Format\n\n```\n(.*?)\n```", text, re.DOTALL)
    assert block, "agents/reviewer.md has no fenced Output Format block"
    _, findings = review_phase.parse_claude_fresh(block.group(1))
    assert [f["rule"] for f in findings] == ["issue", "issue", "minor"]
    assert [(f["file"], f["line"]) for f in findings[:2]] == [
        ("path/to/file.js", 42),
        ("path/to/other.js", 17),
    ]
    # The documented shape ends with its INTERVAL record, so the block as
    # written passes the phase.
    assert block.group(1).splitlines()[-1] == "INTERVAL: none"
    result = review_phase.run_phase(_envelope("claude-fresh", block.group(1)))
    assert result["status"] == "success", result["reason"]
    assert result["intervals"] == []


def test_claude_adapter_parses_the_review_skill_bullets():
    verdict, findings = review_phase.parse_claude_fresh(REVIEW_SKILL_FINDINGS)
    assert verdict == "changes_requested"
    assert [f["rule"] for f in findings] == ["bug", "security"]
    assert [f["line"] for f in findings] == [188, 77]
    assert findings[1]["file"] == "src/report.sh"
    assert {f["severity"] for f in findings} == {"error"}


def test_claude_adapter_parses_the_review_skill_clean_sentinel():
    verdict, findings = review_phase.parse_claude_fresh(REVIEW_SKILL_CLEAN)
    assert verdict == "approved"
    assert findings == []


def test_claude_adapter_accepts_clean_sentinel_inside_issues():
    # Real reviewer-agent approve block + the review skill's clean sentinel
    # inside `### Issues` — the form that previously failed as unparsed.
    verdict, findings = review_phase.parse_claude_fresh(CLAUDE_ISSUES_CLEAN)
    assert verdict == "approved"
    assert findings == []
    document = {
        "schema": "review-findings/v1",
        "backend": "claude-fresh",
        "verdict": verdict,
        "findings": findings,
    }
    assert review_phase.validate_findings(document) == []


def test_claude_adapter_accepts_clean_sentinel_as_issues_bullet():
    text = (
        "## Verdict: APPROVE\n\n"
        "### Issues\n"
        "- No bugs or security issues found.\n"
    )
    verdict, findings = review_phase.parse_claude_fresh(text)
    assert verdict == "approved"
    assert findings == []


def test_claude_adapter_accepts_clean_approve():
    verdict, findings = review_phase.parse_claude_fresh(CLEAN_APPROVE)
    assert verdict == "approved"
    assert findings == []
    document = {
        "schema": "review-findings/v1",
        "backend": "claude-fresh",
        "verdict": verdict,
        "findings": findings,
    }
    assert review_phase.validate_findings(document) == []


def test_claude_adapter_refuses_sentinel_plus_non_sentinel_prose_in_issues():
    text = (
        "## Verdict: APPROVE\n\n"
        "### Issues\n"
        "No bugs or security issues found.\n"
        "This paragraph is not a bullet or a numbered item at all.\n"
    )
    with pytest.raises(review_phase.ParseError, match="ISSUES"):
        review_phase.parse_claude_fresh(text)


def test_claude_adapter_parses_the_skill_mds_own_worked_example():
    # SKILL.md's Output-format section now carries one worked `- [BUG] …`
    # example, on a single line, to remove the "separate bullets" reading
    # F4 flagged. Pull that exact example out of the doc and prove the
    # adapter parses it the one-line way the doc claims — so a future
    # doc edit that drifts from the parser fails this test, not a reviewer.
    skill_md = (SKILL_DIR / "SKILL.md").read_text()
    match = re.search(r"e\.g\.\s*`- (\[BUG\][^`]*)`", skill_md, re.DOTALL)
    assert match, "SKILL.md's Output format section no longer carries a worked example"
    example = re.sub(r"\s+", " ", match.group(1)).strip()
    text = f"## Verdict: REQUEST CHANGES\n\n### Issues\n- {example}\n"
    verdict, findings = review_phase.parse_claude_fresh(text)
    assert verdict == "changes_requested"
    assert len(findings) == 1
    assert findings[0]["file"] == "skills/example/tool.py"
    assert findings[0]["line"] == 41


def test_skill_md_asks_for_the_interval_record_its_parser_requires():
    # The skill's own output format must end a review with the line the phase
    # refuses a review without; the worked example plus that line passes.
    skill_md = (SKILL_DIR / "SKILL.md").read_text()
    assert "End the review with an `INTERVAL:` line" in skill_md
    assert "`INTERVAL: none`" in skill_md
    match = re.search(r"e\.g\.\s*`- (\[BUG\][^`]*)`", skill_md, re.DOTALL)
    example = re.sub(r"\s+", " ", match.group(1)).strip()
    result = review_phase.run_phase(
        _envelope("claude-fresh", f"- {example}\n\nINTERVAL: none\n")
    )
    assert result["status"] == "success", result["reason"]
    assert result["intervals"] == []


def test_claude_adapter_parses_the_headless_verdict_label_shape():
    text = (FIXTURES / "claude-fresh-headless.md").read_text()
    verdict, findings = review_phase.parse_claude_fresh(text)
    assert verdict == "approved"
    assert len(findings) == 1
    assert findings[0]["file"] == "skills/example/tool.py"
    assert findings[0]["line"] == 12
    assert findings[0]["rule"] == "finding"
    assert findings[0]["severity"] == "warning"
    assert findings[0]["description"] == (
        "the slice drops the last element when the list has exactly one item"
    )


def test_claude_adapter_without_a_verdict_raises():
    with pytest.raises(review_phase.ParseError):
        review_phase.parse_claude_fresh(NO_VERDICT)


def test_claude_adapter_parses_numbered_issues_with_severity_tokens_and_locators():
    text = (FIXTURES / "claude-fresh-numbered.md").read_text()
    verdict, findings = review_phase.parse_claude_fresh(text)
    assert verdict == "changes_requested"
    assert len(findings) == 3
    # `.github/…:12` — a hidden-directory-rooted locator with a line number.
    assert findings[0]["file"] == ".github/scripts/cache.py"
    assert findings[0]["line"] == 12
    assert findings[0]["severity"] == "warning"  # (major)
    # `/abs/tools/cache.py:3` — an absolute-path locator.
    assert findings[1]["file"] == "/abs/tools/cache.py"
    assert findings[1]["line"] == 3
    assert findings[1]["severity"] == "error"  # (blocker)
    # A backticked path with no line number at all.
    assert findings[2]["file"] == ".claude/skills/cache.py"
    assert findings[2]["line"] is None
    assert findings[2]["severity"] == "info"  # (minor)


def test_claude_adapter_raises_when_a_section_does_not_parse_as_findings():
    text = (
        "## Verdict: REQUEST CHANGES\n\n"
        "### Issues\n"
        "This paragraph is not a bullet or a numbered item at all.\n"
    )
    with pytest.raises(review_phase.ParseError, match="ISSUES"):
        review_phase.parse_claude_fresh(text)


# --- a return that states two different verdicts is refused ------------------


def _claude_two_verdicts(first: str, second: str) -> str:
    return (
        f"{first}\n\n"
        "### Issues\n"
        "- `src/report.py:14` — the total skips the last row; include it in the sum.\n\n"
        f"{second}\n\n"
        "INTERVAL: none\n"
    )


@pytest.mark.parametrize(
    "first, second",
    [
        pytest.param(
            "## Verdict: REQUEST CHANGES", "## Verdict: APPROVE",
            id="request-changes-then-approve",
        ),
        pytest.param(
            "## Verdict: APPROVE", "## Verdict: REQUEST CHANGES",
            id="approve-then-request-changes",
        ),
        pytest.param(
            "VERDICT: REQUEST CHANGES", "VERDICT: APPROVE",
            id="label-request-changes-then-approve",
        ),
        pytest.param(
            "VERDICT: APPROVE", "VERDICT: REQUEST CHANGES",
            id="label-approve-then-request-changes",
        ),
        pytest.param(
            "VERDICT: REQUEST CHANGES", "## Verdict: APPROVE WITH FINDINGS",
            id="label-request-changes-then-heading-approve",
        ),
    ],
)
def test_claude_adapter_refuses_two_different_verdicts(first, second):
    text = _claude_two_verdicts(first, second)
    with pytest.raises(review_phase.VerdictConflict, match="conflicting verdicts"):
        review_phase.parse_claude_fresh(text)
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "failure", result["reason"]
    assert result["reason"].startswith("claude-fresh: conflicting verdicts")
    assert result["artifacts"]["findings.json"] is None


@pytest.mark.parametrize(
    "first, second, expected",
    [
        pytest.param("## Verdict: APPROVE", "VERDICT: APPROVE", "approved", id="approve-heading-and-label"),
        pytest.param(
            "## Verdict: APPROVE", "## Verdict: APPROVE WITH FINDINGS", "approved",
            id="approve-and-approve-with-findings",
        ),
        pytest.param(
            "## Verdict: REQUEST CHANGES", "## Verdict: REQUEST CHANGES", "changes_requested",
            id="request-changes-twice",
        ),
    ],
)
def test_claude_adapter_accepts_the_same_verdict_twice(first, second, expected):
    verdict, findings = review_phase.parse_claude_fresh(_claude_two_verdicts(first, second))
    assert verdict == expected
    assert [(f["file"], f["line"]) for f in findings] == [("src/report.py", 14)]


_DECORATED_REQUEST_CHANGES = [
    pytest.param("## Verdict: **REQUEST CHANGES**", id="bold-value-heading"),
    pytest.param("## **Verdict**: **REQUEST CHANGES**", id="bold-key-and-value-heading"),
    pytest.param("## **Verdict:** REQUEST CHANGES", id="bold-key-with-colon-heading"),
    pytest.param("VERDICT: [REQUEST CHANGES]", id="bracketed-value-label"),
    pytest.param("VERDICT: `REQUEST CHANGES`", id="backticked-value-label"),
    pytest.param("## `Verdict`: `REQUEST CHANGES`", id="backticked-key-and-value-heading"),
    pytest.param("## [Verdict]: [REQUEST CHANGES]", id="bracketed-key-and-value-heading"),
    pytest.param("## _Verdict_: _REQUEST CHANGES_", id="underscored-key-and-value-heading"),
    pytest.param(
        "## **[Verdict]**: **[REQUEST CHANGES]**",
        id="composed-key-and-value-heading",
    ),
]


_FULL_SPAN_REQUEST_CHANGES = [
    pytest.param("**Verdict: REQUEST CHANGES**", id="bold-full-label"),
    pytest.param("## **Verdict: REQUEST CHANGES**", id="bold-full-heading"),
    pytest.param("*Verdict: REQUEST CHANGES*", id="italic-full-label"),
    pytest.param("`VERDICT: REQUEST CHANGES`", id="backticked-full-label"),
]


_ISSUE = "- `src/report.py:14` — the total skips the last row.\n"


def _adapter_envelope(backend: str, text: str) -> dict:
    extras = (
        {"model": "example-model", "thinking": "medium"}
        if backend == "omp"
        else {}
    )
    return _envelope(backend, text, **extras)


@pytest.mark.parametrize(
    "backend, parser",
    [
        pytest.param("claude-fresh", review_phase.parse_claude_fresh, id="claude-fresh"),
        pytest.param("grok-notools", review_phase.parse_grok_notools, id="grok-notools"),
    ],
)
@pytest.mark.parametrize("decorated", _FULL_SPAN_REQUEST_CHANGES)
@pytest.mark.parametrize("reverse", [False, True], ids=["changes-first", "approve-first"])
def test_every_adapter_refuses_full_span_conflicting_verdicts(
    backend, parser, decorated, reverse
):
    labels = [decorated, "## Verdict: APPROVE"]
    if reverse:
        labels.reverse()
    text = "\n\n".join(labels) + "\n\nINTERVAL: none\n"
    with pytest.raises(review_phase.VerdictConflict, match="conflicting verdicts"):
        parser(text)
    result = review_phase.run_phase(_adapter_envelope(backend, text))
    assert result["status"] == "failure", (
        f"{backend} accepted full-span conflicting verdicts: {result['reason']}"
    )
    assert result["reason"].startswith(f"{backend}: conflicting verdicts")


@pytest.mark.parametrize(
    "backend, parser",
    [
        pytest.param("claude-fresh", review_phase.parse_claude_fresh, id="claude-fresh"),
        pytest.param("grok-notools", review_phase.parse_grok_notools, id="grok-notools"),
    ],
)
@pytest.mark.parametrize("decorated", _DECORATED_REQUEST_CHANGES)
@pytest.mark.parametrize("reverse", [False, True], ids=["changes-first", "approve-first"])
def test_every_adapter_refuses_decorated_conflicting_verdicts(
    backend, parser, decorated, reverse
):
    labels = [decorated, "## Verdict: APPROVE"]
    if reverse:
        labels.reverse()
    text = "\n\n".join(labels) + "\n\nINTERVAL: none\n"
    with pytest.raises(review_phase.VerdictConflict, match="conflicting verdicts"):
        parser(text)
    result = review_phase.run_phase(_adapter_envelope(backend, text))
    assert result["status"] == "failure", (
        f"{backend} accepted decorated conflicting verdicts: {result['reason']}"
    )
    assert result["reason"].startswith(f"{backend}: conflicting verdicts")


@pytest.mark.parametrize(
    "backend, parser",
    [
        pytest.param("claude-fresh", review_phase.parse_claude_fresh, id="claude-fresh"),
        pytest.param("grok-notools", review_phase.parse_grok_notools, id="grok-notools"),
    ],
)
@pytest.mark.parametrize("reverse", [False, True], ids=["flagged-first", "approve-first"])
def test_every_adapter_refuses_cross_vocabulary_conflict(backend, parser, reverse):
    labels = [
        "VERDICT: flagged — the total omits the last row.",
        "## Verdict: APPROVE",
    ]
    if reverse:
        labels.reverse()
    text = "\n\n".join(labels) + "\n\nINTERVAL: none\n"
    with pytest.raises(review_phase.VerdictConflict, match="conflicting verdicts"):
        parser(text)
    result = review_phase.run_phase(_adapter_envelope(backend, text))
    assert result["status"] == "failure", (
        f"{backend} accepted cross-vocabulary verdicts: {result['reason']}"
    )
    assert result["reason"].startswith(f"{backend}: conflicting verdicts")


@pytest.mark.parametrize(
    "backend, parser",
    [
        pytest.param("claude-fresh", review_phase.parse_claude_fresh, id="claude-fresh"),
        pytest.param("grok-notools", review_phase.parse_grok_notools, id="grok-notools"),
    ],
)
@pytest.mark.parametrize("reverse", [False, True], ids=["flagged-first", "cleared-first"])
def test_every_adapter_refuses_inline_grok_heading_conflict(backend, parser, reverse):
    labels = [
        "## VERDICT: flagged — the total omits the last row.",
        "## VERDICT\ncleared — the total is complete.",
    ]
    if reverse:
        labels.reverse()
    text = "## FINDINGS\n- none\n\n" + "\n\n".join(labels) + "\n\nINTERVAL: none\n"
    with pytest.raises(review_phase.VerdictConflict, match="conflicting verdicts"):
        parser(text)
    result = review_phase.run_phase(_adapter_envelope(backend, text))
    assert result["status"] == "failure", (
        f"{backend} accepted conflicting verdict headings: {result['reason']}"
    )
    assert result["reason"].startswith(f"{backend}: conflicting verdicts")


@pytest.mark.parametrize(
    "backend, parser",
    [
        pytest.param("claude-fresh", review_phase.parse_claude_fresh, id="claude-fresh"),
        pytest.param("grok-notools", review_phase.parse_grok_notools, id="grok-notools"),
    ],
)
def test_every_adapter_rejects_an_unknown_verdict_value(backend, parser):
    text = "## Verdict: APPROVE\n\nVERDICT: MAYBE\n\nINTERVAL: none\n"
    with pytest.raises(review_phase.ParseError, match="unrecognized verdict"):
        parser(text)
    result = review_phase.run_phase(_adapter_envelope(backend, text))
    assert result["status"] == "failure", (
        f"{backend} ignored an unknown verdict value: {result['reason']}"
    )
    assert "unrecognized verdict" in result["reason"]
@pytest.mark.parametrize(
    "label, following, expected",
    [
        pytest.param(
            "**Verdict: REQUEST CHANGES**",
            "### Issues\n" + _ISSUE,
            "changes_requested",
            id="bold-request-changes",
        ),
        pytest.param(
            "## **Verdict: REQUEST CHANGES**",
            "### Issues\n" + _ISSUE,
            "changes_requested",
            id="heading-bold-request-changes",
        ),
        pytest.param(
            "**VERDICT: REQUEST CHANGES**",
            "FINDINGS:\n" + _ISSUE,
            "changes_requested",
            id="uppercase-bold-request-changes",
        ),
        pytest.param(
            "*Verdict: REQUEST CHANGES*",
            "### Issues\n" + _ISSUE,
            "changes_requested",
            id="italic-request-changes",
        ),
        pytest.param(
            "`VERDICT: REQUEST CHANGES`",
            "FINDINGS:\n" + _ISSUE,
            "changes_requested",
            id="backticked-request-changes",
        ),
        pytest.param(
            "**Verdict: APPROVE**",
            "No bugs or security issues found.\n",
            "approved",
            id="bold-approve",
        ),
        pytest.param(
            "**Verdict: REQUEST CHANGES**",
            "Approve once the fix below lands.\n\n### Issues\n" + _ISSUE,
            "changes_requested",
            id="request-changes-before-approve-prose",
        ),
        pytest.param(
            "**Verdict: APPROVE**",
            "Flagged nothing blocking; one note below.\n\n### Minor notes\n" + _ISSUE,
            "approved",
            id="approve-before-flagged-prose",
        ),
        pytest.param(
            "## **Verdict: REQUEST CHANGES**",
            "Cleared the earlier concern, but one bug remains.\n\n## FINDINGS\n" + _ISSUE,
            "changes_requested",
            id="request-changes-before-cleared-prose",
        ),
    ],
)
def test_claude_adapter_uses_the_value_inside_a_full_span_label(
    label, following, expected
):
    verdict, _ = review_phase.parse_claude_fresh(f"{label}\n\n{following}")
    assert verdict == expected




def test_findings_bullet_named_verdict_is_not_a_label():
    text = (
        "## Verdict: APPROVE\n\n"
        "### Minor notes\n"
        "* Verdict: the earlier note still stands\n"
        "* `src/report.py:14` — rename the total.\n"
    )
    verdict, findings = review_phase.parse_claude_fresh(text)
    assert verdict == "approved"
    assert len(findings) == 2


@pytest.mark.parametrize(
    "backend, parser",
    [
        pytest.param("claude-fresh", review_phase.parse_claude_fresh, id="claude-fresh"),
        pytest.param("grok-notools", review_phase.parse_grok_notools, id="grok-notools"),
    ],
)
@pytest.mark.parametrize("unknown_first", [True, False], ids=["unknown-first", "unknown-last"])
@pytest.mark.parametrize("reverse", [False, True], ids=["approve-first", "changes-first"])
def test_mappable_conflict_precedes_an_unknown_verdict_error(
    backend, parser, unknown_first, reverse
):
    conflicting = ["## Verdict: APPROVE", "## Verdict: REQUEST CHANGES"]
    if reverse:
        conflicting.reverse()
    labels = ["VERDICT: MAYBE", *conflicting] if unknown_first else [
        *conflicting,
        "VERDICT: MAYBE",
    ]
    text = "\n\n".join(labels) + "\n\nINTERVAL: none\n"
    with pytest.raises(review_phase.VerdictConflict, match="conflicting verdicts"):
        parser(text)
    result = review_phase.run_phase(_adapter_envelope(backend, text))
    assert result["status"] == "failure", result["reason"]
    assert result["reason"].startswith(f"{backend}: conflicting verdicts")



def test_claude_clean_sentinel_does_not_conflict_with_a_request_changes_label():
    text = "No bugs or security issues found.\n\nVERDICT: REQUEST CHANGES\n"
    verdict, _ = review_phase.parse_claude_fresh(text)
    assert verdict == "changes_requested"


def _grok_two_verdicts(first: str, second: str) -> str:
    return (
        "## FINDINGS\n"
        "- `src/report.py:14` — the total skips the last row.\n\n"
        f"## VERDICT\n{first} — the first reading.\n\n"
        f"## VERDICT\n{second} — the second reading.\n\n"
        "INTERVAL: none\n"
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_grok_two_verdicts("flagged", "cleared"), id="flagged-then-cleared"),
        pytest.param(_grok_two_verdicts("cleared", "flagged"), id="cleared-then-flagged"),
        pytest.param(
            "FINDINGS:\n- none\nVERDICT: flagged — one.\nVERDICT: cleared — two.\n"
            "\nINTERVAL: none\n",
            id="label-flagged-then-cleared",
        ),
    ],
)
def test_grok_adapter_refuses_two_different_verdicts(text):
    with pytest.raises(review_phase.VerdictConflict, match="conflicting verdicts"):
        review_phase.parse_grok_notools(text)
    result = review_phase.run_phase(_envelope("grok-notools", text))
    assert result["status"] == "failure", result["reason"]
    assert result["reason"].startswith("grok-notools: conflicting verdicts")
    assert result["artifacts"]["findings.json"] is None


@pytest.mark.parametrize("token, expected", [("cleared", "approved"), ("flagged", "changes_requested")])
def test_grok_adapter_accepts_the_same_verdict_twice(token, expected):
    verdict, findings = review_phase.parse_grok_notools(_grok_two_verdicts(token, token))
    assert verdict == expected
    assert len(findings) == 1


def test_run_phase_accepts_claude_clean_sentinel_inside_issues():
    result = review_phase.run_phase(_envelope("claude-fresh", CLAUDE_ISSUES_CLEAN))
    assert result["status"] == "success", result["reason"]
    findings = result["artifacts"]["findings.json"]
    assert findings["schema"] == "review-findings/v1"
    assert findings["verdict"] == "approved"
    assert findings["findings"] == []
    assert review_phase.validate_findings(findings) == []


def test_run_phase_accepts_grok_findings_none():
    result = review_phase.run_phase(_envelope("grok-notools", GROK_CLEARED))
    assert result["status"] == "success", result["reason"]
    findings = result["artifacts"]["findings.json"]
    assert findings["schema"] == "review-findings/v1"
    assert findings["verdict"] == "approved"
    assert findings["findings"] == []
    assert review_phase.validate_findings(findings) == []


def test_run_phase_accepts_clean_approve_on_claude_fresh():
    result = review_phase.run_phase(_envelope("claude-fresh", CLEAN_APPROVE))
    assert result["status"] == "success", result["reason"]
    findings = result["artifacts"]["findings.json"]
    assert findings["schema"] == "review-findings/v1"
    assert findings["verdict"] == "approved"
    assert findings["findings"] == []
    assert review_phase.validate_findings(findings) == []


def test_run_phase_refuses_non_sentinel_prose_in_findings():
    text = (FIXTURES / "grok-notools-findings-unparsed.md").read_text()
    result = review_phase.run_phase(_envelope("grok-notools", text))
    assert result["status"] == "failure", result
    assert result["artifacts"]["findings.json"] is None
    assert "FINDINGS" in result["reason"]


def test_run_phase_refuses_non_sentinel_prose_in_issues():
    text = (
        "## Verdict: REQUEST CHANGES\n\n"
        "### Issues\n"
        "This paragraph is not a bullet or a numbered item at all.\n\n"
        "INTERVAL: none\n"
    )
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "failure", result
    assert result["artifacts"]["findings.json"] is None
    assert "ISSUES" in result["reason"]


# --- the phase result --------------------------------------------------------


def test_run_phase_returns_a_v1_result_with_both_artifacts():
    envelope = _envelope("grok-notools", GROK_FLAGGED)
    before = copy.deepcopy(envelope)
    result = review_phase.run_phase(envelope)
    assert envelope == before
    assert set(result) == {
        "phase_result",
        "status",
        "artifacts",
        "reason",
        "resume_token",
        "intervals",
    }
    assert result["intervals"] == []
    assert result["phase_result"] == "v1"
    assert result["status"] == "success"
    assert result["resume_token"] is None
    assert set(result["artifacts"]) == {"review.md", "findings.json"}
    assert result["artifacts"]["review.md"] == GROK_FLAGGED
    findings = result["artifacts"]["findings.json"]
    assert findings["backend"] == "grok-notools"
    assert findings["verdict"] == "changes_requested"
    assert review_phase.validate_findings(findings) == []


def test_run_phase_reports_failure_when_the_return_has_no_verdict():
    result = review_phase.run_phase(_envelope("claude-fresh", NO_VERDICT))
    assert result["status"] == "failure"
    assert "verdict" in result["reason"]
    assert result["artifacts"]["review.md"] == NO_VERDICT
    assert result["artifacts"]["findings.json"] is None
    assert result["resume_token"] is None


def test_run_phase_reads_return_file_when_return_text_is_absent(tmp_path: Path):
    return_file = tmp_path / "return.md"
    return_file.write_text(GROK_CLEARED, encoding="utf-8")
    envelope = _envelope("grok-notools", None)
    envelope["inputs"].pop("return_text")
    envelope["inputs"]["return_file"] = str(return_file)
    result = review_phase.run_phase(envelope)
    assert result["status"] == "success", result["reason"]
    assert result["artifacts"]["review.md"] == GROK_CLEARED
    assert result["artifacts"]["findings.json"]["verdict"] == "approved"


def test_run_phase_reports_blocked_not_a_traceback_on_undecodable_return_file(tmp_path: Path):
    return_file = tmp_path / "return.md"
    return_file.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    envelope = _envelope("grok-notools", None)
    envelope["inputs"].pop("return_text")
    envelope["inputs"]["return_file"] = str(return_file)
    result = review_phase.run_phase(envelope)
    assert result["status"] == "blocked"
    assert "return_file" in result["reason"]


def test_from_text_mode_reports_exit_2_not_a_traceback_on_undecodable_input(tmp_path: Path):
    bad = tmp_path / "return.md"
    bad.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    result = _run_cli("--from-text", str(bad), "--backend", "grok-notools")
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda e: e.pop("budget"), "budget"),
        (lambda e: e.__setitem__("extra", 1), "extra"),
        (lambda e: e["inputs"].__setitem__("backend", "codex"), "codex"),
        (lambda e: e["inputs"].pop("return_text"), "return_text"),
    ],
)
def test_run_phase_is_blocked_on_a_bad_envelope(mutate, fragment):
    envelope = _envelope("grok-notools", GROK_FLAGGED)
    mutate(envelope)
    result = review_phase.run_phase(envelope)
    assert result["status"] == "blocked"
    assert fragment in result["reason"]
    assert result["resume_token"] is None


def test_run_cli_exit_codes_track_status(tmp_path: Path):
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps(_envelope("claude-fresh", CLAUDE_REQUEST_CHANGES)))
    raw = ok.read_bytes()
    result = _run_cli("--envelope", str(ok))
    assert result.returncode == 0, result.stderr
    assert ok.read_bytes() == raw
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert len(payload["artifacts"]["findings.json"]["findings"]) == 3

    fail = tmp_path / "fail.json"
    fail.write_text(json.dumps(_envelope("claude-fresh", NO_VERDICT)))
    result = _run_cli("--envelope", str(fail))
    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "failure"

    blocked = tmp_path / "blocked.json"
    blocked.write_text(json.dumps({"inputs": {}}))
    result = _run_cli("--envelope", str(blocked))
    assert result.returncode == 3
    assert json.loads(result.stdout)["status"] == "blocked"


def test_from_text_mode_writes_the_result_file(tmp_path: Path):
    out = tmp_path / "result.json"
    result = _run_cli(
        "--from-text", str(FIXTURES / "grok-notools-cleared.md"),
        "--backend", "grok-notools",
        "--out", str(out),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(out.read_text())
    assert payload["phase_result"] == "v1"
    assert payload["status"] == "success"
    assert payload["artifacts"]["findings.json"]["verdict"] == "approved"
    assert payload["artifacts"]["findings.json"]["findings"] == []


def test_from_text_mode_refuses_an_unknown_backend():
    result = _run_cli("--from-text", str(FIXTURES / "no-verdict.md"), "--backend", "codex")
    assert result.returncode == 2
    assert "codex" in result.stderr


# --- both call-site paths, one schema ---------------------------------------


def test_standalone_and_caller_paths_validate_against_the_same_schema(tmp_path: Path):
    """Both real call sites, each producing an artifact file the CLI reads.

    Standalone: `review`/SKILL.md's own documented invocation —
    `--from-text <file> --backend claude-fresh` on a saved return. Caller:
    a run envelope built the way a caller assembles one from an
    already-obtained return (`--envelope <file>`), not a hand-built dict fed
    straight to `validate_findings`. Each artifact is written to its own file
    and re-read by a fresh `--validate <file>` subprocess, so the schema file
    on disk is the thing actually being read, twice, not a Python object
    already sitting in memory.
    """
    standalone_out = tmp_path / "standalone-result.json"
    standalone_run = _run_cli(
        "--from-text", str(FIXTURES / "review-skill-findings.md"),
        "--backend", "claude-fresh",
        "--out", str(standalone_out),
    )
    assert standalone_run.returncode == 0, standalone_run.stderr

    caller_envelope = tmp_path / "caller-envelope.json"
    caller_envelope.write_text(json.dumps(_envelope("grok-notools", GROK_FLAGGED)))
    caller_out = tmp_path / "caller-result.json"
    caller_run = _run_cli("--envelope", str(caller_envelope), "--out", str(caller_out))
    assert caller_run.returncode == 0, caller_run.stderr

    for result_file in (standalone_out, caller_out):
        result = json.loads(result_file.read_text())
        assert result["status"] == "success"
        findings_file = tmp_path / f"{result_file.stem}-findings.json"
        findings_file.write_text(json.dumps(result["artifacts"]["findings.json"]))
        validated = _run_cli("--validate", str(findings_file))
        assert validated.returncode == 0, validated.stderr
        assert result["artifacts"]["findings.json"]["schema"] == "review-findings/v1"


# --- the INTERVAL record -----------------------------------------------------
#
# The last non-blank line of every return is its INTERVAL record:
# `INTERVAL: none`, or one measured line per interval the verdict relies on.
# Nothing else in a return is read for time, so the cases below put durations,
# commit shas and quoted INTERVAL lines everywhere else and expect them to
# change nothing.

DURATION_WITHOUT_INTERVAL = (FIXTURES / "duration-without-interval.md").read_text()
DURATION_WITH_INTERVAL = (FIXTURES / "duration-with-interval.md").read_text()
MEASURED = (
    "INTERVAL: from request start, events.tsv:14 to result write, "
    "result.md mtime = 41m"
)
MEASURED_RECORD = {
    "start_event": "request start",
    "start_source": "events.tsv:14",
    "end_event": "result write",
    "end_source": "result.md mtime",
    "duration": "41m",
}
SECOND_MEASURED = (
    "INTERVAL: from first retry, worker.log:88 to last retry, worker.log:131 = 2.5h"
)
SECOND_RECORD = {
    "start_event": "first retry",
    "start_source": "worker.log:88",
    "end_event": "last retry",
    "end_source": "worker.log:131",
    "duration": "2.5h",
}


def _timed_grok(*record_lines: str) -> str:
    """A cleared grok return; its record lines start on line 7."""
    lines = [
        "## FINDINGS",
        "- none",
        "",
        "## VERDICT",
        "cleared — the caller waited for 41 minutes.",
        "",
        *record_lines,
        "",
    ]
    return "\n".join(lines)


def _reviewer_block(
    *tail: str,
    summary: str = "Checked the change.",
    issues: str = "- none",
    evidence: str = "- Ran: checks passed",
) -> str:
    """The reviewer agent's block. With one-line parts, Evidence is line 9
    and the tail starts on line 11."""
    return "\n".join(
        [
            "## Verdict: APPROVE",
            "",
            summary,
            "",
            "### Issues",
            issues,
            "",
            "### Evidence",
            evidence,
            "",
            *tail,
            "",
        ]
    )


def _accepted(text: str, backend: str = "claude-fresh") -> dict:
    result = review_phase.run_phase(_envelope(backend, text))
    assert result["status"] == "success", result["reason"]
    assert result["artifacts"]["review.md"] == text
    return result


def _refused(text: str, backend: str = "claude-fresh") -> str:
    result = review_phase.run_phase(_envelope(backend, text))
    assert result["status"] == "failure", result
    assert result["artifacts"]["review.md"] == text
    assert result["artifacts"]["findings.json"] is None
    assert result["resume_token"] is None
    assert "intervals" not in result
    return result["reason"]


def test_clean_review_with_interval_none_carries_an_empty_interval_list():
    result = review_phase.run_phase(_envelope("grok-notools", GROK_CLEARED))
    expected = review_phase._result(
        "success",
        "grok-notools: approved, 0 finding(s)",
        GROK_CLEARED,
        {
            "schema": "review-findings/v1",
            "backend": "grok-notools",
            "verdict": "approved",
            "findings": [],
        },
    )
    expected["intervals"] = []
    assert result == expected

    skill = review_phase.run_phase(_envelope("claude-fresh", REVIEW_SKILL_CLEAN))
    skill_expected = review_phase._result(
        "success",
        "claude-fresh: approved, 0 finding(s)",
        REVIEW_SKILL_CLEAN,
        {
            "schema": "review-findings/v1",
            "backend": "claude-fresh",
            "verdict": "approved",
            "findings": [],
        },
    )
    skill_expected["intervals"] = []
    assert skill == skill_expected


def test_missing_record_is_refused_naming_the_last_non_blank_line():
    reason = _refused(DURATION_WITHOUT_INTERVAL, backend="grok-notools")
    assert reason == (
        "grok-notools: review_interval_missing: line 5: "
        "cleared — the caller waited for 41 minutes."
    )


@pytest.mark.parametrize(
    ("text", "named"),
    [
        pytest.param(_reviewer_block(), "line 9: - Ran: checks passed", id="absent"),
        pytest.param(
            _reviewer_block("INTERVAL: none", "Happy to look again."),
            "line 12: Happy to look again.",
            id="present-but-not-last",
        ),
        pytest.param(
            _reviewer_block(evidence="- Probe:\n```text\nINTERVAL: none\n```"),
            "line 12: ```",
            id="last-line-is-a-fence-closer",
        ),
        pytest.param(
            _reviewer_block("- INTERVAL: none"),
            "line 11: - INTERVAL: none",
            id="bulleted",
        ),
        pytest.param(
            _reviewer_block("**INTERVAL: none**"),
            "line 11: **INTERVAL: none**",
            id="emphasised",
        ),
    ],
)
def test_review_without_a_trailing_record_is_refused(text, named):
    reason = _refused(text)
    assert reason == f"claude-fresh: review_interval_missing: {named}"


def test_missing_record_refusal_cuts_the_named_line_at_200_characters():
    body = "The caller waited for 41 minutes. " + ("x" * 220)
    reason = _refused(_reviewer_block(evidence=body))
    _, _, named = reason.partition("review_interval_missing: line 9: ")
    assert len(named) == 200
    assert named == body[:200]


def test_blank_return_text_names_line_zero_when_called_directly():
    with pytest.raises(review_phase.ParseError) as exc:
        review_phase._require_intervals("\n  \n")
    assert str(exc.value) == "review_interval_missing: line 0: "


def test_interval_none_is_accepted_whatever_the_rest_of_the_review_says():
    text = _reviewer_block(
        "INTERVAL: none",
        summary=(
            "The job ran for 41 minutes at 919960d; the old INTERVAL: from a, b "
            "to c, d = 3h line was a probe input."
        ),
        issues="- `a.py:3` — the retry waits `2h` and then 3 days before giving up.",
        evidence=(
            "- Ran: `pytest` took 12 seconds; 1df4a5c is the base.\n"
            "```text\n"
            "INTERVAL: from start, a.log:1 to end, a.log:9 = 41m\n"
            "INTERVAL: none\n"
            "it took 2h\n"
            "```\n"
            "- Quoted: `INTERVAL: from x, y to z, w = 5m`"
        ),
    )
    result = _accepted(text)
    assert result["intervals"] == []
    findings = result["artifacts"]["findings.json"]
    assert findings["verdict"] == "approved"
    assert [f["line"] for f in findings["findings"]] == [3]


# The classes the prose duration scan used to get wrong. Each now ends in
# `INTERVAL: none`, and none of them asks for anything more.
NO_LONGER_READ = {
    "short-sha": _reviewer_block(
        "INTERVAL: none", summary="Checked 919960d against the base commit."
    ),
    "duration-in-a-code-span": _reviewer_block(
        "INTERVAL: none", summary="The probe `it took 2h` is quoted from the brief."
    ),
    "duration-in-a-fence": _reviewer_block(
        "INTERVAL: none",
        summary="The probes quoted from the brief:\n```\nit took 2h\nwaits 3d\n```",
    ),
    "sentinel-looking-fence-info-string": _reviewer_block(
        "INTERVAL: none",
        issues="- none",
        evidence=(
            "- Probe:\n"
            "```No bugs or security issues found.\n"
            "it took 2h\n"
            "```"
        ),
    ),
    "duration-in-prose": _reviewer_block(
        "INTERVAL: none", summary="The job ran for 41 minutes before the timeout."
    ),
}


@pytest.mark.parametrize("text", list(NO_LONGER_READ.values()), ids=list(NO_LONGER_READ))
def test_text_outside_the_record_never_demands_an_interval(text):
    result = _accepted(text)
    assert result["intervals"] == []


@pytest.mark.parametrize(
    "name",
    [
        "pr-35-claude-empirical-r2.output.md",
        "pr-36-claude-empirical-r1.attempt1.output.md",
        "pr-36-claude-empirical-r1.output.md",
    ],
)
def test_copied_approve_review_quoting_durations_parses_with_interval_none(name):
    result = _accepted((FIXTURES / name).read_text(encoding="utf-8"))
    findings = result["artifacts"]["findings.json"]
    assert findings["verdict"] == "approved"
    assert result["intervals"] == []
    assert not any(
        f["severity"] == "error" or f["rule"] == "issue" for f in findings["findings"]
    )


def test_copied_request_changes_review_quoting_durations_parses_with_interval_none():
    text = (FIXTURES / "pr-36-claude-empirical-r1.attempt2.output.md").read_text(
        encoding="utf-8"
    )
    result = _accepted(text)
    findings = result["artifacts"]["findings.json"]
    assert findings["verdict"] == "changes_requested"
    assert result["intervals"] == []
    assert any(f["rule"] == "issue" for f in findings["findings"])


def test_duration_with_complete_interval_is_parsed_onto_the_phase_result():
    result = _accepted(DURATION_WITH_INTERVAL, backend="grok-notools")
    assert result["intervals"] == [MEASURED_RECORD]
    findings = result["artifacts"]["findings.json"]
    assert findings["verdict"] == "approved"
    assert findings["findings"] == []
    assert "intervals" not in findings
    assert review_phase.validate_findings(findings) == []


def test_two_distinct_records_are_parsed_in_order():
    result = _accepted(_timed_grok(MEASURED, SECOND_MEASURED), backend="grok-notools")
    assert result["intervals"] == [MEASURED_RECORD, SECOND_RECORD]


def test_end_source_holding_an_equals_sign_splits_on_the_last_one():
    line = (
        "INTERVAL: from request start, events.tsv:14 to result write, "
        "worker log line status=done = 41m"
    )
    result = _accepted(_timed_grok(line), backend="grok-notools")
    assert result["intervals"] == [
        dict(MEASURED_RECORD, end_source="worker log line status=done")
    ]


def test_blank_line_ends_the_record_block():
    # Only the run of INTERVAL lines that ends at the last non-blank line is
    # the record; the line above the blank is never read.
    result = _accepted(
        _timed_grok("INTERVAL: none", "", "INTERVAL: none"), backend="grok-notools"
    )
    assert result["intervals"] == []
    result = _accepted(_timed_grok(MEASURED, "", SECOND_MEASURED), backend="grok-notools")
    assert result["intervals"] == [SECOND_RECORD]


@pytest.mark.parametrize(
    "line",
    [
        "INTERVAL: none",
        "interval: NONE",
        "  Interval : None",
        "\tINTERVAL:\tnone\t",
    ],
)
def test_interval_none_label_and_value_are_case_insensitive(line):
    result = _accepted(_timed_grok(line), backend="grok-notools")
    assert result["intervals"] == []


@pytest.mark.parametrize(
    ("line", "detail"),
    [
        pytest.param("INTERVAL: maybe", "", id="neither-none-nor-from"),
        pytest.param("INTERVAL:", "", id="empty"),
        pytest.param("INTERVAL: none.", "", id="none-with-a-period"),
        pytest.param("INTERVAL: none, nothing measured", "", id="none-with-more"),
        pytest.param("INTERVAL: fromage a, b to c, d = 1s", "", id="from-glued-to-a-word"),
        pytest.param(
            "INTERVAL: from a, b to c, d = 41 minutes",
            " (duration is not a number then ms, s, m, h or d)",
            id="spelled-out-unit",
        ),
        pytest.param(
            "INTERVAL: from a, b to c, d = 41min",
            " (duration is not a number then ms, s, m, h or d)",
            id="unknown-unit",
        ),
        pytest.param(
            "INTERVAL: from a, b to c, d = 41 m",
            " (duration is not a number then ms, s, m, h or d)",
            id="spaced-unit",
        ),
        pytest.param(
            "INTERVAL: from a, b to c, d = about 41m",
            " (duration is not a number then ms, s, m, h or d)",
            id="words-before-the-number",
        ),
    ],
)
def test_malformed_record_is_refused(line, detail):
    reason = _refused(_timed_grok(line), backend="grok-notools")
    assert reason == f"grok-notools: review_interval_malformed: line 7: {line}{detail}"


@pytest.mark.parametrize(
    "duration", ["41m", "500ms", "2.5h", "3d", "90s", "0.25s"]
)
def test_every_documented_duration_unit_is_accepted(duration):
    line = f"INTERVAL: from a, b to c, d = {duration}"
    result = _accepted(_timed_grok(line), backend="grok-notools")
    assert result["intervals"][0]["duration"] == duration


@pytest.mark.parametrize(
    ("line", "missing"),
    [
        pytest.param(
            "INTERVAL: from , events.tsv:14 to result write, result.md mtime = 41m",
            "start_event",
            id="start-event",
        ),
        pytest.param(
            "INTERVAL: from request start, to result write, result.md mtime = 41m",
            "start_source",
            id="start-source",
        ),
        pytest.param(
            "INTERVAL: from request start, events.tsv:14 to , result.md mtime = 41m",
            "end_event",
            id="end-event",
        ),
        pytest.param(
            "INTERVAL: from request start, events.tsv:14 to result write,  = 41m",
            "end_source",
            id="end-source",
        ),
        pytest.param(
            "INTERVAL: from request start, events.tsv:14 to result write, result.md mtime =",
            "duration",
            id="duration",
        ),
        pytest.param(
            "INTERVAL: from request start to result write = 41m",
            "start_event, start_source, end_event, end_source, duration",
            id="no-separators",
        ),
    ],
)
def test_record_with_an_empty_part_is_refused_naming_the_part(line, missing):
    reason = _refused(_timed_grok(line), backend="grok-notools")
    assert reason == (
        f"grok-notools: review_interval_endpoints_missing: line 7: {line} (missing {missing})"
    )


@pytest.mark.parametrize(
    "lines",
    [
        pytest.param(("INTERVAL: none", MEASURED), id="none-then-measured"),
        pytest.param((MEASURED, "INTERVAL: none"), id="measured-then-none"),
        pytest.param(("INTERVAL: none", "interval: None"), id="none-twice"),
    ],
)
def test_contradictory_record_is_refused_at_its_second_line(lines):
    reason = _refused(_timed_grok(*lines), backend="grok-notools")
    assert reason == (
        f"grok-notools: review_interval_contradictory: line 8: {lines[1]}"
    )


@pytest.mark.parametrize(
    "second",
    [
        pytest.param(MEASURED, id="identical"),
        pytest.param("  " + MEASURED + "\t", id="identical-after-trimming"),
    ],
)
def test_duplicate_measured_line_is_refused(second):
    reason = _refused(_timed_grok(MEASURED, SECOND_MEASURED, second), backend="grok-notools")
    assert reason == f"grok-notools: review_interval_duplicate: line 9: {second}"


def test_first_failing_line_wins_in_text_order():
    reason = _refused(
        _timed_grok(MEASURED, "INTERVAL: maybe", "INTERVAL: none"), backend="grok-notools"
    )
    assert reason == "grok-notools: review_interval_malformed: line 8: INTERVAL: maybe"


def test_the_adapters_never_see_the_trailing_record():
    # Each of these ends on a findings section, so a record line read by the
    # adapter would be foreign prose in it.
    for name, backend in [
        ("claude-fresh-numbered.md", "claude-fresh"),
        ("clean-approve.md", "claude-fresh"),
        ("claude-fresh-headless.md", "claude-fresh"),
    ]:
        text = (FIXTURES / name).read_text(encoding="utf-8")
        assert text.rstrip().endswith("INTERVAL: none"), name
        assert _accepted(text, backend=backend)["intervals"] == [], name


def test_an_interval_line_above_the_record_is_read_like_any_other_line():
    text = _reviewer_block(
        "INTERVAL: none", issues="INTERVAL: none\n- `a.py:1` — the loop never exits"
    )
    with pytest.raises(review_phase.ParseError, match="ISSUES"):
        review_phase.parse_claude_fresh(text)
    reason = _refused(text)
    assert "ISSUES" in reason


def test_grok_record_after_the_verdict_value_parses_and_before_it_refuses():
    after = "## FINDINGS\n- none\n\n## VERDICT\ncleared — fine\nINTERVAL: none\n"
    assert _accepted(after, backend="grok-notools")["intervals"] == []
    before = "## FINDINGS\n- none\n\n## VERDICT\nINTERVAL: none\ncleared — fine\n"
    reason = _refused(before, backend="grok-notools")
    assert reason == "grok-notools: review_interval_missing: line 6: cleared — fine"


def test_from_text_cli_refuses_a_missing_record_with_exit_1(tmp_path: Path):
    review = tmp_path / "return.md"
    review.write_text("No bugs or security issues found.\n", encoding="utf-8")
    proc = _run_cli("--from-text", str(review), "--backend", "claude-fresh")
    assert proc.returncode == review_phase.EXIT_FAILURE, proc.stderr
    result = json.loads(proc.stdout)
    assert result["reason"] == (
        "claude-fresh: review_interval_missing: line 1: No bugs or security issues found."
    )


class _ApprovingJev:
    calls = 0

    def decide(self, text: str) -> tuple[str, float]:
        type(self).calls += 1
        return review_phase.APPROVED, 0.95


def _jev_fallback_run(monkeypatch, text: str, backend: str = "claude-fresh") -> dict:
    monkeypatch.setattr(_ApprovingJev, "calls", 0)
    monkeypatch.setattr(review_phase, "JevClient", _ApprovingJev)
    envelope = _envelope(backend, text)
    envelope["adapters"] = {"jev_fallback": True}
    return review_phase.run_phase(envelope)


def test_jev_fallback_refuses_a_missing_record_before_jev_is_asked(monkeypatch):
    result = _jev_fallback_run(monkeypatch, "The diff looks reasonable.\n")
    assert _ApprovingJev.calls == 0
    assert result["status"] == "failure"
    assert result["reason"] == (
        "claude-fresh: review_interval_missing: line 1: The diff looks reasonable."
    )
    assert "intervals" not in result


def test_jev_fallback_accepts_interval_none(monkeypatch):
    result = _jev_fallback_run(monkeypatch, "The diff looks reasonable.\n\nINTERVAL: none\n")
    assert _ApprovingJev.calls == 1
    assert result["status"] == "success", result["reason"]
    assert result["verdict_source"] == "jev"
    assert result["intervals"] == []


def test_jev_fallback_success_carries_the_measured_record(monkeypatch):
    result = _jev_fallback_run(monkeypatch, "The diff looks reasonable.\n\n" + MEASURED + "\n")
    assert _ApprovingJev.calls == 1
    assert result["status"] == "success", result["reason"]
    assert result["verdict_source"] == "jev"
    assert result["intervals"] == [MEASURED_RECORD]


def test_jev_fallback_other_adapter_path_still_requires_the_record(monkeypatch):
    text = "## Verdict: APPROVE\n\n### Issues\n- none\n"
    result = _jev_fallback_run(monkeypatch, text, backend="grok-notools")
    assert _ApprovingJev.calls == 0
    assert result["status"] == "failure"
    assert result["reason"] == "grok-notools: review_interval_missing: line 4: - none"
    accepted = _jev_fallback_run(monkeypatch, text + "INTERVAL: none\n", backend="grok-notools")
    assert accepted["status"] == "success", accepted["reason"]
    assert accepted["verdict_source"] == "regex"
    assert accepted["intervals"] == []


def test_jev_shadow_runs_only_after_the_record_passes(monkeypatch):
    monkeypatch.setenv("REVIEW_PHASE_JEV_SHADOW", "1")
    monkeypatch.setattr(_ApprovingJev, "calls", 0)
    monkeypatch.setattr(review_phase, "JevClient", _ApprovingJev)
    refused = review_phase.run_phase(_envelope("grok-notools", DURATION_WITHOUT_INTERVAL))
    assert refused["status"] == "failure"
    assert "review_interval_missing" in refused["reason"]
    assert "jev_verdict" not in refused
    assert _ApprovingJev.calls == 0
    accepted = review_phase.run_phase(_envelope("grok-notools", DURATION_WITH_INTERVAL))
    assert accepted["status"] == "success", accepted["reason"]
    assert accepted["intervals"] == [MEASURED_RECORD]
    assert accepted["jev_verdict"] == "approved"
    assert _ApprovingJev.calls == 1


# Each shape ends on a findings section, so a last line that is not a record
# would be foreign prose in that section if an adapter read it first. `{tail}`
# starts on the line given.
_SECTION_LAST_SHAPES = [
    pytest.param(
        "claude-fresh",
        "## Verdict: APPROVE\n\nChecked the change.\n\n### Issues\n- none\n{tail}\n",
        7,
        id="claude-issues",
    ),
    pytest.param(
        "claude-fresh",
        "## Verdict: APPROVE\n\nChecked the change.\n\n### Issues\n- none\n\n"
        "### Minor notes\n- `a.py:3` — the helper name is vague\n{tail}\n",
        10,
        id="claude-minor-notes",
    ),
    pytest.param(
        "claude-fresh",
        "VERDICT: APPROVE\n\nFINDINGS:\n- none\n{tail}\n",
        5,
        id="claude-headless-findings",
    ),
    pytest.param(
        "grok-notools",
        "## VERDICT\ncleared — fine\n\n## FINDINGS\n- none\n{tail}\n",
        6,
        id="grok-findings",
    ),
    pytest.param(
        "grok-notools",
        "VERDICT: cleared — fine\n\nFINDINGS:\n- none\n{tail}\n",
        5,
        id="grok-headless-findings",
    ),
]


@pytest.mark.parametrize("jev_fallback", [False, True], ids=["plain", "jev-fallback"])
@pytest.mark.parametrize("last", ["**INTERVAL: none**", "Happy to look again."])
@pytest.mark.parametrize(("backend", "shape", "tail_line"), _SECTION_LAST_SHAPES)
def test_section_ending_in_a_non_record_line_is_refused_as_missing(
    monkeypatch, backend, shape, tail_line, last, jev_fallback
):
    text = shape.format(tail=last)
    if jev_fallback:
        result = _jev_fallback_run(monkeypatch, text, backend=backend)
        assert _ApprovingJev.calls == 0
        assert result["status"] == "failure"
        reason = result["reason"]
    else:
        reason = _refused(text, backend=backend)
    assert reason == f"{backend}: review_interval_missing: line {tail_line}: {last}"


@pytest.mark.parametrize("jev_fallback", [False, True], ids=["plain", "jev-fallback"])
@pytest.mark.parametrize(("backend", "shape", "tail_line"), _SECTION_LAST_SHAPES)
def test_malformed_record_under_an_unparsed_section_is_refused_by_its_own_name(
    monkeypatch, backend, shape, tail_line, jev_fallback
):
    text = shape.format(tail="Happy to look again.\nINTERVAL: maybe")
    if jev_fallback:
        result = _jev_fallback_run(monkeypatch, text, backend=backend)
        assert _ApprovingJev.calls == 0
        assert result["status"] == "failure"
        reason = result["reason"]
    else:
        reason = _refused(text, backend=backend)
    assert reason == (
        f"{backend}: review_interval_malformed: line {tail_line + 1}: INTERVAL: maybe"
    )


RECORD_LIMIT_SECONDS = 1.0


def test_record_check_finishes_long_inputs_within_a_second():
    many_records = "\n".join(
        f"INTERVAL: from start {n}, a.log:{n} to end {n}, b.log:{n} = {n}s"
        for n in range(40_000)
    )
    long_line = "INTERVAL: from a, b to c, " + "k=v " * 50_000 + "= 1s"
    repeated_separators = "INTERVAL: from a, b " + "to x, " * 40_000 + "= 1s"
    many_blank_lines = "No bugs or security issues found.\n" + " \n" * 100_000
    shapes = {
        "40000-records": (many_records, 40_000),
        "200kb-end-source-of-equals-signs": (long_line, 1),
        "240kb-repeated-separators": (repeated_separators, 1),
        "100000-trailing-blank-lines": (many_blank_lines, None),
    }
    for name, (text, count) in shapes.items():
        start = time.perf_counter()
        try:
            records = review_phase._require_intervals(text)
        except review_phase.ParseError:
            records = None
        elapsed = time.perf_counter() - start
        assert elapsed <= RECORD_LIMIT_SECONDS, (
            f"the record check took {elapsed:.2f}s on {name}"
        )
        assert (records is None) if count is None else (len(records) == count), name


# --- prose clean markers ----------------------------------------------------


def _claude_approve_with(body: str, issues: str = "- none") -> str:
    return (
        "## Verdict: APPROVE\n\n"
        f"### Summary\n{body}\n\n"
        f"### Issues\n{issues}\n\n"
        "INTERVAL: none\n"
    )


PROSE_CLEAN_MARKERS = [
    "None",
    "None.",
    "None blocking.",
    "No issues",
    "No issues.",
    "none blocking",
    "NO ISSUES.",
    "- None",
    "- None blocking.",
    "- No issues.",
]


@pytest.mark.parametrize("marker", PROSE_CLEAN_MARKERS)
def test_prose_clean_marker_under_issues_parses_as_zero_findings(marker):
    text = _claude_approve_with("Looks fine.", issues=marker)
    verdict, findings = review_phase.parse_claude_fresh(text)
    assert verdict == "approved"
    assert findings == []
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "success", result["reason"]


@pytest.mark.parametrize("marker", PROSE_CLEAN_MARKERS)
def test_prose_clean_marker_without_a_verdict_line_still_refuses(marker):
    text = f"### Summary\nLooks fine.\n\n### Issues\n{marker}\n"
    with pytest.raises(review_phase.ParseError, match="no verdict"):
        review_phase.parse_claude_fresh(text)


@pytest.mark.parametrize(
    "prose",
    [
        "Nothing much to report here.",
        "None of the call sites were updated.",
        "No issues were found in the parser but the tests are thin.",
        "Several things look odd.",
    ],
)
def test_other_prose_under_issues_still_refuses(prose):
    text = _claude_approve_with("Looks fine.", issues=prose)
    with pytest.raises(review_phase.ParseError, match="ISSUES"):
        review_phase.parse_claude_fresh(text)


# --- prose after a bulleted clean marker refuses ----------------------------

CLEAN_MARKER_THEN_PROSE = "- None blocking.\nA real issue is described in prose."
CLEAN_MARKER_THEN_CONTINUATION = "- None blocking.\n  Checked every call site."


def test_bulleted_clean_marker_followed_by_prose_refuses():
    text = _claude_approve_with("Checked.", issues=CLEAN_MARKER_THEN_PROSE)
    with pytest.raises(review_phase.ParseError, match="ISSUES"):
        review_phase.parse_claude_fresh(text)


def test_bulleted_clean_marker_followed_by_an_indented_continuation_approves():
    text = _claude_approve_with("Checked.", issues=CLEAN_MARKER_THEN_CONTINUATION)
    verdict, _findings = review_phase.parse_claude_fresh(text)
    assert verdict == "approved"


def test_run_phase_refuses_prose_after_a_bulleted_clean_marker():
    refused = review_phase.run_phase(
        _envelope(
            "claude-fresh",
            _claude_approve_with("Checked.", issues=CLEAN_MARKER_THEN_PROSE),
        )
    )
    assert refused["status"] == "failure"
    assert "ISSUES" in refused["reason"]
    assert refused["artifacts"]["findings.json"] is None
    continued = review_phase.run_phase(
        _envelope(
            "claude-fresh",
            _claude_approve_with("Checked.", issues=CLEAN_MARKER_THEN_CONTINUATION),
        )
    )
    assert continued["status"] == "success", continued["reason"]
    assert continued["artifacts"]["findings.json"]["verdict"] == "approved"


@pytest.mark.parametrize(
    "issues",
    [
        pytest.param("- none\nNothing to report.", id="adjacent"),
        pytest.param("- none\n\nNothing to report.", id="blank-line-between"),
        pytest.param("- none\nnothing to report", id="lowercase-no-period"),
    ],
)
def test_bulleted_clean_marker_followed_by_nothing_to_report_approves(issues):
    text = _claude_approve_with("Checked.", issues=issues)
    verdict, findings = review_phase.parse_claude_fresh(text)
    assert verdict == "approved"
    assert findings == []
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "success", result["reason"]


@pytest.mark.parametrize(
    "prose",
    [
        "A real issue is described in prose.",
        "Nothing much to report here.",
        "None of the call sites were updated.",
        "Nothing to report, but see below.",
    ],
)
def test_bulleted_clean_marker_followed_by_other_prose_refuses(prose):
    text = _claude_approve_with("Checked.", issues=f"- none\n{prose}")
    with pytest.raises(review_phase.ParseError, match="ISSUES"):
        review_phase.parse_claude_fresh(text)


def test_nothing_to_report_with_more_on_the_line_is_foreign_prose():
    text = _claude_approve_with("Checked.", issues="Nothing to report, but see below.")
    with pytest.raises(review_phase.ParseError, match="ISSUES"):
        review_phase.parse_claude_fresh(text)
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "failure"
    assert "ISSUES" in result["reason"]


def test_prose_before_a_bulleted_clean_marker_refuses():
    text = _claude_approve_with("Checked.", issues="A real issue is described in prose.\n- none")
    with pytest.raises(review_phase.ParseError, match="ISSUES"):
        review_phase.parse_claude_fresh(text)


# --- (b) prose under a findings section -------------------------------------

FINDING = "- `a.py:1` — first issue"
ISSUES_REFUSED = {
    "finding-then-paragraph": f"{FINDING}\nA second issue is described in prose.",
    "paragraph-then-finding": "A first issue is described in prose.\n- `a.py:1` — second issue",
    "finding-blank-line-then-paragraph": f"{FINDING}\n\nA second issue is described in prose.",
    "clean-marker-then-paragraph": "- none\nA real issue is described in prose.",
}
# (issues, expected finding descriptions)
ISSUES_ACCEPTED = {
    "finding-with-indented-continuation": (
        f"{FINDING}\n  continued on an indented line.",
        ["first issue continued on an indented line."],
    ),
    "finding-with-continuation-after-a-blank-line": (
        f"{FINDING}\n\n  continued after a blank line.",
        ["first issue continued after a blank line."],
    ),
    "finding-then-an-exact-clean-marker": (f"{FINDING}\nNothing to report.", ["first issue"]),
    "none-then-nothing-to-report": ("- none\nNothing to report.", []),
    "clean-marker-then-indented-continuation": (
        "- None blocking.\n  Checked every call site.",
        ["None blocking. Checked every call site."],
    ),
}


@pytest.mark.parametrize("issues", list(ISSUES_REFUSED.values()), ids=list(ISSUES_REFUSED))
def test_foreign_prose_under_issues_refuses_beside_any_bullet(issues):
    text = _claude_approve_with("Checked.", issues=issues)
    with pytest.raises(review_phase.ParseError, match="ISSUES"):
        review_phase.parse_claude_fresh(text)


@pytest.mark.parametrize(
    "issues, descriptions", list(ISSUES_ACCEPTED.values()), ids=list(ISSUES_ACCEPTED)
)
def test_bullets_continuations_and_exact_clean_markers_parse(issues, descriptions):
    text = _claude_approve_with("Checked.", issues=issues)
    verdict, findings = review_phase.parse_claude_fresh(text)
    assert verdict == "approved"
    assert [f["description"] for f in findings] == descriptions


def test_foreign_prose_beside_a_minor_note_refuses():
    text = (
        "## Verdict: APPROVE\n\n### Issues\n- none\n\n"
        "### Minor notes\n- `a.py:1` — a note\nA paragraph about something else.\n"
    )
    with pytest.raises(review_phase.ParseError, match="MINOR NOTES"):
        review_phase.parse_claude_fresh(text)


def test_foreign_prose_beside_a_grok_finding_refuses():
    text = (
        "## FINDINGS\n- `a.py:1` — an issue\nA paragraph about something else.\n\n"
        "## VERDICT\nflagged — one issue\n"
    )
    with pytest.raises(review_phase.ParseError, match="FINDINGS"):
        review_phase.parse_grok_notools(text)


# --- (c) end to end through run_phase ----------------------------------------


def _sol_probe_return(body: str, issues: str = "- none") -> str:
    """The return shape of the second-family review's probes."""
    return (
        "## Verdict: APPROVE\n\n"
        f"{body}\n\n"
        "### Issues\n"
        f"{issues}\n\n"
        "### Evidence\n"
        "- Ran: checks passed\n\n"
        "INTERVAL: none\n"
    )


SOL_ISSUES_PROBES_REFUSED = {
    "clean-then-foreign": "- none\nA real issue is described in prose.",
    "real-then-foreign": "- `a.py:1` — first issue\nA second issue is described in prose.",
    "foreign-then-real": "A first issue is described in prose.\n- `a.py:1` — second issue",
}
SOL_ISSUES_PROBES_ACCEPTED = {
    "clean-then-indented": "- None blocking.\n  Checked every call site.",
}


@pytest.mark.parametrize(
    "issues",
    list(SOL_ISSUES_PROBES_REFUSED.values()),
    ids=list(SOL_ISSUES_PROBES_REFUSED),
)
def test_run_phase_refuses_the_second_family_prose_probes(issues):
    result = review_phase.run_phase(
        _envelope("claude-fresh", _sol_probe_return("Checked.", issues=issues))
    )
    assert result["status"] == "failure"
    assert "ISSUES" in result["reason"]
    assert result["artifacts"]["findings.json"] is None


@pytest.mark.parametrize(
    "issues",
    list(SOL_ISSUES_PROBES_ACCEPTED.values()),
    ids=list(SOL_ISSUES_PROBES_ACCEPTED),
)
def test_run_phase_accepts_the_second_family_continuation_probe(issues):
    result = review_phase.run_phase(
        _envelope("claude-fresh", _sol_probe_return("Checked.", issues=issues))
    )
    assert result["status"] == "success", result["reason"]
    assert result["artifacts"]["findings.json"]["verdict"] == "approved"


@pytest.mark.parametrize("issues", list(ISSUES_REFUSED.values()), ids=list(ISSUES_REFUSED))
def test_run_phase_refuses_foreign_prose_under_issues(issues):
    result = review_phase.run_phase(
        _envelope("claude-fresh", _claude_approve_with("Checked.", issues=issues))
    )
    assert result["status"] == "failure"
    assert "ISSUES" in result["reason"]
    assert result["artifacts"]["findings.json"] is None


@pytest.mark.parametrize(
    "issues, descriptions", list(ISSUES_ACCEPTED.values()), ids=list(ISSUES_ACCEPTED)
)
def test_run_phase_accepts_bullets_continuations_and_exact_clean_markers(issues, descriptions):
    result = review_phase.run_phase(
        _envelope("claude-fresh", _claude_approve_with("Checked.", issues=issues))
    )
    assert result["status"] == "success", result["reason"]
    findings = result["artifacts"]["findings.json"]["findings"]
    assert [f["description"] for f in findings] == descriptions


# --- the growth tests share one timing helper --------------------------------
#
# The growth tests below time spans of a few milliseconds or less, where runner
# noise alone can triple a timing. A pattern that was once quadratic or cubic
# cannot be fed an input long enough to take 5 ms in one call (the old heading
# form took seconds on 2,000 spaces), so the tests keep their inputs short and
# repeat the call until one timed span lasts GROWTH_WINDOW_SECONDS. Each size is
# timed several times and the fastest per-call time is kept, since noise only
# ever adds time. The longer input is four times the shorter, so a linear scan
# grows about four times and a quadratic one about sixteen; the bound of eight
# sits between them.
GROWTH_REPETITIONS = 9
GROWTH_RATIO_BOUND = 8
GROWTH_WINDOW_SECONDS = 0.005


def _span_seconds(scan, text: str, calls: int, clock=time.perf_counter) -> float:
    start = clock()
    for _ in range(calls):
        scan(text)
    return clock() - start


def _fastest_call_seconds(scan, text: str, clock=time.perf_counter) -> float:
    """The fastest per-call time over several timed spans.

    A span repeats the call until it lasts GROWTH_WINDOW_SECONDS (a single call
    when one already does). The spans that find that call count are discarded,
    since they are shorter than the window and so the noisiest samples. A first
    call over HEADING_LIMIT_SECONDS ends it, so a pattern that is far too slow
    is not timed again. `clock` is a seam for the helper's own test.
    """
    calls = 1
    span = _span_seconds(scan, text, calls, clock)
    while span < GROWTH_WINDOW_SECONDS:
        calls *= 2
        span = _span_seconds(scan, text, calls, clock)
    if calls == 1 and span > HEADING_LIMIT_SECONDS:
        return span
    best = span / calls
    for _ in range(GROWTH_REPETITIONS - 1):
        best = min(best, _span_seconds(scan, text, calls, clock) / calls)
    return best


class _VirtualClock:
    """A clock that moves only when a scan spends time on it."""

    def __init__(self, costs):
        self.now = 0.0
        self.costs = costs
        self.calls = 0

    def __call__(self) -> float:
        return self.now

    def scan(self, text: str) -> None:
        self.now += self.costs(self.calls)
        self.calls += 1


def test_fastest_call_ignores_the_spans_shorter_than_the_window():
    # The first four calls are cheap (a warm-up or a coarse timer reading), so
    # every calibration span reads as fast. Only spans that reach the window
    # count, and those measure the steady 1 ms per call.
    clock = _VirtualClock(lambda index: 0.0002 if index < 4 else 0.001)
    fastest = _fastest_call_seconds(clock.scan, "", clock)
    assert fastest >= 0.0007, (
        f"the fastest per-call time was {fastest * 1e3:.3f}ms, "
        "a span shorter than the window was counted"
    )


def test_fastest_call_returns_a_first_call_over_the_limit_without_timing_again():
    clock = _VirtualClock(lambda index: 2 * HEADING_LIMIT_SECONDS)
    assert _fastest_call_seconds(clock.scan, "", clock) == 2 * HEADING_LIMIT_SECONDS
    assert clock.calls == 1


# --- the heading pattern stays linear and keeps its grammar ------------------
#
# `_HEADING` runs on every line of every review. One line of `# a`, 3,000
# spaces and `b` used to take over ten seconds. The forms below are the
# headings the adapter tests already exercise, plus the edge forms, each with
# the title the earlier pattern gave.

HEADING_LIMIT_SECONDS = 1.0
THREE_THOUSAND_SPACE_LINE = "# a" + " " * 3000 + "b"


def _timed_heading_match(line: str):
    start = time.perf_counter()
    matched = review_phase._HEADING.match(line)
    return matched, time.perf_counter() - start


def test_heading_match_finishes_the_3000_space_line_within_a_second():
    matched, elapsed = _timed_heading_match(THREE_THOUSAND_SPACE_LINE)
    assert elapsed <= HEADING_LIMIT_SECONDS, (
        f"_HEADING took {elapsed:.2f}s on a line of `# a`, 3,000 spaces and `b`"
    )
    assert matched is not None
    assert review_phase._heading_title(matched) == "a" + " " * 3000 + "b"


def test_claude_adapter_parses_a_review_holding_the_3000_space_line_within_a_second():
    text = "\n".join(
        ["## Verdict: APPROVE", "", "### Issues", "- none", "", "### Evidence",
         THREE_THOUSAND_SPACE_LINE, ""]
    )
    start = time.perf_counter()
    parsed = review_phase.parse_claude_fresh(text)
    elapsed = time.perf_counter() - start
    assert parsed == ("approved", [])
    assert elapsed <= HEADING_LIMIT_SECONDS, f"parse_claude_fresh took {elapsed:.2f}s"


HEADING_GROWTH_SHAPES = {
    "hash-a-spaces-b": lambda n: "# a" + " " * n + "b",
    "hash-a-spaces-hash-b": lambda n: "# a" + " " * n + "#" + " " * n + "b",
    "hash-spaces-then-hashes": lambda n: "#" + " " * n + "###" + " " * n + "x",
    "alternating-space-hash": lambda n: "# a" + " #" * (n // 2) + "b",
    "b-spaces-hash-spaces-hash": lambda n: "# b" + " " * n + "#" + " " * n + "#",
    "seven-hashes-spaces-b": lambda n: "#######" + " " * n + "b",
    "only-spaces-after-hash": lambda n: "#" + " " * n,
    "hash-a-spaces-newline-spaces-x": lambda n: "# a" + " " * n + "\n" + " " * n + "x",
    "label-colon-spaces-newline-spaces-x": (
        lambda n: "FINDINGS:" + " " * n + "\n" + " " * n + "x"
    ),
}


# The earlier heading pattern took seconds on 2,000 spaces, so these lengths
# stay small enough for that form to finish and fail the growth bound.
HEADING_SHORT_LENGTH = 500
HEADING_LONG_LENGTH = 4 * HEADING_SHORT_LENGTH


@pytest.mark.parametrize("shape", list(HEADING_GROWTH_SHAPES))
def test_heading_match_time_grows_linearly_with_line_length(shape):
    match = review_phase._HEADING.match
    shorter = _fastest_call_seconds(match, HEADING_GROWTH_SHAPES[shape](HEADING_SHORT_LENGTH))
    assert shorter <= HEADING_LIMIT_SECONDS, (
        f"_HEADING took {shorter:.2f}s on the {HEADING_SHORT_LENGTH}-space {shape} line"
    )
    longer = _fastest_call_seconds(match, HEADING_GROWTH_SHAPES[shape](HEADING_LONG_LENGTH))
    assert longer <= GROWTH_RATIO_BOUND * shorter, (
        f"_HEADING on a {shape} line: quadrupling {HEADING_SHORT_LENGTH} to "
        f"{HEADING_LONG_LENGTH} spaces took {shorter * 1e3:.4f}ms to "
        f"{longer * 1e3:.4f}ms, more than {GROWTH_RATIO_BOUND} times"
    )


HEADING_FORMS = [
    ('## FINDINGS', 'FINDINGS'),
    ('## VERDICT', 'VERDICT'),
    ('FINDINGS:', 'FINDINGS'),
    ('VERDICT: flagged — the retry logic needs the backoff reset before merge.', 'VERDICT'),
    ('## Verdict: REQUEST CHANGES', 'Verdict: REQUEST CHANGES'),
    ('## Verdict: APPROVE', 'Verdict: APPROVE'),
    ('### Issues', 'Issues'),
    ('### Minor notes', 'Minor notes'),
    ('### Evidence', 'Evidence'),
    ('VERDICT: APPROVE', 'VERDICT'),
    ('VERDICT: APPROVE_WITH_FINDINGS', 'VERDICT'),
    ('ANALYSIS:', 'ANALYSIS'),
    ('#', None),
    ('  #', None),
    ('##', '#'),
    ('#######', '#'),
    ('# ', ' '),
    ('#   ', ' '),
    ('##  ', ' '),
    ('\t# x', 'x'),
    ('# Title', 'Title'),
    ('#Title', 'Title'),
    ('## Title ##', 'Title'),
    ('## Title##', 'Title'),
    ('## a # b', 'a # b'),
    ('## a #', 'a'),
    ('## a# ##', 'a#'),
    ('# # #', '#'),
    ('###### x', 'x'),
    ('####### x', '# x'),
    ('# a\t#\t', 'a'),
    ('**ISSUES**', 'ISSUES'),
    ('**ISSUES**:', 'ISSUES'),
    ('ISSUES: none', 'ISSUES'),
    ('  minor notes  ', 'minor notes'),
    ('Findings', 'Findings'),
    ('#\n', None),
    ('#\r\n', '\r'),
    ('##\n', '#'),
    ('FINDINGS: x\n  ', 'FINDINGS'),
    ('FINDINGS: x\n  y', None),
    ('FINDINGS x', None),
    ('Issue: x', None),
    ('- ## x', None),
    ('', None),
    (' ', None),
    ('x', None),
]


@pytest.mark.parametrize(
    ("line", "title"), HEADING_FORMS, ids=[repr(line) for line, _ in HEADING_FORMS]
)
def test_heading_forms_keep_main_titles(line, title):
    matched = review_phase._HEADING.match(line)
    got = None if matched is None else review_phase._heading_title(matched)
    assert got == title


# --- the verdict lines stay linear on a long whitespace run ------------------
#
# The verdict label reader runs on every line of a review. A colon, a long
# space run, an optional `[` or `*` mark and a second space run once let two
# adjacent whitespace sub-patterns split the first run every way, which is
# quadratic; the reader must stay linear on each of those shapes.


VERDICT_SHORT_LENGTH = 3000
VERDICT_LONG_LENGTH = 4 * VERDICT_SHORT_LENGTH


VERDICT_GROWTH_CASES = [
    ("heading", "colon-spaces-x", lambda n: "## Verdict:" + " " * n + "x"),
    (
        "heading",
        "colon-spaces-bracket-spaces-x",
        lambda n: "## Verdict:" + " " * n + "[" + " " * n + "x",
    ),
    ("label", "colon-spaces-x", lambda n: "VERDICT:" + " " * n + "x"),
    (
        "label",
        "colon-spaces-stars-spaces-x",
        lambda n: "VERDICT:" + " " * n + "**" + " " * n + "x",
    ),
]


@pytest.mark.parametrize(
    ("name", "shape", "build"),
    VERDICT_GROWTH_CASES,
    ids=[f"{name}-{shape}" for name, shape, _ in VERDICT_GROWTH_CASES],
)
def test_verdict_match_time_grows_linearly_with_line_length(name, shape, build):
    def match(line):
        return review_phase._verdict_labels([line])

    shorter = _fastest_call_seconds(match, build(VERDICT_SHORT_LENGTH))
    assert shorter <= HEADING_LIMIT_SECONDS, (
        f"{name} took {shorter:.2f}s on the {VERDICT_SHORT_LENGTH}-space {shape} line"
    )
    longer = _fastest_call_seconds(match, build(VERDICT_LONG_LENGTH))
    assert longer <= GROWTH_RATIO_BOUND * shorter, (
        f"{name} on a {shape} line: quadrupling {VERDICT_SHORT_LENGTH} to "
        f"{VERDICT_LONG_LENGTH} spaces took {shorter * 1e3:.4f}ms to "
        f"{longer * 1e3:.4f}ms, more than {GROWTH_RATIO_BOUND} times"
    )


# --- INTERVAL line and body patterns stay linear and keep their grammar ------
#
# `_INTERVAL_LINE` runs on the trailing lines of every successfully parsed
# review. `_INTERVAL_PARTS` runs on each measured body, always one line. A
# body of `from a,` plus n spaces plus `b` used to backtrack about cubically;
# a line of `INTERVAL: a`, n spaces and `b` was quadratic. The cases below are
# every fixture INTERVAL line plus the edge forms, each checked against the
# backtracking patterns that define the grammar. The body grammar is the lazy
# one throughout except that the end source runs to the last `=`, so an end
# source may itself hold `=`.

OLD_INTERVAL_LINE = re.compile(
    r"^[ \t]*INTERVAL[ \t]*:[ \t]*(.*?)[ \t]*$",
    re.IGNORECASE,
)
LAST_EQUALS_INTERVAL_PARTS = re.compile(
    r"^from\s+(?P<start_event>.*?),\s*"
    r"(?P<start_source>.*?)\s*to\s+"
    r"(?P<end_event>.*?),\s*"
    r"(?P<end_source>.*?)\s*=(?!.*=)\s*"
    r"(?P<duration>.*)$",
    re.IGNORECASE,
)

INTERVAL_PARTS_LIMIT_SECONDS = 1.0
INTERVAL_LINE_LIMIT_SECONDS = 0.05
INTERVAL_INTERNAL_WS_LIMIT_SECONDS = 0.05
INTERVAL_NEWLINE_FREE_OUTER_LIMIT_SECONDS = 0.001
INTERVAL_NEWLINE_FREE_TO_LIMIT_SECONDS = 0.01
INTERVAL_NEWLINE_FREE_COMMA_LIMIT_SECONDS = 0.01
REPEATED_TO_OUTER_PARTS = "from a, " + "to x, " * 4000 + "y"
REPEATED_TO_LEVEL_PARTS = "from a, src " + "to x " * 40000 + "y"
REPEATED_COMMA_LEVEL_PARTS = "from a, src to end, " + "x, " * 40000 + "y"
TEN_THOUSAND_SPACE_PARTS = "from a," + " " * 10_000 + "b"
TEN_THOUSAND_SPACE_LINE = "INTERVAL: a" + " " * 10_000 + "b"
TEN_THOUSAND_INTERNAL_WS_PARTS = (
    "from a, foo" + " " * 10_000 + "x to end, bar" + " " * 10_000 + "y = 1s"
)
_INTERVAL_FUZZ_ALPHABET = ("a", ",", " ", "t", "o", "=", "F")
_INTERVAL_S_TRAILING = re.compile(r"\s+$")


def _fixture_interval_lines() -> list[str]:
    found = []
    for path in sorted(FIXTURES.glob("*.md")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "INTERVAL" in line.upper():
                found.append(line)
    return found


def _interval_line_cases() -> list[str]:
    return _fixture_interval_lines() + [
        "INTERVAL:",
        "INTERVAL:   ",
        "  INTERVAL: x",
        "\tINTERVAL\t:\tfoo\t",
        "interval: from a, b to c, d = 1s",
        "Interval : from a, b to c, d = 1s",
        "INTERVAL: a" + (" " * 20) + "b",
        "INTERVAL: a" + (" " * 20) + "b   ",
        "INTERVAL:" + (" " * 20),
        "INTERVAL:" + ("\t" * 5) + "x" + ("\t" * 3),
        "INTERVAL:from a, b to c, d = 1s",
        "INTERVAL  :  body  ",
        " INTERVAL : body",
        "XINTERVAL: x",
        "INTERVALS: x",
        "INTERVAL x",
        "INTERVAL",
        "# INTERVAL: x",
        "INTERVAL :",
        "interval:",
        "InTeRvAl: z",
        "INTERVAL:\ta\tb\t",
        "INTERVAL: three\n",
        "INTERVAL:\n",
        "INTERVAL: x \n",
        "",
        "   ",
        TEN_THOUSAND_SPACE_LINE,
    ]


def _interval_parts_cases() -> list[str]:
    bodies = []
    for line in _fixture_interval_lines():
        matched = OLD_INTERVAL_LINE.match(line)
        if matched is not None:
            bodies.append(matched.group(1))
            bodies.append(matched.group(1).strip())
    return bodies + [
        "from a, b to c, d = e",
        "from request start, events.tsv:14 to result write, result.md mtime = 41m",
        "FROM A, B TO C, D = E",
        "from  a,  b  to  c,  d  =  e",
        "from\ta,\tb\tto\tc,\td\t=\te",
        "from a, b to c, d =",
        "from , b to c, d = e",
        "from a,  to c, d = e",
        "from a, b to , d = e",
        "from a, b to c,  = e",
        "from a, b to c, d = e  ",
        "from a, b to c, d =  e",
        "from a,b to c,d =e",
        "from a, xto y to c, d = e",
        "from a, total time to c, d = e",
        "from a, b to c, d = e, extra",
        "from a, b, extra to c, d = e",
        "from , , to x, y = 1",
        "from a," + (" " * 50) + "b",
        "from a, b to c, d=e",
        "from a, b to c,d= e",
        "not from a, b to c, d = e",
        "fromage a, b to c, d = e",
        "from a, b toward c, d = e",
        "from a, b to c, d == e",
        "from a b to c, d = e",
        "from a, b to c d = e",
        "from a, b to c, d",
        "froma, b to c, d = e",
        " from a, b to c, d = e",
        "from a, photo to album, x = 1s",
        "from a , b to c, d = e",
        "from a, b to c , d = e",
        "from a,to b, c = d",
        "from a, to b, c = d",
        "from to x, y to z, w = 1",
        "from a, x to y to z, w = 1",
        "from a  a, b  b to c  c, d  d = e  e",
        "from a, b to c, d = e f g",
        "",
        "from",
        "from ",
        "from event,ignored, source to end, result = 1s",
        "from a, b to foo to end, x = 1s",
        "from a, b to c, d = e = 1s",
        "from a, b to c, worker log status=done = 41m",
        "from a, k=v to c, d = 1s",
        "from a=b, c to d, e = 1s",
        "from a, b to c, d = = 1s",
        "from a, b to c, d ==",
        "from a, b to c, =",
        "from a, b to c, == 1s",
    ]


def _line_groups(matched):
    if matched is None:
        return None
    return matched.groups()


def _parts_groups(matched):
    if matched is None:
        return None
    return tuple(matched.group(name) for name in review_phase._INTERVAL_FIELDS)


def test_interval_patterns_accept_the_line_grammar_and_split_on_the_last_equals():
    mismatches = []
    for text in _interval_line_cases():
        old = _line_groups(OLD_INTERVAL_LINE.match(text))
        new = _line_groups(review_phase._INTERVAL_LINE.match(text))
        if old != new:
            mismatches.append(("line", text, old, new))
    for text in _interval_parts_cases():
        old = _parts_groups(LAST_EQUALS_INTERVAL_PARTS.match(text))
        new = _parts_groups(review_phase._INTERVAL_PARTS.match(text))
        if old != new:
            mismatches.append(("parts", text, old, new))
    assert mismatches == [], mismatches


def test_interval_parts_match_finishes_10000_spaces_well_under_a_second():
    code = (
        "import time, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import review_phase\n"
        "body = sys.stdin.buffer.read().decode('utf-8')\n"
        "start = time.perf_counter()\n"
        "matched = review_phase._INTERVAL_PARTS.match(body)\n"
        "elapsed = time.perf_counter() - start\n"
        "print('matched' if matched else 'none')\n"
        "print(elapsed)\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code, str(SKILL_DIR)],
            input=TEN_THOUSAND_SPACE_PARTS,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=INTERVAL_PARTS_LIMIT_SECONDS,
            check=True,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            f"_INTERVAL_PARTS still running after {INTERVAL_PARTS_LIMIT_SECONDS:.0f}s "
            "on a 10,000-space body"
        )
    status, elapsed_s = proc.stdout.strip().splitlines()
    assert status == "none"
    elapsed = float(elapsed_s)
    assert elapsed <= INTERVAL_PARTS_LIMIT_SECONDS, (
        f"_INTERVAL_PARTS took {elapsed:.2f}s on a 10,000-space body"
    )


def test_interval_line_match_finishes_10000_spaces_well_under_a_second():
    start = time.perf_counter()
    matched = review_phase._INTERVAL_LINE.match(TEN_THOUSAND_SPACE_LINE)
    elapsed = time.perf_counter() - start
    assert matched is not None
    assert matched.group(1) == "a" + " " * 10_000 + "b"
    assert elapsed <= INTERVAL_LINE_LIMIT_SECONDS, (
        f"_INTERVAL_LINE took {elapsed:.2f}s on a 10,000-space line"
    )


def _interval_parts_fuzz_bodies() -> list[str]:
    bodies = []
    for n in range(0, 5):
        for prod in itertools.product(_INTERVAL_FUZZ_ALPHABET, repeat=n):
            tail = "".join(prod)
            bodies.append(tail)
            bodies.append("from " + tail)
    rng = random.Random(35)
    for _ in range(400):
        n = rng.randint(5, 12)
        tail = "".join(rng.choice(_INTERVAL_FUZZ_ALPHABET) for _ in range(n))
        bodies.append(tail)
        bodies.append("from " + tail)
    return bodies


def test_interval_parts_match_the_last_equals_grammar_on_fuzz():
    mismatches = []
    for text in _interval_parts_fuzz_bodies():
        old = _parts_groups(LAST_EQUALS_INTERVAL_PARTS.match(text))
        new = _parts_groups(review_phase._INTERVAL_PARTS.match(text))
        if old != new:
            mismatches.append((text, old, new))
    assert mismatches == [], mismatches[:5]


def _s_whitespace_code_points() -> list[str]:
    return [chr(cp) for cp in range(0x110000) if _INTERVAL_S_TRAILING.match(chr(cp))]


def test_interval_parts_rstrip_strips_every_s_whitespace_code_point():
    chars = _s_whitespace_code_points()
    named = {"\u0085", "\u00a0", "\u2028", "\u3000"}
    assert named <= set(chars)
    for ch in chars:
        assert ("keep" + ch).rstrip() == "keep"
        body = "from a, src" + ch + " to end, dst" + ch + " = 1s"
        old = _parts_groups(LAST_EQUALS_INTERVAL_PARTS.match(body))
        new = _parts_groups(review_phase._INTERVAL_PARTS.match(body))
        assert new == old
        assert new == ("a", "src", "end", "dst", "1s")


def test_interval_parts_match_finishes_10000_internal_spaces_well_under_a_second():
    code = (
        "import time, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import review_phase\n"
        "body = sys.stdin.buffer.read().decode('utf-8')\n"
        "start = time.perf_counter()\n"
        "matched = review_phase._INTERVAL_PARTS.match(body)\n"
        "elapsed = time.perf_counter() - start\n"
        "print('matched' if matched else 'none')\n"
        "print(elapsed)\n"
        "if matched:\n"
        "    print(matched.group('start_source'))\n"
        "    print(matched.group('end_source'))\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code, str(SKILL_DIR)],
            input=TEN_THOUSAND_INTERNAL_WS_PARTS,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=INTERVAL_PARTS_LIMIT_SECONDS,
            check=True,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            f"_INTERVAL_PARTS still running after {INTERVAL_PARTS_LIMIT_SECONDS:.0f}s "
            "on a 10,000-internal-space body"
        )
    lines = proc.stdout.strip().splitlines()
    status, elapsed_s = lines[0], lines[1]
    assert status == "matched"
    elapsed = float(elapsed_s)
    assert elapsed <= INTERVAL_INTERNAL_WS_LIMIT_SECONDS, (
        f"_INTERVAL_PARTS took {elapsed:.2f}s on a 10,000-internal-space body"
    )
    assert lines[2] == "foo" + " " * 10_000 + "x"
    assert lines[3] == "bar" + " " * 10_000 + "y"


def _interval_parts_timed_match(body: str) -> tuple[str, float]:
    code = (
        "import time, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import review_phase\n"
        "body = sys.stdin.buffer.read().decode('utf-8')\n"
        "start = time.perf_counter()\n"
        "matched = review_phase._INTERVAL_PARTS.match(body)\n"
        "elapsed = time.perf_counter() - start\n"
        "print('matched' if matched else 'none')\n"
        "print(elapsed)\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code, str(SKILL_DIR)],
            input=body,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=INTERVAL_PARTS_LIMIT_SECONDS,
            check=True,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            f"_INTERVAL_PARTS still running after "
            f"{INTERVAL_PARTS_LIMIT_SECONDS:.0f}s on a newline-free interval body"
        )
    status, elapsed_s = proc.stdout.strip().splitlines()
    return status, float(elapsed_s)


def test_interval_parts_match_finishes_repeated_to_x_well_under_bound():
    small = "from a, " + "to x, " * 3 + "y"
    assert _parts_groups(review_phase._INTERVAL_PARTS.match(small)) == (
        _parts_groups(LAST_EQUALS_INTERVAL_PARTS.match(small))
    )
    assert "\n" not in REPEATED_TO_OUTER_PARTS
    status, elapsed = _interval_parts_timed_match(REPEATED_TO_OUTER_PARTS)
    assert status == "none"
    assert elapsed <= INTERVAL_NEWLINE_FREE_OUTER_LIMIT_SECONDS, (
        f"_INTERVAL_PARTS took {elapsed:.4f}s on a repeated-to-x body"
    )


def test_interval_parts_match_finishes_repeated_to_without_comma_well_under_bound():
    assert "\n" not in REPEATED_TO_LEVEL_PARTS
    status, elapsed = _interval_parts_timed_match(REPEATED_TO_LEVEL_PARTS)
    assert status == "none"
    assert elapsed <= INTERVAL_NEWLINE_FREE_TO_LIMIT_SECONDS, (
        f"_INTERVAL_PARTS took {elapsed:.4f}s on a repeated-to body"
    )


def test_interval_parts_match_finishes_repeated_end_event_comma_well_under_bound():
    assert "\n" not in REPEATED_COMMA_LEVEL_PARTS
    status, elapsed = _interval_parts_timed_match(REPEATED_COMMA_LEVEL_PARTS)
    assert status == "none"
    assert elapsed <= INTERVAL_NEWLINE_FREE_COMMA_LIMIT_SECONDS, (
        f"_INTERVAL_PARTS took {elapsed:.4f}s on a repeated end-event-comma body"
    )

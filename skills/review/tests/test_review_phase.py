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
    # The documented shape carries no timing claim, so it never needs a
    # caller's timing record to pass the phase.
    result = review_phase.run_phase(_envelope("claude-fresh", block.group(1)))
    assert result["status"] == "success"


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
        "This paragraph is not a bullet or a numbered item at all.\n"
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
    assert set(result) == {"phase_result", "status", "artifacts", "reason", "resume_token"}
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


# --- measured-interval records ----------------------------------------------

DURATION_WITHOUT_INTERVAL = (FIXTURES / "duration-without-interval.md").read_text()
DURATION_WITH_INTERVAL = (FIXTURES / "duration-with-interval.md").read_text()


def _timed_grok(interval_line: str | None) -> str:
    lines = [
        "## FINDINGS",
        "- none",
        "",
        "## VERDICT",
        "cleared — the caller waited for 41 minutes.",
        "",
    ]
    if interval_line is not None:
        lines.append(interval_line)
        lines.append("")
    return "\n".join(lines)


def test_clean_review_with_no_timing_claim_is_byte_for_byte_unchanged():
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
    assert result == expected
    assert "intervals" not in result

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
    assert skill == skill_expected
    assert "intervals" not in skill


def test_code_timeout_mentions_are_not_duration_claims():
    result = review_phase.run_phase(_envelope("grok-notools", GROK_FLAGGED))
    assert result["status"] == "success"
    assert "intervals" not in result
    assert result["artifacts"]["review.md"] == GROK_FLAGGED


def test_from_text_parses_a_120_second_timeout_finding(tmp_path: Path):
    text = _claude_approve_with(
        "The change sizes the weekly chain timeout.",
        issues=(
            "- `skills/sample-monitor/sample-monitor-run.sh:212` — "
            "the 120 second timeout never fires."
        ),
    )
    review = tmp_path / "return.md"
    review.write_text(text, encoding="utf-8")
    proc = _run_cli("--from-text", str(review), "--backend", "claude-fresh")
    result = json.loads(proc.stdout)
    assert result["status"] == "success", result["reason"]
    assert proc.returncode == review_phase.EXIT_SUCCESS, proc.stderr
    assert "intervals" not in result
    assert result["artifacts"]["findings.json"] is not None


@pytest.mark.parametrize(
    "issues",
    [
        pytest.param("- the 120 second timeout never fires.", id="spaced-timeout"),
        pytest.param("- the 120 second bound never fires.", id="spaced-bound"),
        pytest.param("- the 120 second limit never fires.", id="spaced-limit"),
        pytest.param("- the 120 second cap never fires.", id="spaced-cap"),
        pytest.param("- the 120 second deadline never fires.", id="spaced-deadline"),
        pytest.param("- the 120-second timeout never fires.", id="hyphenated-timeout"),
        pytest.param("- the 120-second bound never fires.", id="hyphenated-bound"),
        pytest.param("- the 120-second limit never fires.", id="hyphenated-limit"),
        pytest.param("- the 120-second cap never fires.", id="hyphenated-cap"),
        pytest.param("- the 120-second deadline never fires.", id="hyphenated-deadline"),
        pytest.param("- the `120 second timeout` never fires.", id="backticked-timeout"),
        pytest.param("- the `120 second bound` never fires.", id="backticked-bound"),
        pytest.param("- the `120 second limit` never fires.", id="backticked-limit"),
        pytest.param("- the `120 second cap` never fires.", id="backticked-cap"),
        pytest.param("- the `120 second deadline` never fires.", id="backticked-deadline"),
        pytest.param("- the 120 SECOND TIMEOUT never fires.", id="spaced-timeout-caps"),
        pytest.param("- the `120 second` timeout never fires.", id="backticked-unit-then-timeout"),
    ],
)
def test_configured_bound_is_not_a_duration_claim(issues):
    text = _claude_approve_with(
        "The change sizes the weekly chain timeout.",
        issues=issues,
    )
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "success", result["reason"]
    assert "intervals" not in result


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("The job ran for 120 seconds.", id="ran-for-120-seconds"),
        pytest.param("The caller took 41 seconds.", id="took-41-seconds"),
        pytest.param(
            "The caller took 120 seconds before the timeout.",
            id="took-120-seconds-before-the-timeout",
        ),
    ],
)
def test_measured_duration_still_requires_interval_endpoints(body):
    text = _claude_approve_with(body)
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]


def test_bound_noun_on_next_line_does_not_exempt_a_duration_claim():
    text = _claude_approve_with("The caller took 120 seconds\nlimit hit")
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("The caller took 120 seconds limit", id="space-before-limit"),
        pytest.param("The caller took 120 seconds\tlimit", id="tab-before-limit"),
    ],
)
def test_same_line_bound_noun_still_exempts(body):
    text = _claude_approve_with(body)
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "success", result["reason"]
    assert "intervals" not in result


def test_duration_without_interval_is_refused():
    result = review_phase.run_phase(
        _envelope("grok-notools", DURATION_WITHOUT_INTERVAL)
    )
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]
    assert "line 5:" in result["reason"]
    assert "the caller waited for 41 minutes." in result["reason"]
    assert result["artifacts"]["review.md"] == DURATION_WITHOUT_INTERVAL
    assert result["artifacts"]["findings.json"] is None
    assert result["resume_token"] is None


def test_duration_with_complete_interval_is_parsed_onto_the_phase_result():
    result = review_phase.run_phase(
        _envelope("grok-notools", DURATION_WITH_INTERVAL)
    )
    assert result["status"] == "success", result["reason"]
    assert result["intervals"] == [
        {
            "start_event": "request start",
            "start_source": "events.tsv:14",
            "end_event": "result write",
            "end_source": "result.md mtime",
            "duration": "41m",
        }
    ]
    findings = result["artifacts"]["findings.json"]
    assert findings["verdict"] == "approved"
    assert findings["findings"] == []
    assert "intervals" not in findings
    assert review_phase.validate_findings(findings) == []


def test_interval_missing_start_event_is_refused():
    text = _timed_grok(
        "INTERVAL: from , events.tsv:14 to result write, "
        "result.md mtime = 41m"
    )
    result = review_phase.run_phase(_envelope("grok-notools", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]
    assert "line 7:" in result["reason"]
    assert result["artifacts"]["findings.json"] is None


def test_interval_missing_start_source_is_refused():
    text = _timed_grok(
        "INTERVAL: from request start, to result write, "
        "result.md mtime = 41m"
    )
    result = review_phase.run_phase(_envelope("grok-notools", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]
    assert "line 7:" in result["reason"]
    assert result["artifacts"]["findings.json"] is None


def test_interval_missing_end_event_is_refused():
    text = _timed_grok(
        "INTERVAL: from request start, events.tsv:14 to , "
        "result.md mtime = 41m"
    )
    result = review_phase.run_phase(_envelope("grok-notools", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]
    assert "line 7:" in result["reason"]
    assert result["artifacts"]["findings.json"] is None


def test_interval_missing_end_source_is_refused():
    text = _timed_grok(
        "INTERVAL: from request start, events.tsv:14 to result write,  = 41m"
    )
    result = review_phase.run_phase(_envelope("grok-notools", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]
    assert "line 7:" in result["reason"]
    assert result["artifacts"]["findings.json"] is None


def test_interval_missing_duration_is_refused():
    text = _timed_grok(
        "INTERVAL: from request start, events.tsv:14 to result write, "
        "result.md mtime = "
    )
    result = review_phase.run_phase(_envelope("grok-notools", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]
    assert "line 7:" in result["reason"]
    assert result["artifacts"]["findings.json"] is None


# --- section/file references are not duration claims; prose clean markers ----


def _claude_approve_with(body: str, issues: str = "- none") -> str:
    return (
        "## Verdict: APPROVE\n\n"
        f"### Summary\n{body}\n\n"
        f"### Issues\n{issues}\n"
    )


@pytest.mark.parametrize(
    "body",
    [
        "Checked the rule in §3d against the diff.",
        "Checked § 3d against the diff.",
        "See section 3d for the rule.",
        "See Section 3d for the rule.",
        "See SECTION 12h for the rule.",
        "The call site at foo.py:12d is unchanged.",
        "The call site at src/pkg/foo.py:7m is unchanged.",
        "See [the rule](docs/3d/run.md) for the details.",
        "Only touches docs/3d/notes.md and nothing else.",
        "Only touches docs/3d/x.md and nothing else.",
        "Only touches src/days/run.py and nothing else.",
        "Only touches `docs/2h/notes.md` and nothing else.",
        "See section 3.2 for the rule.",
        "See § 3.2 for the rule.",
    ],
)
def test_section_and_file_references_are_not_duration_claims(body):
    text = _claude_approve_with(body)
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "success", result["reason"]
    assert result["artifacts"]["findings.json"]["verdict"] == "approved"
    assert "intervals" not in result


@pytest.mark.parametrize(
    "body",
    [
        "The caller waits 3d for the lock.",
        "The job ran 3 days before failing.",
        "It returned in 2h.",
        "Throughput is 3d/day at peak.",
        "It took 3 days/run.",
        "It took 3 hours/day/week.",
        'The caller waits "3d/foo.py" before retrying.',
        "The caller waits `3d/foo.py` before retrying.",
        "The caller waits _3d/foo.py_ before retrying.",
        "The caller waits *3d/foo.py* before retrying.",
        "The caller waits __3d/foo.py__ before retrying.",
        "The caller waits (3 days/foo.txt) before retrying.",
        "The caller waits '2h/x.md' before retrying.",
        "See [took 3 days](docs/run.md) for the details.",
        "See [3h](https://example.com/x/y) for the details.",
        "The caller waits 3d/foo.py before retrying.",
        "It took 3 days/foo.txt to finish.",
        "It ran 3 days/run/x.",
        "See docs/3d/notes for the rule.",
        "Compare a/3d/b with the other copy.",
        "It took section 5 minutes.",
        "The caller waited in section 3 hours.",
        "It took § 5 minutes.",
        "Each used 4 hours/session.",
        "See §3d, but the caller also waits 3d.",
    ],
)
def test_real_duration_claims_still_require_an_interval(body):
    text = _claude_approve_with(body)
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]


# --- file:line references and delimiter-glued paths never hide a claim --------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("it waited 3d/foo.py:12 before failing", id="plain"),
        pytest.param("it waited _3d/foo.py:12_ before failing", id="underscore-wrapped"),
        pytest.param("it waited `3d/foo.py:12` before failing", id="backtick-wrapped"),
    ],
)
def test_file_line_reference_with_a_duration_first_component_is_a_claim(text):
    assert review_phase._has_duration_claim(text) is True, (
        "a file:line reference whose first path component is a duration must "
        "stay visible to the scan"
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("see foo.py:12 for the bound", id="plain-reference"),
        pytest.param("see docs/3d/x.md:12 for the bound", id="later-component-duration"),
        pytest.param("see src/days/run.py:7 for the bound", id="later-component-unit-word"),
        pytest.param("The call site at foo.py:12d is unchanged.", id="line-suffix"),
    ],
)
def test_file_line_reference_without_a_duration_first_component_is_not_a_claim(text):
    assert review_phase._has_duration_claim(text) is False, (
        "a file:line reference whose first path component is not a duration "
        "must stay a non-claim"
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("waited 41m:/x/y.md", id="colon"),
        pytest.param("3h;src/a.py", id="semicolon"),
        pytest.param("41m,docs/x.md", id="comma"),
        pytest.param("waited _3h;src/a.py_ now", id="wrapped-semicolon"),
        pytest.param("see docs/x.md,3d/y.md", id="later-list-entry"),
        pytest.param("see docs/x.md,3d", id="trailing-list-duration"),
    ],
)
def test_delimiter_glued_duration_before_or_after_a_path_is_a_claim(text):
    assert review_phase._has_duration_claim(text) is True, (
        "a duration glued to a path by a colon, semicolon or comma must stay "
        "visible to the scan"
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("docs/x.md,docs/y.md", id="comma-list"),
        pytest.param("src/a.py;docs/b.md", id="semicolon-list"),
        pytest.param("src/a.py:12, docs/b.md:7", id="reference-list"),
        pytest.param("see docs/3d/x.md,docs/2h/y.md", id="later-components-durations"),
        pytest.param("see https://example.com/x/y and more", id="url-scheme"),
    ],
)
def test_delimited_path_lists_without_a_duration_are_not_claims(text):
    assert review_phase._has_duration_claim(text) is False, (
        "delimited paths with no duration first component must stay non-claims"
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("ran x.y:3 days", id="bare-file"),
        pytest.param("ran src/x.py:3 days", id="path-file"),
        pytest.param("ran x.y:3 hours before failing", id="longer-sentence"),
    ],
)
def test_file_line_reference_followed_by_a_spaced_duration_unit_is_a_claim(text):
    assert review_phase._has_duration_claim(text) is True, (
        "a file:line reference followed by a spaced duration unit word is a "
        "claim, the same as a section id followed by one"
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("see x.y:3 for the bound", id="bare-file"),
        pytest.param("see src/x.py:3 for the bound", id="path-file"),
        pytest.param("see x.y:3, then continue", id="comma-after"),
    ],
)
def test_file_line_reference_followed_by_other_words_is_not_a_claim(text):
    assert review_phase._has_duration_claim(text) is False, (
        "a file:line reference followed by ordinary words must stay a non-claim"
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


# --- URLs, wrapped paths and trailing punctuation are references -------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("See https://example.com/3d/rule for the details.", id="bare-url"),
        pytest.param("See <https://example.com/3d/rule> for the details.", id="autolink"),
        pytest.param("Only touches **docs/3d/x.md** and nothing else.", id="strong-path"),
        pytest.param("Only touches docs/3d/x.md?", id="path-question"),
        pytest.param("Only touches docs/3d/x.md!", id="path-exclamation"),
    ],
)
def test_url_wrapped_and_punctuated_references_are_not_claims(text):
    assert review_phase._has_duration_claim(text) is False, (
        "a URL, an autolink, an emphasised path, or a path before sentence "
        "punctuation is a reference, not a timing claim"
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("See 3d/https://example.com/rule for the details.", id="bare-url"),
        pytest.param("See <3d/https://example.com/rule> for the details.", id="autolink"),
        pytest.param("Only touches **3d/x.md** and nothing else.", id="strong-path"),
        pytest.param("Only touches 3d/x.md?", id="path-question"),
        pytest.param("Only touches 3d/x.md!", id="path-exclamation"),
    ],
)
def test_same_reference_forms_with_a_duration_first_component_are_claims(text):
    assert review_phase._has_duration_claim(text) is True, (
        "a duration-shaped first component keeps the token visible to the "
        "scan however the reference is wrapped or punctuated"
    )


def test_run_phase_accepts_an_autolink_and_refuses_its_duration_first_variant():
    accepted = review_phase.run_phase(
        _envelope(
            "claude-fresh",
            _claude_approve_with("See <https://example.com/3d/rule> for the details."),
        )
    )
    assert accepted["status"] == "success", accepted["reason"]
    assert accepted["reason"] == "claude-fresh: approved, 0 finding(s)"
    refused = review_phase.run_phase(
        _envelope(
            "claude-fresh",
            _claude_approve_with("See <3d/https://example.com/rule> for the details."),
        )
    )
    assert refused["status"] == "failure"
    assert "review_interval_endpoints_missing" in refused["reason"]


# --- a duration first component wrapped on both sides stays a claim ----------
#
# Every text here is prose (the Summary section, never a quoted Evidence or
# Minor notes span), so the reference scan decides it, not the quoted-input
# blanking that runs before it.

WRAPPED_FIRST_COMPONENT_CLAIMS = [
    pytest.param("The caller waits **3h**/a.py before retrying.", id="bold"),
    pytest.param("The caller waits `41m`/x.md before retrying.", id="backtick"),
    pytest.param('The caller waits "3h"/a.py before retrying.', id="double-quote"),
    pytest.param("The caller waits '3h'/a.py before retrying.", id="single-quote"),
    pytest.param("The caller waits *3h*/a.py before retrying.", id="star"),
    pytest.param("The caller waits _3h_/a.py before retrying.", id="underscore"),
    pytest.param("The caller waits [3h]/a.py before retrying.", id="square-bracket"),
    pytest.param("The caller waits (3h)/a.py before retrying.", id="paren"),
    pytest.param("The caller waits {3h}/a.py before retrying.", id="brace"),
    pytest.param("The caller waits <3h>/a.py before retrying.", id="angle"),
    pytest.param("The caller waits 3h**/a.py before retrying.", id="bold-right-only"),
    pytest.param("The caller waits **3h/a.py before retrying.", id="bold-left-only"),
    pytest.param("The caller waits 3h_/a.py before retrying.", id="underscore-right-only"),
    pytest.param("The caller waits **3h**?q/a.py before retrying.", id="bold-query"),
    pytest.param("The caller waits _3h_?q/a.py before retrying.", id="underscore-query"),
    pytest.param("The caller waits _3h_/a.py:12 before retrying.", id="underscore-file-line"),
    # A later piece of a delimited token can carry an opening bracket after
    # its first component; a closing one ends the token before any `/`.
    pytest.param("It waited docs/x.md,3h(/a.py before retrying.", id="paren-right-in-list"),
    pytest.param("It waited docs/x.md,3h[/a.py before retrying.", id="bracket-right-in-list"),
    pytest.param("It took 3 **days**/run.", id="bold-unit-word"),
    pytest.param("The caller waits _3d/foo.py_ before retrying.", id="whole-token-underscore"),
    pytest.param("The caller waits 3d/foo.py before retrying.", id="bare"),
]
WRAPPED_FIRST_COMPONENT_REFERENCES = [
    pytest.param("Only touches docs/3d/x.md and nothing else.", id="later-component"),
    pytest.param("Only touches **docs**/3d/x.md and nothing else.", id="bold-name"),
    pytest.param("Only touches `docs`/3d/x.md and nothing else.", id="backtick-name"),
    pytest.param('Only touches "docs"/3d/x.md and nothing else.', id="quoted-name"),
    pytest.param("Only touches **/a.py and nothing else.", id="wrapper-only"),
]


def _assert_not_quoted_input(text: str) -> None:
    lines = text.splitlines()
    assert review_phase._blank_quoted_inputs(lines) == lines, (
        "the text must reach the reference scan as written, outside any quoted span"
    )


@pytest.mark.parametrize("text", WRAPPED_FIRST_COMPONENT_CLAIMS)
def test_wrapped_duration_first_component_is_a_claim(text):
    _assert_not_quoted_input(text)
    assert review_phase._has_duration_claim(text) is True, (
        "a duration-shaped first path component is a claim whatever formatting "
        "wraps it, on either side or both"
    )


@pytest.mark.parametrize("text", WRAPPED_FIRST_COMPONENT_REFERENCES)
def test_wrapped_non_duration_first_component_is_still_a_reference(text):
    _assert_not_quoted_input(text)
    assert review_phase._has_duration_claim(text) is False, (
        "a path whose first component is not a duration stays a reference "
        "however that component is wrapped"
    )


def _assert_summary_claim_refused(body: str) -> None:
    text = _claude_approve_with(body)
    _assert_not_quoted_input(text)
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "failure", result["reason"]
    assert "review_interval_endpoints_missing" in result["reason"]


def test_run_phase_refuses_a_bold_wrapped_duration_first_component():
    _assert_summary_claim_refused("The caller waits **3h**/a.py before retrying.")


def test_run_phase_refuses_a_backtick_wrapped_duration_first_component():
    _assert_summary_claim_refused("The caller waits `41m`/x.md before retrying.")


# Composed wrappers: any mix or nesting of the formatting wrappers around a
# duration first component. The path-token pass splits a token at brackets, so
# `(_3h_)/a.py` never reached the first-component test as one token. These run
# the whole gate (`review_phase.py --from-text`) on a review that carries the
# token in Summary prose and has no INTERVAL record.
COMPOSED_WRAPPER_CLAIMS = [
    pytest.param("(_3h_)/a.py", id="underscore-in-paren"),
    pytest.param("[_3h_]/a.py", id="underscore-in-bracket"),
    pytest.param("_(3h)_/a.py", id="paren-in-underscore"),
    pytest.param("(**3h**)/a.py", id="bold-in-paren"),
    pytest.param("_[3h]_/a.py", id="bracket-in-underscore"),
    pytest.param("_[_3h_]_/a.py", id="underscore-bracket-underscore"),
    pytest.param("([_3h_])/a.py", id="paren-bracket-underscore"),
    pytest.param("(_3h_)?q/a.py", id="underscore-in-paren-query"),
    pytest.param("[_3h_]/a.py:12", id="underscore-in-bracket-file-line"),
    pytest.param("docs/x.md,(_3h_)/a.py", id="underscore-in-paren-list-piece"),
    pytest.param("a.py,(_3h_)/b.py", id="underscore-in-paren-after-a-plain-entry"),
    pytest.param("~~3h~~/a.py", id="strikethrough"),
]
COMPOSED_WRAPPER_REFERENCES = [
    pytest.param("(_docs_)/3d/x.md", id="underscore-in-paren"),
    pytest.param("[_docs_]/3d/x.md", id="underscore-in-bracket"),
    pytest.param("(**docs**)/3d/x.md", id="bold-in-paren"),
    # Strikethrough inside bold: with a bare closing `~~` right before the
    # slash, the install check's outside-reference scan reads it as a home path.
    pytest.param("**~~docs~~**/3d/x.md", id="strikethrough-in-bold"),
]


def _run_from_text(tmp_path: Path, token: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    text = _claude_approve_with(f"The caller waits {token} before retrying.")
    _assert_not_quoted_input(text)
    review = tmp_path / "return.md"
    review.write_text(text, encoding="utf-8")
    proc = _run_cli("--from-text", str(review), "--backend", "claude-fresh")
    return proc, json.loads(proc.stdout)


@pytest.mark.parametrize("token", COMPOSED_WRAPPER_CLAIMS)
def test_from_text_refuses_a_duration_first_component_in_composed_wrappers(
    tmp_path: Path, token
):
    proc, result = _run_from_text(tmp_path, token)
    assert result["status"] == "failure", (
        f"a duration first component in composed wrappers must need an INTERVAL "
        f"record: {result['reason']}"
    )
    assert "review_interval_endpoints_missing" in result["reason"]
    assert proc.returncode == review_phase.EXIT_FAILURE, proc.stderr


@pytest.mark.parametrize("token", COMPOSED_WRAPPER_REFERENCES)
def test_from_text_accepts_a_non_duration_first_component_in_composed_wrappers(
    tmp_path: Path, token
):
    proc, result = _run_from_text(tmp_path, token)
    assert result["status"] == "success", (
        f"a path whose first component is not a duration stays a reference "
        f"however that component is wrapped: {result['reason']}"
    )
    assert proc.returncode == review_phase.EXIT_SUCCESS, proc.stderr


# Emphasis underscores around a duration are a boundary, so a timing claim
# written in italics still needs an INTERVAL record. Underscores that a letter
# or digit touches belong to an identifier, which stays a non-claim.
EMPHASISED_DURATION_CLAIMS = [
    pytest.param("The suite took _3h_", id="italic-at-line-end"),
    pytest.param("The suite took __3h__", id="strong-underscore"),
    pytest.param("The suite took _3h_.", id="italic-before-period"),
    pytest.param("The suite took _3 hours_ in all.", id="italic-spaced-unit"),
    pytest.param("The suite took ___2.5h___ in all.", id="triple-underscore-decimal"),
    pytest.param("The suite took *_41m_* in all.", id="italic-inside-star"),
    pytest.param("The suite took (_3h_) in all.", id="italic-in-paren"),
    pytest.param("See [_3h_](docs/run.md) for the run.", id="italic-link-label"),
    pytest.param("The suite took _3h in all.", id="leading-underscore-only"),
    pytest.param("The suite took 3h_ in all.", id="trailing-underscore-only"),
    pytest.param("The suite took _3 Hours_ in all.", id="italic-capitalised-unit"),
]
EMPHASISED_DURATION_NON_CLAIMS = [
    pytest.param("Set retry_3h_window to two.", id="identifier-inner"),
    pytest.param("Call foo_3h now.", id="identifier-suffix"),
    pytest.param("Call _3h_window now.", id="identifier-leading-underscore"),
    pytest.param("Call 3h_window now.", id="identifier-duration-first"),
    pytest.param("Set v1_3h now.", id="identifier-digit-before"),
]


def _run_body_from_text(tmp_path: Path, body: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    text = _claude_approve_with(body)
    _assert_not_quoted_input(text)
    review = tmp_path / "return.md"
    review.write_text(text, encoding="utf-8")
    proc = _run_cli("--from-text", str(review), "--backend", "claude-fresh")
    return proc, json.loads(proc.stdout)


@pytest.mark.parametrize("body", EMPHASISED_DURATION_CLAIMS)
def test_from_text_refuses_an_emphasised_duration_without_an_interval(tmp_path: Path, body):
    proc, result = _run_body_from_text(tmp_path, body)
    assert result["status"] == "failure", (
        f"a duration in emphasis underscores is a timing claim: {result['reason']}"
    )
    assert "review_interval_endpoints_missing" in result["reason"]
    assert proc.returncode == review_phase.EXIT_FAILURE, proc.stderr


@pytest.mark.parametrize("body", EMPHASISED_DURATION_NON_CLAIMS)
def test_from_text_accepts_a_duration_shaped_part_of_an_identifier(tmp_path: Path, body):
    proc, result = _run_body_from_text(tmp_path, body)
    assert result["status"] == "success", (
        f"a duration-shaped part of an identifier is not a timing claim: {result['reason']}"
    )
    assert proc.returncode == review_phase.EXIT_SUCCESS, proc.stderr


# Only a path's first component can be a claim. The path-token pass splits a
# token at brackets, so a bracketed later component (`docs/(3h)/x.md`) used to
# stay visible on its own; the whole recognised path is now read first and its
# later components blanked. Text in front of a bracket in the first component
# stays as before (`took(3h)/run.md`), and a file:line reference keeps working.
LATER_COMPONENT_REFERENCES = [
    pytest.param("It reads (docs/_3h_/x.md) first.", id="paren-around-path-italic-later"),
    pytest.param("It reads [a/3h/b.md] first.", id="bracket-around-path"),
    pytest.param("It reads docs/(3h)/x.md first.", id="paren-later"),
    pytest.param("It reads docs/(3h)/x.md.", id="paren-later-before-period"),
    pytest.param("It uses a (docs/3h/x.md)-style layout.", id="paren-path-glued-to-a-word"),
    pytest.param("It reads docs/(_3h_)/x.md first.", id="paren-italic-later"),
    pytest.param("It reads docs/[_3h_]/x.md first.", id="bracket-italic-later"),
    pytest.param("It reads docs/[3h]/x.md first.", id="bracket-later"),
    pytest.param("It reads docs/_(3h)_/x.md first.", id="italic-paren-later"),
    pytest.param("It reads docs/a/(3h)/x.md first.", id="paren-third-component"),
    pytest.param("It reads docs/(_3h_).md first.", id="paren-italic-file-name"),
    pytest.param("It reads docs/(3h)/x.md:12 first.", id="paren-later-file-line"),
    pytest.param("It reads docs/a.md,b.py:12d first.", id="list-entry-file-line"),
    pytest.param("It reads ./docs/3h:12 first.", id="extensionless-name-then-line"),
    pytest.param("It reads docs/3h.md,12 first.", id="file-name-then-comma-number"),
    pytest.param("It reads docs/3h.md:x first.", id="file-name-then-colon-word"),
]
LATER_COMPONENT_CLAIMS = [
    pytest.param("It waits took(3h)/run.md here.", id="paren-in-first-component"),
    pytest.param("It waits took[3h]/run.md here.", id="bracket-in-first-component"),
    pytest.param("It waited 3h(docs/x.md) here.", id="duration-before-paren-path"),
    pytest.param("It waited 3h[docs/x.md] here.", id="duration-before-bracket-path"),
    pytest.param("It waits docs/x.md,(3h)/y.md here.", id="duration-first-list-entry"),
    pytest.param("It ran docs/(x)/y.md:3 days", id="file-line-then-unit-word"),
    pytest.param("It waited )3h/a.py here.", id="stray-close-paren-before-duration"),
    pytest.param("It waited ]3h/a.py here.", id="stray-close-bracket-before-duration"),
]


@pytest.mark.parametrize("body", LATER_COMPONENT_REFERENCES)
def test_from_text_accepts_a_duration_later_component_inside_brackets(tmp_path: Path, body):
    proc, result = _run_body_from_text(tmp_path, body)
    assert result["status"] == "success", (
        f"a later path component is never a claim, bracketed or not: {result['reason']}"
    )
    assert proc.returncode == review_phase.EXIT_SUCCESS, proc.stderr


@pytest.mark.parametrize("body", LATER_COMPONENT_CLAIMS)
def test_from_text_refuses_a_first_component_or_glued_duration_beside_a_bracketed_path(
    tmp_path: Path, body
):
    proc, result = _run_body_from_text(tmp_path, body)
    assert result["status"] == "failure", (
        f"a duration before the path's later components is still a claim: {result['reason']}"
    )
    assert "review_interval_endpoints_missing" in result["reason"]
    assert proc.returncode == review_phase.EXIT_FAILURE, proc.stderr


# The shield for later components blanks only a recognised path's later
# components, from its first `/` to its end. It never blanks the first
# component or text in front of it, whatever bracket sits before the `/`; a
# closer with no opener ends the path, and what follows it is read again; an
# unpaired opener ends the path before it, so the opener and what follows it
# stay visible. Every text here refuses: the `3h` is outside any recognised
# path's later components.
SHIELD_KEEPS_CLAIMS = [
    pytest.param("It took note(3h/docs/a/b.md here.", id="unmatched-paren-before-duration"),
    pytest.param("It waited note[3h/run.md here.", id="unmatched-bracket-before-duration"),
    pytest.param("It ran x)3h/docs/a/b.md here.", id="preceding-close-paren"),
    pytest.param("It waited note]3h/run.md here.", id="preceding-close-bracket"),
    pytest.param("A note[_3h_/docs/x/y.md here.", id="prefix-then-bracket-italic-duration"),
    pytest.param("A note(**3h**/docs/x/y.md here.", id="prefix-then-paren-bold-duration"),
    pytest.param("A note({3h}/docs/x/y.md here.", id="prefix-then-paren-brace-duration"),
    pytest.param("It read docs/a.md)3h/b.md here.", id="duration-after-a-path-and-closer"),
    pytest.param("It read (docs/a.md)3h/b.md here.", id="duration-after-a-paren-wrapped-path"),
    pytest.param("It read [docs/a.md]3h/b.md here.", id="duration-after-a-bracket-wrapped-path"),
    pytest.param("It read docs/(a]3h/b.md here.", id="mismatched-closer-ends-the-path"),
    pytest.param("It read ./docs/(3h here.", id="unpaired-opener-and-what-follows"),
    pytest.param("It read ./docs/(3h/(a here.", id="first-of-two-unpaired-openers"),
    pytest.param("It read docs/(a/[3h]/x.md here.", id="unpaired-opener-around-a-pair"),
    pytest.param("It read ./a)x/3h here.", id="next-path-read-from-the-closer"),
    pytest.param(
        "It read docs/a.md)./x/(a)3h/b here.", id="next-path-text-starts-at-the-closer"
    ),
    pytest.param("It read (docs/a.md)3h/b.md) here.", id="duration-between-two-closers"),
    pytest.param("It read x/3h)y/b.md here.", id="unrecognised-path-before-a-closer"),
    pytest.param("It read ./docs/([3h)/x) here.", id="crossed-brackets-end-the-path"),
]
# A path after a closer is still recognised and shielded, and only the path
# that runs to the end of the piece keeps its file name for a `:line`.
SHIELD_BLANKS_LATER_COMPONENTS = [
    pytest.param("It read docs/a.md)x/(3h)/b.md here.", id="next-path-later-component"),
    pytest.param("It read ./docs/3h)x/b.md:12 here.", id="file-line-name-only-on-the-last-path"),
    pytest.param("It reads /docs/(a)3h/x.md first.", id="absolute-path-pair-then-duration"),
    pytest.param(
        "It reads docs/a.md,docs/(3h)/x.md first.", id="bracketed-later-component-in-a-list-piece"
    ),
]


# An opener left unpaired after the `/` ends the shielded path before it, so
# these tokens reach the path-token pass whole. That pass still reads the first
# component with every formatting wrapper stripped from both sides, so the
# duration stays a claim. A `?` in front of a file:line path hides it from the
# shield's path test in the same way, and the file:line pass reads its first
# component with the wrappers stripped too.
SHIELD_KEEPS_CLAIMS += [
    pytest.param("It waited **3h**/(a.py here.", id="bold-first-component-unpaired-opener"),
    pytest.param("It waited `3h`/(a.py here.", id="backtick-first-component-unpaired-opener"),
    pytest.param('It waited "3h"/(a.py here.', id="double-quote-first-component-unpaired-opener"),
    pytest.param("It waited '3h'/(a.py here.", id="single-quote-first-component-unpaired-opener"),
    pytest.param("It waited _3h_/(a.py here.", id="underscore-first-component-unpaired-opener"),
    pytest.param("It waited ~~3h~~/(a.py here.", id="strikethrough-first-component-unpaired-opener"),
    pytest.param("It waited {3h}/(a.py here.", id="brace-first-component-unpaired-opener"),
    pytest.param("It waited <3h>/(a.py here.", id="angle-first-component-unpaired-opener"),
    pytest.param("It waited 3h{/(a.py here.", id="open-brace-after-duration-unpaired-opener"),
    pytest.param("It waited 3h</(a.py here.", id="open-angle-after-duration-unpaired-opener"),
    pytest.param("It waited x?3h_/a.py:12 here.", id="query-mark-before-file-line-duration"),
]


@pytest.mark.parametrize("body", SHIELD_KEEPS_CLAIMS)
def test_from_text_refuses_a_duration_the_shield_cannot_place_in_later_components(
    tmp_path: Path, body
):
    proc, result = _run_body_from_text(tmp_path, body)
    assert result["status"] == "failure", (
        f"a duration outside a recognised path's later components is a claim: "
        f"{result['reason']}"
    )
    assert "review_interval_endpoints_missing" in result["reason"]
    assert proc.returncode == review_phase.EXIT_FAILURE, proc.stderr


@pytest.mark.parametrize("body", SHIELD_BLANKS_LATER_COMPONENTS)
def test_from_text_accepts_a_duration_in_a_later_component_after_a_closer(
    tmp_path: Path, body
):
    proc, result = _run_body_from_text(tmp_path, body)
    assert result["status"] == "success", (
        f"a later path component is never a claim: {result['reason']}"
    )
    assert proc.returncode == review_phase.EXIT_SUCCESS, proc.stderr


# A closer that does not pair with the open bracket (`(` then `]`) ends the
# path, and the scan for the next path starts at that closer, not at the
# opener before it. Text in front of the closer is not part of the next path,
# so an absolute prefix after the opener (`(/`, `(./`) cannot make it a path
# and blank a duration that follows the closer.
#
# Level one: the shield itself, as `(piece, line_number, shield)`. Each piece
# keeps its trailing `3h` visible; reading the next path from the opener
# instead would blank it.
SHIELD_KEEPS_A_DURATION_AFTER_A_MISMATCHED_CLOSER = [
    pytest.param("/(/]/3h", False, " (/]/3h", id="rooted-paren-bracket"),
    pytest.param("3h/(/]/3h", False, "3h/(/]/3h", id="duration-first-paren-bracket"),
    pytest.param("/[/)/3h", False, " [/)/3h", id="rooted-bracket-paren"),
    pytest.param("/3h(/]/3h", False, "   (/]/3h", id="rooted-duration-then-paren-bracket"),
    pytest.param("docs/(./x]a/3h", False, "docs/(./x]a/3h", id="dot-slash-after-the-opener"),
    pytest.param("docs/(./x]a/x/3h", False, "docs/(./x]a/x/3h", id="duration-two-components-on"),
    pytest.param("/(/]/3h", True, " (/]/3h", id="rooted-paren-bracket-before-a-line-number"),
    pytest.param(
        "docs/(./x]a/3h", True, "docs/(./x]a/3h", id="dot-slash-after-the-opener-before-a-line"
    ),
]


@pytest.mark.parametrize(
    ("piece", "line_number", "shield"), SHIELD_KEEPS_A_DURATION_AFTER_A_MISMATCHED_CLOSER
)
def test_shield_reads_the_next_path_from_a_mismatched_closer_not_the_opener(
    piece: str, line_number: bool, shield: str
):
    assert review_phase._shield_later_components(piece, line_number) == shield


# Level two, through the review text: each pair is a mismatched-closer piece
# with a trailing duration that refuses, and the same text without the
# duration, which approves. The duration follows the first component of the
# path that starts at the closer (`a/3h`), so it is a claim; the text in front
# of the closer starts like a path (`(./`, `(/`, `(~/`), which is the only
# thing that could hide it.
MISMATCHED_CLOSER_DURATION_PAIRS = [
    pytest.param(
        "It read docs/(./x]a/3h here.", "It read docs/(./x]a here.", id="dot-slash-paren-bracket"
    ),
    pytest.param(
        "It read docs/(/x]a/3h here.", "It read docs/(/x]a here.", id="slash-paren-bracket"
    ),
    pytest.param(
        "It read docs/(~/x]a/3h here.", "It read docs/(~/x]a here.", id="tilde-paren-bracket"
    ),
    pytest.param(
        "It read docs/(https://x]a/3h here.",
        "It read docs/(https://x]a here.",
        id="uri-paren-bracket",
    ),
    pytest.param(
        "It read docs/[./x)a/3h here.", "It read docs/[./x)a here.", id="dot-slash-bracket-paren"
    ),
    pytest.param(
        "It read x/(/a]a/3h here.", "It read x/(/a]a here.", id="rooted-after-a-component"
    ),
    pytest.param(
        "It read docs/(./x]a/3h:12 here.",
        "It read docs/(./x]a:12 here.",
        id="before-a-line-number",
    ),
]


@pytest.mark.parametrize(("with_duration", "without_duration"), MISMATCHED_CLOSER_DURATION_PAIRS)
def test_from_text_refuses_a_duration_after_a_mismatched_closer_and_approves_without_it(
    tmp_path: Path, with_duration: str, without_duration: str
):
    proc, result = _run_body_from_text(tmp_path, with_duration)
    assert result["status"] == "failure", (
        f"a duration after a closer that pairs with no opener is a claim: {result['reason']}"
    )
    assert "review_interval_endpoints_missing" in result["reason"]
    assert proc.returncode == review_phase.EXIT_FAILURE, proc.stderr
    proc, result = _run_body_from_text(tmp_path, without_duration)
    assert result["status"] == "success", (
        f"the same text without the duration approves: {result['reason']}"
    )
    assert proc.returncode == review_phase.EXIT_SUCCESS, proc.stderr


# The closer can also be followed by `/` at once (`]/3h/run.md`). That text is
# itself a rooted path whose later components are `3h` and `run.md`, so the
# duration is a later component of a recognised path and approves, with or
# without the opener's text in front of it.
SHIELD_BLANKS_A_ROOTED_PATH_AFTER_A_MISMATCHED_CLOSER = [
    pytest.param("see docs/(a]/3h/run.md", id="rooted-path-after-the-closer"),
    pytest.param("It reads docs/(x]/3h/b.md first.", id="token-glued-rooted-path-after-the-closer"),
]


@pytest.mark.parametrize("body", SHIELD_BLANKS_A_ROOTED_PATH_AFTER_A_MISMATCHED_CLOSER)
def test_from_text_accepts_a_duration_in_a_rooted_path_after_a_mismatched_closer(
    tmp_path: Path, body: str
):
    proc, result = _run_body_from_text(tmp_path, body)
    assert result["status"] == "success", (
        f"a later component of the path that starts at the closer is not a claim: "
        f"{result['reason']}"
    )
    assert proc.returncode == review_phase.EXIT_SUCCESS, proc.stderr


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


# --- (a) one normalised reference form: wrappers x reference kinds x suffixes -

# Opening and closing text around a reference.
REFERENCE_WRAPPINGS = {
    "plain": ("", ""),
    "star": ("*", "*"),
    "strong": ("**", "**"),
    "underscore": ("_", "_"),
    "backtick": ("`", "`"),
    "angle": ("<", ">"),
}
# Each kind holds a duration-shaped LATER component, so only reading it as a
# reference keeps it from being a claim.
REFERENCE_KINDS = {
    "dot-slash": "./docs/3d/notes",
    "dot-dot-slash": "../docs/3d/notes",
    "slash": "/docs/3d/notes",
    "tilde-slash": "~/3d",
    "uri-scheme": "https://example.com/3d/rule",
    "relative-extension": "docs/3d/x.md",
}
# The same kinds led by a duration-shaped FIRST component, plus a duration
# glued to a query or fragment (`41m?x/y.md`).
DURATION_FIRST_KINDS = {
    "dot-slash": "3d/./docs/notes",
    "dot-dot-slash": "3d/../docs/notes",
    "slash": "3d//docs/notes",
    "tilde-slash": "3d/~/notes",
    "uri-scheme": "3d/https://example.com/rule",
    "relative-extension": "3d/docs/x.md",
    "query-first-component": "41m?x/y.md",
    "fragment-first-component": "41m#x/y.md",
}
# (id, text inside the wrapper, text after the wrapper).
REFERENCE_SUFFIXES = [("none", "", "")]
for _suffix_id, _suffix in (
    ("query", "?q=1"),
    ("fragment", "#frag"),
    ("period", "."),
    ("comma", ","),
    ("question", "?"),
    ("exclamation", "!"),
    ("paren", ")"),
):
    REFERENCE_SUFFIXES.append((f"{_suffix_id}-inside", _suffix, ""))
    REFERENCE_SUFFIXES.append((f"{_suffix_id}-outside", "", _suffix))


def _reference_table(kinds: dict) -> list:
    return [
        pytest.param(
            f"{opening}{reference}{inside}{closing}{outside}",
            id=f"{wrapping}-{kind}-{suffix}",
        )
        for wrapping, (opening, closing) in REFERENCE_WRAPPINGS.items()
        for kind, reference in kinds.items()
        for suffix, inside, outside in REFERENCE_SUFFIXES
    ]


@pytest.mark.parametrize("reference", _reference_table(REFERENCE_KINDS))
def test_wrapped_and_suffixed_reference_is_not_a_claim(reference):
    text = f"Only touches {reference} and nothing else."
    assert review_phase._has_duration_claim(text) is False, (
        "an explicit path, a URI or a relative path with an extension is a "
        "reference however it is wrapped, suffixed or punctuated"
    )


@pytest.mark.parametrize("reference", _reference_table(DURATION_FIRST_KINDS))
def test_wrapped_and_suffixed_reference_with_a_duration_first_component_is_a_claim(
    reference,
):
    text = f"The caller waits {reference} before retrying."
    assert review_phase._has_duration_claim(text) is True, (
        "a duration-shaped first component stays a claim however the token is "
        "wrapped, suffixed or punctuated"
    )


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
        "- Ran: checks passed\n"
    )


SOL_REFERENCE_PROBES = {
    "bare-url": "See https://example.com/3d/rule for the details.",
    "autolink": "See <https://example.com/3d/rule> for the details.",
    "strong-extension-path": "Only touches **docs/3d/x.md** and nothing else.",
    "question-path": "Only touches docs/3d/x.md?",
    "exclamation-path": "Only touches docs/3d/x.md!",
    "strong-explicit-relative-no-extension": "Only touches **./docs/3d/notes** and nothing else.",
    "strong-explicit-absolute-no-extension": "Only touches **/docs/3d/notes** and nothing else.",
    "relative-fragment": "Only touches docs/3d/x.md#anchor.",
    "relative-query": "Only touches docs/3d/x.md?view=all.",
}
SOL_ISSUES_PROBES_REFUSED = {
    "clean-then-foreign": "- none\nA real issue is described in prose.",
    "real-then-foreign": "- `a.py:1` — first issue\nA second issue is described in prose.",
    "foreign-then-real": "A first issue is described in prose.\n- `a.py:1` — second issue",
}
SOL_ISSUES_PROBES_ACCEPTED = {
    "clean-then-indented": "- None blocking.\n  Checked every call site.",
}


@pytest.mark.parametrize(
    "body", list(SOL_REFERENCE_PROBES.values()), ids=list(SOL_REFERENCE_PROBES)
)
def test_run_phase_accepts_the_second_family_reference_probes(body):
    result = review_phase.run_phase(_envelope("claude-fresh", _sol_probe_return(body)))
    assert result["status"] == "success", result["reason"]
    assert result["artifacts"]["findings.json"]["verdict"] == "approved"


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


def test_run_phase_accepts_a_wrapped_query_reference_and_refuses_its_duration_first_form():
    accepted = review_phase.run_phase(
        _envelope(
            "claude-fresh",
            _claude_approve_with("Only touches **./docs/3d/notes?q=1** and nothing else."),
        )
    )
    assert accepted["status"] == "success", accepted["reason"]
    refused = review_phase.run_phase(
        _envelope(
            "claude-fresh",
            _claude_approve_with("The caller waits **41m?x/y.md** before retrying."),
        )
    )
    assert refused["status"] == "failure"
    assert "review_interval_endpoints_missing" in refused["reason"]


# --- (d) the reference scan is linear in the length of a long token ----------

REDOS_LIMIT_SECONDS = 1.0


def _base64_like(length: int) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    return "".join(alphabet[(index * 7 + index // 64) % 64] for index in range(length))


def _minified_js_like(length: int) -> str:
    chunk = "function a(b,c){return b.c+d.e(f,g);}var h=i.j.k(l);if(m){n.o=p/q}"
    return (chunk * (length // len(chunk) + 1))[:length]


LONG_TOKENS = {
    "dotless": lambda length: "x" * length,
    "dotless-then-slashes": lambda length: "x" * length + "/" * 50,
    "base64-like": _base64_like,
    "minified-js-like": _minified_js_like,
    "unclosed-link-targets": lambda length: "](" * (length // 2),
    "dotted-run": lambda length: "a." * (length // 2),
    "closer-separated-paths": lambda length: ("a/b.md)" * length)[:length],
    "unpaired-openers": lambda length: ("a/(" * length)[:length],
}
REFERENCE_SCANS = {
    "has-duration-claim": lambda text: review_phase._has_duration_claim(text),
    "blank-references": lambda text: review_phase._blank_references(text),
}


def _one_call_seconds(scan, text: str) -> float:
    start = time.perf_counter()
    scan(text)
    return time.perf_counter() - start


def _best_seconds_per_call(scan, text: str, trials: int = 5, calls: int = 10) -> float:
    """The fastest per-call time over several trials; one slow call ends it."""
    best = float("inf")
    for _ in range(trials):
        start = time.perf_counter()
        for _ in range(calls):
            call = _one_call_seconds(scan, text)
            if call > REDOS_LIMIT_SECONDS:
                return call
        best = min(best, (time.perf_counter() - start) / calls)
    return best


@pytest.mark.parametrize("scan", list(REFERENCE_SCANS))
@pytest.mark.parametrize("token", list(LONG_TOKENS))
def test_reference_scan_finishes_a_4000_character_token_within_a_second(scan, token):
    text = LONG_TOKENS[token](4000)
    elapsed = _one_call_seconds(REFERENCE_SCANS[scan], text)
    assert elapsed <= REDOS_LIMIT_SECONDS, (
        f"{scan} took {elapsed:.2f}s on a {len(text)}-character {token} token"
    )


# The growth test sizes its inputs so a linear scan takes several milliseconds
# (a 4000-character token takes well under one, where runner noise alone can
# triple a timing). Each size is timed several times and the fastest call is
# kept, since noise only ever adds time. The longer input is four times the
# shorter, so a linear scan grows about four times and a quadratic one about
# sixteen; the bound of eight sits between them.
GROWTH_SHORT_LENGTH = 40_000
GROWTH_LONG_LENGTH = 4 * GROWTH_SHORT_LENGTH
GROWTH_REPETITIONS = 9
GROWTH_RATIO_BOUND = 8


def _fastest_call_seconds(scan, text: str) -> float:
    """The fastest of several single calls; one call over the limit ends it."""
    best = float("inf")
    for _ in range(GROWTH_REPETITIONS):
        call = _one_call_seconds(scan, text)
        if call > REDOS_LIMIT_SECONDS:
            return call
        best = min(best, call)
    return best


@pytest.mark.parametrize("scan", list(REFERENCE_SCANS))
@pytest.mark.parametrize("token", list(LONG_TOKENS))
def test_reference_scan_time_grows_linearly_with_token_length(scan, token):
    shorter = _fastest_call_seconds(
        REFERENCE_SCANS[scan], LONG_TOKENS[token](GROWTH_SHORT_LENGTH)
    )
    assert shorter <= REDOS_LIMIT_SECONDS, (
        f"{scan} took {shorter:.2f}s on a {GROWTH_SHORT_LENGTH}-character {token} token"
    )
    longer = _fastest_call_seconds(
        REFERENCE_SCANS[scan], LONG_TOKENS[token](GROWTH_LONG_LENGTH)
    )
    assert longer <= GROWTH_RATIO_BOUND * shorter, (
        f"{scan} on a {token} token: quadrupling {GROWTH_SHORT_LENGTH} to "
        f"{GROWTH_LONG_LENGTH} characters took {shorter * 1e3:.3f}ms to "
        f"{longer * 1e3:.3f}ms, more than {GROWTH_RATIO_BOUND} times"
    )


def test_run_phase_accepts_a_review_holding_a_long_hash_quickly():
    body = "Checked the artifact " + "x" * 4000 + "/" * 50 + " against its digest."
    start = time.perf_counter()
    result = review_phase.run_phase(_envelope("claude-fresh", _claude_approve_with(body)))
    elapsed = time.perf_counter() - start
    assert result["status"] == "success", result["reason"]
    assert elapsed <= REDOS_LIMIT_SECONDS, f"run_phase took {elapsed:.2f}s"


def test_bound_exemption_finishes_a_100kb_line_within_a_second():
    chunk = "120 second timeout "
    line = chunk * (100_000 // len(chunk) + 1)
    assert len(line) >= 100_000
    start = time.perf_counter()
    claimed = review_phase._has_duration_claim(line)
    elapsed = time.perf_counter() - start
    assert claimed is False, "a 100 KB line of configured bounds is not a claim"
    assert elapsed <= REDOS_LIMIT_SECONDS, (
        f"_has_duration_claim took {elapsed:.2f}s on a {len(line)}-character bound line"
    )
    mixed = line[:100_000] + " took 120 seconds before the timeout"
    start = time.perf_counter()
    claimed = review_phase._has_duration_claim(mixed)
    elapsed = time.perf_counter() - start
    assert claimed is True, "a measured interval after 100 KB of bounds is still a claim"
    assert elapsed <= REDOS_LIMIT_SECONDS, (
        f"_has_duration_claim took {elapsed:.2f}s on a 100KB near-miss line"
    )


# --- (e) a Markdown link target is a reference; its label is not --------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("See [label](docs/x.md) for the rule.", id="docs-target"),
        pytest.param("See [x](#3d) for the rule.", id="hash-target"),
        pytest.param("See [label](docs/3d) for the rule.", id="extensionless-target"),
        pytest.param("See [label](3d/x.md) for the rule.", id="duration-first-target"),
        pytest.param("See [label](docs/x.md) and §3d for the rule.", id="docs-target-beside-section"),
        pytest.param("See [x](#3d) and docs/3d/x.md for the rule.", id="hash-target-beside-path"),
    ],
)
def test_link_target_as_a_pure_reference_is_not_a_claim(text):
    assert review_phase._has_duration_claim(text) is False, (
        "a Markdown link target is a reference, never a timing claim"
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("See [label](docs/x.md); the caller waits 3d.", id="docs-target"),
        pytest.param("See [x](#3d); the caller waits 3d.", id="hash-target"),
        pytest.param("See [label](docs/3d); it took 2h.", id="extensionless-target"),
        pytest.param("See [took 3d](#x) for the rule.", id="claim-in-the-label"),
        pytest.param("See [x](#3d for the rule.", id="unclosed-target-stays-visible"),
    ],
)
def test_link_target_beside_a_real_claim_is_a_claim(text):
    assert review_phase._has_duration_claim(text) is True, (
        "a real claim beside a link target, in its label, or after an "
        "unclosed target stays a claim"
    )


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


def _best_heading_seconds(line: str, trials: int = 7, calls: int = 50) -> float:
    """The fastest per-call time over several trials; one slow call ends it."""
    best = float("inf")
    for _ in range(trials):
        start = time.perf_counter()
        for _ in range(calls):
            _, call = _timed_heading_match(line)
            if call > HEADING_LIMIT_SECONDS:
                return call
        best = min(best, (time.perf_counter() - start) / calls)
    return best


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


@pytest.mark.parametrize("shape", list(HEADING_GROWTH_SHAPES))
def test_heading_match_time_grows_linearly_with_line_length(shape):
    shorter = _best_heading_seconds(HEADING_GROWTH_SHAPES[shape](3000))
    assert shorter <= HEADING_LIMIT_SECONDS, (
        f"_HEADING took {shorter:.2f}s on the 3000-space {shape} line"
    )
    longer = _best_heading_seconds(HEADING_GROWTH_SHAPES[shape](6000))
    assert longer <= 3 * shorter, (
        f"_HEADING on a {shape} line: doubling 3000 to 6000 spaces took "
        f"{shorter * 1e3:.4f}ms to {longer * 1e3:.4f}ms, more than three times"
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
# The verdict heading and label patterns run on every line of a review. A
# colon, a long space run, an optional `[` or `*` mark and a second space run
# used to let two adjacent whitespace sub-patterns split the first run every
# way, which is quadratic. Each form below keeps the verdict word the earlier
# patterns gave.


def _best_match_seconds(pattern, line: str, trials: int = 7, calls: int = 50) -> float:
    """The fastest per-call time over several trials; one slow call ends it."""
    best = float("inf")
    for _ in range(trials):
        start = time.perf_counter()
        for _ in range(calls):
            call_start = time.perf_counter()
            pattern.match(line)
            call = time.perf_counter() - call_start
            if call > HEADING_LIMIT_SECONDS:
                return call
        best = min(best, (time.perf_counter() - start) / calls)
    return best


VERDICT_GROWTH_CASES = [
    ("_CLAUDE_VERDICT_HEADING", "colon-spaces-x", lambda n: "## Verdict:" + " " * n + "x"),
    (
        "_CLAUDE_VERDICT_HEADING",
        "colon-spaces-bracket-spaces-x",
        lambda n: "## Verdict:" + " " * n + "[" + " " * n + "x",
    ),
    ("_CLAUDE_VERDICT_LABEL", "colon-spaces-x", lambda n: "VERDICT:" + " " * n + "x"),
    (
        "_CLAUDE_VERDICT_LABEL",
        "colon-spaces-stars-spaces-x",
        lambda n: "VERDICT:" + " " * n + "**" + " " * n + "x",
    ),
    ("_GROK_VERDICT_LABEL", "colon-spaces-x", lambda n: "VERDICT:" + " " * n + "x"),
    (
        "_GROK_VERDICT_LABEL",
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
    pattern = getattr(review_phase, name)
    shorter = _best_match_seconds(pattern, build(3000))
    assert shorter <= HEADING_LIMIT_SECONDS, (
        f"{name} took {shorter:.2f}s on the 3000-space {shape} line"
    )
    longer = _best_match_seconds(pattern, build(6000))
    assert longer <= 3 * shorter, (
        f"{name} on a {shape} line: doubling 3000 to 6000 spaces took "
        f"{shorter * 1e3:.4f}ms to {longer * 1e3:.4f}ms, more than three times"
    )


VERDICT_FORMS = [
    ("_CLAUDE_VERDICT_HEADING", "## Verdict: APPROVE", "APPROVE"),
    ("_CLAUDE_VERDICT_HEADING", "## Verdict: [APPROVE_WITH_FINDINGS]", "APPROVE_WITH_FINDINGS"),
    ("_CLAUDE_VERDICT_HEADING", "## Verdict: [ REQUEST CHANGES ]", "REQUEST CHANGES"),
    ("_CLAUDE_VERDICT_HEADING", "### **Verdict**:REQUEST-CHANGES", "REQUEST-CHANGES"),
    ("_CLAUDE_VERDICT_HEADING", "## Verdict:\t[\tapprove", "approve"),
    ("_CLAUDE_VERDICT_HEADING", "## Verdict: [[APPROVE", None),
    ("_CLAUDE_VERDICT_HEADING", "## Verdict: * APPROVE", None),
    ("_CLAUDE_VERDICT_HEADING", "####### Verdict: APPROVE", None),
    ("_CLAUDE_VERDICT_LABEL", "VERDICT: APPROVE", "APPROVE"),
    ("_CLAUDE_VERDICT_LABEL", "**VERDICT:** REQUEST_CHANGES", "REQUEST_CHANGES"),
    ("_CLAUDE_VERDICT_LABEL", "VERDICT: ** APPROVE", "APPROVE"),
    ("_CLAUDE_VERDICT_LABEL", "VERDICT:**APPROVE**", "APPROVE"),
    ("_CLAUDE_VERDICT_LABEL", "VERDICT: * * APPROVE", None),
    ("_CLAUDE_VERDICT_LABEL", "VERDICT: [APPROVE", None),
    ("_CLAUDE_VERDICT_LABEL", "VERDICT: APPROVED", None),
    ("_GROK_VERDICT_LABEL", "VERDICT: flagged — the retry logic needs work.", "flagged"),
    ("_GROK_VERDICT_LABEL", "**VERDICT**: **cleared**", "cleared"),
    ("_GROK_VERDICT_LABEL", "VERDICT:\tcleared", "cleared"),
    ("_GROK_VERDICT_LABEL", "VERDICT: * * cleared", None),
    ("_GROK_VERDICT_LABEL", "VERDICT: clearedx", None),
]


@pytest.mark.parametrize(
    ("name", "line", "word"),
    VERDICT_FORMS,
    ids=[f"{name}-{line!r}" for name, line, _ in VERDICT_FORMS],
)
def test_verdict_forms_keep_main_words(name, line, word):
    matched = getattr(review_phase, name).match(line)
    assert (None if matched is None else matched.group(1)) == word


# --- quoted duration inputs in Evidence and Minor notes ----------------------
#
# A review of the duration scanner has to quote its probe inputs. A
# duration-shaped token inside a backtick code span (inline or fenced) in a
# `### Evidence` or `### Minor notes` section is a quoted input, not the
# reviewer's own timing claim, so it needs no INTERVAL record. Prose stays in
# scope everywhere, and so do Issues, every heading line, any other heading
# form, and any markup that does not close where it opens.


def _quoted_approve(evidence: str, issues: str = "- none", minor: str | None = None) -> str:
    parts = [
        "## Verdict: APPROVE",
        "",
        "Checked the duration scanner.",
        "",
        "### Issues",
        issues,
        "",
    ]
    if minor is not None:
        parts += ["### Minor notes", minor, ""]
    parts += ["### Evidence", evidence, ""]
    return "\n".join(parts)


def _assert_quoted_parses(text: str, backend: str = "claude-fresh") -> None:
    result = review_phase.run_phase(_envelope(backend, text))
    assert result["status"] == "success", result["reason"]
    assert "intervals" not in result


def _assert_claim_refused(text: str, backend: str = "claude-fresh") -> None:
    result = review_phase.run_phase(_envelope(backend, text))
    assert result["status"] == "failure", "a timing claim parsed without an INTERVAL record"
    assert "review_interval_endpoints_missing" in result["reason"], result["reason"]


QUOTED_DURATION_INPUTS = {
    "evidence-inline": _quoted_approve("- Probe: `waits 3d/foo.py` is refused as a claim"),
    "evidence-inline-spaced-unit": _quoted_approve("- Probe: `ran for 120 seconds` is refused"),
    "evidence-double-backtick-span": _quoted_approve("- Probe: ``a ` then 41m`` is refused"),
    "evidence-backtick-fence": _quoted_approve(
        "- Probes:\n```\nwaits 3d/foo.py\nit took 2h\n```"
    ),
    "evidence-tilde-fence": _quoted_approve("- Probes:\n~~~text\nit took 2h\n~~~"),
    "evidence-longer-closing-fence": _quoted_approve("```\nit took 2h\n`````"),
    "minor-notes-inline": _quoted_approve(
        "- Ran: checks passed", minor="- the probe `41m?x/y.md` reads as a claim"
    ),
    "minor-notes-indented-fence": _quoted_approve(
        "- Ran: checks passed", minor="- the probes:\n  ```\n  it took 2h\n  ```"
    ),
    "bold-evidence-heading": _quoted_approve("- Probe: `it took 2h`").replace(
        "### Evidence", "### **Evidence**"
    ),
    "indented-evidence-heading": _quoted_approve("- Probe: `it took 2h`").replace(
        "### Evidence", "   ###  Evidence"
    ),
    "closed-evidence-heading": _quoted_approve("- Probe: `it took 2h`").replace(
        "### Evidence", "### Evidence ###"
    ),
}


@pytest.mark.parametrize(
    "text", list(QUOTED_DURATION_INPUTS.values()), ids=list(QUOTED_DURATION_INPUTS)
)
def test_quoted_duration_input_in_evidence_or_minor_notes_parses_without_an_interval(text):
    _assert_quoted_parses(text)


def test_same_token_unquoted_in_evidence_still_refuses():
    _assert_claim_refused(_quoted_approve("- Probe: waits 3d/foo.py is refused as a claim"))


def test_quoted_duration_in_issues_still_refuses():
    _assert_claim_refused(
        "## Verdict: REQUEST CHANGES\n\n"
        "### Issues\n- `a.py:3` — the probe `waits 3d` is accepted\n\n"
        "### Evidence\n- Ran: checks passed\n"
    )


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param("- Ran: the suite finished in 3 minutes", id="prose"),
        pytest.param(
            "- Probe `waits 3d` passed; the suite finished in 3 minutes", id="beside-a-span"
        ),
        pytest.param("- Probe `waits 3d` passed; the caller waits 3d", id="same-token-both-sides"),
    ],
)
def test_unquoted_timing_claim_in_evidence_still_refuses(evidence):
    _assert_claim_refused(_quoted_approve(evidence))


QUOTED_VERDICT_LINES = {
    "grok-verdict-line": (
        "grok-notools",
        "## FINDINGS\n- none\n\n## VERDICT\ncleared — the probe `waits 3d` passed\n",
    ),
    "headless-label-after-evidence": (
        "claude-fresh",
        "### Evidence\n- Ran: `checks`\nVERDICT: APPROVE — the probe `waits 3d` passed\n",
    ),
    "claude-heading": (
        "claude-fresh",
        "## Verdict: APPROVE `3d`\n\n### Evidence\n- Ran: checks passed\n",
    ),
}


@pytest.mark.parametrize(
    ("backend", "text"), list(QUOTED_VERDICT_LINES.values()), ids=list(QUOTED_VERDICT_LINES)
)
def test_quoted_duration_on_a_verdict_line_still_refuses(backend, text):
    _assert_claim_refused(text, backend)


MALFORMED_QUOTES_IN_EVIDENCE = {
    "unclosed-fence": "- Probes:\n```\nit took 2h\n",
    "unclosed-fence-then-inline-span": "```\n- probe `it took 2h`",
    "unclosed-fence-before-heading": "```\nit took 2h\n### Notes\n- ok",
    "unmatched-backtick": "- Probe: `it took 2h and more",
    "mismatched-run-lengths": "- Probe: ``it took 2h`",
    "unmatched-run-beside-a-span": "- Probe: `` then `it took 2h`",
    "span-across-lines": "- Probe: `it took\n  2h` passed",
    "escaped-backtick": "- Probe: \\`it took 2h\\` passed",
    "backtick-in-backtick-fence-info": "```x`\nit took 2h\n```",
    "fence-info-string": "~~~ `2h`\nok\n~~~",
    "fence-opener-number-joins-first-line": "```text 3\nhours of retries\n```",
    "comment-heading-inside-fence": "```\n# probe\nit took 2h\n```",
    "tagged-bullet-in-evidence": "- [BUG] `a.py:3` — the probe `it took 2h` passes",
    "after-tagged-bullet": "- [BUG] `a.py:3` — wrong\n- probe `it took 2h`",
}


@pytest.mark.parametrize(
    "evidence",
    list(MALFORMED_QUOTES_IN_EVIDENCE.values()),
    ids=list(MALFORMED_QUOTES_IN_EVIDENCE),
)
def test_malformed_or_finding_markup_in_evidence_stays_in_scope(evidence):
    _assert_claim_refused(_quoted_approve(evidence))


@pytest.mark.parametrize(
    "title",
    ["Minor issues", "Evidence (extra)", "Evidence and timing", "Summary", "Notes"],
)
def test_quoted_duration_under_any_other_section_title_still_refuses(title):
    text = _quoted_approve("- Ran: checks passed", minor="- the probe `it took 2h`").replace(
        "### Minor notes", f"### {title}"
    )
    _assert_claim_refused(text)


# Each heading line replaces `### Evidence` or `### Minor notes`, and the
# section under it quotes a duration-shaped input in a backtick span. Only the
# level-three hash heading with a space after the hashes is exempt.
OTHER_HEADING_FORMS = {
    "level-one-evidence": ("evidence", "# Evidence"),
    "level-two-evidence": ("evidence", "## Evidence"),
    "level-four-evidence": ("evidence", "#### Evidence"),
    "level-six-evidence": ("evidence", "###### Evidence"),
    "no-separator-evidence": ("evidence", "###Evidence"),
    "tab-separator-evidence": ("evidence", "###\tEvidence"),
    "four-space-indented-evidence": ("evidence", "    ### Evidence"),
    "glued-closing-hash-evidence": ("evidence", "### Evidence#"),
    "glued-closing-hashes-bold-evidence": ("evidence", "### **Evidence**##"),
    "level-two-minor-notes": ("minor", "## Minor notes"),
    "level-four-minor-notes": ("minor", "#### Minor notes"),
    "no-separator-minor-notes": ("minor", "###Minor notes"),
    "glued-closing-hash-minor-notes": ("minor", "### Minor notes#"),
    "bare-minor-notes-label": ("minor", "MINOR NOTES:"),
    "bold-bare-minor-notes-label": ("minor", "**MINOR NOTES**:"),
}


@pytest.mark.parametrize(
    ("section", "heading"), list(OTHER_HEADING_FORMS.values()), ids=list(OTHER_HEADING_FORMS)
)
def test_quoted_duration_under_another_heading_form_still_refuses(section, heading):
    if section == "evidence":
        text = _quoted_approve("- Probe: `it took 2h`").replace("### Evidence", heading)
    else:
        text = _quoted_approve("- Ran: checks passed", minor="- the probe `it took 2h`")
        text = text.replace("### Minor notes", heading)
    _assert_claim_refused(text)


def test_quoted_duration_under_a_bare_evidence_label_still_refuses():
    # `Evidence:` is not a heading at all, so the line and what follows it stay
    # in the section above, here the verdict's.
    _assert_claim_refused(
        "## Verdict: APPROVE\n\nEvidence:\n- Probe: `it took 2h`\n\n### Issues\n- none\n"
    )


GROK_WITH_EVIDENCE = (
    "## FINDINGS\n- none\n\n## VERDICT\ncleared — no blocking issues.\n\n### Evidence\n{}\n"
)


def test_grok_return_quoting_a_duration_input_in_evidence_parses_without_an_interval():
    _assert_quoted_parses(
        GROK_WITH_EVIDENCE.format("- Probe: `waits 3d/foo.py` is refused as a claim"),
        "grok-notools",
    )


def test_grok_return_with_the_same_token_unquoted_in_evidence_still_refuses():
    _assert_claim_refused(
        GROK_WITH_EVIDENCE.format("- Probe: waits 3d/foo.py is refused as a claim"),
        "grok-notools",
    )


# Neither adapter finds a verdict here, so the forced Jev fallback decides it.
JEV_FALLBACK_WITH_EVIDENCE = "Checked the duration scanner.\n\n### Evidence\n{}\n"


class _ApprovingJev:
    calls = 0

    def decide(self, text: str) -> tuple[str, float]:
        type(self).calls += 1
        return review_phase.APPROVED, 0.95


def _forced_jev_fallback(monkeypatch, evidence: str) -> dict:
    monkeypatch.setattr(_ApprovingJev, "calls", 0)
    monkeypatch.setattr(review_phase, "JevClient", _ApprovingJev)
    envelope = _envelope("claude-fresh", JEV_FALLBACK_WITH_EVIDENCE.format(evidence))
    envelope["adapters"] = {"jev_fallback": True}
    result = review_phase.run_phase(envelope)
    assert _ApprovingJev.calls == 1, "the review did not reach the Jev fallback"
    return result


def test_jev_fallback_quoting_a_duration_input_in_evidence_parses_without_an_interval(
    monkeypatch,
):
    result = _forced_jev_fallback(
        monkeypatch, "- Probe: `waits 3d/foo.py` is refused as a claim"
    )
    assert result["status"] == "success", result["reason"]
    assert result["verdict_source"] == "jev"
    assert "intervals" not in result


def test_jev_fallback_with_the_same_token_unquoted_in_evidence_still_refuses(monkeypatch):
    result = _forced_jev_fallback(monkeypatch, "- Probe: waits 3d/foo.py is refused as a claim")
    assert result["status"] == "failure", "a timing claim parsed without an INTERVAL record"
    assert "review_interval_endpoints_missing" in result["reason"], result["reason"]


QUOTED_INPUT_SCAN_SHAPES = {
    "many-spans": lambda n: "### Evidence\n- " + "`a` " * n,
    "many-run-widths": lambda n: "### Evidence\n- "
    + " ".join("`" * width for width in range(1, int((2 * n) ** 0.5)))
    + " x",
    "many-fences": lambda n: "### Evidence\n" + "```\nit took 2h\n```\n" * (n // 4),
    "unclosed-fences": lambda n: "### Evidence\n" + "```x\n" * (n // 4),
}


@pytest.mark.parametrize("shape", list(QUOTED_INPUT_SCAN_SHAPES))
def test_quoted_input_scan_time_grows_linearly(shape):
    scan = review_phase._has_duration_claim
    shorter = _best_seconds_per_call(scan, QUOTED_INPUT_SCAN_SHAPES[shape](4000))
    assert shorter <= REDOS_LIMIT_SECONDS, (
        f"the duration scan took {shorter:.2f}s on the 4000-unit {shape} review"
    )
    longer = _best_seconds_per_call(scan, QUOTED_INPUT_SCAN_SHAPES[shape](8000))
    assert longer <= REDOS_LIMIT_SECONDS, (
        f"the duration scan took {longer:.2f}s on the 8000-unit {shape} review"
    )
    assert longer <= 3 * shorter, (
        f"the duration scan on {shape}: doubling 4000 to 8000 units took "
        f"{shorter * 1e3:.3f}ms to {longer * 1e3:.3f}ms, more than three times"
    )


# --- EOR47: quoted duration examples are not claims; refusals name the line --


def _eor47_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


EOR47_APPROVE_FIXTURES = [
    "pr-35-claude-empirical-r2.output.md",
    "pr-36-claude-empirical-r1.attempt1.output.md",
    "pr-36-claude-empirical-r1.output.md",
]


@pytest.mark.parametrize("name", EOR47_APPROVE_FIXTURES)
def test_copied_approve_review_quoting_duration_examples_parses(name):
    result = review_phase.run_phase(_envelope("claude-fresh", _eor47_fixture(name)))
    assert result["status"] == "success", result["reason"]
    findings = result["artifacts"]["findings.json"]
    assert findings["verdict"] == "approved"
    assert "intervals" not in result
    assert not any(
        f["severity"] == "error" or f["rule"] == "issue" for f in findings["findings"]
    )


def test_copied_request_changes_review_quoting_duration_examples_parses():
    result = review_phase.run_phase(
        _envelope(
            "claude-fresh",
            _eor47_fixture("pr-36-claude-empirical-r1.attempt2.output.md"),
        )
    )
    assert result["status"] == "success", result["reason"]
    findings = result["artifacts"]["findings.json"]
    assert findings["verdict"] == "changes_requested"
    assert "intervals" not in result
    assert any(f["rule"] == "issue" for f in findings["findings"])


def test_unquoted_took_41_seconds_is_refused():
    text = _claude_approve_with("I ran it and it took 41 seconds")
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing: line 4:" in result["reason"]
    assert "I ran it and it took 41 seconds" in result["reason"]


def test_took_quoted_bare_duration_is_still_a_claim():
    text = _claude_approve_with('The probe took "41 seconds" before returning')
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing: line 4:" in result["reason"]
    assert 'took "41 seconds"' in result["reason"]


def test_quoted_example_phrase_in_summary_parses_without_an_interval():
    text = _claude_approve_with(
        'Measured durations such as "ran for 120 seconds" still count as claims.'
    )
    _assert_quoted_parses(text)


def test_quoted_example_phrase_in_issues_parses_without_an_interval():
    text = (
        "## Verdict: REQUEST CHANGES\n\n"
        '### Issues\n- Probe: "took 41 seconds before the timeout" still claims\n\n'
        "### Evidence\n- Ran: checks passed\n"
    )
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "success", result["reason"]
    assert "intervals" not in result


def test_interval_refusal_truncates_the_named_line_to_200_characters():
    body = "I ran it and it took 41 seconds. " + ("x" * 220)
    result = review_phase.run_phase(_envelope("claude-fresh", _claude_approve_with(body)))
    assert result["status"] == "failure"
    _, _, named = result["reason"].partition("line 4: ")
    assert len(named) == 200
    assert named == body[:200]


def test_quoted_example_scan_finishes_a_100kb_line_within_a_second():
    line = ('x "ran for 120 seconds" y ' * 4000)[:100_000]
    text = _claude_approve_with(line)
    start = time.perf_counter()
    claimed = review_phase._has_duration_claim(text)
    elapsed = time.perf_counter() - start
    assert claimed is False
    assert elapsed <= REDOS_LIMIT_SECONDS, f"quoted-example scan took {elapsed:.2f}s"


def test_quoted_example_scan_finishes_a_100kb_unmatched_quote_within_a_second():
    line = '"' + ("x" * 99_999)
    text = _claude_approve_with(line)
    start = time.perf_counter()
    review_phase._has_duration_claim(text)
    elapsed = time.perf_counter() - start
    assert elapsed <= REDOS_LIMIT_SECONDS, f"unmatched-quote scan took {elapsed:.2f}s"


def test_overlong_quoted_span_does_not_stop_a_later_quoted_example():
    overlong = "x" * 201
    text = _claude_approve_with(
        f'Preamble "{overlong}" then a quoted example "ran for 120 seconds".'
    )
    _assert_quoted_parses(text)


def test_overlong_quoted_span_containing_a_duration_still_refuses():
    interior = ("x" * 201) + " took 41 seconds"
    text = _claude_approve_with(f'"{interior}"')
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]


def test_unmatched_opening_quote_with_a_duration_still_refuses():
    text = _claude_approve_with('"then it took 41 seconds')
    result = review_phase.run_phase(_envelope("claude-fresh", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]


# --- INTERVAL line and body patterns stay linear and keep their grammar ------
#
# `_INTERVAL_LINE` runs on every line of every successfully parsed review.
# `_INTERVAL_PARTS` runs on each body it accepted. A body of `from a,` plus n
# spaces plus `b` used to backtrack about cubically; a line of `INTERVAL: a`,
# n spaces and `b` was quadratic. The cases below are every fixture INTERVAL
# line plus the edge forms, each with the match/groups the earlier patterns
# gave.

OLD_INTERVAL_LINE = re.compile(
    r"^[ \t]*INTERVAL[ \t]*:[ \t]*(.*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
OLD_INTERVAL_PARTS = re.compile(
    r"^from\s+(?P<start_event>.*?),\s*"
    r"(?P<start_source>.*?)\s*to\s+"
    r"(?P<end_event>.*?),\s*"
    r"(?P<end_source>.*?)\s*=\s*"
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
SOL_INTERVAL_NEWLINE_BODY = "from event,ignored,\nsource to end, result = 1s"
SOL_INTERVAL_NEWLINE_GROUPS = ("event,ignored", "source", "end", "result", "1s")
_INTERVAL_FUZZ_ALPHABET = ("a", ",", " ", "\n", "t", "o", "=", "F")
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
        "from a, b to c, d = e\n",
        "from a, b to c, d = e\n\n",
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
        "from event,ignored,\nsource to end, result = 1s",
        "from a, b to foo\n to end, x = 1s",
        "from a, b to c, d = e \n = 1s",
        "from a,\nb to c, d = e",
        "from\na, b to c, d = e",
    ]


def _line_groups(matched):
    if matched is None:
        return None
    return matched.groups()


def _parts_groups(matched):
    if matched is None:
        return None
    return tuple(matched.group(name) for name in review_phase._INTERVAL_FIELDS)


def test_interval_patterns_accept_exactly_the_old_grammar():
    mismatches = []
    for text in _interval_line_cases():
        old = _line_groups(OLD_INTERVAL_LINE.match(text))
        new = _line_groups(review_phase._INTERVAL_LINE.match(text))
        if old != new:
            mismatches.append(("line", text, old, new))
        old_iter = [m.groups() for m in OLD_INTERVAL_LINE.finditer(text)]
        new_iter = [m.groups() for m in review_phase._INTERVAL_LINE.finditer(text)]
        if old_iter != new_iter:
            mismatches.append(("line-finditer", text, old_iter, new_iter))
    for text in _interval_parts_cases():
        old = _parts_groups(OLD_INTERVAL_PARTS.match(text))
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


def test_interval_parts_match_skips_comma_when_newline_blocks_source():
    old = _parts_groups(OLD_INTERVAL_PARTS.match(SOL_INTERVAL_NEWLINE_BODY))
    new = _parts_groups(review_phase._INTERVAL_PARTS.match(SOL_INTERVAL_NEWLINE_BODY))
    assert old == SOL_INTERVAL_NEWLINE_GROUPS
    assert new == old


def _interval_parts_fuzz_bodies() -> list[str]:
    bodies = [SOL_INTERVAL_NEWLINE_BODY]
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


def test_interval_parts_match_old_grammar_on_newline_fuzz():
    mismatches = []
    for text in _interval_parts_fuzz_bodies():
        old = _parts_groups(OLD_INTERVAL_PARTS.match(text))
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
        old = _parts_groups(OLD_INTERVAL_PARTS.match(body))
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
        _parts_groups(OLD_INTERVAL_PARTS.match(small))
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

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
import json
import re
import subprocess
import sys
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


def test_duration_without_interval_is_refused():
    result = review_phase.run_phase(
        _envelope("grok-notools", DURATION_WITHOUT_INTERVAL)
    )
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]
    assert "no INTERVAL record" in result["reason"]
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
    assert "start_event" in result["reason"]
    assert result["artifacts"]["findings.json"] is None


def test_interval_missing_start_source_is_refused():
    text = _timed_grok(
        "INTERVAL: from request start, to result write, "
        "result.md mtime = 41m"
    )
    result = review_phase.run_phase(_envelope("grok-notools", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]
    assert "start_source" in result["reason"]
    assert result["artifacts"]["findings.json"] is None


def test_interval_missing_end_event_is_refused():
    text = _timed_grok(
        "INTERVAL: from request start, events.tsv:14 to , "
        "result.md mtime = 41m"
    )
    result = review_phase.run_phase(_envelope("grok-notools", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]
    assert "end_event" in result["reason"]
    assert result["artifacts"]["findings.json"] is None


def test_interval_missing_end_source_is_refused():
    text = _timed_grok(
        "INTERVAL: from request start, events.tsv:14 to result write,  = 41m"
    )
    result = review_phase.run_phase(_envelope("grok-notools", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]
    assert "end_source" in result["reason"]
    assert result["artifacts"]["findings.json"] is None


def test_interval_missing_duration_is_refused():
    text = _timed_grok(
        "INTERVAL: from request start, events.tsv:14 to result write, "
        "result.md mtime = "
    )
    result = review_phase.run_phase(_envelope("grok-notools", text))
    assert result["status"] == "failure"
    assert "review_interval_endpoints_missing" in result["reason"]
    assert "duration" in result["reason"]
    assert result["artifacts"]["findings.json"] is None

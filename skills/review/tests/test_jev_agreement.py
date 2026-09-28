"""Review-agreement report over existing review result records."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
sys.path.insert(0, str(SKILL_DIR))
import jev_agreement as ja  # noqa: E402


def _result(regex: str, jev: str, confidence, agrees):
    return {
        "phase_result": "v1",
        "status": "success",
        "artifacts": {
            "review.md": "verbatim review return\n",
            "findings.json": {
                "schema": "review-findings/v1",
                "backend": "grok-notools",
                "verdict": regex,
                "findings": [],
            },
        },
        "reason": "parsed",
        "resume_token": None,
        "jev_verdict": jev,
        "confidence": confidence,
        "agrees_with_regex": agrees,
    }


def _write_result(scratch: Path, name: str, value: dict) -> None:
    path = scratch / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _fixture_scratch(tmp_path: Path) -> Path:
    scratch = tmp_path / "scratch"
    _write_result(scratch, "r1-fresh.result.json", _result("approved", "approved", 0.94, True))
    _write_result(
        scratch,
        "nested/r2-grok.result.json",
        _result("changes_requested", "approved", 0.82, False),
    )
    _write_result(
        scratch,
        "r3-fresh.result.json",
        _result("changes_requested", "changes_requested", 0.89, True),
    )
    _write_result(
        scratch,
        "r3-grok.result.json",
        _result("approved", "unavailable", None, None),
    )
    _write_result(
        scratch,
        "flag-off.result.json",
        {
            "phase_result": "v1",
            "status": "success",
            "artifacts": {"findings.json": {"verdict": "approved"}},
        },
    )
    return scratch


def test_fixture_records_report_n_percentage_and_each_disagreement(tmp_path):
    report = ja.build_report(_fixture_scratch(tmp_path))
    text = ja.render_text(report)

    assert len(report.records) == 4
    assert len(report.comparable) == 3
    assert len(report.disagreements) == 1
    assert "returns (n): 4" in text
    assert "agreement: 66.7% (2/3 comparable)" in text
    assert "unavailable: 1" in text
    assert "nested/r2-grok.result.json" in text
    assert "| changes_requested | approved | 0.82 |" in text
    assert "r1-fresh.result.json" not in text
    assert "flag-off.result.json" not in text


def test_write_report_uses_existing_result_records_and_writes_markdown(tmp_path):
    scratch = _fixture_scratch(tmp_path)
    out = tmp_path / "logs" / "jev-agreement.md"

    returned = ja.write_report(scratch, out)

    assert returned == out
    assert out.read_text(encoding="utf-8") == ja.render_text(ja.build_report(scratch))



#!/usr/bin/env python3
"""Close-time agreement receipt for REVIEW phase Jev shadow records.

The review phase already persists one ``*.result.json`` beside each raw review
return.  This module reads those existing records from the session scratch
directory; it creates no second ledger.  Only records carrying ``jev_verdict``
participate, so pre-shadow and flag-off returns do not dilute the measurement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERDICTS = frozenset(("approved", "changes_requested"))
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class AgreementRecord:
    source: str
    regex_verdict: str
    jev_verdict: str
    confidence: float | None
    agrees: bool | None


@dataclass(frozen=True)
class AgreementReport:
    records: tuple[AgreementRecord, ...]
    read_errors: tuple[str, ...] = ()

    @property
    def comparable(self) -> tuple[AgreementRecord, ...]:
        return tuple(record for record in self.records if record.agrees is not None)

    @property
    def disagreements(self) -> tuple[AgreementRecord, ...]:
        return tuple(record for record in self.comparable if not record.agrees)


def _record(source: str, value: Any) -> AgreementRecord | None:
    if not isinstance(value, dict) or "jev_verdict" not in value:
        return None
    artifacts = value.get("artifacts")
    findings = artifacts.get("findings.json") if isinstance(artifacts, dict) else None
    regex_verdict = findings.get("verdict") if isinstance(findings, dict) else None
    jev_verdict = value.get("jev_verdict")
    confidence = value.get("confidence")
    agrees = value.get("agrees_with_regex")

    if regex_verdict not in VERDICTS:
        regex_verdict = UNAVAILABLE
    if jev_verdict not in VERDICTS | {UNAVAILABLE}:
        jev_verdict = UNAVAILABLE
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        confidence = None
    else:
        confidence = float(confidence)
    if not isinstance(agrees, bool):
        agrees = None
    if regex_verdict == UNAVAILABLE or jev_verdict == UNAVAILABLE:
        agrees = None

    return AgreementRecord(source, regex_verdict, jev_verdict, confidence, agrees)


def build_report(scratch: Path) -> AgreementReport:
    """Aggregate existing ``*.result.json`` shadow records under ``scratch``.

    Missing directories and malformed/unreadable JSON degrade into an empty or
    partially populated receipt with explicit read errors; close is never gated.
    """
    root = Path(scratch)
    if not root.is_dir():
        return AgreementReport(())

    records: list[AgreementRecord] = []
    errors: list[str] = []
    for path in sorted(root.rglob("*.result.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"{path.relative_to(root)}: {type(exc).__name__}")
            continue
        record = _record(str(path.relative_to(root)), value)
        if record is not None:
            records.append(record)
    return AgreementReport(tuple(records), tuple(errors))


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_text(report: AgreementReport) -> str:
    comparable = report.comparable
    agreements = sum(record.agrees is True for record in comparable)
    if comparable:
        percentage = 100 * agreements / len(comparable)
        agreement_line = f"agreement: {percentage:.1f}% ({agreements}/{len(comparable)} comparable)"
    else:
        agreement_line = "agreement: unavailable (0 comparable)"
    unavailable = sum(record.jev_verdict == UNAVAILABLE for record in report.records)

    lines = [
        "# Jev agreement",
        "",
        f"returns (n): {len(report.records)}",
        agreement_line,
        f"unavailable: {unavailable}",
        "",
        "## Disagreements",
        "",
    ]
    if not report.disagreements:
        lines.append("None.")
    else:
        lines.extend(
            (
                "| return | regex verdict | Jev verdict | confidence |",
                "|---|---|---|---:|",
            )
        )
        for record in report.disagreements:
            confidence = "unavailable" if record.confidence is None else f"{record.confidence:.2f}"
            lines.append(
                f"| {_cell(record.source)} | {record.regex_verdict} | "
                f"{record.jev_verdict} | {confidence} |"
            )
    if report.read_errors:
        lines.extend(("", "## Read errors", ""))
        lines.extend(f"- {_cell(error)}" for error in report.read_errors)
    return "\n".join(lines) + "\n"


def write_report(scratch: Path, out_path: Path) -> Path:
    report = build_report(scratch)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_text(report), encoding="utf-8")
    return out_path

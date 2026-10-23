#!/usr/bin/env python3
"""The one REVIEW entry point: a run envelope in, a `phase_result: v1` out.

Every review surface — the `review` skill run by hand, the `reviewer` agent,
and a second no-tools caller — returns free text in its own shape. This module
is the single place that text becomes a phase result, so every consumer
reads one contract instead of three:

    phase_result: v1
    status:        success | failure | blocked
    artifacts:     {"review.md": <the return text, verbatim>,
                    "findings.json": <a review-findings/v1 document, or null>}
    reason:        why this status
    resume_token:  null  (a review is single-shot; a re-review is a new run)

`status` is the runner-generic lifecycle outcome. The review verdict
(`approved` / `changes_requested`) lives inside `findings.json`, which is
validated against `findings.schema.json` beside this file: every object is
closed (`additionalProperties: false`) with a complete `required` list, and
each finding carries exactly `file`, `line`, `rule`, `description`,
`severity`.

Every return ends with an INTERVAL record: its last non-blank line is
`INTERVAL: none`, or one `INTERVAL: from <start event>, <start source> to
<end event>, <end source> = <duration>` line per measured interval the verdict
relies on. That record is the only place this phase reads for time; the
adapters never see it, and on success the phase result carries `intervals`
(`[]` for `none`). A return whose record is missing, malformed, missing an
endpoint, contradictory or duplicated is `failure`, with a reason naming the
line (references/structured-interval.md).

With `REVIEW_PHASE_JEV_SHADOW=1`, a successful regex parse also sends
`jev_client.trim_payload(review.md)` through the existing `JevClient` and adds
advisory `jev_verdict`, `confidence`, `agrees_with_regex`, and `jev_reason`
fields to the phase result. `jev_reason` classifies an unavailable outcome and
is null otherwise. The regex verdict remains authoritative. The flag is off by
default; any shadow failure cannot change review status, artifacts, reason, or
verdict.

Backends (one adapter each, chosen by the envelope's `inputs.backend`):
    claude-fresh   the reviewer agent's `## Verdict:` block with `### Issues`
                   / `### Minor notes` (clean: "No bugs or security issues found."
                   inside `### Issues`), the headless `VERDICT:` + `FINDINGS`
                   shape, and the `review` skill's `[BUG]` / `[SECURITY]`
                   bullets (or its "No bugs or security issues found." line)
    grok-notools   `## FINDINGS` bullets (`none` when clean) + `## VERDICT`
                   `<cleared|flagged> — <sentence>`

The adapters normalise a return that a runner already obtained; dispatching
the model (credentials, tools, pools) stays runner-owned, so an envelope
whose `inputs` carry no `return_text` / `return_file` is `blocked`.

USAGE:
    python3 review_phase.py --envelope <envelope.json> [--out <result.json>]
    python3 review_phase.py --from-text <return.md> --backend <name>
                            [--target <what was reviewed>] [--out <result.json>]
    python3 review_phase.py --validate <findings.json>

The envelope is a JSON object with exactly the six run-envelope keys
(`repo/ref`, `inputs`, `constraints`, `harness_root`, `adapters`, `budget`);
it is read, never written. `--from-text` builds that envelope from the flags
and takes the same path. The phase result is printed to stdout as JSON and,
with `--out`, written to that file as well.

EXIT:
    0  status success (or `--validate`: the document conforms)
    1  status failure (or `--validate`: it does not; each error on stderr)
    2  usage, unreadable input, or an unknown backend on the command line
    3  status blocked (the envelope is not something this phase can run)
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import sys
from pathlib import Path

from jev_client import _ANY_HEADING, JevClient, JevClosed, trim_payload

SCHEMA_PATH = Path(__file__).resolve().parent / "findings.schema.json"
SCHEMA_ID = "review-findings/v1"
PHASE_RESULT_VERSION = "v1"
ENVELOPE_KEYS = ("repo/ref", "inputs", "constraints", "harness_root", "adapters", "budget")

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3

APPROVED = "approved"
CHANGES_REQUESTED = "changes_requested"


class ParseError(ValueError):
    """The return text does not carry the shape this backend promises."""


# --- text helpers shared by the adapters -------------------------------------

# A markdown heading, or a bare section label (`FINDINGS:`, `VERDICT: …`) in
# the headless shape that writes no `#` at all. This is the same compiled
# pattern the Jev payload trim uses, so the two can never drift apart; its
# grammar, and why it stays linear on any line, is described in jev_client.py.
_HEADING = _ANY_HEADING


def _heading_title(match: re.Match[str]) -> str:
    return next(group for group in match.groups() if group is not None)
_BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.*\S)\s*$")
# The clean markers under an Issues heading: `none`, `None.`, `None blocking.`,
# `No issues`, `No issues.`, `Nothing to report.`, with an optional leading `- `.
# Each is the whole line; `Nothing to report, but see below.` is prose.
_NONE_BULLET = re.compile(
    r"^(?:[-*]\s+)?`?(?:none(?:\s+blocking)?|no\s+issues|nothing\s+to\s+report)`?\.?$",
    re.IGNORECASE,
)
_CLEAN_SENTINEL = re.compile(r"^\W*No bugs or security issues found\.?\W*$", re.IGNORECASE)


def _is_clean_marker(text: str) -> bool:
    stripped = text.strip()
    return bool(_NONE_BULLET.match(stripped) or _CLEAN_SENTINEL.match(stripped))

_PLACEHOLDER_BULLET = re.compile(r"^<[^>]*>$")
_TAG = re.compile(r"^\[(BUG|SECURITY)\]\s*", re.IGNORECASE)
# A leading `.` (hidden dir, e.g. `.github/…`) or `/` (an absolute path) is one
# more allowed first character; the component after it still needs an
# alnum/underscore start, same as every later `/`-separated component.
_PATH = r"[./]?[A-Za-z0-9_][A-Za-z0-9_.+-]*(?:/[A-Za-z0-9_.+-]+)*"
# `path:42 — text`: the locator the reviewer agent and the review skill lead with.
_LOCATOR = re.compile(rf"^`?({_PATH}):(\d+)`?\s*(?:[—–-]|:)\s*")
_BACKTICKED = re.compile(r"`([^`]+)`")
_PATH_TOKEN = re.compile(rf"^({_PATH})(?::(\d+))?$")
# A severity token anywhere in the bullet, in parens or brackets, overrides
# the rule/section-derived default; the reviewer's own three-tier vocabulary
# (blocker/major/minor) maps onto the schema's error/warning/info.
_SEVERITY_TOKEN = re.compile(
    r"[(\[]\s*(blocker|major|minor|error|warning|info)\s*[)\]]", re.IGNORECASE
)
_SEVERITY_MAP = {
    "blocker": "error",
    "major": "warning",
    "minor": "info",
    "error": "error",
    "warning": "warning",
    "info": "info",
}


def _strip_marks(text: str) -> str:
    return text.strip().strip("*_ ").strip()


def _bullets(lines: list[str], start: int) -> tuple[list[str], bool, int]:
    """Bullets from `start` until the next heading.

    Returns `(real_bullets, unparsed, index)`. `unparsed` is true when the
    section held foreign prose (a non-blank line that is not a bullet, not an
    indented continuation of one, and not exactly a clean marker), whether or
    not real findings sit beside it; or when it held at least one non-blank
    line but none of it survived as a real finding and the section was not an
    explicit clean sentinel (`- none` or `No bugs or security issues found.`).
    The caller should treat that as a parse failure, never a silent zero and
    never a silently dropped paragraph.
    """
    bullets: list[str] = []
    had_content = False
    saw_clean = False
    saw_foreign_prose = False
    i = start
    while i < len(lines):
        line = lines[i]
        if _HEADING.match(line):
            break
        if line.strip():
            had_content = True
        matched = _BULLET.match(line)
        if matched:
            bullets.append(matched.group(1))
        elif bullets and line.startswith((" ", "\t")) and line.strip():
            bullets[-1] = bullets[-1] + " " + line.strip()
        elif line.strip():
            # Any other non-bullet line, before or after a bullet, is prose: a
            # clean marker, or foreign prose that refuses the section (`- none`
            # then a paragraph, or a finding then a paragraph).
            if _is_clean_marker(line):
                saw_clean = True
            else:
                saw_foreign_prose = True
        i += 1
    legit_empty = not saw_foreign_prose and (
        saw_clean or (bool(bullets) and all(_is_clean_marker(b) for b in bullets))
    )
    real = [
        b for b in bullets if not (_is_clean_marker(b) or _PLACEHOLDER_BULLET.match(b))
    ]
    # Foreign prose refuses the section even beside a real finding: accepting
    # the findings would drop that paragraph without a word.
    unparsed = had_content and (saw_foreign_prose or (not real and not legit_empty))
    return real, unparsed, i


def _looks_like_path(token: str) -> bool:
    return bool(_PATH_TOKEN.match(token)) and ("." in token or "/" in token)


def _finding(text: str, rule: str, severity: str) -> dict:
    """One schema-shaped finding from a bullet's text.

    A leading `path:line` locator names the file and line and is dropped
    from the description; otherwise the description is the bullet verbatim
    and the file is the first backticked token that reads as a path.
    """
    file: str | None = None
    line: int | None = None
    description = text
    located = _LOCATOR.match(text)
    if located:
        file, line = located.group(1), int(located.group(2))
        description = text[located.end():]
    else:
        for token in _BACKTICKED.findall(text):
            candidate = _PATH_TOKEN.match(token)
            if candidate and _looks_like_path(candidate.group(1)):
                file = candidate.group(1)
                line = int(candidate.group(2)) if candidate.group(2) else None
                break
    severity_token = _SEVERITY_TOKEN.search(text)
    if severity_token:
        severity = _SEVERITY_MAP[severity_token.group(1).lower()]
    return {
        "file": file,
        "line": line,
        "rule": rule,
        "description": description.strip() or text,
        "severity": severity,
    }


# --- backend adapters --------------------------------------------------------

_GROK_VERDICT = re.compile(r"^`?\*{0,2}(cleared|flagged)\b", re.IGNORECASE)
# The bare `VERDICT: flagged — …` shape carries the value on the heading's
# own line, not the next one — the generic `_HEADING` match discards it.
# The whitespace after the colon and the whitespace after optional `*` marks
# are two runs only when a `*` sits between them, so one long space run is
# never split two ways (the `\s*\**\s*` form it replaces was quadratic).
_GROK_VERDICT_LABEL = re.compile(
    r"^\s*\**VERDICT\**\s*:\s*(?:\*+\s*)?(cleared|flagged)\b", re.IGNORECASE
)


def parse_grok_notools(text: str) -> tuple[str, list[dict]]:
    """`## FINDINGS` bullets + `## VERDICT <cleared|flagged> — <sentence>`."""
    lines = _review_lines(text)
    bullets: list[str] | None = None
    unparsed = False
    verdict: str | None = None
    i = 0
    while i < len(lines):
        heading = _HEADING.match(lines[i])
        if not heading:
            i += 1
            continue
        title = _strip_marks(_heading_title(heading)).upper()
        if title.startswith("FINDINGS"):
            bullets, unparsed, i = _bullets(lines, i + 1)
            continue
        if title.startswith("VERDICT"):
            inline = _GROK_VERDICT_LABEL.match(lines[i])
            if inline:
                verdict = inline.group(1).lower()
            else:
                for candidate in lines[i + 1:]:
                    if candidate.strip():
                        matched = _GROK_VERDICT.match(candidate.strip())
                        if matched:
                            verdict = matched.group(1).lower()
                        break
        i += 1
    if bullets is None:
        raise ParseError("no `## FINDINGS` section in the return")
    if unparsed:
        raise ParseError(
            "the FINDINGS section has content that did not parse as findings "
            "(expected `-`/`*`/`N.`/`N)` items)"
        )
    if verdict is None:
        raise ParseError("no `## VERDICT` line reading cleared or flagged")
    outcome = CHANGES_REQUESTED if verdict == "flagged" else APPROVED
    severity = "error" if outcome == CHANGES_REQUESTED else "warning"
    return outcome, [_finding(b, "finding", severity) for b in bullets]


# These run on every line of a review. The whitespace after the colon and the
# whitespace after an optional `[` or run of `*` marks are two runs only when
# a mark sits between them, so one long space run is never split two ways (the
# `\s*\[?\s*` and `\s*\**\s*` forms they replace were quadratic).
_CLAUDE_VERDICT_HEADING = re.compile(
    r"^\s*#{1,6}\s*\**Verdict\**\s*:\s*(?:\[\s*)?(APPROVE(?:[ _-]WITH[ _-]FINDINGS)?|REQUEST[ _-]CHANGES)",
    re.IGNORECASE,
)
_CLAUDE_VERDICT_LABEL = re.compile(
    r"^\s*\**VERDICT\**\s*:\s*(?:\*+\s*)?(APPROVE(?:[ _-]WITH[ _-]FINDINGS)?|REQUEST[ _-]CHANGES)\b",
    re.IGNORECASE,
)
_CLAUDE_SECTIONS = (
    ("ISSUES", "issue"),
    ("MINOR NOTES", "minor"),
    ("MINOR", "minor"),
    ("FINDINGS", "finding"),
)


def parse_claude_fresh(text: str) -> tuple[str, list[dict]]:
    """The reviewer agent's block, the headless `VERDICT:` shape, or the
    `review` skill's tagged bullets."""
    lines = _review_lines(text)
    verdict: str | None = None
    for line in lines:
        matched = _CLAUDE_VERDICT_HEADING.match(line) or _CLAUDE_VERDICT_LABEL.match(line)
        if matched:
            token = matched.group(1).upper()
            verdict = CHANGES_REQUESTED if token.startswith("REQUEST") else APPROVED
        elif _CLEAN_SENTINEL.match(line):
            verdict = verdict or APPROVED

    collected: list[tuple[str, str]] = []  # (rule, bullet text)
    unparsed_sections: list[str] = []
    i = 0
    while i < len(lines):
        heading = _HEADING.match(lines[i])
        if heading:
            title = _strip_marks(_heading_title(heading)).upper()
            rule = next((r for prefix, r in _CLAUDE_SECTIONS if title.startswith(prefix)), None)
            if rule:
                bullets, unparsed, i = _bullets(lines, i + 1)
                if unparsed:
                    unparsed_sections.append(title)
                collected.extend((rule, b) for b in bullets)
                continue
            i += 1
            continue
        matched = _BULLET.match(lines[i])
        if matched and _TAG.match(matched.group(1)):
            bullets, _, i = _bullets(lines, i)
            collected.extend(("tagged", b) for b in bullets)
            continue
        i += 1

    if unparsed_sections:
        raise ParseError(
            "content that did not parse as findings (expected `-`/`*`/`N.`/`N)` "
            "items) in section(s): " + ", ".join(unparsed_sections)
        )

    findings: list[dict] = []
    for rule, bullet in collected:
        tag = _TAG.match(bullet)
        if tag:
            rule = tag.group(1).lower()
            bullet = bullet[tag.end():]
        findings.append((rule, bullet))
    if verdict is None:
        if any(rule in ("bug", "security") for rule, _ in findings):
            verdict = CHANGES_REQUESTED
        else:
            raise ParseError(
                "no verdict in the return: expected `## Verdict: APPROVE | REQUEST CHANGES`, "
                "a `VERDICT:` line, `[BUG]`/`[SECURITY]` bullets, or "
                "\"No bugs or security issues found.\""
            )
    shaped: list[dict] = []
    for rule, bullet in findings:
        if rule == "minor":
            severity = "info"
        elif rule in ("bug", "security") or verdict == CHANGES_REQUESTED:
            severity = "error"
        else:
            severity = "warning"
        shaped.append(_finding(bullet, rule, severity))
    return verdict, shaped


ADAPTERS = {
    "claude-fresh": parse_claude_fresh,
    "grok-notools": parse_grok_notools,
}


# --- the schema validator ----------------------------------------------------

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value: object, name: str) -> bool:
    expected = _TYPES[name]
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_findings(document: object, schema: dict | None = None) -> list[str]:
    """Errors for `document` against findings.schema.json; empty when it conforms.

    Interprets the keywords that schema uses — type, properties, required,
    additionalProperties, items, enum, const, minimum, minLength — reading
    them from the schema file so the file stays the one source of truth.
    """
    schema = load_schema() if schema is None else schema
    errors: list[str] = []
    _check(document, schema, "$", errors)
    return errors


def _check(value: object, schema: dict, path: str, errors: list[str]) -> None:
    types = schema.get("type")
    if types is not None:
        names = [types] if isinstance(types, str) else list(types)
        if not any(_type_ok(value, n) for n in names):
            errors.append(f"{path}: expected {' or '.join(names)}, got {type(value).__name__}")
            return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must be {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        errors.append(f"{path}: shorter than {schema['minLength']}")
    if isinstance(value, int) and not isinstance(value, bool) and "minimum" in schema:
        if value < schema["minimum"]:
            errors.append(f"{path}: {value} is below the minimum {schema['minimum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing required field {name!r}")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    errors.append(f"{path}: unexpected field {name!r}")
        for name, child in properties.items():
            if name in value:
                _check(value[name], child, f"{path}.{name}", errors)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _check(item, schema["items"], f"{path}[{index}]", errors)


# --- the INTERVAL record ------------------------------------------------------

# Every review ends with an INTERVAL record. Its last non-blank line is
# `INTERVAL: none`, or one `INTERVAL: from <start event>, <start source> to
# <end event>, <end source> = <duration>` line per measured interval the
# verdict relies on, consecutive. The record block is the run of INTERVAL
# lines that ends at the last non-blank line; a blank line ends the run.
# Nothing else in the review is read for a time or for the word INTERVAL, so
# a duration, a commit sha or a quoted INTERVAL example anywhere else never
# matters. `review-findings/v1` is closed, so parsed records live on the
# phase result as `intervals`, not inside findings.json. The grammar and the
# refusals are written out in references/structured-interval.md.
INTERVAL_MISSING = "review_interval_missing"
INTERVAL_MALFORMED = "review_interval_malformed"
INTERVAL_ENDPOINTS_MISSING = "review_interval_endpoints_missing"
INTERVAL_CONTRADICTORY = "review_interval_contradictory"
INTERVAL_DUPLICATE = "review_interval_duplicate"
_INTERVAL_FIELDS = ("start_event", "start_source", "end_event", "end_source", "duration")
# One line. The body ends on a non-space/tab (or is empty), so the trailing
# `[ \t]*` never shares a whitespace run with the body group (the lazy
# `(.*?)[ \t]*$` form it replaces was quadratic on `INTERVAL: a` + n spaces +
# `b`).
_INTERVAL_LINE = re.compile(
    r"^[ \t]*INTERVAL[ \t]*:[ \t]*((?:[^\n]*[^ \t\n])?)[ \t]*$",
    re.IGNORECASE,
)
_INTERVAL_FROM = re.compile(r"^from\s+", re.IGNORECASE)
_INTERVAL_TO = re.compile(r"to\s+", re.IGNORECASE)
_INTERVAL_WS = re.compile(r"\s*")
_INTERVAL_DURATION = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:ms|s|m|h|d)")
_REFUSAL_LINE_MAX = 200


class _IntervalPartsMatch:
    def __init__(self, groups: dict[str, str]) -> None:
        self._groups = groups

    def group(self, name: str) -> str:
        return self._groups[name]


class _IntervalParts:
    """INTERVAL body grammar on one line: from <e>, <s> to <e>, <s> = <d>.

    The first `,` ends the start event, the first `to` plus whitespace after
    it ends the start source, the next `,` ends the end event, and the last
    `=` ends the end source, so an end source may itself hold `=`
    (`a log line k=v`). Each separator is found once, left to right (the last
    `=` from the right), so the walk is linear in the line. Whitespace after
    a separator is skipped; trailing whitespace on the two sources is
    stripped with `str.rstrip()`, the same Unicode set as `\\s`.
    """

    def match(self, text: str) -> _IntervalPartsMatch | None:
        headed = _INTERVAL_FROM.match(text)
        if headed is None:
            return None
        start_comma = text.find(",", headed.end())
        if start_comma < 0:
            return None
        start_source_at = _INTERVAL_WS.match(text, start_comma + 1).end()
        to = _INTERVAL_TO.search(text, start_source_at)
        if to is None:
            return None
        end_comma = text.find(",", to.end())
        if end_comma < 0:
            return None
        end_source_at = _INTERVAL_WS.match(text, end_comma + 1).end()
        equals = text.rfind("=", end_source_at)
        if equals < 0:
            return None
        return _IntervalPartsMatch(
            {
                "start_event": text[headed.end() : start_comma],
                "start_source": text[start_source_at : to.start()].rstrip(),
                "end_event": text[to.end() : end_comma],
                "end_source": text[end_source_at:equals].rstrip(),
                "duration": text[_INTERVAL_WS.match(text, equals + 1).end() :],
            }
        )


_INTERVAL_PARTS = _IntervalParts()


def _named_refusal(rule: str, line_number: int, line: str, detail: str = "") -> str:
    shown = line if len(line) <= _REFUSAL_LINE_MAX else line[:_REFUSAL_LINE_MAX]
    refusal = f"{rule}: line {line_number}: {shown}"
    return f"{refusal} ({detail})" if detail else refusal


def _parse_interval_body(body: str) -> tuple[dict | None, list[str]]:
    matched = _INTERVAL_PARTS.match(body.strip())
    if not matched:
        return None, list(_INTERVAL_FIELDS)
    record = {name: matched.group(name).strip() for name in _INTERVAL_FIELDS}
    missing = [name for name in _INTERVAL_FIELDS if not record[name]]
    if missing:
        return None, missing
    return record, []


def _record_block(lines: list[str]) -> tuple[int, int]:
    """Where the INTERVAL record sits in `lines`, as `(start, end)` indexes.

    `end` is one past the last non-blank line and `start` is the first line
    of the run of INTERVAL lines that ends there, so `start == end` when the
    last non-blank line is not an INTERVAL line.
    """
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    start = end
    while start and _INTERVAL_LINE.match(lines[start - 1]):
        start -= 1
    return start, end


def _review_lines(text: str) -> list[str]:
    """The lines an adapter reads: `text` without its INTERVAL record.

    The record follows the verdict block, so without this a review whose last
    section is Issues or FINDINGS would read the record as prose in that
    section. Only the trailing record is removed; an INTERVAL line anywhere
    else stays and is read like any other line.
    """
    lines = text.splitlines()
    start, end = _record_block(lines)
    return lines[:start] + lines[end:]


def _require_intervals(text: str) -> list[dict]:
    """The measured records of `text`'s INTERVAL record, in order.

    `[]` for `INTERVAL: none`. Raises ParseError naming the first line that
    fails, in text order, when the record is missing, malformed, missing an
    endpoint, contradictory or duplicated.
    """
    lines = text.splitlines()
    start, end = _record_block(lines)
    if start == end:
        last = lines[end - 1] if end else ""
        raise ParseError(_named_refusal(INTERVAL_MISSING, end, last))
    records: list[dict] = []
    seen: set[str] = set()
    saw_none = False
    for index in range(start, end):
        line = lines[index]
        number = index + 1
        body = _INTERVAL_LINE.match(line).group(1)
        if body.lower() == "none":
            if saw_none or records:
                raise ParseError(_named_refusal(INTERVAL_CONTRADICTORY, number, line))
            saw_none = True
            continue
        if not _INTERVAL_FROM.match(body):
            raise ParseError(_named_refusal(INTERVAL_MALFORMED, number, line))
        record, missing = _parse_interval_body(body)
        if missing:
            raise ParseError(
                _named_refusal(
                    INTERVAL_ENDPOINTS_MISSING, number, line, "missing " + ", ".join(missing)
                )
            )
        if not _INTERVAL_DURATION.fullmatch(record["duration"]):
            raise ParseError(
                _named_refusal(
                    INTERVAL_MALFORMED,
                    number,
                    line,
                    "duration is not a number then ms, s, m, h or d",
                )
            )
        if saw_none:
            raise ParseError(_named_refusal(INTERVAL_CONTRADICTORY, number, line))
        key = line.strip()
        if key in seen:
            raise ParseError(_named_refusal(INTERVAL_DUPLICATE, number, line))
        seen.add(key)
        records.append(record)
    return records


def _attach_intervals(result: dict, intervals: list[dict]) -> dict:
    if result["status"] == "success":
        result["intervals"] = intervals
    return result


# --- the phase ---------------------------------------------------------------


def _result(status: str, reason: str, review_md: str | None, findings: dict | None) -> dict:
    return {
        "phase_result": PHASE_RESULT_VERSION,
        "status": status,
        "artifacts": {"review.md": review_md, "findings.json": findings},
        "reason": reason,
        "resume_token": None,
    }


def run_phase(envelope: object) -> dict:
    """Turn one run envelope into a phase result. Never mutates `envelope`."""
    if not isinstance(envelope, dict):
        return _result("blocked", "envelope is not a JSON object", None, None)
    missing = [k for k in ENVELOPE_KEYS if k not in envelope]
    extra = [k for k in envelope if k not in ENVELOPE_KEYS]
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {missing}")
        if extra:
            parts.append(f"unexpected {extra}")
        return _result("blocked", "envelope keys: " + ", ".join(parts), None, None)
    inputs = envelope["inputs"]
    if not isinstance(inputs, dict):
        return _result("blocked", "envelope inputs is not an object", None, None)
    backend = inputs.get("backend")
    if backend not in ADAPTERS:
        return _result(
            "blocked",
            f"unknown backend {backend!r}; known: {', '.join(sorted(ADAPTERS))}",
            None,
            None,
        )
    text = inputs.get("return_text")
    if text is None and inputs.get("return_file"):
        try:
            text = Path(inputs["return_file"]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return _result("blocked", f"cannot read return_file: {exc}", None, None)
    if not isinstance(text, str) or not text.strip():
        return _result(
            "blocked",
            "inputs carry no return_text or return_file; the model dispatch is "
            "runner-owned, so the caller's return must arrive in the envelope",
            None,
            None,
        )
    # The record is checked before any adapter reads the return. A return
    # whose last line is not a record would otherwise reach an adapter with
    # that line still in its last section and fail as an unparsed section, and
    # with the fallback on it would reach the other adapters and Jev first.
    try:
        intervals = _require_intervals(text)
    except ParseError as exc:
        return _result("failure", f"{backend}: {exc}", text, None)
    selected = ADAPTERS[backend]
    try:
        verdict, findings = selected(text)
    except ParseError as exc:
        if not _jev_fallback_enabled(envelope):
            return _result("failure", f"{backend}: {exc}", text, None)
        parsed = _try_other_adapters(backend, text)
        if parsed is None:
            return _consult_jev(backend, text, intervals)
        verdict, findings = parsed
    source = "regex" if _jev_fallback_enabled(envelope) else None
    result = _finish(backend, verdict, findings, text, source=source)
    _attach_intervals(result, intervals)
    if _jev_shadow_enabled() and result["status"] == "success":
        _record_jev_shadow(result, verdict, text)
    return result


def _jev_fallback_enabled(envelope: dict) -> bool:
    adapters = envelope.get("adapters")
    return isinstance(adapters, dict) and adapters.get("jev_fallback") is True


def _try_other_adapters(backend: str, text: str) -> tuple[str, list[dict]] | None:
    for name, adapter in ADAPTERS.items():
        if name == backend:
            continue
        try:
            return adapter(text)
        except ParseError:
            continue
    return None


def _jev_shadow_enabled() -> bool:
    return os.environ.get("REVIEW_PHASE_JEV_SHADOW") == "1"


def _jev_shadow_failure_reason(exc: BaseException) -> str:
    """Return a short, secret-free classification for an unavailable shadow."""
    if isinstance(exc, TimeoutError):
        return "jev: timeout"
    if not isinstance(exc, JevClosed):
        return f"jev: unexpected {type(exc).__name__}"

    message = str(exc)
    if "OP_CONNECT_HOST" in message:
        return "jev: OP_CONNECT_HOST unresolvable"
    if "OP_CONNECT_TOKEN" in message:
        return "jev: OP_CONNECT_TOKEN unresolvable"
    if "could not resolve secret" in message:
        return "jev: could not resolve secret"
    if message == "jev: could not load op-resolve":
        return message
    if message.startswith("jev: Connect request ") and " failed:" in message:
        return "jev: Connect connection failed"
    connect_http_status = re.match(r"jev: Connect returned HTTP (\d{3})\b", message)
    if connect_http_status:
        return f"jev: Connect HTTP {connect_http_status.group(1)}"
    if message.startswith("jev: vault not found:"):
        return "jev: vault not found"
    if message.startswith("jev: item not found:"):
        return "jev: item not found"
    if message == "jev: malformed response":
        return message
    if message == "jev: request failed":
        return message
    if message == "jev: timeout":
        return message
    if message.startswith("jev: choice "):
        return "jev: choice is not APPROVE or REQUEST_CHANGES"
    if message.startswith("jev: confidence ") and " below floor " in message:
        return "jev: confidence below floor"
    if message.startswith("jev: confidence "):
        return "jev: invalid confidence"
    http_status = re.match(r"jev: HTTP (\d{3})\b", message)
    if http_status:
        return f"jev: HTTP {http_status.group(1)}"
    return "jev: unavailable (JevClosed)"


def _record_jev_shadow(result: dict, regex_verdict: str, text: str) -> None:
    """Add advisory Jev metadata without changing the regex phase result."""
    jev_reason = None
    try:
        payload = trim_payload(text)
        client = JevClient()
        jev_verdict, confidence = client.decide(payload)
        if jev_verdict not in (APPROVED, CHANGES_REQUESTED):
            raise ValueError("unknown Jev verdict")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not math.isfinite(confidence)
        ):
            raise ValueError("invalid Jev confidence")
    except (JevClosed, TimeoutError) as exc:
        jev_verdict, confidence = "unavailable", None
        jev_reason = _jev_shadow_failure_reason(exc)
    except Exception as exc:
        jev_verdict, confidence = "unavailable", None
        jev_reason = _jev_shadow_failure_reason(exc)

    result["jev_verdict"] = jev_verdict
    result["confidence"] = confidence
    result["agrees_with_regex"] = (
        jev_verdict == regex_verdict if jev_verdict != "unavailable" else None
    )
    result["jev_reason"] = jev_reason


def _consult_jev(backend: str, text: str, intervals: list[dict]) -> dict:
    """Ask Jev for the verdict; `intervals` is the record run_phase checked."""
    try:
        client = JevClient()
        verdict, confidence = client.decide(text)
    except JevClosed as exc:
        return _result("failure", str(exc), text, None)
    except Exception:
        return _result("failure", "jev: request failed", text, None)
    result = _finish(backend, verdict, [], text, source="jev", confidence=confidence)
    return _attach_intervals(result, intervals)


def _finish(
    backend: str,
    verdict: str,
    findings: list[dict],
    text: str,
    *,
    source: str | None = None,
    confidence: float | None = None,
) -> dict:
    document = {
        "schema": SCHEMA_ID,
        "backend": backend,
        "verdict": verdict,
        "findings": findings,
    }
    errors = validate_findings(document)
    if errors:
        return _result(
            "failure",
            f"{backend}: findings did not validate: {'; '.join(errors)}",
            text,
            None,
        )
    summary = f"{backend}: {verdict}, {len(findings)} finding(s)"
    result = _result("success", summary, text, copy.deepcopy(document))
    if source is not None:
        result["verdict_source"] = source
    if confidence is not None:
        result["confidence"] = confidence
    return result


# --- CLI ---------------------------------------------------------------------


def _usage_error(message: str) -> int:
    sys.stderr.write(f"review_phase: {message}\n")
    return EXIT_USAGE


def _emit(result: dict, out: str | None) -> int:
    rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if out:
        try:
            Path(out).write_text(rendered, encoding="utf-8")
        except OSError as exc:
            return _usage_error(f"cannot write {out}: {exc}")
    sys.stdout.write(rendered)
    return {"success": EXIT_SUCCESS, "failure": EXIT_FAILURE, "blocked": EXIT_BLOCKED}[result["status"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="review_phase.py",
        description="Run the REVIEW phase on a run envelope, or validate a findings.json.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--envelope", metavar="FILE", help="run envelope JSON (six keys)")
    mode.add_argument("--from-text", metavar="FILE", help="a caller's raw return text")
    mode.add_argument("--validate", metavar="FILE", help="a findings.json to check")
    parser.add_argument("--backend", choices=sorted(ADAPTERS), help="adapter for --from-text")
    parser.add_argument("--target", default="", help="what was reviewed, for --from-text")
    parser.add_argument("--out", metavar="FILE", help="also write the phase result here")
    args = parser.parse_args(argv)

    if args.validate:
        try:
            document = json.loads(Path(args.validate).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return _usage_error(f"cannot read {args.validate}: {exc}")
        errors = validate_findings(document)
        for error in errors:
            sys.stderr.write(f"review_phase: {error}\n")
        return EXIT_FAILURE if errors else EXIT_SUCCESS

    if args.from_text:
        if not args.backend:
            return _usage_error("--from-text needs --backend")
        try:
            text = Path(args.from_text).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return _usage_error(f"cannot read {args.from_text}: {exc}")
        envelope = {
            "repo/ref": args.target,
            "inputs": {"target": args.target, "backend": args.backend, "brief": "", "return_text": text},
            "constraints": {},
            "harness_root": None,
            "adapters": {},
            "budget": {},
        }
        return _emit(run_phase(envelope), args.out)

    try:
        envelope = json.loads(Path(args.envelope).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _usage_error(f"cannot read {args.envelope}: {exc}")
    return _emit(run_phase(envelope), args.out)


if __name__ == "__main__":
    raise SystemExit(main())

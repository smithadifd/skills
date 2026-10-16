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
    omp            the reply of an `omp -p --model <id> --thinking=<level>`
                   review: the reviewer agent's own `## Verdict:` block, which
                   must carry an explicit verdict line. The envelope's
                   `inputs.model` and `inputs.thinking` name the served model
                   and thinking level; both are required, and the phase
                   result carries them as `attribution` with the lane name

The adapters normalise a return that a runner already obtained; dispatching
the model (credentials, tools, pools) stays runner-owned, so an envelope
whose `inputs` carry no `return_text` / `return_file` is `blocked`. The
same holds for the omp model and thinking level: the runner chose them, so
they arrive as envelope parameters and are only echoed back here.

USAGE:
    python3 review_phase.py --envelope <envelope.json> [--out <result.json>]
    python3 review_phase.py --from-text <return.md> --backend <name>
                            [--target <what was reviewed>] [--out <result.json>]
                            [--model <id> --thinking <level>]   (omp only)
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

from jev_client import JevClient, JevClosed, trim_payload

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
# the headless shape that writes no `#` at all.
_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*(.+?)\s*#*"
    r"|\**(FINDINGS|ISSUES|MINOR NOTES|VERDICT|ANALYSIS|SUMMARY|NOTES|RECOMMENDATION)\**\s*:.*"
    r"|\**(FINDINGS|ISSUES|MINOR NOTES|VERDICT|ANALYSIS|SUMMARY|NOTES|RECOMMENDATION)\**"
    r")\s*$",
    re.IGNORECASE,
)


def _heading_title(match: re.Match[str]) -> str:
    return next(group for group in match.groups() if group is not None)
_BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.*\S)\s*$")
_NONE_BULLET = re.compile(r"^`?none`?\.?$", re.IGNORECASE)
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
    section held at least one non-blank line but none of it survived as a
    real finding and the section was not an explicit clean sentinel
    (`- none` or `No bugs or security issues found.`) — the caller should
    treat that as a parse failure, never a silent zero.
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
        elif line.strip() and not bullets:
            if _is_clean_marker(line):
                saw_clean = True
            else:
                saw_foreign_prose = True
            i += 1
            continue
        i += 1
    legit_empty = (saw_clean and not saw_foreign_prose) or (
        bool(bullets) and all(_is_clean_marker(b) for b in bullets)
    )
    real = [
        b for b in bullets if not (_is_clean_marker(b) or _PLACEHOLDER_BULLET.match(b))
    ]
    unparsed = had_content and not real and not legit_empty
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
_GROK_VERDICT_LABEL = re.compile(r"^\s*\**VERDICT\**\s*:\s*\**\s*(cleared|flagged)\b", re.IGNORECASE)


def parse_grok_notools(text: str) -> tuple[str, list[dict]]:
    """`## FINDINGS` bullets + `## VERDICT <cleared|flagged> — <sentence>`."""
    lines = text.splitlines()
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


_CLAUDE_VERDICT_HEADING = re.compile(
    r"^\s*#{1,6}\s*\**Verdict\**\s*:\s*\[?\s*(APPROVE(?:[ _-]WITH[ _-]FINDINGS)?|REQUEST[ _-]CHANGES)",
    re.IGNORECASE,
)
_CLAUDE_VERDICT_LABEL = re.compile(
    r"^\s*\**VERDICT\**\s*:\s*\**\s*(APPROVE(?:[ _-]WITH[ _-]FINDINGS)?|REQUEST[ _-]CHANGES)\b",
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
    lines = text.splitlines()
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


# The reviewer agent's verdict heading, exactly as its output format writes it.
# Stricter than the claude-fresh patterns on purpose: a bare `VERDICT:` label,
# a different token (`APPROVED`) or extra text on the line is not this agent's
# block, so it does not count as the verdict line.
_OMP_VERDICT_LINE = re.compile(r"## Verdict: (?:APPROVE|REQUEST CHANGES)[ \t]*")


def parse_omp(text: str) -> tuple[str, list[dict]]:
    """An omp review reply: the reviewer agent's block, verdict line required.

    The reviewer agent always writes `## Verdict: APPROVE | REQUEST CHANGES`,
    so a reply without that line (empty, cut off, or some other shape) is a
    parse failure rather than an approval inferred from the rest of the text.
    """
    lines = text.splitlines()
    if not any(_OMP_VERDICT_LINE.fullmatch(l) for l in lines):
        raise ParseError(
            "no verdict line in the omp reply: expected "
            "`## Verdict: APPROVE | REQUEST CHANGES`"
        )
    return parse_claude_fresh(text)


ADAPTERS = {
    "claude-fresh": parse_claude_fresh,
    "grok-notools": parse_grok_notools,
    "omp": parse_omp,
}

# Backends whose phase result names the lane and the served model. The values
# are the envelope `inputs` keys that must carry them.
OMP_LANE = "omp"
OMP_PARAMETERS = ("model", "thinking")
# The thinking levels `omp --thinking` accepts.
OMP_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "auto")
# A model id as a command-line value: one token, no spaces or line breaks.
_OMP_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*")


def omp_attribution(model: object, thinking: object) -> dict:
    """The attribution an omp review carries, from the runner's own parameters.

    Raises ValueError when the model is missing or not a single token, or the
    thinking level is not one omp accepts; nothing here supplies a default, so
    the record always names what the runner actually asked for.
    """
    if not isinstance(model, str) or not _OMP_MODEL.fullmatch(model):
        raise ValueError(
            f"omp needs inputs.model as a single non-empty token, got {model!r}"
        )
    if not isinstance(thinking, str) or thinking not in OMP_THINKING_LEVELS:
        raise ValueError(
            "omp needs inputs.thinking as one of "
            f"{', '.join(OMP_THINKING_LEVELS)}, got {thinking!r}"
        )
    return {"lane": OMP_LANE, "model": model, "thinking": thinking}


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


# --- measured-interval records ----------------------------------------------

# A review verdict that relies on a measured interval must name both physical
# endpoints. `review-findings/v1` is closed, so parsed records live on the
# phase result as `intervals`, not inside findings.json.
INTERVAL_ENDPOINTS_MISSING = "review_interval_endpoints_missing"
_INTERVAL_FIELDS = ("start_event", "start_source", "end_event", "end_source", "duration")
_INTERVAL_LINE = re.compile(r"^[ \t]*INTERVAL[ \t]*:[ \t]*(.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_INTERVAL_PARTS = re.compile(
    r"^from\s+(?P<start_event>.*?),\s*"
    r"(?P<start_source>.*?)\s*to\s+"
    r"(?P<end_event>.*?),\s*"
    r"(?P<end_source>.*?)\s*=\s*"
    r"(?P<duration>.*)$",
    re.IGNORECASE,
)
# Conservative detector: a number followed by a time unit, word-bounded.
# Compact attached units omit `s` so a code timeout like `900s` is not a
# measured-interval claim; `41m` / `2h` / `3d` / `500ms` still are. Spaced
# `min` is omitted for the same reason (`15 min` in a finding about a bound).
_DURATION_CLAIM = re.compile(
    r"(?<![\w.])(?:"
    r"\d+(?:\.\d+)?(?:ms|[mhd])"
    r"|"
    r"\d+(?:\.\d+)?\s+(?:milliseconds|seconds|second|minutes|minute|"
    r"hours|hour|days|day|ms|secs|sec|mins|hrs|hr|[smhd])"
    r")\b",
    re.IGNORECASE,
)


def _text_for_duration_scan(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not _INTERVAL_LINE.match(line)
    )


def _has_duration_claim(text: str) -> bool:
    return _DURATION_CLAIM.search(_text_for_duration_scan(text)) is not None


def _parse_interval_body(body: str) -> tuple[dict | None, list[str]]:
    matched = _INTERVAL_PARTS.match(body.strip())
    if not matched:
        return None, list(_INTERVAL_FIELDS)
    start_event = matched.group("start_event").strip()
    start_source = matched.group("start_source").strip()
    end_event = matched.group("end_event").strip()
    end_source = matched.group("end_source").strip()
    duration = matched.group("duration").strip()
    missing: list[str] = []
    if not start_event:
        missing.append("start_event")
    if not start_source:
        missing.append("start_source")
    if not end_event:
        missing.append("end_event")
    if not end_source:
        missing.append("end_source")
    if not duration:
        missing.append("duration")
    if missing:
        return None, missing
    return {
        "start_event": start_event,
        "start_source": start_source,
        "end_event": end_event,
        "end_source": end_source,
        "duration": duration,
    }, []


def _collect_intervals(text: str) -> tuple[list[dict], list[list[str]]]:
    complete: list[dict] = []
    incomplete: list[list[str]] = []
    for match in _INTERVAL_LINE.finditer(text):
        record, missing = _parse_interval_body(match.group(1))
        if missing:
            incomplete.append(missing)
        else:
            complete.append(record)
    return complete, incomplete


def _require_intervals(text: str) -> list[dict]:
    complete, incomplete = _collect_intervals(text)
    if not _has_duration_claim(text):
        return complete
    if complete:
        return complete
    if incomplete:
        raise ParseError(
            f"{INTERVAL_ENDPOINTS_MISSING}: missing {', '.join(incomplete[0])}"
        )
    raise ParseError(f"{INTERVAL_ENDPOINTS_MISSING}: no INTERVAL record")


def _attach_intervals(result: dict, intervals: list[dict]) -> dict:
    if result["status"] == "success" and intervals:
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
    attribution = None
    if backend == OMP_LANE:
        try:
            attribution = omp_attribution(*(inputs.get(k) for k in OMP_PARAMETERS))
        except ValueError as exc:
            return _result("blocked", str(exc), None, None)
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
    result = _parse_return(envelope, backend, text)
    if attribution is not None:
        result["attribution"] = attribution
    return result


def _parse_return(envelope: dict, backend: str, text: str) -> dict:
    selected = ADAPTERS[backend]
    try:
        verdict, findings = selected(text)
    except ParseError as exc:
        # An omp reply without the reviewer agent's verdict line is a failure
        # even with the fallback on: another adapter or Jev would infer a
        # verdict the reviewer never wrote.
        if backend == OMP_LANE or not _jev_fallback_enabled(envelope):
            return _result("failure", f"{backend}: {exc}", text, None)
        parsed = _try_other_adapters(backend, text)
        if parsed is None:
            return _consult_jev(backend, text)
        verdict, findings = parsed
    try:
        intervals = _require_intervals(text)
    except ParseError as exc:
        return _result("failure", f"{backend}: {exc}", text, None)
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


def _consult_jev(backend: str, text: str) -> dict:
    try:
        client = JevClient()
        verdict, confidence = client.decide(text)
    except JevClosed as exc:
        return _result("failure", str(exc), text, None)
    except Exception:
        return _result("failure", "jev: request failed", text, None)
    try:
        intervals = _require_intervals(text)
    except ParseError as exc:
        return _result("failure", f"{backend}: {exc}", text, None)
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
    parser.add_argument("--model", help="for --backend omp: the model id the runner served")
    parser.add_argument("--thinking", help="for --backend omp: the thinking level the runner set")
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
        if args.backend == OMP_LANE:
            envelope["inputs"]["model"] = args.model
            envelope["inputs"]["thinking"] = args.thinking
        return _emit(run_phase(envelope), args.out)

    try:
        envelope = json.loads(Path(args.envelope).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _usage_error(f"cannot read {args.envelope}: {exc}")
    return _emit(run_phase(envelope), args.out)


if __name__ == "__main__":
    raise SystemExit(main())

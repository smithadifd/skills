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
_DURATION_TOKEN = (
    r"(?:"
    r"\d+(?:\.\d+)?(?:ms|[mhd])"
    r"|"
    r"\d+(?:\.\d+)?\s+(?:milliseconds|seconds|second|minutes|minute|"
    r"hours|hour|days|day|ms|secs|sec|mins|hrs|hr|[smhd])"
    r")"
)
_DURATION_CLAIM = re.compile(rf"(?<![\w.]){_DURATION_TOKEN}\b", re.IGNORECASE)
# Emphasis underscores around a duration (`_3h_`, `__3h__`, `_3 hours_`) are
# word characters, so they would hide the claim's word boundary. A run of
# underscores on either side of a duration token is read as a boundary when
# the whole run sits at a word edge: nothing word-like (or a `.`) before the
# leading run, nothing word-like after the trailing run. Those underscores
# become spaces in the scan text. An identifier keeps its underscores and
# stays a non-claim, because a letter or digit touches the run
# (`retry_3h_window`, `foo_3h`, `_3h_window`, `3h_window`).
_EMPHASISED_DURATION = re.compile(
    rf"(?<![\w.])(_*)({_DURATION_TOKEN})(_*)(?!\w)", re.IGNORECASE
)


def _unwrap_emphasised_duration(match: re.Match[str]) -> str:
    return " " * len(match.group(1)) + match.group(2) + " " * len(match.group(3))


# Section and file references that merely look like a duration: `§3d`,
# `section 3d`, `foo.py:12d`, and tokens that are paths (`docs/3d/x.md`; a
# duration-shaped directory later in the path is still just a path).
# A section id followed by a duration unit word ("section 5 minutes") is a
# real claim, not a reference, so the id is not erased in that case.
_UNIT_WORD = (
    r"(?:milliseconds|seconds?|minutes?|hours?|days?|weeks?|"
    r"ms|secs?|mins?|hrs?|wks?|[smhd])\b"
)
_NOT_A_CLAIM = rf"(?![\w.]|\s+{_UNIT_WORD})"
_SECTION_REFERENCE = re.compile(
    rf"§\s*\d[\w.]*{_NOT_A_CLAIM}"
    rf"|\bsections?\s+\d[\w.]*{_NOT_A_CLAIM}",
    re.IGNORECASE,
)
# A file:line reference (`foo.py:12`, `src/pkg/foo.py:7m`) is a run of path
# characters that ends in `<name>.<ext>` (the name holding a letter or `_`, the
# extension starting with a letter), then `:` and a line number. It is found by
# reading each run of path characters once (`_next_file_line`), never by one
# regex with nested quantifiers over the run: that regex tried every split of a
# long token and took seconds on a 4000-character hash, blob or minified line.
_PATH_RUN = re.compile(r"[\w./+-]+")
_LINE_NUMBER = re.compile(r":(\d\w*)")
_EXTENSION = re.compile(r"\.[A-Za-z]\w*", re.IGNORECASE)
_NAME_LETTER = re.compile(r"[A-Za-z_]", re.IGNORECASE)
# Only the FIRST lexical component of a path-like token can be a claim glued
# to it ("waits 3d/foo.py"), read from the token's one normalised form
# (`_reference_form`: wrappers, then a `?query` / `#fragment` suffix, then
# sentence punctuation stripped). That component is tested with the
# formatting wrappers on each side of it stripped too (`_first_component`),
# so a component wrapped on both sides (`**3h**/a.py`, `` `41m`/x.md ``) is
# read as `3h` / `41m`. When it is a duration ("3d", "days", "2.5h", the
# `41m` of `41m?x/y.md`), the token stays visible to the scan with its outer
# formatting wrappers (the opening delimiters leading, the closing ones and
# trailing punctuation trailing) turned into spaces, so the scan sees a word
# boundary before the claim (`_3d/foo.py_` is read as ` 3d/foo.py `).
# Underscores left right after the duration (`3h_/a.py`) are read as a
# boundary by `_EMPHASISED_DURATION`. Later components never make a token a
# claim (`docs/3d/x.md` is just a path). Only the scan text is rewritten,
# never the review artifact.
_DURATION_COMPONENT = re.compile(
    r"^(?:\d+(?:\.\d+)?)?(?:milliseconds|seconds?|minutes?|hours?|days?|weeks?|"
    r"ms|secs?|mins?|hrs?|wks?|[smhd])$",
    re.IGNORECASE,
)
# The `(...)` target of a Markdown link; the label stays visible to the scan.
# A `](` whose run never closes is matched too and kept as it is, so the scan
# moves past it once instead of re-reading it from every later `](` in it.
_LINK_TARGET = re.compile(r"(?<=\])\([^)\s]*\)?")
_PATH_PREFIX = re.compile(r"^(?:/|\./|\.\./|~/)")
_FILE_EXTENSION = re.compile(r"\.[A-Za-z]\w*$")
_OPENING_WRAPPERS = "\"'`*_([{<"
_CLOSING_WRAPPERS = _OPENING_WRAPPERS + ".,;:)]}>"
# The formatting wrappers that can sit on either side of a path's first
# component, alone or composed (`**3h**`, `_~~3h~~_`): emphasis and
# strikethrough marks, quotes, backticks, braces and angle brackets. Round and
# square brackets are not among them: the path-token pass never lets one into
# a token before its first `/`, and a file:line run holds none.
_FORMATTING_WRAPPERS = "\"'`*_~{}<>"
# Sentence punctuation that can close a path (`see docs/x.md?`).
_SENTENCE_END = "?!"
# A URI scheme (RFC 3986: a letter, then letters, digits, `+`, `-`, `.`) opening
# the token, as in a bare URL or a `<...>` autolink. A scheme cannot start with
# a digit, so a duration glued in front of a URL never reads as one.
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


_UNIT_AFTER = re.compile(rf"\s+{_UNIT_WORD}", re.IGNORECASE)


def _first_component(form: str) -> str:
    """The first `/`-component of `form`, formatting wrappers stripped from each side.

    This is the text tested against `_DURATION_COMPONENT`: `**3h**/a.py`,
    `` `41m`/x.md `` and `"3h"/a.py` all give a duration, `docs/3d/x.md` gives
    `docs`.
    """
    return form.split("/", 1)[0].strip(_FORMATTING_WRAPPERS)


def _blank_link_target(match: re.Match[str]) -> str:
    target = match.group(0)
    return "()" if target.endswith(")") else target


def _blank_file_line(line: str, start: int, end: int, number: str) -> str:
    """What the scan should see instead of the file:line reference `line[start:end]`."""
    token = line[start:end]
    # A file:line reference follows the same path-token rule: only the first
    # `/`-component can be a claim. When it is a duration the whole token stays
    # visible, for the path-token pass to handle (`3d/foo.py:12`).
    if "/" in token and _DURATION_COMPONENT.match(_first_component(token)):
        return token
    # A file:line reference followed by a spaced duration unit word is a claim
    # (`x.y:3 days`): only the reference is erased, the number stays.
    if _UNIT_AFTER.match(line, end):
        return " " + number
    return " "


def _ends_in_file_name(line: str, start: int, end: int) -> bool:
    """Whether the path run `line[start:end]` ends in `<name>.<ext>`."""
    mark = max(line.rfind(separator, start, end) for separator in "./+-")
    if mark < start or line[mark] != "." or not _EXTENSION.fullmatch(line, mark, end):
        return False
    name_start = max(start, line.rfind(".", start, mark) + 1, line.rfind("/", start, mark) + 1)
    return _NAME_LETTER.search(line, name_start, mark) is not None


def _next_file_line(line: str, pos: int) -> tuple[int, int, str] | None:
    """The leftmost file:line reference at or after `pos`: (start, end, line number).

    A run that is not a file:line reference from its first position is not one
    from any later position either (the name and extension tests only see less
    of it), so each run is read once and the walk is linear in the line.
    """
    while True:
        run = _PATH_RUN.search(line, pos)
        if run is None:
            return None
        start, end = run.span()
        number = _LINE_NUMBER.match(line, end)
        if number and _ends_in_file_name(line, start, end):
            return start, number.end(), number.group(1)
        pos = end


def _blank_references(line: str) -> str:
    """`line` with each section and file:line reference blanked for the scan.

    The references are taken leftmost first and never overlap, the way one
    `re.sub` over both kinds would take them; each kind's next match is looked
    up again only once the previous replacement has passed its start.
    """
    pieces: list[str] = []
    pos = 0
    section = _SECTION_REFERENCE.search(line)
    file_line = _next_file_line(line, 0)
    while section is not None or file_line is not None:
        if file_line is None or (section is not None and section.start() < file_line[0]):
            start, end, replacement = section.start(), section.end(), " "
        else:
            start, end, number = file_line
            replacement = _blank_file_line(line, start, end, number)
        pieces.append(line[pos:start])
        pieces.append(replacement)
        pos = end
        if section is not None and section.start() < pos:
            section = _SECTION_REFERENCE.search(line, pos)
        if file_line is not None and file_line[0] < pos:
            file_line = _next_file_line(line, pos)
    pieces.append(line[pos:])
    return "".join(pieces)


# A path-like token is judged one piece at a time, split at the delimiters that
# can glue a claim to a path (`41m:/x/y.md`, `3h;src/a.py`, `41m,docs/x.md`).
# A `:` that opens `//` is a URL scheme and is not a split point.
_PATH_DELIMITERS = re.compile(r"([;,]+|:+(?!//))")


def _blank_path_token(match: re.Match[str]) -> str:
    pieces = _PATH_DELIMITERS.split(match.group(0))
    # Odd entries are the delimiters themselves; they are kept as they are.
    return "".join(
        piece if index % 2 else _blank_path_piece(piece)
        for index, piece in enumerate(pieces)
    )


def _reference_form(token: str) -> str:
    """The one normalised form that every check on a path piece reads.

    The opening and closing Markdown wrappers come off first (`**docs/x.md**`,
    `<./docs/notes>`), then a `?query` or `#fragment` suffix
    (`docs/x.md?view=all`, and `41m?x/y.md` reads as `41m`), then trailing
    sentence punctuation (`docs/x.md#top.`).
    """
    form = token.lstrip(_OPENING_WRAPPERS).rstrip(_CLOSING_WRAPPERS)
    form = form.split("?", 1)[0].split("#", 1)[0]
    return form.rstrip(_CLOSING_WRAPPERS + _SENTENCE_END)


def _blank_path_piece(token: str) -> str:
    form = _reference_form(token)
    # Fail closed: a duration-shaped FIRST component of the normalised form,
    # however the piece or that component is wrapped or suffixed, keeps the
    # piece visible to the scan. Its outer wrappers become spaces so the claim
    # has a word boundary in front of it.
    if _DURATION_COMPONENT.match(_first_component(form)):
        unwrapped = token.lstrip(_OPENING_WRAPPERS)
        core = unwrapped.rstrip(_CLOSING_WRAPPERS)
        leading = " " * (len(token) - len(unwrapped))
        trailing = " " * (len(unwrapped) - len(core))
        return leading + core + trailing
    # A piece with no `/` is not a path; it stays visible (`41m` in `41m,x/y`).
    if "/" not in token:
        return token
    return " " if _is_path_form(form) else token


def _is_path_form(form: str) -> bool:
    """Whether the normalised form of a piece is clearly a path.

    That is an explicit path prefix, a URI scheme, or a file extension on the
    last component (`**./docs/notes**`, `docs/x.md?view=all`, `docs/x.md?`).
    A plain `word/word` such as `days/run` or `3d/day` is a rate, so a
    duration claim in front of it ("3 days/run") still counts.
    """
    return bool(
        _PATH_PREFIX.match(form)
        or _URI_SCHEME.match(form)
        or _FILE_EXTENSION.search(form.rsplit("/", 1)[-1])
    )


# A path-like token: a run holding a `/`. The lookbehind lets it start only
# where a run starts, the one place the scan can ever match it (a start later
# in the same run needs the same `/` and fails the same way); without it every
# start inside a long run with no `/` re-read the rest of the run.
_PATH_TOKEN_IN_TEXT = re.compile(r"(?<![^\s/\[\]()])[^\s/\[\]()]*/[^\s\])]*")


# A whitespace-delimited token, read whole before the path-token pass splits
# it at brackets. Without this pass, `docs/(_3h_)/x.md` became the path
# token `docs/(_3h_` (no extension, so kept) and the path `/x.md`, and the
# later component `_3h_` stayed visible as a claim.
_WHITESPACE_TOKEN = re.compile(r"\S+")
# The brackets a path's later components may hold, each closer mapped to the
# opener it pairs with.
_BRACKET = re.compile(r"[\[\]()]")
_OPENERS = "(["
_OPENER_OF = {")": "(", "]": "["}


def _shield_later_components(piece: str, line_number: bool) -> str:
    """`piece` with the later components of each recognised path in it blanked.

    Only a path's first component can be a claim, so this pass blanks a
    path's later components and nothing else: the span from its first `/` to
    its end. It never blanks the first component (the text before that `/`)
    or anything in front of it, so `note(3h/run.md`, `x)3h/run.md` and
    `took(3h)/run.md` keep their `3h` for the passes that read it as a claim.

    A path runs from its first `/` while the brackets after that `/` pair up
    (`docs/(3h)/x.md`, `docs/_(3h)_/x.md`). A closer with no opener to pair
    with ends it there, and the scan looks for the next path from that closer
    on (`(docs/a.md)3h/b.md`: the path is `docs/a.md`, and `3h/b.md` after
    the `)` is a path of its own whose `3h` stays). An opener left unpaired
    ends the path just before it (`./docs/(3h`), so neither an unpaired
    bracket nor the text after it is ever blanked as a later component.

    A path is recognised when its text, from the start of the piece or from
    the closer that ended the path before it, is clearly a path
    (`_is_path_form` of `_reference_form`). Anything else is left as it is,
    for the passes after this one.

    When a `:` and a line number follow the piece (`line_number`) and the
    path that runs to the piece's end has a file name last, that name stays,
    so the file:line pass still reads `foo.py:7m` as a reference (and
    `x.md:3 days` as a claim).
    """
    parts: list[str] = []
    kept = 0
    start = 0
    slash = piece.find("/")
    while slash >= 0:
        openers: list[int] = []
        resume = len(piece)
        for bracket in _BRACKET.finditer(piece, slash):
            mark = bracket.group()
            if mark in _OPENERS:
                openers.append(bracket.start())
            elif openers and piece[openers[-1]] == _OPENER_OF[mark]:
                openers.pop()
            else:
                resume = bracket.start()
                break
        end = openers[0] if openers else resume
        if _is_path_form(_reference_form(piece[start:end])):
            stop = end
            name = piece.rindex("/", slash, end) + 1
            if line_number and end == len(piece) and _FILE_EXTENSION.search(piece, name):
                stop = name
            parts += [piece[kept:slash], " " * (stop - slash)]
            kept = stop
        start = resume
        slash = piece.find("/", resume)
    return "".join(parts) + piece[kept:]


def _shield_path_token(match: re.Match[str]) -> str:
    # Each piece between `;`, `,` and `:` delimiters is its own path, as in
    # the path-token pass (`docs/x.md,(3h)/y.md` keeps the `3h` claim).
    pieces = _PATH_DELIMITERS.split(match.group(0))
    for index in range(0, len(pieces), 2):
        line_number = (
            index + 1 < len(pieces)
            and pieces[index + 1].startswith(":")
            and pieces[index + 2][:1].isdigit()
        )
        pieces[index] = _shield_later_components(pieces[index], line_number)
    return "".join(pieces)


def _strip_references(line: str) -> str:
    line = _LINK_TARGET.sub(_blank_link_target, line)
    line = _WHITESPACE_TOKEN.sub(_shield_path_token, line)
    line = _blank_references(line)
    return _PATH_TOKEN_IN_TEXT.sub(_blank_path_token, line)


# --- quoted inputs in the Evidence and Minor notes sections ------------------
#
# Evidence and Minor notes record what the reviewer ran and fed to it, so a
# duration-shaped token there may be a quoted probe input, not the reviewer's
# own timing claim. Only the inside of a well-formed backtick code span, or of
# a fenced block that closes inside the section, counts as quoted, and only in
# those two sections. Everything else stays in scope: prose in every section,
# every heading line (so every verdict line), the Issues section quoted or not,
# and any markup that does not close where it opens.
#
# A section is one of the two only under the documented level-three heading:
# `### Evidence` or `### Minor notes` (bold allowed, as in `### **Evidence**`).
# Other levels, `###Evidence` with no space, a `#` run glued to the title, and
# the bare `MINOR NOTES:` label all stay in scope.
#
# Sections are found with `_HEADING`, the pattern the adapters split sections
# with, so the scan and the parser never disagree on where a section ends. That
# pattern does not know about fences: a `# comment` inside a fenced block is a
# heading to the parser, so it ends the section here too.
_QUOTED_INPUT_SECTIONS = frozenset({"EVIDENCE", "MINOR NOTES"})
# Exactly three `#` (up to three spaces before them), then at least one space.
_QUOTED_INPUT_HEADING = re.compile(r"^ {0,3}### +")
# A fence line: up to three spaces, three or more backticks or tildes, then the
# rest of the line (an opener's info string; a closer allows only whitespace).
_FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_BACKTICK_RUN = re.compile(r"`+")
# A number at the end of a fence opener, bounded the way `_DURATION_CLAIM`
# bounds one, so `\s+` across the newline would join it to a unit below.
_NUMBER_AT_END = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*$")


def _is_quoted_input_section(line: str, heading: re.Match[str]) -> bool:
    if _QUOTED_INPUT_HEADING.match(line) is None:
        return False
    if _strip_marks(_heading_title(heading)).upper() not in _QUOTED_INPUT_SECTIONS:
        return False
    # A closing `#` run counts only when a space or tab sets it apart from the
    # title (`### Evidence ###`); `### Evidence#` is not the documented heading.
    line = line.rstrip()
    title_end = line.rstrip("#")
    return title_end == line or title_end.endswith((" ", "\t"))


def _is_tagged_bullet(line: str) -> bool:
    bullet = _BULLET.match(line)
    return bool(bullet and _TAG.match(bullet.group(1)))


def _fence_opener(line: str) -> str | None:
    """The fence marker that `line` opens, or None.

    A backtick fence whose info string holds a backtick is not a fence
    (CommonMark); that line is read for inline code spans instead.
    """
    fence = _FENCE_LINE.match(line)
    if fence is None:
        return None
    marker, info = fence.group(1), fence.group(2)
    if marker[0] == "`" and "`" in info:
        return None
    return marker


def _closes_fence(line: str, marker: str) -> bool:
    fence = _FENCE_LINE.match(line)
    return (
        fence is not None
        and fence.group(1)[0] == marker[0]
        and len(fence.group(1)) >= len(marker)
        and not fence.group(2).strip()
    )


def _blank_code_spans(line: str) -> str:
    """`line` with the inside of each inline code span turned into spaces.

    A span opens at a run of backticks and closes at the next run of exactly
    the same length on the same line. The backticks themselves stay, so the
    text on either side keeps the neighbours it had. Fail closed: when any run
    finds no closing run on its line (a span never crosses a line), or any
    backtick follows a backslash, the line is returned unchanged.
    """
    if "`" not in line or "\\`" in line:
        return line
    runs = [run.span() for run in _BACKTICK_RUN.finditer(line)]
    pieces: list[str] = []
    pos = 0
    index = 0
    while index < len(runs):
        open_start, open_end = runs[index]
        width = open_end - open_start
        close = index + 1
        while close < len(runs) and runs[close][1] - runs[close][0] != width:
            close += 1
        if close == len(runs):
            return line
        close_start, close_end = runs[close]
        pieces.append(line[pos:open_end])
        pieces.append(" " * (close_start - open_end))
        pos = close_start
        index = close + 1
    pieces.append(line[pos:])
    return "".join(pieces)


def _blank_quoted_section(lines: list[str], out: list[str], start: int, stop: int) -> None:
    """Blank the quoted inputs of `lines[start:stop]` into `out`."""
    i = start
    while i < stop:
        marker = _fence_opener(lines[i])
        if marker is None:
            out[i] = _blank_code_spans(lines[i])
            i += 1
            continue
        close = i + 1
        while close < stop and not _closes_fence(lines[close], marker):
            close += 1
        if close == stop:
            # Fail closed: an unclosed fence leaves its own line and every
            # later line of the section in scope, as written.
            return
        if _NUMBER_AT_END.search(lines[i]):
            # The opener ends in a number that the scan reads as a claim
            # together with the block's first line ("3", then "hours"), so the
            # claim is not wholly inside the block: nothing in it is quoted.
            i = close + 1
            continue
        # The fence lines stay as written; only the lines between are quoted.
        for inner in range(i + 1, close):
            out[inner] = ""
        i = close + 1


def _blank_quoted_inputs(lines: list[str]) -> list[str]:
    """`lines` with the quoted inputs of Evidence and Minor notes blanked."""
    out = list(lines)
    i = 0
    while i < len(lines):
        heading = _HEADING.match(lines[i])
        line = lines[i]
        i += 1
        if heading is None or not _is_quoted_input_section(line, heading):
            continue
        end = i
        while end < len(lines) and not _HEADING.match(lines[end]):
            end += 1
        # A `[BUG]` / `[SECURITY]` bullet is a finding wherever it sits (the
        # adapter collects it, and in Evidence every bullet after it), so the
        # section's quoting ends at the first one.
        stop = i
        while stop < end and not _is_tagged_bullet(lines[stop]):
            stop += 1
        _blank_quoted_section(lines, out, i, stop)
        i = end
    return out


# --- quoted examples (double quotes, any non-verdict line) -------------------
#
# A duration the reviewer quotes as an example of input text is not a
# measured-interval claim. The span is a same-line pair of ASCII double
# quotes whose interior is 1..200 characters, contains a duration token,
# and is not bare: the stripped interior does not fullmatch a duration
# token, and it contains whitespace. So `"ran for 120 seconds"` is an
# example; `"41 seconds"` and `"3d/foo.py"` stay claims (`took "41 seconds"`
# still needs INTERVAL endpoints). Unmatched quotes, interiors longer than
# 200, heading lines, and grok `cleared`/`flagged` verdict-value lines fail
# closed (stay in scope). Backtick spans stay the Evidence / Minor notes
# rule above; a backtick-wrapped `"…"` is handled here.
#
# The walk is left-to-right str.find of `"`. No regex, no nested
# quantifiers; a 100 KB adversarial line stays linear.

_QUOTED_EXAMPLE_MAX = 200
_REFUSAL_LINE_MAX = 200


def _is_verdict_or_heading_line(line: str) -> bool:
    return bool(
        _HEADING.match(line)
        or _GROK_VERDICT.match(line)
        or _GROK_VERDICT_LABEL.match(line)
        or _CLAUDE_VERDICT_HEADING.match(line)
        or _CLAUDE_VERDICT_LABEL.match(line)
    )


def _is_quoted_example(interior: str) -> bool:
    stripped = interior.strip()
    if not stripped or len(interior) > _QUOTED_EXAMPLE_MAX:
        return False
    if _DURATION_CLAIM.search(stripped) is None:
        return False
    if _DURATION_CLAIM.fullmatch(stripped):
        return False
    if not any(ch.isspace() for ch in stripped):
        return False
    return True


def _blank_double_quoted_examples(line: str) -> str:
    """`line` with quoted-example interiors turned into spaces."""
    if '"' not in line:
        return line
    pieces: list[str] = []
    pos = 0
    length = len(line)
    while pos < length:
        open_at = line.find('"', pos)
        if open_at < 0:
            pieces.append(line[pos:])
            break
        pieces.append(line[pos : open_at + 1])
        close_limit = open_at + 1 + _QUOTED_EXAMPLE_MAX
        close_at = line.find('"', open_at + 1, close_limit + 1)
        if close_at < 0:
            pieces.append(line[open_at + 1 :])
            break
        interior = line[open_at + 1 : close_at]
        if _is_quoted_example(interior):
            pieces.append(" " * len(interior))
        else:
            pieces.append(interior)
        pieces.append('"')
        pos = close_at + 1
    return "".join(pieces)


def _blank_quoted_examples(original: str, scanned: str) -> str:
    if _is_verdict_or_heading_line(original):
        return scanned
    return _blank_double_quoted_examples(scanned)


def _scan_line(original: str, quoted_blanked: str) -> str:
    scanned = _blank_quoted_examples(original, quoted_blanked)
    return _EMPHASISED_DURATION.sub(
        _unwrap_emphasised_duration, _strip_references(scanned)
    )


def _iter_scanned_lines(text: str):
    lines = text.splitlines()
    quoted = _blank_quoted_inputs(lines)
    for number, (original, blanked) in enumerate(zip(lines, quoted), start=1):
        yield number, original, _scan_line(original, blanked)


def _text_for_duration_scan(text: str) -> str:
    return "\n".join(
        scanned
        for _, original, scanned in _iter_scanned_lines(text)
        if not _INTERVAL_LINE.match(original)
    )


def _named_refusal(rule: str, line_number: int, line: str) -> str:
    shown = line if len(line) <= _REFUSAL_LINE_MAX else line[:_REFUSAL_LINE_MAX]
    return f"{rule}: line {line_number}: {shown}"


def _first_duration_claim_line(text: str) -> tuple[int, str] | None:
    """The first original line that participates in a duration claim.

    The claim is found on the joined scan text, the same string
    `_has_duration_claim` searches, so a number at the end of one line and a
    unit on the next (`3` then `hours`) still names the line that holds the
    number.
    """
    pairs = [
        (number, original, scanned)
        for number, original, scanned in _iter_scanned_lines(text)
        if not _INTERVAL_LINE.match(original)
    ]
    if not pairs:
        return None
    joined = "\n".join(scanned for _, _, scanned in pairs)
    match = _DURATION_CLAIM.search(joined)
    if match is None:
        return None
    index = joined.count("\n", 0, match.start())
    number, original, _ = pairs[index]
    return number, original


def _first_incomplete_interval_line(text: str) -> tuple[int, str] | None:
    for number, line in enumerate(text.splitlines(), start=1):
        matched = _INTERVAL_LINE.match(line)
        if matched is None:
            continue
        _, missing = _parse_interval_body(matched.group(1))
        if missing:
            return number, line
    return None


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
    claim = _first_duration_claim_line(text)
    if claim is None:
        return complete
    if complete:
        return complete
    if incomplete:
        located = _first_incomplete_interval_line(text)
        number, line = located if located is not None else claim
        raise ParseError(_named_refusal(INTERVAL_ENDPOINTS_MISSING, number, line))
    number, line = claim
    raise ParseError(_named_refusal(INTERVAL_ENDPOINTS_MISSING, number, line))


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
    selected = ADAPTERS[backend]
    try:
        verdict, findings = selected(text)
    except ParseError as exc:
        if not _jev_fallback_enabled(envelope):
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

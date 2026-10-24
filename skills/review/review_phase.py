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
is null otherwise. The selected adapter's verdict remains authoritative. The
flag is off by default; any shadow failure cannot change review status,
artifacts, reason, or verdict.

Backends (one adapter each, chosen by the envelope's `inputs.backend`):
    claude-fresh   the reviewer agent's `## Verdict:` block with `### Issues`
                   / `### Minor notes` (clean: "No bugs or security issues found."
                   inside `### Issues`), the headless `VERDICT:` + `FINDINGS`
                   shape, and the `review` skill's `[BUG]` / `[SECURITY]`
                   bullets (or its "No bugs or security issues found." line)
    grok-notools   `## FINDINGS` bullets (`none` when clean) + `## VERDICT`
                   `<cleared|flagged> — <sentence>`
    omp            exactly one fenced `review-result` JSON record with a
                   verdict, bugs, nits, and reviewed_head. The envelope's
                   `inputs.model` and `inputs.thinking` name the served model
                   and thinking level; both are required, and the phase
                   result carries them as `attribution` with the lane name

The OMP verdict comes only from that record, compared byte-exact; missing,
multiple, unterminated, invalid-JSON, and schema-invalid records have distinct
refusals. Prose outside the record never supplies a verdict. It is folded to
ASCII where Unicode itself says how (NFKC, then a Latin fold read from each
letter's name), a word mixing ASCII and non-ASCII letters is refused as
`ReviewProseUnsafe`, and only the ASCII word `verdict` is a key: a prose
verdict that differs from the record, or that the grammar cannot read, is
`VerdictConflict`, and an uppercase `APPROVE` or `REQUEST CHANGES` on a line
with no key is `ReviewProseUnsafe`. The claude-fresh and grok-notools adapters
retain their text contracts.

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
import html.entities
import json
import math
import os
import re
import sys
import unicodedata
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


class VerdictConflict(ParseError):
    """The return states two different verdicts, so neither one can be taken.

    A failure even with `adapters.jev_fallback` on: another adapter or Jev
    would pick one of two verdicts the reviewer wrote.
    """

class ReviewProseUnsafe(ParseError):
    """The OMP reply contains machine-unsafe prose outside its record."""


class ReviewRecordMissing(ParseError):
    """The OMP reply contains no fenced review-result record."""


class ReviewRecordMultiple(ParseError):
    """The OMP reply contains more than one fenced review-result record."""


class ReviewRecordUnterminated(ParseError):
    """The OMP reply contains an unclosed review-result record."""


class ReviewRecordInvalidJSON(ParseError):
    """The OMP review-result record is not valid JSON."""


class ReviewRecordSchema(ParseError):
    """The OMP review-result record does not conform to its closed schema."""


def _single_verdict(verdicts: list[str]) -> str | None:
    """The one verdict a return's verdict labels state, or None when it has none.

    Every label counts, not the first or the last, so a later label never
    overrides an earlier one: two labels that disagree raise VerdictConflict
    whichever comes first. The same verdict repeated is still that verdict.
    """
    if len(set(verdicts)) > 1:
        other = next(v for v in verdicts if v != verdicts[0])
        raise VerdictConflict(
            f"conflicting verdicts in the return: {verdicts[0]} and {other}"
        )
    return verdicts[0] if verdicts else None


_VERDICT_HEADING = re.compile(r"^#{1,6}\s*")
_VERDICT_WRAPPERS = (
    ("**", "**"),
    ("__", "__"),
    ("*", "*"),
    ("_", "_"),
    ("`", "`"),
    ("[", "]"),
)
_VERDICT_VALUE = re.compile(
    r"(APPROVE(?:[ _-]+WITH[ _-]+FINDINGS)?|APPROVED|"
    r"REQUEST[ _-]+CHANGES|cleared|flagged)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _peel_verdict_wrapper_prefix(text: str) -> tuple[str, list[str]]:
    """Peel every adjacent wrapper prefix and return its closing sequence."""
    closings: list[str] = []
    while True:
        wrapped = next(
            (
                (opening, closing)
                for opening, closing in _VERDICT_WRAPPERS
                if text.startswith(opening)
                and len(text) > len(opening)
                and not text[len(opening)].isspace()
            ),
            None,
        )
        if wrapped is None:
            return text, closings
        opening, closing = wrapped
        text = text[len(opening):]
        closings.append(closing)


def _peel_leading_verdict_closings(
    text: str, closings: list[str]
) -> str | None:
    for closing in reversed(closings):
        text = text.lstrip()
        if not text.startswith(closing):
            return None
        text = text[len(closing):]
    return text.lstrip()


def _peel_trailing_verdict_closings(
    text: str, closings: list[str]
) -> str | None:
    for closing in closings:
        text = text.rstrip()
        if not text.endswith(closing):
            return None
        text = text[:-len(closing)]
    return text.rstrip()


def _verdict_label_value(line: str) -> tuple[str, bool] | None:
    """Return a verdict label's raw value and whether it is a Markdown heading."""
    text = line.strip()
    heading = _VERDICT_HEADING.match(text)
    if heading:
        text = text[heading.end():].lstrip()

    text, closings = _peel_verdict_wrapper_prefix(text)
    if text[:7].casefold() != "verdict":
        return None
    text = text[7:]
    if text and (text[0].isalnum() or text[0] == "-"):
        return None
    text = text.lstrip()

    if closings:
        after_key = _peel_leading_verdict_closings(text, closings)
        if after_key is not None:
            text = after_key
            if text.startswith(":"):
                text = text[1:].lstrip()
            elif text:
                return None
        elif text.startswith(":"):
            text = text[1:].lstrip()
            after_colon = _peel_leading_verdict_closings(text, closings)
            if after_colon is not None:
                text = after_colon
            else:
                full_span = _peel_trailing_verdict_closings(text, closings)
                if full_span is None:
                    return None
                text = full_span
        else:
            return None
    elif text.startswith(":"):
        text = text[1:].lstrip()
    elif text:
        return None

    return text, bool(heading)


def _normalize_verdict_value(
    value: str, *, heading: bool
) -> tuple[str, str] | None:
    text = value.strip()
    if heading and re.fullmatch(
        r"\[\s*APPROVE\s*\|\s*REQUEST[ _-]+CHANGES\s*\]", text, re.IGNORECASE
    ):
        return APPROVED, "claude"

    text, closings = _peel_verdict_wrapper_prefix(text)
    matched = _VERDICT_VALUE.match(text)
    if not matched:
        return None
    remainder = text[matched.end():]
    if closings:
        remainder = _peel_leading_verdict_closings(remainder, closings)
        if remainder is None:
            return None
    elif remainder.startswith("_"):
        return None

    token = re.sub(r"[ _-]+", " ", matched.group(1).upper())
    if token == "APPROVED" and not heading:
        return None
    if token in ("APPROVE", "APPROVE WITH FINDINGS", "APPROVED"):
        return APPROVED, "claude"
    if token == "CLEARED":
        return APPROVED, "grok"
    if token == "FLAGGED":
        return CHANGES_REQUESTED, "grok"
    return CHANGES_REQUESTED, "claude"


def _verdict_labels(
    lines: list[str],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Collect normalized labels and the values that cannot be normalized."""
    labels: list[tuple[str, str]] = []
    unrecognized: list[str] = []
    for index, line in enumerate(lines):
        labeled = _verdict_label_value(line)
        if labeled is None:
            continue
        value, heading = labeled
        if not value:
            value = next(
                (item.strip() for item in lines[index + 1:] if item.strip()), ""
            )
        normalized = _normalize_verdict_value(value, heading=heading)
        if normalized is None:
            unrecognized.append(value or line.strip())
        else:
            labels.append(normalized)
    return labels, unrecognized


def _extract_verdict(lines: list[str], vocabulary: str) -> str | None:
    labels, unrecognized = _verdict_labels(lines)
    _single_verdict([outcome for outcome, _ in labels])
    if unrecognized:
        raise ParseError(f"unrecognized verdict value: {unrecognized[0]!r}")
    return next(
        (outcome for outcome, family in labels if family == vocabulary), None
    )


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

def parse_grok_notools(text: str) -> tuple[str, list[dict]]:
    """`## FINDINGS` bullets + `## VERDICT <cleared|flagged> — <sentence>`."""
    lines = _review_lines(text)
    verdict = _extract_verdict(lines, "grok")
    bullets: list[str] | None = None
    unparsed = False
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
    severity = "error" if verdict == CHANGES_REQUESTED else "warning"
    return verdict, [_finding(b, "finding", severity) for b in bullets]


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
    # The clean sentinel only stands in when there is no verdict label at all.
    verdict = _extract_verdict(lines, "claude")
    if verdict is None and any(_CLEAN_SENTINEL.match(line) for line in lines):
        verdict = APPROVED

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


_OMP_REVIEW_RECORD_FIELDS = {"verdict", "bugs", "nits", "reviewed_head"}
_OMP_REVIEWED_HEAD = re.compile(r"[0-9a-f]{40}")
# A CommonMark raw HTML tag on one line: an open tag (a tag name directly
# after `<`, its attributes, an optional `/`) or a closing tag (a tag name
# directly after `</`). Any other `<` is text a reader sees (`n < 0`,
# `Map<K, V>`, `a <- b`), so it stays in the prose the checks read.
_OMP_HTML_TAG = re.compile(
    r"<(?:[A-Za-z][A-Za-z0-9-]*"
    r"(?:[ \t\r\n]+[A-Za-z_:][A-Za-z0-9_.:-]*"
    r"(?:[ \t\r\n]*=[ \t\r\n]*(?:[^ \t\r\n\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)*"
    r"[ \t\r\n]*/?|/[A-Za-z][A-Za-z0-9-]*[ \t\r\n]*)>"
)
# Every character reference an HTML5 decoder consumes: any `&...;`, a numeric
# or hex reference with or without its semicolon (`&#101`, `&#x65`), and the
# legacy named references HTML5 decodes without one (`&shy`, `&amp`).
_OMP_HTML_REFERENCE = re.compile(
    r"&(?:[#A-Za-z0-9]+;|#[0-9]|#[xX][0-9A-Fa-f]|"
    + "|".join(
        sorted(name for name in html.entities.html5 if not name.endswith(";"))
    )
    + ")"
)
_OMP_PROSE_QUALIFIER = re.compile(r"^\s*\([^()\n]*\)\s*")
_OMP_UNSAFE_CATEGORIES = frozenset({"Cf", "Mn", "Me", "Zl", "Zp", "Co", "Cn", "Cs"})
_OMP_UNSAFE_FILLERS = frozenset({"\u115f", "\u1160", "\u2800", "\u3164", "\uffa0"})
# The Latin fold reads a letter's own Unicode name; there is no lookalike
# table. A name with one letter after `LETTER` (or `LETTER DOTLESS`), or a
# small capital, folds to that letter; every other letter stays as it is.
_OMP_LATIN_LETTER_NAME = re.compile(
    r"LATIN (SMALL|CAPITAL) LETTER (?:DOTLESS )?([A-Z])(?: WITH [A-Z ]+)?"
)
_OMP_LATIN_SMALL_CAPITAL_NAME = re.compile(r"LATIN LETTER SMALL CAPITAL ([A-Z])")
_OMP_LETTER = re.compile(r"[^\W\d_]")
# A word: a maximal run of letters, digits, `_`, `-` and `'`.
_OMP_WORD = re.compile(r"[\w'-]+")
# The key is the ASCII word `verdict` on a folded, casefolded line; any run of
# non-letters may separate its letters. The leading bound is checked in
# `_omp_verdict_keys`, since its camel-case rule reads the uncasefolded line.
_OMP_VERDICT_KEY = re.compile(
    r"[\W\d_]*".join("verdict") + r"s?(?![^\W\d_])"
)
# A key candidate: the key's grammar with each letter widened to that ASCII
# letter or any non-ASCII letter, read at every start on the casefolded line.
# Its letters may stand apart (`V/Ε/R/D/I/C/T`), so the mixed-word rule reads
# it as one word.
_OMP_VERDICT_KEY_CANDIDATE = re.compile(
    "(?=("
    + r"[\W\d_]*".join(rf"(?:{letter}|[^\W\d_A-Za-z])" for letter in "verdict")
    + r")s?(?![^\W\d_]))"
)
# The value anchor: a whole-word uppercase verdict value on the folded line
# before casefold, its words separated as value normalization separates them.
_OMP_VALUE_ANCHOR = re.compile(
    r"(?<![^\W\d_])(?:APPROVE|REQUEST[\s_-]+CHANGES)(?![^\W\d_])"
)
_OMP_LINE_ENDINGS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
# A verdict value read as a prefix, for the unstripped line: raw HTML, code or
# other text may follow it on the same line.
_OMP_VALUE_PREFIX = re.compile(r"(approve|request[\s_-]+changes)(?![^\W\d_])")


def _strip_omp_decoration(text: str) -> str:
    """Strip surrounding Markdown marks, whitespace, and Unicode punctuation."""
    start = 0
    end = len(text)
    while start < end and (
        text[start].isspace()
        or unicodedata.category(text[start])[:1] in ("P", "S")
    ):
        start += 1
    while end > start and (
        text[end - 1].isspace()
        or unicodedata.category(text[end - 1])[:1] in ("P", "S")
    ):
        end -= 1
    return text[start:end]


def _latin_fold(character: str) -> str:
    """One non-ASCII Latin letter as the ASCII letter Unicode says it is.

    A letter whose name begins `LATIN ` folds when its NFKD decomposition
    begins with an ASCII letter (é, ü, İ), or when its name is one letter
    with an optional `DOTLESS` and `WITH ...` (ı, ø) or a small capital (ᴠ,
    ʀ), keeping the case the name states. Anything else is returned as is.
    """
    if character.isascii() or not character.isalpha():
        return character
    name = unicodedata.name(character, "")
    if not name.startswith("LATIN "):
        return character
    first = unicodedata.normalize("NFKD", character)[:1]
    if first.isascii() and first.isalpha():
        return first
    named = _OMP_LATIN_LETTER_NAME.fullmatch(name)
    if named:
        letter = named.group(2)
        return letter if named.group(1) == "CAPITAL" else letter.lower()
    small_capital = _OMP_LATIN_SMALL_CAPITAL_NAME.fullmatch(name)
    if small_capital:
        return small_capital.group(1).lower()
    return character


def _strip_omp_raw_html(text: str) -> str:
    """`text` without its CommonMark raw HTML: tags and comments.

    A comment is `<!--` through the first `-->` after it (`<!-->` and
    `<!--->` included). An unclosed `<!--` is text, and so is every later
    one, since no `-->` follows it either.
    """
    kept = []
    index = 0
    comments = True
    while (start := text.find("<", index)) != -1:
        end = None
        if comments and text.startswith("<!--", start):
            close = text.find("-->", start + 2)
            if close == -1:
                comments = False
            else:
                end = close + 3
        else:
            tag = _OMP_HTML_TAG.match(text, start)
            if tag:
                end = tag.end()
        if end is None:
            kept.append(text[index:start + 1])
            index = start + 1
        else:
            kept.append(text[index:start])
            index = end
    kept.append(text[index:])
    return "".join(kept)


def _canonicalize_omp_prose(text: str, strip: bool = True) -> tuple[str, str]:
    """Raw HTML stripped, NFKC, then the Latin fold: `(cased, cased.casefold())`.

    The cased copy keeps letter case for the camel-case key bound and the
    uppercase value anchor; the casefolded copy is what the key matches. With
    `strip` off the raw HTML stays, for the unstripped reading.
    """
    text = unicodedata.normalize("NFKC", _strip_omp_raw_html(text) if strip else text)
    cased = "".join(_latin_fold(character) for character in text)
    return cased, cased.casefold()


def _validate_omp_prose(lines: list[str | None]) -> None:
    """Refuse characters that make machine-addressed prose ambiguous."""
    for line in lines:
        if line is None:
            continue
        if _OMP_HTML_REFERENCE.search(line):
            raise ReviewProseUnsafe(
                "ReviewProseUnsafe: HTML character reference outside review record"
            )
        for character in line:
            category = unicodedata.category(character)
            if category in _OMP_UNSAFE_CATEGORIES:
                raise ReviewProseUnsafe(
                    "ReviewProseUnsafe: "
                    f"U+{ord(character):04X} category {category} outside review record"
                )
            if character in _OMP_UNSAFE_FILLERS:
                raise ReviewProseUnsafe(
                    "ReviewProseUnsafe: "
                    f"filler U+{ord(character):04X} outside review record"
                )


def _refuse_mixed_words(canonical: list[tuple[str, str] | None]) -> None:
    """Refuse a word holding both ASCII and non-ASCII letters, reply-wide.

    The ASCII letters around a lookalike are the tell, so a homoglyph key is
    refused here rather than matched or missed. A key candidate counts as one
    word, however its letters are separated.
    """
    for item in canonical:
        if item is None:
            continue
        words = _OMP_WORD.findall(item[0]) + [
            match.group(1) for match in _OMP_VERDICT_KEY_CANDIDATE.finditer(item[1])
        ]
        for word in words:
            letters = [character for character in word if character.isalpha()]
            if any(character.isascii() for character in letters) and not all(
                character.isascii() for character in letters
            ):
                raise ReviewProseUnsafe(
                    f"ReviewProseUnsafe: mixed-script word {word!r}"
                )


def _fenced_review_records(
    text: str,
) -> tuple[list[tuple[str, bool]], list[str | None]]:
    """Return review-result bodies and prose, marking each record opening."""
    raw_lines = text.splitlines(keepends=True)
    lines = [line.rstrip(_OMP_LINE_ENDINGS) for line in raw_lines]
    records: list[tuple[str, bool]] = []
    prose: list[str | None] = []
    index = 0
    while index < len(lines):
        opening = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", lines[index])
        if opening is None:
            prose.append(raw_lines[index])
            index += 1
            continue
        marker = opening.group(1)
        info = opening.group(2).strip()
        closing = re.compile(
            rf" {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*"
        )
        end = index + 1
        while end < len(lines) and closing.fullmatch(lines[end]) is None:
            end += 1
        closed = end < len(lines)
        content_end = end if closed else len(lines)
        if info == "review-result":
            records.append(("".join(raw_lines[index + 1:content_end]), closed))
            prose.append(None)
        else:
            prose.extend(raw_lines[index:(end + 1 if closed else len(lines))])
        if not closed:
            break
        index = end + 1
    return records, prose


def _omp_prose_value(text: str) -> str:
    """Remove structural decoration surrounding a prose verdict value."""
    text = text.strip()
    while (qualifier := _OMP_PROSE_QUALIFIER.match(text)) is not None:
        text = text[qualifier.end():].lstrip()
    return _strip_omp_decoration(text)


def _omp_verdict_keys(cased: str, folded: str) -> list[re.Match[str]]:
    """Every `verdict` key on a line, with its leading bound applied.

    A key starts the line, follows a non-letter, or follows an ASCII
    lowercase letter as an uppercase `V` (`FinalVerdict`); a key after any
    other letter is part of a longer word (`never dictate`, `SERVER_DICT`).
    """
    origin = [
        index for index, character in enumerate(cased) for _ in character.casefold()
    ]
    keys = []
    for match in _OMP_VERDICT_KEY.finditer(folded):
        start = match.start()
        if start and _OMP_LETTER.match(folded[start - 1]):
            at = origin[start]
            if not (cased[at] == "V" and at and "a" <= cased[at - 1] <= "z"):
                continue
        keys.append(match)
    return keys


def _omp_prose_verdicts(lines: list[str | None]) -> list[str]:
    """Return every prose verdict, refusing unsafe or unclassifiable prose."""
    _validate_omp_prose(lines)
    canonical = [
        None if line is None else _canonicalize_omp_prose(line)
        for line in lines
    ]
    _refuse_mixed_words(canonical)
    verdicts = []
    consumed: set[int] = set()
    for index, item in enumerate(canonical):
        if item is None:
            continue
        cased, folded = item
        keys = _omp_verdict_keys(cased, folded)
        if not keys:
            if index not in consumed and _OMP_VALUE_ANCHOR.search(cased):
                raise ReviewProseUnsafe(
                    "ReviewProseUnsafe: verdict value without a verdict key"
                )
            continue
        if len(keys) != 1:
            raise VerdictConflict(
                "VerdictConflict: prose line contains multiple verdict keys"
            )
        value = _omp_prose_value(folded[keys[0].end():])
        if not value:
            # A key-only line looks forward only: its value is the next
            # non-blank item, and when that item is the record it confirms
            # the record by construction.
            following = next(
                (
                    later
                    for later in range(index + 1, len(canonical))
                    if canonical[later] is None or canonical[later][1].strip()
                ),
                None,
            )
            if following is not None:
                if canonical[following] is None:
                    continue
                consumed.add(following)
                value = _omp_prose_value(canonical[following][1])
        normalized = re.sub(r"[\s_-]+", " ", value)
        if normalized == "approve":
            verdicts.append(APPROVED)
        elif normalized == "request changes":
            verdicts.append(CHANGES_REQUESTED)
        else:
            raise VerdictConflict(
                f"VerdictConflict: unrecognized prose verdict value {value!r}"
            )
    # Stripping raw HTML may only add refusals, never hide a verdict: the same
    # lines read as written must not show one the stripped lines hid. Raw HTML
    # is not told apart from code spans, fenced or indented code, or escaped
    # text, so a verdict in any of them still vetoes a record it disagrees
    # with (declared over-refusal).
    unstripped = [
        None if line is None else _canonicalize_omp_prose(line, strip=False)
        for line in lines
    ]
    _refuse_mixed_words(unstripped)
    verdicts.extend(_omp_unstripped_verdicts(canonical, unstripped))
    return verdicts


def _omp_unstripped_verdicts(
    stripped: list[tuple[str, str] | None],
    unstripped: list[tuple[str, str] | None],
) -> list[str]:
    """Every verdict the unstripped lines show: each key's value and each anchor.

    A key's value runs to the next key on its line and is read as a prefix,
    since raw HTML or code may follow it. When a line's last key has no value
    there, or its stripped line is key-only, the next non-blank unstripped
    line is read as that value too.
    """
    verdicts = []
    for index, item in enumerate(unstripped):
        if item is None:
            continue
        cased, folded = item
        keys = _omp_verdict_keys(cased, folded)
        values = [
            _omp_prose_value(
                folded[key.end():following.start() if following else len(folded)]
            )
            for key, following in zip(keys, keys[1:] + [None])
        ]
        stripped_keys = _omp_verdict_keys(*stripped[index])
        key_only = bool(stripped_keys) and not _omp_prose_value(
            stripped[index][1][stripped_keys[-1].end():]
        )
        if keys and (not values[-1] or key_only):
            following = next(
                (
                    later
                    for later in range(index + 1, len(unstripped))
                    if unstripped[later] is None or unstripped[later][1].strip()
                ),
                None,
            )
            if following is not None and unstripped[following] is not None:
                values.append(_omp_prose_value(unstripped[following][1]))
        for value in values:
            prefix = _OMP_VALUE_PREFIX.match(value)
            if prefix:
                verdicts.append(
                    APPROVED if prefix.group(1) == "approve" else CHANGES_REQUESTED
                )
        for anchor in _OMP_VALUE_ANCHOR.finditer(cased):
            verdicts.append(
                APPROVED if anchor.group() == "APPROVE" else CHANGES_REQUESTED
            )
    return verdicts


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    record = {}
    for key, value in pairs:
        if key in record:
            raise ReviewRecordSchema(
                f"ReviewRecordSchema: duplicate field {key!r}"
            )
        record[key] = value
    return record


def _omp_review_record(text: str) -> tuple[dict, list[str | None]]:
    records, prose = _fenced_review_records(text)
    if not records:
        raise ReviewRecordMissing(
            "ReviewRecordMissing: expected exactly one fenced `review-result` record"
        )
    if len(records) > 1:
        raise ReviewRecordMultiple(
            f"ReviewRecordMultiple: found {len(records)} fenced `review-result` records"
        )
    body, closed = records[0]
    if not closed:
        raise ReviewRecordUnterminated(
            "ReviewRecordUnterminated: fenced `review-result` record is not closed"
        )
    try:
        record = json.loads(body, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReviewRecordInvalidJSON(
            f"ReviewRecordInvalidJSON: {exc.msg}"
        ) from exc
    if not isinstance(record, dict):
        raise ReviewRecordSchema("ReviewRecordSchema: record must be a JSON object")
    missing = sorted(_OMP_REVIEW_RECORD_FIELDS - record.keys())
    extra = sorted(record.keys() - _OMP_REVIEW_RECORD_FIELDS)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {missing}")
        if extra:
            details.append(f"unexpected {extra}")
        raise ReviewRecordSchema("ReviewRecordSchema: " + ", ".join(details))
    verdict = record["verdict"]
    if not isinstance(verdict, str):
        raise ReviewRecordSchema("ReviewRecordSchema: verdict must be a string")
    if verdict not in ("APPROVE", "REQUEST CHANGES"):
        raise ReviewRecordSchema(f"ReviewRecordSchema: unknown verdict {verdict!r}")
    for field in ("bugs", "nits"):
        values = record[field]
        if not isinstance(values, list):
            raise ReviewRecordSchema(f"ReviewRecordSchema: {field} must be a list")
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ReviewRecordSchema(
                f"ReviewRecordSchema: {field} entries must be non-empty strings"
            )
        # A JSON-escaped lone surrogate survives json.loads but cannot be
        # written as UTF-8, so the phase result could never be emitted.
        if any("\ud800" <= character <= "\udfff" for value in values for character in value):
            raise ReviewRecordSchema(
                f"ReviewRecordSchema: {field} entries must not contain surrogate code points"
            )
    if verdict == "APPROVE" and record["bugs"]:
        raise ReviewRecordSchema(
            "ReviewRecordSchema: APPROVE record must have an empty bugs list"
        )
    reviewed_head = record["reviewed_head"]
    if (
        not isinstance(reviewed_head, str)
        or _OMP_REVIEWED_HEAD.fullmatch(reviewed_head) is None
    ):
        raise ReviewRecordSchema(
            "ReviewRecordSchema: reviewed_head must be a 40-character lowercase hex commit"
        )
    return record, prose


def _without_interval_record(text: str) -> str:
    """`text` without its trailing INTERVAL record, line endings kept."""
    raw_lines = text.splitlines(keepends=True)
    start, end = _record_block(text.splitlines())
    return "".join(raw_lines[:start] + raw_lines[end:])


def parse_omp(text: str) -> tuple[str, list[dict]]:
    """Parse one closed OMP review record; prose can only confirm or veto it.

    The trailing INTERVAL record is run_phase's, so it is not read as prose.
    """
    record, prose = _omp_review_record(_without_interval_record(text))
    verdict = (
        APPROVED if record["verdict"] == "APPROVE" else CHANGES_REQUESTED
    )
    for prose_verdict in _omp_prose_verdicts(prose):
        if prose_verdict != verdict:
            raise VerdictConflict(
                "VerdictConflict: prose verdict "
                f"{prose_verdict!r} disagrees with record verdict {record['verdict']!r}"
            )
    findings = [
        _finding(item, "bug", "error") for item in record["bugs"]
    ] + [
        _finding(item, "minor", "info") for item in record["nits"]
    ]
    return verdict, findings


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
    # The record is checked before any adapter reads the return. A return
    # whose last line is not a record would otherwise reach an adapter with
    # that line still in its last section and fail as an unparsed section, and
    # with the fallback on it would reach the other adapters and Jev first.
    try:
        intervals = _require_intervals(text)
    except ParseError as exc:
        result = _result("failure", f"{backend}: {exc}", text, None)
    else:
        result = _parse_return(envelope, backend, text, intervals)
    if attribution is not None:
        result["attribution"] = attribution
    return result


def _parse_return(
    envelope: dict, backend: str, text: str, intervals: list[dict]
) -> dict:
    selected = ADAPTERS[backend]
    try:
        verdict, findings = selected(text)
    except ParseError as exc:
        # OMP's structured review record is mandatory even with fallback on:
        # another adapter or Jev would reintroduce a text-derived verdict.
        # A conflict in any backend likewise cannot be resolved by choosing
        # one of the verdicts the reviewer wrote.
        if (
            backend == OMP_LANE
            or isinstance(exc, VerdictConflict)
            or not _jev_fallback_enabled(envelope)
        ):
            return _result("failure", f"{backend}: {exc}", text, None)
        try:
            parsed = _try_other_adapters(backend, text)
        except VerdictConflict as exc:
            return _result("failure", f"{backend}: {exc}", text, None)
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
        except VerdictConflict:
            raise
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

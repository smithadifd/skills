"""The omp reply grammar: one record is the verdict, and prose may only veto it.

Every case is a whole reply parsed by `run_phase` with `backend: omp`. A case
id starts with its row in the grammar's test matrix: A the record, B unsafe
prose, C folding and the mixed-word rule, D the key's bounds, E values, F the
value read without a key, G orders and fallback, H the prompt contract, R the
raw HTML of earlier rounds, now refused rather than stripped. A prose case
runs in both orders, prose
before the record and the record before the prose, and expects the same result
in each (row G1); the key-only rows E8-E12 and R4 run in the one order they
name, since a key-only line reads forward.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(SKILL_DIR))
import review_phase  # noqa: E402

INTERVAL_NONE = "INTERVAL: none\n"
APPROVE_HEAD = "a" * 40
CHANGES_HEAD = "b" * 40


def _fence(body: str, info: str = "review-result", marker: str = "```") -> str:
    return f"{marker}{info}\n{body}\n{marker}\n"


def _record(verdict: object = "APPROVE", **fields) -> str:
    payload = {
        "verdict": verdict,
        "bugs": fields.pop("bugs", []),
        "nits": fields.pop("nits", []),
        "reviewed_head": fields.pop("reviewed_head", APPROVE_HEAD),
    }
    payload.update(fields)
    return _fence(json.dumps(payload, indent=2, ensure_ascii=False))


RA = _record()
RC = _record(
    "REQUEST CHANGES",
    bugs=["src/export.py:58 — the limit is applied after the rows are written."],
    reviewed_head=CHANGES_HEAD,
)


def _reply(*parts: str) -> str:
    """The parts as one reply, ending with the INTERVAL record run_phase checks."""
    return "\n".join(parts) + "\n" + INTERVAL_NONE


def _styled(text: str, upper: int, lower: int) -> str:
    return "".join(
        chr(upper + ord(c) - 65) if "A" <= c <= "Z"
        else chr(lower + ord(c) - 97) if "a" <= c <= "z"
        else c
        for c in text
    )


def _bold(text: str) -> str:
    """MATHEMATICAL BOLD letters, which NFKC folds back to ASCII."""
    return _styled(text, 0x1D400, 0x1D41A)


def _envelope(text: str) -> dict:
    return {
        "repo/ref": "example/invoice-cli@def456",
        "inputs": {
            "target": "example/invoice-cli#7",
            "backend": "omp",
            "brief": "Review the pull request for correctness and safety.",
            "return_text": text,
            "model": "example-model",
            "thinking": "medium",
        },
        "constraints": {"scope": "diff only"},
        "harness_root": None,
        "adapters": {},
        "budget": {"tokens": 0},
    }


APPROVED = ("success", "approved")
CHANGES = ("success", "changes_requested")
DISAGREES_WITH_APPROVE = (
    "failure",
    "VerdictConflict: prose verdict 'changes_requested' disagrees with record verdict 'APPROVE'",
)
DISAGREES_WITH_CHANGES = (
    "failure",
    "VerdictConflict: prose verdict 'approved' disagrees with record verdict 'REQUEST CHANGES'",
)
MIXED = ("failure", "ReviewProseUnsafe: mixed-script word")
RAW_HTML = ("failure", "ReviewProseUnsafe: raw HTML outside review record")
HTML_REFERENCE = ("failure", "ReviewProseUnsafe: HTML character reference")
NO_VALUE = ("failure", "VerdictConflict: verdict key without a verdict value")


def _refused(fragment: str) -> tuple[str, str]:
    return ("failure", fragment)


def _check(result: dict, expected: tuple[str, str]) -> None:
    status, detail = expected
    assert result["status"] == status, result["reason"]
    if status == "success":
        assert result["artifacts"]["findings.json"]["verdict"] == detail
    else:
        assert detail in result["reason"], result["reason"]
        assert result["artifacts"]["findings.json"] is None


# --- A. the record: the only verdict, compared byte-exact (one order) --------

_HEAD_FIELDS = f'"bugs": [], "nits": [], "reviewed_head": "{APPROVE_HEAD}"'

RECORD_CASES = [
    ("A1", _reply("The change looks correct to me."), _refused("ReviewRecordMissing")),
    ("A2", _reply(RA, RA), _refused("ReviewRecordMultiple: found 2")),
    ("A3", _reply(RA.rsplit("```", 1)[0]), _refused("ReviewRecordUnterminated")),
    ("A4", _reply(RA, RC.rsplit("```", 1)[0]), _refused("ReviewRecordMultiple")),
    ("A5", _reply(_fence('{"verdict": "APPROVE",}')), _refused("ReviewRecordInvalidJSON")),
    (
        "A6",
        _reply(_fence('{"verdict": "REQUEST CHANGES", "verdict": "APPROVE", ' + _HEAD_FIELDS + "}")),
        _refused("ReviewRecordSchema: duplicate field 'verdict'"),
    ),
    ("A7", _reply(_fence("[]")), _refused("ReviewRecordSchema: record must be a JSON object")),
    (
        "A8",
        _reply(_fence(f'{{"verdict": "APPROVE", "bugs": [], "reviewed_head": "{APPROVE_HEAD}"}}')),
        _refused("ReviewRecordSchema: missing ['nits']"),
    ),
    ("A9", _reply(_record(note="extra")), _refused("ReviewRecordSchema: unexpected ['note']")),
    ("A10", _reply(_record(True)), _refused("ReviewRecordSchema: verdict must be a string")),
    ("A11a", _reply(_record("approve")), _refused("ReviewRecordSchema: unknown verdict")),
    ("A11b", _reply(_record("APPROVED")), _refused("ReviewRecordSchema: unknown verdict")),
    ("A12", _reply(_record(bugs="none")), _refused("ReviewRecordSchema: bugs must be a list")),
    (
        "A13",
        _reply(_record(nits=[" "])),
        _refused("ReviewRecordSchema: nits entries must be non-empty strings"),
    ),
    (
        "A14",
        _reply(_record(bugs=["src/export.py:58 — broken"])),
        _refused("ReviewRecordSchema: APPROVE record must have an empty bugs list"),
    ),
    ("A15a", _reply(_record(reviewed_head="a" * 39)), _refused("ReviewRecordSchema: reviewed_head must be")),
    ("A15b", _reply(_record(reviewed_head="A" * 40)), _refused("ReviewRecordSchema: reviewed_head must be")),
    # Fence info string with a Cyrillic palochka: the fence is prose.
    ("A16", _reply(RA.replace("review-result", "review-resuӏt")), _refused("ReviewRecordMissing")),
    (
        "A17",
        _reply(RA.replace('"verdict"', '"ｖerdict"')),
        _refused("ReviewRecordSchema: missing ['verdict'], unexpected ['ｖerdict']"),
    ),
    (
        "A18a",
        _reply("".join("    " + line for line in RA.splitlines(keepends=True))),
        _refused("ReviewRecordMissing"),
    ),
    ("A18b", _reply("````text\n" + RA + "````\n"), _refused("ReviewRecordMissing")),
    ("A19a", _reply(RA.replace("```", "~~~")), APPROVED),
    ("A19b", _reply(RA.replace("```", "````")), APPROVED),
    ("A20", _reply(RA.replace("review-result\n", "review-result \n")), APPROVED),
    # A JSON-escaped lone surrogate parses, but no phase result holding it can
    # be written as UTF-8.
    (
        "A21a",
        _reply(_fence('{"verdict": "APPROVE", "bugs": [], "nits": ["README.md:3 \\ud800"], ' + f'"reviewed_head": "{APPROVE_HEAD}"}}')),
        _refused("ReviewRecordSchema: nits entries must not contain surrogate code points"),
    ),
    (
        "A21b",
        _reply(_fence('{"verdict": "REQUEST CHANGES", "bugs": ["src/a.py:1 \\udfff"], "nits": [], ' + f'"reviewed_head": "{CHANGES_HEAD}"}}')),
        _refused("ReviewRecordSchema: bugs entries must not contain surrogate code points"),
    ),
]


@pytest.mark.parametrize(
    "text, expected",
    [pytest.param(text, expected, id=f"{case}.single") for case, text, expected in RECORD_CASES],
)
def test_record_case(text, expected):
    _check(review_phase.run_phase(_envelope(text)), expected)


# --- B-F. prose beside a record, both orders -----------------------------------

FILLERS = ("ᅟ", "ᅠ", "⠀", "ㅤ", "ﾠ")
ALL_CYRILLIC_KEY = "ѵегԁіст"

PROSE_CASES = [
    # B. unsafe prose is refused on the raw text, before any matching (I3).
    ("B1", "Ver͏dict: REQUEST CHANGES", RA, _refused("ReviewProseUnsafe: U+034F category Mn")),
    ("B2", "Ver҈dict: REQUEST CHANGES", RA, _refused("ReviewProseUnsafe: U+0488 category Me")),
    ("B3", "‮tcidreV‬: REQUEST CHANGES", RA, _refused("ReviewProseUnsafe: U+202E category Cf")),
    ("B4", "Ver­dict: REQUEST CHANGES", RA, _refused("ReviewProseUnsafe: U+00AD category Cf")),
    ("B5", "Ver dict: REQUEST CHANGES", RA, _refused("ReviewProseUnsafe: U+2028 category Zl")),
    ("B6", "Safe prose. ", RA, _refused("ReviewProseUnsafe: U+2029 category Zp")),
    ("B7", "Safe  prose.", RA, _refused("ReviewProseUnsafe: U+E000 category Co")),
    ("B8", "Safe ͸ prose.", RA, _refused("ReviewProseUnsafe: U+0378 category Cn")),
    ("B9", "Safe \ud800 prose.", RA, _refused("ReviewProseUnsafe: U+D800 category Cs")),
    *(
        (
            f"B10{letter}",
            f"Ver{filler}dict: REQUEST CHANGES",
            RA,
            _refused(f"ReviewProseUnsafe: filler U+{ord(filler):04X}"),
        )
        for letter, filler in zip("abcde", FILLERS)
    ),
    ("B11a", "Ver&shy;dict: REQUEST CHANGES", RA, _refused("ReviewProseUnsafe: HTML character reference")),
    ("B11b", "&#86;erdict: REQUEST CHANGES", RA, _refused("ReviewProseUnsafe: HTML character reference")),
    # Decomposed accents are refused, precomposed ones pass (declared over-refusal).
    ("B12", "Le résumé est correct.", RA, _refused("ReviewProseUnsafe: U+0301 category Mn")),
    ("B13", "Le résumé est correct.", RA, APPROVED),
    ("B14", "Verdict:​\nREQUEST CHANGES", RA, _refused("ReviewProseUnsafe: U+200B category Cf")),
    # HTML5 decodes numeric and hex references without their semicolon, and
    # the legacy named ones too.
    ("B15a", "v&#101rdict: request changes", RA, HTML_REFERENCE),
    ("B15b", "v&#x65rdict: request changes", RA, HTML_REFERENCE),
    ("B15c", "v&#X65rdict: request changes", RA, HTML_REFERENCE),
    ("B15d", "Ver&shydict: request changes", RA, HTML_REFERENCE),
    # A bare ampersand that starts no reference is prose.
    ("B15e", "AT&T and R&D signed off; Q&A is done.", RA, APPROVED),
    # C. folding (I4) and the mixed-word rule (I5).
    ("C1", "Vеrdict: REQUEST CHANGES", RA, MIXED),
    ("C2", "VΕRDICT: REQUEST CHANGES", RA, MIXED),
    ("C3", "ᴠerdict: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("C4", "vᴇrdict: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("C5", "verdiᴄt: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("C6", "verdɩct: REQUEST CHANGES", RA, MIXED),
    ("C7", "verdӏct: REQUEST CHANGES", RA, MIXED),
    ("C8a", "ᏙERDICT: REQUEST CHANGES", RA, MIXED),
    ("C8b", "ꮩerdict: REQUEST CHANGES", RA, MIXED),
    (
        "C9",
        "ᴠᴇʀᴅɪᴄᴛ: REQUEST CHANGES",
        RA,
        DISAGREES_WITH_APPROVE,
    ),
    ("C10", _bold("Verdict") + ": REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("C11a", _styled("verdict", 0x24B6, 0x24D0) + ": REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("C11b", _styled("verdict", 0xFF21, 0xFF41) + ": REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("C12", "VERDİCT: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("C13", "Verdictѕ: REQUEST CHANGES", RA, MIXED),
    ("C14", "Verdict: АPPROVE", RC, MIXED),
    ("C15", "The résumé change is naïve but Müller's fix is correct.", RA, APPROVED),
    ("C16a", "Καλημέρα, the change is correct.", RA, APPROVED),
    ("C16b", "Die Änderung ist korrekt.", RA, APPROVED),
    # A whole-script lookalike key is outside the grammar (declared bound):
    # no fold can read it. The value beside it is read on its own.
    ("C17", ALL_CYRILLIC_KEY + ": approve", RA, APPROVED),
    # The same key ending in an ASCII `t` is a mixed word, refused by name.
    ("C17x", ALL_CYRILLIC_KEY[:-1] + "t: approve", RA, MIXED),
    ("C18", ALL_CYRILLIC_KEY + ": REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("C18x", ALL_CYRILLIC_KEY[:-1] + "t: REQUEST CHANGES", RA, MIXED),
    # œ has no single-letter name, so it stays and the word is mixed
    # (declared over-refusal).
    ("C19", "Le cœur du changement est correct.", RA, MIXED),
    # Raw HTML is refused before the mixed-word rule reads anything.
    ("C20a", "<b>Vеrdict</b>: REQUEST CHANGES", RA, RAW_HTML),
    ("C20b", "V<b>е</b>rdict: REQUEST CHANGES", RA, RAW_HTML),
    # The key's letters may stand apart, so the mixed-word rule reads the
    # separator-tolerant key candidate as one word.
    ("C21a", "V/\u0395/R/D/I/C/T: request changes", RA, MIXED),
    ("C21b", "V \u0395 R D I C T: request changes", RA, MIXED),
    # A `<` that starts no CommonMark tag is visible text. Nothing is deleted,
    # so the value between such marks vetoes the record.
    ("C22a", "If `n < 0` the loop never ends. Verdict: REQUEST CHANGES; see `Map<K, V>`.", RA, DISAGREES_WITH_APPROVE),
    ("C22b", "Bug: `i < len` is off by one, so REQUEST CHANGES until `x > 0` holds.", RA, DISAGREES_WITH_APPROVE),
    ("C22c", "a <- b; Verdict: REQUEST CHANGES; c -> d", RA, DISAGREES_WITH_APPROVE),
    ("C22d", "Use `x<y` here. Verdict: REQUEST CHANGES; then `y>z`.", RA, DISAGREES_WITH_APPROVE),
    # Real raw HTML is refused, never stripped, whether or not it agrees with
    # the record (declared over-refusal).
    ("C23a", "<b>Verdict</b>: REQUEST CHANGES", RC, RAW_HTML),
    ("C23b", "<b>Verdict</b>: REQUEST CHANGES", RA, RAW_HTML),
    ("C23c", '<abbr title="APPROVE">LGTM</abbr>, ship it.', RC, RAW_HTML),
    ("C23d", "Looks fine.<!-- REQUEST CHANGES was a first draft -->", RA, RAW_HTML),
    # D. the key's bounds (I6).
    ("D1", "FinalVerdict: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("D2", "ReviewVerdict: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("D3", "FINAL VERDICT: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    # Not a key (no lowercase letter before `V`); the value conflicts alone.
    ("D4", "FINALVERDICT: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("D5", "We should never dictate the schema here.", RA, APPROVED),
    ("D6a", "The `server_dict` is rebuilt on each call.", RA, APPROVED),
    ("D6b", "the driver dict is cached", RA, APPROVED),
    ("D7", "SERVER_DICT: rebuilt per call", RA, APPROVED),
    ("D8", "Verdicts: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("D9", "The verdictive tone aside, fine.", RA, APPROVED),
    ("D10a", "V_e_r_d_i_c_t: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("D10b", "Ver-dict: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("D10c", "Verdict_: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("D10d", "ver.dict: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("D11", "v e r d i c t: request changes", RA, DISAGREES_WITH_APPROVE),
    ("D12", "V E R D I C T: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("D13", "Note: Verdict: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    (
        "D14",
        "| field | value |\n|---|---|\n| Verdict | REQUEST CHANGES |",
        RA,
        DISAGREES_WITH_APPROVE,
    ),
    (
        "D15",
        "Verdict: APPROVE and verdict: REQUEST CHANGES",
        RA,
        DISAGREES_WITH_APPROVE,
    ),
    ("D16", "My verdict: approve", RA, APPROVED),
    # Any prose use of the key on a line with no value is refused (declared
    # over-refusal), and a non-record fence is prose.
    ("D17", "The verdict parser in review_phase.py looks fine.", RA, NO_VALUE),
    ("D18", "```text\nverdict = parse(x)\n```", RA, NO_VALUE),
    # E. values (I7).
    ("E1", "Verdict: APPROVED", RA, NO_VALUE),
    ("E2", "Verdict: approve", RA, APPROVED),
    ("E3", "Verdict: ✅ APPROVE", RA, APPROVED),
    ("E4", "Verdict (final): REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("E5", "Verdict: APPROVE - no bugs", RA, APPROVED),
    ("E6", "Verdict: " + _bold("APPROVE"), RA, APPROVED),
    ("E7a", "Verdict: REQUEST  CHANGES", RC, CHANGES),
    ("E7b", "Verdict: REQUEST_CHANGES", RC, CHANGES),
    ("E7c", "Verdict: request-changes", RC, CHANGES),
    ("E13a", "Verdict: REQUEST CHANGES", RC, CHANGES),
    ("E13b", "Verdict: APPROVE", RA, APPROVED),
    # F. values need no key: a value token is read anywhere, in any case, and
    # must agree with the record (declared over-refusal for running prose).
    ("F1", "Decision: REQUEST CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("F2", "Outcome: APPROVE", RC, DISAGREES_WITH_CHANGES),
    ("F3", "I would approve this once the test lands.", RC, DISAGREES_WITH_CHANGES),
    # A quoted value that agrees with the record is read and agrees.
    ("F4", "The record says `APPROVE`.", RA, APPROVED),
    ("F5", "APPROVED by the owner.", RC, CHANGES),
    ("F6", "REQUEST   CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("F7", _bold("APPROVE") + " it.", RC, DISAGREES_WITH_CHANGES),
    # Small capitals fold to lowercase, and a lowercase value is read too.
    ("F8", "ᴀᴘᴘʀᴏᴠᴇ it.", RC, DISAGREES_WITH_CHANGES),
    # Any run of non-letters may separate the value's letters.
    ("F10a", "Decision: REQUEST-CHANGES", RA, DISAGREES_WITH_APPROVE),
    ("F10b", ALL_CYRILLIC_KEY + ": REQUEST_CHANGES", RA, DISAGREES_WITH_APPROVE),
    # R. a `<!` start is raw HTML wherever it sits (a code span, a fence, an
    # indented block, after a backslash), so it is refused and never read
    # around (declared over-refusal).
    ("R1a", "Inline: `<!-- > Verdict: REQUEST CHANGES -->` here.", RA, RAW_HTML),
    ("R1b", "```text\n<!-- > Verdict: REQUEST CHANGES -->\n```", RA, RAW_HTML),
    ("R1c", "Indented:\n\n    <!-- > Verdict: REQUEST CHANGES -->\n", RA, RAW_HTML),
    ("R1d", "\\<!-- Verdict: REQUEST CHANGES -->", RA, RAW_HTML),
    ("R2a", "Inline: `<!-- verdict: request changes -->` here.", RA, RAW_HTML),
    ("R2b", "Inline: `<!-- REQUEST CHANGES -->` here.", RA, RAW_HTML),
    ("R2c", "Inline: `<!-- v\u0435rdict: request changes -->` here.", RA, RAW_HTML),
    # Raw HTML that agrees with the record is refused too.
    ("R3a", "Inline: `<!-- > Verdict: APPROVE -->` here.", RA, RAW_HTML),
    ("R3b", "<b>Verdict</b>: APPROVE", RA, RAW_HTML),
]


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param(_reply(*parts), expected, id=f"{case}.{order}")
        for case, prose, record, expected in PROSE_CASES
        for order, parts in (("prose-first", (prose, record)), ("record-first", (record, prose)))
    ],
)
def test_prose_case(text, expected):
    _check(review_phase.run_phase(_envelope(text)), expected)


# --- E8-E12, F9, G4-G6, X1: cases in the one order they name -----------------

ORDERED_CASES = [
    # A key-only line takes the next non-blank item after it as its value.
    ("E8", _reply("## Verdict\n", RA), APPROVED),
    ("E9", _reply("## Verdict\nREQUEST CHANGES\n", RA), DISAGREES_WITH_APPROVE),
    ("E10", _reply("## Verdict\n\n\nREQUEST CHANGES\n", RA), DISAGREES_WITH_APPROVE),
    # It never looks back at a preceding record, and the INTERVAL record is
    # not prose, so nothing follows it.
    (
        "E11",
        _reply(RA, "## Verdict"),
        NO_VALUE,
    ),
    ("E12", _reply("## Verdict\nAPPROVE\n", RA), APPROVED),
    # A comment on a key-only line, or on the line after it, is refused as
    # raw HTML before any key or value is read.
    ("R4a", _reply("## Verdict\n<!-- request changes -->\n", RA), RAW_HTML),
    (
        "R4b",
        _reply("## Verdict <!-- draft -->\n<!-- request changes -->\n", RA),
        RAW_HTML,
    ),
    (
        "R4c",
        _reply("<!-- Verdict -->\n<!-- request changes -->\n", RA),
        RAW_HTML,
    ),
    # Record lines are not prose, so the record's own values are never read
    # as prose values.
    ("F9a", _reply(RA), APPROVED),
    ("F9b", _reply(_record(nits=["README.md:3 — the APPROVE example is stale."])), APPROVED),
    ("G4", _reply("No bugs or security issues found."), _refused("ReviewRecordMissing")),
    (
        "G5",
        _reply("No bugs or security issues found.", _record(reviewed_head="a" * 39)),
        _refused("ReviewRecordSchema"),
    ),
    # The INTERVAL record belongs to run_phase; the omp adapter never reads it
    # as prose.
    (
        "X1",
        RA + "\nINTERVAL: from APPROVE posted, review log to merge, gh log = 5m\n",
        APPROVED,
    ),
]


@pytest.mark.parametrize(
    "text, expected",
    [pytest.param(text, expected, id=f"{case}.single") for case, text, expected in ORDERED_CASES],
)
def test_ordered_case(text, expected):
    _check(review_phase.run_phase(_envelope(text)), expected)


@pytest.mark.parametrize("case", ["B15a", "B15b", "B15c", "B15d"])
def test_B15_inputs_are_references_an_html5_decoder_consumes(case):
    prose = next(prose for name, prose, _, _ in PROSE_CASES if name == case)
    assert html.unescape(prose).replace("\u00ad", "").startswith(("verdict", "Verdict"))


def test_A21_a_lone_surrogate_record_is_refused_and_the_result_still_emits(tmp_path):
    reply = tmp_path / "reply.md"
    text = next(text for name, text, _ in RECORD_CASES if name == "A21a")
    reply.write_text(text, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SKILL_DIR / "review_phase.py"),
            "--from-text",
            str(reply),
            "--backend",
            "omp",
            "--model",
            "example-model",
            "--thinking",
            "medium",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
    )
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 1, result.stderr
    reason = json.loads(result.stdout)["reason"]
    assert "ReviewRecordSchema: nits entries must not contain surrogate code points" in reason


def test_G6_a_record_with_no_prose_approves_with_no_findings():
    result = review_phase.run_phase(_envelope(_reply(RA)))
    _check(result, APPROVED)
    assert result["artifacts"]["findings.json"]["findings"] == []
    assert result["intervals"] == []


# --- G. every omp parse error is terminal --------------------------------------


@pytest.mark.parametrize(
    "text, fragment",
    [
        pytest.param(
            (FIXTURES / "omp-no-verdict.md").read_text() + "\n" + INTERVAL_NONE,
            "ReviewRecordMissing",
            id="G2.single",
        ),
        pytest.param(
            _reply("Vеrdict: REQUEST CHANGES", RA),
            "ReviewProseUnsafe: mixed-script word",
            id="G3.single",
        ),
    ],
)
def test_omp_parse_errors_are_terminal(text, fragment):
    result = review_phase.run_phase(_envelope(text))
    assert result["status"] == "failure", result["reason"]
    assert fragment in result["reason"], result["reason"]


# --- H. the prompt contract names what prose must not contain ----------------

REFUSAL_NAMES = (
    "ReviewRecordMissing",
    "ReviewRecordMultiple",
    "ReviewRecordUnterminated",
    "ReviewRecordInvalidJSON",
    "ReviewRecordSchema",
    "ReviewProseUnsafe",
    "VerdictConflict",
)


def test_H1_the_omp_prompt_contract_names_every_refusal_and_reserved_token():
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    contract = re.search(r"^````text\n(.*?)^````$", skill, re.MULTILINE | re.DOTALL)
    assert contract, "SKILL.md has no ````text OMP contract block"
    block = contract.group(1)
    for name in REFUSAL_NAMES:
        assert f"`{name}`" in block, f"the OMP contract does not name {name}"
    for token in ("`verdict`", "`APPROVE`", "`REQUEST CHANGES`", "non-ASCII letter"):
        assert token in block, f"the OMP contract does not reserve {token}"
    assert "The block is the only verdict." in block

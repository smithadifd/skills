"""The omp prose veto, row by row: the reply read as written, nothing deleted.

Each case id is its row in the prose grammar's test matrix. A the record,
B unsafe characters, C raw HTML refused, D links and images refused, E the
fold and mixed-script words, F the key, G the value token, H key lines and
bare keys, I prose placement and fences, J terminal errors, K the prompt
contract, L whitespace the renderer reads differently from `str`, M
CommonMark structure read as written, never after NFKC, N a value token
beside a symbol NFKC turns into letters. A prose
row runs in both orders, prose before the record and the record before the
prose, unless it names one; a "parse" row goes through
`parse_omp`, a "run_phase" row through `run_phase` with `backend: omp`.
"""

from __future__ import annotations

import functools
import json
import re
import sys
import unicodedata
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(SKILL_DIR))
import review_phase  # noqa: E402

HEAD = "a" * 40


def _fixture_record(name: str) -> str:
    """The fenced record of a fixture reply, without the prose before it."""
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return text[text.index("```review-result"):]


RA = _fixture_record("omp-approve.md")
RC = _fixture_record("omp-request-changes.md")
RECORDS = {"A": RA, "C": RC}


def _fence(body: str) -> str:
    return f"```review-result\n{body}\n```\n"


def _body(**fields) -> str:
    payload = {"verdict": "APPROVE", "bugs": [], "nits": [], "reviewed_head": HEAD}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False)


def _styled(text: str, upper: int, lower: int) -> str:
    return "".join(
        chr(upper + ord(c) - 65) if "A" <= c <= "Z"
        else chr(lower + ord(c) - 97) if "a" <= c <= "z"
        else c
        for c in text
    )


def _fullwidth(text: str) -> str:
    return _styled(text, 0xFF21, 0xFF41)


UNSAFE = review_phase.ReviewProseUnsafe
CONFLICT = review_phase.VerdictConflict
HTML = (UNSAFE, "ReviewProseUnsafe: raw HTML outside review record")
LINK = (UNSAFE, "ReviewProseUnsafe: Markdown link outside review record")
MIXED = (UNSAFE, "ReviewProseUnsafe: mixed-script word")
REFERENCE = (UNSAFE, "ReviewProseUnsafe: HTML character reference outside review record")
DISAGREES_A = (
    CONFLICT,
    "VerdictConflict: prose verdict 'changes_requested' disagrees with record verdict 'APPROVE'",
)
DISAGREES_C = (
    CONFLICT,
    "VerdictConflict: prose verdict 'approved' disagrees with record verdict 'REQUEST CHANGES'",
)
NO_VALUE = (CONFLICT, "VerdictConflict: verdict key without a verdict value")
OK_A = ("ok", "approved")
OK_C = ("ok", "changes_requested")


def _expect_parse(text: str, expected: tuple) -> None:
    kind, detail = expected
    if kind == "ok":
        verdict, _ = review_phase.parse_omp(text)
        assert verdict == detail
        return
    with pytest.raises(review_phase.ParseError) as caught:
        review_phase.parse_omp(text)
    assert type(caught.value) is kind, f"{type(caught.value).__name__}: {caught.value}"
    assert detail in str(caught.value), str(caught.value)


def _envelope(text: str) -> dict:
    """A run envelope with every key; the ones not set here are null."""
    envelope = dict.fromkeys(review_phase.ENVELOPE_KEYS)
    envelope.update(
        {
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
            "adapters": {},
            "budget": {"tokens": 0},
        }
    )
    return envelope


# --- A. the record (R0): one order, no prose ----------------------------------

RECORD_ROWS = [
    ("A1", "The change looks correct to me.\n", (review_phase.ReviewRecordMissing, "ReviewRecordMissing")),
    ("A2", RA + RA, (review_phase.ReviewRecordMultiple, "ReviewRecordMultiple: found 2")),
    ("A3", RA.rsplit("```", 1)[0], (review_phase.ReviewRecordUnterminated, "ReviewRecordUnterminated")),
    ("A4", _fence('{"verdict": "APPROVE",}'), (review_phase.ReviewRecordInvalidJSON, "ReviewRecordInvalidJSON")),
    (
        "A5",
        _fence('{"verdict": "REQUEST CHANGES", "verdict": "APPROVE", "bugs": [], "nits": [], '
               f'"reviewed_head": "{HEAD}"}}'),
        (review_phase.ReviewRecordSchema, "ReviewRecordSchema: duplicate field"),
    ),
    (
        "A6",
        _fence(f'{{"verdict": "APPROVE", "bugs": [], "reviewed_head": "{HEAD}"}}'),
        (review_phase.ReviewRecordSchema, "ReviewRecordSchema: missing ['nits']"),
    ),
    ("A7", _fence(_body(verdict="approve")), (review_phase.ReviewRecordSchema, "ReviewRecordSchema: unknown verdict")),
    (
        "A8",
        _fence(_body(verdict=_fullwidth("APPROVE"))),
        (review_phase.ReviewRecordSchema, "ReviewRecordSchema: unknown verdict"),
    ),
    (
        "A9",
        _fence(_body().replace('"verdict"', '"v\u0435rdict"')),
        (
            review_phase.ReviewRecordSchema,
            "ReviewRecordSchema: missing ['verdict'], unexpected ['v\u0435rdict']",
        ),
    ),
    (
        "A10",
        RA.replace("review-result", "r\u0435view-result"),
        (review_phase.ReviewRecordMissing, "ReviewRecordMissing"),
    ),
    (
        "A11",
        _fence(_body(bugs=["x"])),
        (review_phase.ReviewRecordSchema, "ReviewRecordSchema: APPROVE record must have an empty bugs list"),
    ),
    (
        "A12",
        _fence('{"verdict": "REQUEST CHANGES", "bugs": ["\\ud800"], "nits": [], '
               f'"reviewed_head": "{HEAD}"}}'),
        (review_phase.ReviewRecordSchema, "surrogate"),
    ),
    (
        "A13",
        _fence(_body(reviewed_head="a" * 39)),
        (review_phase.ReviewRecordSchema, "ReviewRecordSchema: reviewed_head"),
    ),
    # A head one character too long is what `fullmatch` alone refuses.
    (
        "A13b",
        _fence(_body(reviewed_head="a" * 41)),
        (review_phase.ReviewRecordSchema, "ReviewRecordSchema: reviewed_head"),
    ),
    ("A14", _fence(_body(nits=["x.py:1 \u2014 Verdict: REQUEST CHANGES"])), OK_A),
    ("A15", RA + "INTERVAL: none\n", OK_A),
]


@pytest.mark.parametrize(
    "text, expected", [pytest.param(text, expected, id=case) for case, text, expected in RECORD_ROWS]
)
def test_record_row(text, expected):
    _expect_parse(text, expected)


def test_A16_a_malformed_interval_holding_a_value_is_the_interval_refusal():
    text = RA + "INTERVAL: from x to y request changes\n"
    result = review_phase.run_phase(_envelope(text))
    assert result["status"] == "failure", result["reason"]
    assert "review_interval_" in result["reason"], result["reason"]
    assert "VerdictConflict" not in result["reason"], result["reason"]


def test_A16b_the_interval_record_is_never_read_as_prose():
    text = RC + "INTERVAL: from APPROVE posted, review log to merge, gh log = 5m\n"
    _expect_parse(text, OK_C)


# --- B-I. prose beside a record -----------------------------------------------

BOTH = ("prose-first", "record-first")
PROSE_FIRST = ("prose-first",)
RECORD_FIRST = ("record-first",)

PROSE_ROWS = [
    # B. unsafe characters (R1), on the raw lines, before anything is read.
    ("B1", "Ver\u200bdict: APPROVE", "A", BOTH, (UNSAFE, "ReviewProseUnsafe: U+200B category Cf")),
    ("B2", "Verdict: APPROVE\u202e", "A", BOTH, (UNSAFE, "ReviewProseUnsafe: U+202E category Cf")),
    ("B3", "e\u0301", "A", BOTH, (UNSAFE, "ReviewProseUnsafe: U+0301 category Mn")),
    ("B4", "\u3164", "A", BOTH, (UNSAFE, "ReviewProseUnsafe: filler U+3164")),
    ("B5", "v&#101;rdict: approve", "A", BOTH, REFERENCE),
    ("B6", "v&#101rdict: approve", "A", BOTH, REFERENCE),
    ("B7", "&amp", "A", BOTH, REFERENCE),
    ("B8", "x & y", "A", BOTH, OK_A),
    ("B9", "Ver\u200bdict: <b>x</b>", "A", BOTH, (UNSAFE, "ReviewProseUnsafe: U+200B")),
    # C. raw HTML refused, never stripped (R2).
    ("C1", "Inline: `<!-- <b>Verdict</b>: request changes -->` here.", "A", BOTH, HTML),
    ("C2", "Inline: `<!-- Verdict: <b>request changes</b> -->` here.", "A", BOTH, HTML),
    ("C3", "Inline: `<!-- > Verdict: REQUEST CHANGES -->` here.", "A", BOTH, HTML),
    ("C4", "```text\n<!-- > Verdict: REQUEST CHANGES -->\n```", "A", BOTH, HTML),
    ("C5", "    <!-- > Verdict: REQUEST CHANGES -->", "A", BOTH, HTML),
    ("C6", "\\<!-- Verdict: REQUEST CHANGES -->", "A", BOTH, HTML),
    ("C7", "<!-- Verdict: APPROVE -->", "A", BOTH, HTML),
    ("C8", "<b>Verdict</b>: APPROVE", "A", BOTH, HTML),
    ("C9", "Decision: RE<b>QUEST</b> CHANGES", "A", BOTH, HTML),
    ("C10", "<b>Ver</b>dict: <i>re</i>quest changes", "A", BOTH, HTML),
    ("C11", "</div>", "A", BOTH, HTML),
    ("C12", "<br/>", "A", BOTH, HTML),
    ("C13", '<a href="x">t</a>', "A", BOTH, HTML),
    ("C14", "<?php x ?>", "A", BOTH, HTML),
    ("C15", "<![CDATA[x]]>", "A", BOTH, HTML),
    ("C16", "Vec<T> is fine", "A", BOTH, HTML),
    ("C17", "i<n and n>0", "A", BOTH, HTML),
    ("C18", "Map<K, V> and n<len(xs) and a <- b and n < 0", "A", BOTH, OK_A),
    ("C19", "see <https://example.com> and <me@example.com>", "A", BOTH, OK_A),
    ("C20", "\uff1cb\uff1eVerdict\uff1c/b\uff1e: REQUEST CHANGES", "A", BOTH, HTML),
    ("C21", "Ver<b>dict: REQUEST CHANGES", "A", BOTH, HTML),
    ("C22", "<!-- x", "A", BOTH, HTML),
    ("C23", "<b>x</b>\nV\u0435rdict", "A", BOTH, HTML),
    ("C24", "Ver<a\n href=x>dict: REQ<b\n id=x>UEST CHANGES", "A", BOTH, HTML),
    ("C25", "<!--\nVerdict: REQUEST CHANGES\n-->", "A", BOTH, HTML),
    ("C26", "Ver<a", "A", BOTH, OK_A),
    ("C27", "a <b\n", "A", BOTH, OK_A),
    ("C28", "RE\n<div\nQUEST CHANGES", "A", BOTH, HTML),
    ("C29", "<K, V> at line start", "A", BOTH, HTML),
    # D. links and images refused (R3).
    ("D1", "[Ver](http://x)dict: [re](http://x)quest changes", "A", BOTH, LINK),
    ("D2", "![Ver](x.png)dict: request changes", "A", BOTH, LINK),
    ("D3", "Decision: [RE][1]QUEST CHANGES\n\n[1]: http://x", "A", BOTH, LINK),
    ("D4", "[REQ][]UEST CHANGES", "A", BOTH, LINK),
    ("D5", "see [docs](https://x) first", "A", BOTH, LINK),
    ("D6", "[BUG] file.py:12 \u2014 x; see [x] and https://x", "A", BOTH, OK_A),
    ("D7", "[Ver]dict: APPROVE\n\n[Ver]: http://x", "A", BOTH, OK_A),
    ("D8", "[Ver]dict: REQUEST CHANGES\n\n[Ver]: http://x", "A", BOTH, DISAGREES_A),
    ("D9", "RE![X]QUEST CHANGES\n\n[X]: https://example.com/x.png", "A", BOTH, LINK),
    ("D10", "![alt](x.png) looks fine", "A", BOTH, LINK),
    # E. the fold and mixed-script words (R4, R5).
    ("E1", "V\u0435rdict: meh", "A", BOTH, (UNSAFE, "ReviewProseUnsafe: mixed-script word 'V\u0435rdict'")),
    ("E2", "V\u0395RDICT: REQUEST CHANGES", "A", BOTH, MIXED),
    ("E3", "V/\u0395/R/D/I/C/T: request changes", "A", BOTH,
     (UNSAFE, "ReviewProseUnsafe: mixed-script word 'v/\u03b5/r/d/i/c/t'")),
    ("E4", "Verdict: \u0430pprove", "C", BOTH, (UNSAFE, "ReviewProseUnsafe: mixed-script word '\u0430pprove'")),
    ("E5", "\u0475\u0435\u0433\u0501\u0456\u0441t: request changes", "A", BOTH, MIXED),
    ("E6", "\u0475\u0435\u0433\u0501\u0456\u0441: request changes", "A", BOTH, DISAGREES_A),
    ("E7", "r\u00e9sum\u00e9, M\u00fcller, na\u00efve: APPROVE", "A", BOTH, OK_A),
    ("E8", _fullwidth("VERDICT") + ": " + _fullwidth("REQUEST") + " " + _fullwidth("CHANGES"), "A", BOTH,
     DISAGREES_A),
    ("E9", "V\u00e9rdict: REQUEST CHANGES", "A", BOTH, DISAGREES_A),
    ("E10", "\u1d20\u1d07\u0280\u1d05\u026a\u1d04\u1d1b: REQUEST CHANGES", "A", BOTH, DISAGREES_A),
    ("E11", "c\u0153ur", "A", BOTH, (UNSAFE, "ReviewProseUnsafe: mixed-script word 'c\u0153ur'")),
    ("E12", "\u039a\u03b1\u03bb\u03b7\u03bc\u03ad\u03c1\u03b1\n\u043f\u0440\u0438\u0432\u0435\u0442", "A", BOTH,
     OK_A),
    # F. the key (R6).
    ("F1", "Verdict: APPROVE", "A", BOTH, OK_A),
    ("F2", "Verdict: REQUEST CHANGES", "A", BOTH, DISAGREES_A),
    ("F3a", "**Verdict**: APPROVE", "A", BOTH, OK_A),
    ("F3b", "## Verdict: APPROVE", "A", BOTH, OK_A),
    ("F3c", "| Verdict | APPROVE |", "A", BOTH, OK_A),
    ("F4", "FinalVerdict: REQUEST CHANGES", "A", BOTH, DISAGREES_A),
    ("F4b", "FinalVerdict: approved", "A", BOTH, NO_VALUE),
    ("F5", "never dictate; server_dict; driver dict; SERVER_DICT: approved", "A", BOTH, OK_A),
    ("F6", "V_e_r_d_i_c_t: approved", "A", BOTH, NO_VALUE),
    ("F7", "Verdicts: approved", "A", BOTH, NO_VALUE),
    ("F8", "verdictive: approved", "A", BOTH, OK_A),
    ("F9", "VERDICT: approved", "A", BOTH, NO_VALUE),
    ("F10", "The `verdict` field is required.", "A", BOTH, NO_VALUE),
    ("F11", "Verdict: approved; Verdict: APPROVE", "A", BOTH, OK_A),
    # G. the value token (R7, R8).
    ("G1", "Decision: REQUEST-CHANGES", "A", BOTH, DISAGREES_A),
    ("G2", "Decision: request changes", "A", BOTH, DISAGREES_A),
    ("G3", "RE**QUEST** CHANGES", "A", BOTH, DISAGREES_A),
    ("G4", "a-p-p-r-o-v-e", "C", BOTH, DISAGREES_C),
    ("G5", "requestchanges", "A", BOTH, DISAGREES_A),
    ("G6", "I approve of the rename.", "C", BOTH, DISAGREES_C),
    ("G7", "I approved it; disapprove nothing; approval; approves; requests changes", "C", BOTH, OK_C),
    ("G8a", "Verdict: request_changes", "C", BOTH, OK_C),
    ("G8b", "Verdict: Request-Changes", "C", BOTH, OK_C),
    ("G9", "Verdict: APPROVE, not REQUEST CHANGES", "A", BOTH, DISAGREES_A),
    ("G10", "Bug: `i < len` is off by one, so REQUEST CHANGES until `x > 0` holds.", "A", BOTH, DISAGREES_A),
    ("G11", "If `n < 0` the loop never ends. Verdict: REQUEST CHANGES; see `Map<K, V>`.", "A", BOTH,
     DISAGREES_A),
    ("G12", "a <- b; Verdict: REQUEST CHANGES; c -> d", "A", BOTH, DISAGREES_A),
    ("G13", "Verdict: (final) REQUEST CHANGES", "A", BOTH, DISAGREES_A),
    ("G14", "Verdict: the final call is REQUEST CHANGES", "A", BOTH, DISAGREES_A),
    ("G15", "Verdict: " + _fullwidth("APPROVE"), "C", BOTH, DISAGREES_C),
    ("G16", "weApprove", "C", BOTH, OK_C),
    ("G17", "Verdict: approve2", "A", BOTH, OK_A),
    ("G18", "Decision: REQUEST\nCHANGES", "A", BOTH, DISAGREES_A),
    ("G19", "Decision: REQUEST\n\nCHANGES", "A", BOTH, OK_A),
    ("G21", "RE\n\n[x]: /url\n\nQUEST CHANGES", "A", BOTH, OK_A),
    ("G22", "RE\n```x\n```\nQUEST CHANGES", "A", BOTH, OK_A),
    ("G22b", "RE\n```\n```\nQUEST CHANGES", "A", BOTH, OK_A),
    ("G23", "RE\n\n- [x] .\n\nQUEST CHANGES", "A", BOTH, OK_A),
    ("G24", "RE\n# heading\nQUEST CHANGES", "A", BOTH, OK_A),
    ("G25", "RE\n---\nQUEST CHANGES", "A", BOTH, DISAGREES_A),
    ("G26", "RE\n> QUEST CHANGES", "A", BOTH, DISAGREES_A),
    # H. key lines and bare keys (R9): the one order-dependent section.
    ("H1", "Verdict: approved", "A", BOTH, NO_VALUE),
    ("H2", "Verdict: meh", "A", BOTH, NO_VALUE),
    ("H3", "Verdict: (final)", "A", BOTH, NO_VALUE),
    ("H4", "## Verdict", "A", PROSE_FIRST, OK_A),
    ("H5", "## Verdict", "A", RECORD_FIRST, NO_VALUE),
    ("H6", "## Verdict\nREQUEST CHANGES", "A", PROSE_FIRST, DISAGREES_A),
    ("H7", "## Verdict\nAPPROVE", "A", PROSE_FIRST, OK_A),
    ("H8", "## Verdict\nmeh", "A", PROSE_FIRST, NO_VALUE),
    ("H9", "## Verdict\nmeh", "A", RECORD_FIRST, NO_VALUE),
    ("H10", "## Verdict\n\n\nAPPROVE", "A", PROSE_FIRST, OK_A),
    ("H11", "## Verdict <!-- draft -->\nrequest changes", "A", PROSE_FIRST, HTML),
    ("H12", "Verdict:\n```text\nAPPROVE\n```", "A", PROSE_FIRST, NO_VALUE),
    ("H13", "Verdict: REQUEST\nCHANGES", "A", BOTH, DISAGREES_A),
    ("H13b", "Intro.\nVerdict: REQUEST\nCHANGES", "A", BOTH, DISAGREES_A),
    ("H14", "Verdict: the\ncall is REQUEST CHANGES", "A", BOTH, NO_VALUE),
    ("H15", "Verdict: REQUEST\n\nCHANGES", "A", BOTH, NO_VALUE),
    # I. prose placement and fences.
    ("I1", "```text\nVerdict: REQUEST CHANGES\n```", "A", BOTH, DISAGREES_A),
    ("I4", "Verdict:\u2028REQUEST CHANGES", "A", BOTH, (UNSAFE, "ReviewProseUnsafe: U+2028")),
    ("I5", "Verdict:\u0085REQUEST CHANGES", "A", BOTH, DISAGREES_A),
    ("I6", "\n".join(f"Line {number} of the notes." for number in range(200)), "A", BOTH, OK_A),
]

# Siblings of rows that start a line with `<`: the HTML block check refuses
# those on its own, so these put the same markup mid-line, where only the
# paragraph's tag and `<!` / `<?` search can refuse it. E10b is a small-capital
# value, which only the Latin fold makes a token.
PROSE_ROWS += [
    ("C4b", "```text\nx <!-- > Verdict: REQUEST CHANGES -->\n```", "A", BOTH, HTML),
    ("C7b", "Note: <!-- Verdict: APPROVE -->", "A", BOTH, HTML),
    ("C8b", "Note: <b>Verdict</b>: APPROVE", "A", BOTH, HTML),
    ("C10b", "x <b>Ver</b>dict: <i>re</i>quest changes", "A", BOTH, HTML),
    ("C11b", "x </div>", "A", BOTH, HTML),
    ("C12b", "x <br/>", "A", BOTH, HTML),
    ("C13b", 'x <a href="x">t', "A", BOTH, HTML),
    ("C14b", "x <?php x ?>", "A", BOTH, HTML),
    ("C15b", "x <![CDATA[x]]>", "A", BOTH, HTML),
    ("C22b", "x <!-- x", "A", BOTH, HTML),
    ("C25b", "x <!--\nVerdict: REQUEST CHANGES\n-->", "A", BOTH, HTML),
    ("E10b", "\u1d00\u1d18\u1d18\u0280\u1d0f\u1d20\u1d07", "C", BOTH, DISAGREES_C),
]


def _ordered(prose: str, record: str, order: str) -> str:
    prose = prose.rstrip("\n") + "\n"
    return prose + "\n" + record if order == "prose-first" else record + "\n" + prose


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param(_ordered(prose, RECORDS[record], order), expected, id=f"{case}.{order}")
        for case, prose, record, orders, expected in PROSE_ROWS
        for order in orders
    ],
)
def test_prose_row(text, expected):
    _expect_parse(text, expected)


def test_G20_the_record_ends_a_segment():
    _expect_parse("REQUEST\n" + RA + "CHANGES\n", OK_A)


def test_I2_an_unterminated_fence_after_the_record_is_prose():
    _expect_parse(RA + "\n```\nVerdict: REQUEST CHANGES\n", DISAGREES_A)


def test_I3_prose_before_the_interval_record_is_read():
    result = review_phase.run_phase(
        _envelope("Verdict: REQUEST CHANGES\n\n" + RA + "INTERVAL: none\n")
    )
    assert result["status"] == "failure", result["reason"]
    assert DISAGREES_A[1] in result["reason"], result["reason"]


def test_I3b_prose_after_the_interval_record_is_the_interval_refusal():
    result = review_phase.run_phase(
        _envelope(RA + "INTERVAL: none\nVerdict: REQUEST CHANGES\n")
    )
    assert result["status"] == "failure", result["reason"]
    assert "review_interval_missing" in result["reason"], result["reason"]


# --- J. every omp parse error is terminal (R10) -------------------------------

TERMINAL_ROWS = [
    ("J1", "The change looks correct to me.\n", "ReviewRecordMissing"),
    ("J2", "Verdict: REQUEST CHANGES\n\n" + RA, "VerdictConflict"),
    ("J3", "<b>x</b>\n\n" + RA, "ReviewProseUnsafe"),
]


@pytest.mark.parametrize(
    "text, fragment", [pytest.param(text, fragment, id=case) for case, text, fragment in TERMINAL_ROWS]
)
def test_terminal_row(text, fragment):
    result = review_phase.run_phase(_envelope(text + "INTERVAL: none\n"))
    assert result["status"] == "failure", result["reason"]
    assert fragment in result["reason"], result["reason"]


# --- K. the prompt contract (R11) ---------------------------------------------


def _skill_contract() -> tuple[str, str]:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    contract = re.search(r"^````text\n(.*?)^````\n", skill, re.MULTILINE | re.DOTALL)
    assert contract, "SKILL.md has no ````text OMP contract block"
    return contract.group(1), skill[contract.end():].lstrip("\n").split("\n\n", 1)[0]


def test_K1_the_contract_names_what_prose_must_not_contain():
    block, _ = _skill_contract()
    flat = " ".join(block.split())
    assert "The block is the only verdict." in flat
    for phrase in (
        "`verdict`",
        "`approve` or `request changes` in any case",
        "raw HTML",
        "Markdown links or images",
        "HTML character references",
        "non-ASCII letter inside an otherwise-ASCII word",
        "`Vec[T]`",
    ):
        assert phrase in flat, f"the OMP contract does not say {phrase!r}"


def test_K2_the_paragraph_after_the_contract_states_the_rules():
    _, paragraph = _skill_contract()
    flat = " ".join(paragraph.split())
    for name in ("`ReviewProseUnsafe`", "`VerdictConflict`"):
        assert name in flat, f"the paragraph does not name {name}"
    assert "A key-only line takes the next non-blank line, or the record after it, as its value." in flat
    assert "on a line with no key" not in flat
    assert "raw HTML" in flat and "link" in flat


# --- L. whitespace as the renderer reads it, through run_phase ---------------
#
# Each row sits beside an APPROVE record and must be refused or conflict, never
# approved. L1-L6: a form feed or vertical tab is whitespace inside a tag, so
# the tag is raw HTML. L7-L9: a line of only a no-break, ideographic or em
# space is not blank, so the paragraph and its token run on. L10-L12: `\x1c`,
# `\x85` and `\f` end no line, so no blank or fence line splits the token. The
# controls: a form feed ends an unquoted attribute value, so `<b id=y\f.>` is
# text (L6b); G18 still conflicts (L13); a clean APPROVE paragraph succeeds;
# and a line of spaces and tabs, `\r\n\r\n` or `\r\r` is still a paragraph
# break.

WHITESPACE_ROWS = [
    ("L1", "REQUEST CHAN<b\f>GES", BOTH, HTML),
    ("L2", "REQUEST CHAN<b\fid=x>GES", BOTH, HTML),
    ("L3", "REQUEST CHAN<b\v>GES", BOTH, HTML),
    ("L4", "REQUEST CHAN<b\vid=x>GES", BOTH, HTML),
    ("L5", "REQUEST CHAN</b\f>GES", BOTH, HTML),
    ("L6", "REQUEST CHAN<b id=x\fy=z>GES", BOTH, HTML),
    ("L6b", "x <b id=y\f.> z", BOTH, OK_A),
    ("L7", "REQUEST\n\u00a0\nCHANGES", BOTH, DISAGREES_A),
    ("L8", "REQUEST\n\u3000\nCHANGES", BOTH, DISAGREES_A),
    ("L9", "REQUEST\n\u2003\nCHANGES", BOTH, DISAGREES_A),
    ("L10", "REQUEST\x1c\x1cCHANGES", BOTH, DISAGREES_A),
    ("L11", "REQUEST\x85\x85CHANGES", BOTH, DISAGREES_A),
    # Prose first, the plain fence `splitlines` sees would swallow the record.
    ("L12", "REQUEST\f```\nCHANGES", RECORD_FIRST, DISAGREES_A),
    ("L13", "Decision: REQUEST\nCHANGES", BOTH, DISAGREES_A),
    ("L14", "The change is correct and tested.\nVerdict: APPROVE", BOTH, OK_A),
    ("L15", "Decision: REQUEST\n \t\nCHANGES", BOTH, OK_A),
    ("L16", "Decision: REQUEST\r\n\r\nCHANGES", BOTH, OK_A),
    ("L17", "Decision: REQUEST\r\rCHANGES", BOTH, OK_A),
]


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param(_ordered(prose, RA, order), expected, id=f"{case}.{order}")
        for case, prose, orders, expected in WHITESPACE_ROWS
        for order in orders
    ],
)
def test_whitespace_row(text, expected):
    result = review_phase.run_phase(_envelope(text + "INTERVAL: none\n"))
    kind, detail = expected
    if kind == "ok":
        assert result["status"] == "success", result["reason"]
        assert result["artifacts"]["findings.json"]["verdict"] == detail
        return
    findings = (result.get("artifacts") or {}).get("findings.json") or {}
    assert result["status"] == "failure", f"{result['status']} {findings.get('verdict')}"
    assert detail in result["reason"], result["reason"]


# --- M. CommonMark structure read as written, never after NFKC --------------
#
# Every structure test (line splitting, blank lines, fence openings with the
# info-string rule, HTML block starts, raw HTML tags and comments, links and
# images, character references) reads the raw line; NFKC text feeds token
# matching and can only add a refusal. M1-M4: a fullwidth quote or `<` is
# legal inside an attribute value, so the tag is raw HTML. M5-M6: a fullwidth
# backtick or tilde run is no fence, so the paragraph and its token run on.
# M7-M8: a backtick line whose info string holds a backtick is no fence
# either, and is refused; prose first, the record fence opens on it and
# swallows the record. M9 is G18, and M10 a clean APPROVE paragraph.

NOT_A_FENCE = (UNSAFE, "ReviewProseUnsafe: backtick line that is not a fence outside review record")
MISSING = (review_phase.ReviewRecordMissing, "ReviewRecordMissing")

STRUCTURE_ROWS = [
    ("M1", "REQUEST CHAN<b x=\"\uff02\">GES", {"prose-first": HTML, "record-first": HTML}),
    ("M2", "REQUEST CHAN<b x=a\uff02>GES", {"prose-first": HTML, "record-first": HTML}),
    ("M3", "REQUEST CHAN<b x=a\uff1c>GES", {"prose-first": HTML, "record-first": HTML}),
    ("M4", "REQUEST CHAN<b x='\uff07'>GES", {"prose-first": HTML, "record-first": HTML}),
    ("M5", "REQUEST\n\uff40\uff40\uff40\nCHANGES", {"prose-first": DISAGREES_A, "record-first": DISAGREES_A}),
    ("M6", "REQUEST\n\uff5e\uff5e\uff5e\nCHANGES", {"prose-first": DISAGREES_A, "record-first": DISAGREES_A}),
    ("M7", "REQUEST\n```x`\nCHANGES", {"prose-first": MISSING, "record-first": NOT_A_FENCE}),
    ("M8", "REQUEST\n```.`\nCHANGES", {"prose-first": MISSING, "record-first": NOT_A_FENCE}),
    ("M9", "Decision: REQUEST\nCHANGES", {"prose-first": DISAGREES_A, "record-first": DISAGREES_A}),
    ("M10", "The change is correct and tested.\nVerdict: APPROVE", {"prose-first": OK_A, "record-first": OK_A}),
]


def _expect_run_phase(text: str, expected: tuple) -> None:
    result = review_phase.run_phase(_envelope(text + "INTERVAL: none\n"))
    kind, detail = expected
    if kind == "ok":
        assert result["status"] == "success", result["reason"]
        assert result["artifacts"]["findings.json"]["verdict"] == detail
        return
    findings = (result.get("artifacts") or {}).get("findings.json") or {}
    assert result["status"] == "failure", f"{result['status']} {findings.get('verdict')}"
    assert detail in result["reason"], result["reason"]


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param(_ordered(prose, RA, order), expected, id=f"{case}.{order}")
        for case, prose, by_order in STRUCTURE_ROWS
        for order, expected in by_order.items()
    ],
)
def test_structure_row(text, expected):
    _expect_run_phase(text, expected)


@functools.cache
def _nfkc_changed() -> tuple[str, ...]:
    """Every code point whose NFKC form differs from itself."""
    return tuple(
        chr(point)
        for point in range(0x110000)
        if not 0xD800 <= point <= 0xDFFF
        and unicodedata.normalize("NFKC", chr(point)) != chr(point)
    )


def _has_letter(text: str) -> bool:
    return any(unicodedata.category(character).startswith("L") for character in text)


# Where the code point sits out of the reader's sight: inside a tag or a
# comment, which the renderer does not show.
HIDDEN_POSITIONS = {
    "attr-double-quoted": "REQUEST CHAN<b x=\"{c}\">GES",
    "attr-single-quoted": "REQUEST CHAN<b x='{c}'>GES",
    "attr-unquoted": "REQUEST CHAN<b x=a{c}>GES",
    "comment": "REQUEST CHAN<!--{c}-->GES",
}
# Where the reader sees it, between the halves of the token, or (bare-key)
# on the line a key-only line takes as its value.
VISIBLE_POSITIONS = {
    "fence-run": "REQUEST\n{c}{c}{c}\nCHANGES",
    "fence-info": "REQUEST\n```{c}`\nCHANGES",
    "fence-info-letter": "REQUEST\n```{c}`x\nCHANGES",
    "blank-line": "REQUEST\n{c}\nCHANGES",
    "html-block-start": "REQUEST\n<{c}\nCHANGES",
    "link-close": "REQUEST CHAN]{c}GES",
    "link-open": "REQUEST CHAN{c}(GES",
    "image": "REQUEST CHAN!{c}GES",
    "bare-key": "Verdict:\n{c}\nAPPROVE",
}


def _approvals(template: str, points) -> list[str]:
    """The code points whose prose, in either order beside APPROVE, approves."""
    approved = []
    for point in points:
        prose = template.replace("{c}", point)
        for order in BOTH:
            try:
                verdict, _ = review_phase.parse_omp(_ordered(prose, RA, order))
            except review_phase.ParseError:
                continue
            if verdict == "approved":
                approved.append(f"U+{ord(point):04X} {order}")
                break
    return approved


@pytest.mark.parametrize("position", list(HIDDEN_POSITIONS))
def test_sweep_no_nfkc_changing_code_point_in_a_hidden_position_approves(position):
    """Every NFKC-changing code point, letters included, inside a tag or comment.

    The tag or comment is raw HTML as written, so the reply is refused,
    whatever NFKC makes of the code point.
    """
    approved = _approvals(HIDDEN_POSITIONS[position], _nfkc_changed())
    assert approved == [], f"{len(approved)} approved, first {approved[:12]}"


@pytest.mark.parametrize("position", list(VISIBLE_POSITIONS))
def test_sweep_no_nfkc_changing_non_letter_in_a_visible_position_approves(position):
    """Every NFKC-changing code point that is not a letter, raw or after NFKC.

    Excluded: a code point that is a letter (category L*) as written or whose
    NFKC form holds one. The reader sees a letter between the halves, which
    the design declares is not a token (a visible letter inside a token,
    `REQ-x-UEST`). Under Unicode 16.0 (Python 3.14) that is 4,467 of the
    4,964 NFKC-changing code points, leaving 497 swept here; other Unicode
    versions shift both counts slightly. The fence-info-letter position holds
    the letter `x` by construction (the M7 shape): a backtick line that is not
    a fence is refused, so that letter never decides it. NFKC turns a fullwidth
    grave accent (U+FF40) there into a backtick and the line into a fence,
    which is why the not-a-fence test reads the raw line.
    """
    points = [
        point
        for point in _nfkc_changed()
        if not _has_letter(point) and not _has_letter(unicodedata.normalize("NFKC", point))
    ]
    assert points, "no non-letter NFKC-changing code point to sweep"
    approved = _approvals(VISIBLE_POSITIONS[position], points)
    assert approved == [], f"{len(approved)} approved, first {approved[:12]}"


# --- N. a value token beside a symbol NFKC turns into letters -----------------
# NFKC turns some symbols that are not letters into letters (U+2122 into `TM`),
# which would adjoin a token that is whole as written; the token is also read
# as written, so the reply is refused.

ADJOINING_ROWS = [
    ("N1", "REQUEST CHANGES\u2122"),
    ("N2", "\u2122REQUEST CHANGES"),
    ("N3", "This needs work: REQUEST CHANGES\u2120"),
    ("N4", "REQUEST CHANGES\u3389"),
    ("N5", "REQUEST CHANGES\u24c7"),
]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_ordered(prose, RA, order), id=f"{case}.{order}")
        for case, prose in ADJOINING_ROWS
        for order in BOTH
    ],
)
def test_a_token_beside_a_symbol_nfkc_makes_a_letter_is_read(text):
    _expect_parse(text, DISAGREES_A)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            _ordered("The change is correct and the tests cover it.", RA, order),
            id=f"N6.{order}",
        )
        for order in BOTH
    ],
)
def test_a_clean_approve_record_with_ordinary_prose_approves(text):
    _expect_parse(text, OK_A)


ADJOINING_POSITIONS = {
    "after": "REQUEST CHANGES{c}",
    "before": "{c}REQUEST CHANGES",
}


@pytest.mark.parametrize("position", list(ADJOINING_POSITIONS))
def test_sweep_no_nfkc_changing_non_letter_adjoining_a_token_approves(position):
    """Every NFKC-changing code point that is not a letter as written.

    Its NFKC form may hold letters (U+2122 is `TM`), and those are swept
    too. Excluded: a letter (category L*) and a letter number (category Nl:
    the Roman numerals U+2160-U+217F, whose glyphs are Latin letters, and
    U+3038-U+303A), which the token's bound reads as a letter as written.
    Under Unicode 16.0 (Python 3.14) that leaves 1,411 of the 4,964
    NFKC-changing code points, 944 of them with a letter in their NFKC form;
    other Unicode versions shift the counts slightly.
    """
    points = [
        point
        for point in _nfkc_changed()
        if not _has_letter(point) and unicodedata.category(point) != "Nl"
    ]
    assert points, "no non-letter NFKC-changing code point to sweep"
    approved = _approvals(ADJOINING_POSITIONS[position], points)
    assert approved == [], f"{len(approved)} approved, first {approved[:12]}"


# The structure tests below read the raw text; the first three also search
# the NFKC text, which can only add a refusal. On each, no code point the raw
# reading matches is lost by NFKC, so reading it after NFKC alone could never
# approve more, and no approval sweep can turn red when it is moved there;
# this asserts that, over every code point the unsafe-character check admits. A
# line NFKC leaves alone, around a code point the Latin fold leaves alone,
# reads the same both ways, so only the other lines are compared.

SUPERSET_CHECKS = {
    "html-block-start": (
        lambda line: review_phase._OMP_HTML_BLOCK_START.match(line),
        ["<{c}", "<a{c}", "   <a{c}", "</{c}", "<!{c}", "<?{c}", "{c}<a"],
    ),
    "link-image": (
        lambda line: review_phase._OMP_LINK.search(line),
        ["]({c}", "x]({c}", "][{c}", "![{c}", "{c}](", "{c}![", "{c}]["],
    ),
    "comment-pi": (
        lambda line: review_phase._OMP_RAW_HTML.search(line),
        ["x<{c}", "x<!{c}", "x<?{c}", "{c}<!", "{c}<?"],
    ),
    "character-reference": (
        lambda line: review_phase._OMP_HTML_REFERENCE.search(line),
        ["&#1{c}", "&#x6{c}", "&amp{c}", "{c}&amp;"],
    ),
}


@functools.cache
def _admitted() -> tuple[tuple[str, ...], frozenset[str]]:
    """The code points the unsafe-character check admits, and those it folds."""
    admitted = tuple(
        chr(point)
        for point in range(0x110000)
        if unicodedata.category(chr(point)) not in review_phase._OMP_UNSAFE_CATEGORIES
        and chr(point) not in review_phase._OMP_UNSAFE_FILLERS
    )
    folded = frozenset(
        point for point in admitted if review_phase._latin_fold(point) != point
    )
    return admitted, folded


@pytest.mark.parametrize("check", list(SUPERSET_CHECKS))
def test_the_nfkc_reading_never_loses_a_raw_structure_match(check):
    matches, templates = SUPERSET_CHECKS[check]
    admitted, folded = _admitted()
    lost = []
    for template in templates:
        for point in admitted:
            line = template.replace("{c}", point)
            if point not in folded and unicodedata.is_normalized("NFKC", line):
                continue
            if matches(line) and not matches(review_phase._canonicalize_omp_prose(line)[0]):
                lost.append(f"{template!r} U+{ord(point):04X}")
    assert lost == [], f"{len(lost)} lost, first {lost[:12]}"


def test_nfkc_never_makes_or_removes_a_line_ending():
    """Line splitting reads the raw text; NFKC adds no `\\r` or `\\n` to any
    code point and changes neither, so the split is the same either way."""
    assert unicodedata.normalize("NFKC", "\r\n") == "\r\n"
    made = [
        f"U+{ord(point):04X}"
        for point in _nfkc_changed()
        if set(unicodedata.normalize("NFKC", point)) & {"\r", "\n"}
    ]
    assert made == []

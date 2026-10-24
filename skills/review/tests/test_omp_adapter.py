"""Tests for the structured `omp` backend of review_phase.py."""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SKILL_DIR / "review_phase.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(SKILL_DIR))
import review_phase  # noqa: E402

OMP_REQUEST_CHANGES = (FIXTURES / "omp-request-changes.md").read_text()
OMP_APPROVE = (FIXTURES / "omp-approve.md").read_text()
OMP_NO_RECORD = (FIXTURES / "omp-no-verdict.md").read_text()

MODEL = "example-model"
THINKING = "medium"
HEAD = "a" * 40
# Every return ends with the INTERVAL record run_phase checks before any
# adapter reads it; each case here is about the omp record, so it adds none.
INTERVAL_NONE = "\nINTERVAL: none\n"


def _record(
    verdict: object = "APPROVE",
    *,
    bugs: object = None,
    nits: object = None,
    reviewed_head: object = HEAD,
    extra: dict | None = None,
) -> str:
    payload = {
        "verdict": verdict,
        "bugs": [] if bugs is None else bugs,
        "nits": [] if nits is None else nits,
        "reviewed_head": reviewed_head,
    }
    if extra:
        payload.update(extra)
    return "```review-result\n" + json.dumps(payload, indent=2) + "\n```\n"


def _envelope(return_text: str | None, **params) -> dict:
    inputs = {
        "target": "example/invoice-cli#7",
        "backend": "omp",
        "brief": "Review the pull request for correctness and safety.",
        "return_text": return_text + INTERVAL_NONE,
        "model": MODEL,
        "thinking": THINKING,
    }
    inputs.update(params)
    return {
        "repo/ref": "example/invoice-cli@def456",
        "inputs": inputs,
        "constraints": {"scope": "diff only"},
        "harness_root": None,
        "adapters": {},
        "budget": {"tokens": 0},
    }


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
    )


def _reason(text: str) -> str:
    return review_phase.run_phase(_envelope(text))["reason"]


# --- one structured record supplies the only approving verdict ---------------


def test_request_changes_record_parses_into_the_findings_schema():
    result = review_phase.run_phase(_envelope(OMP_REQUEST_CHANGES))
    assert result["status"] == "success", result["reason"]
    assert result["reason"] == "omp: changes_requested, 3 finding(s)"
    assert result["artifacts"]["review.md"] == OMP_REQUEST_CHANGES + INTERVAL_NONE
    findings = result["artifacts"]["findings.json"]
    assert review_phase.validate_findings(findings) == []
    assert findings["schema"] == "review-findings/v1"
    assert findings["backend"] == "omp"
    assert findings["verdict"] == "changes_requested"
    assert findings["findings"] == [
        {
            "file": "src/export.py",
            "line": 58,
            "rule": "bug",
            "description": (
                "the limit is applied after the rows are written, so `--limit 10` "
                "still writes every row; slice the query result before the write loop."
            ),
            "severity": "error",
        },
        {
            "file": "src/export.py",
            "line": 31,
            "rule": "bug",
            "description": (
                "a negative `--limit` is accepted and silently drops the last row; "
                "reject values below 1 at parse time."
            ),
            "severity": "error",
        },
        {
            "file": "tests/test_export.py",
            "line": 12,
            "rule": "minor",
            "description": "the test checks the exit code only, not the number of rows written.",
            "severity": "info",
        },
    ]


def test_approve_record_parses_with_no_findings():
    result = review_phase.run_phase(_envelope(OMP_APPROVE))
    assert result["status"] == "success", result["reason"]
    assert result["artifacts"]["findings.json"] == {
        "schema": "review-findings/v1",
        "backend": "omp",
        "verdict": "approved",
        "findings": [],
    }


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_record().replace("\n", "\r\n"), id="crlf"),
        pytest.param(_record().replace("```", "~~~"), id="tilde-fence"),
    ],
)
def test_supported_fence_variants_parse(text):
    result = review_phase.run_phase(_envelope(text))
    assert result["status"] == "success", result["reason"]
    assert result["artifacts"]["findings.json"]["verdict"] == "approved"


def test_schema_accepts_omp_as_a_backend():
    schema = json.loads((SKILL_DIR / "findings.schema.json").read_text())
    assert schema["properties"]["backend"]["enum"] == [
        "claude-fresh",
        "grok-notools",
        "omp",
    ]


# --- generated decoration matrix ---------------------------------------------


DECORATIONS = (
    "{key}: {value}",
    "## {key}: {value}",
    "**{key}**: **{value}**",
    "**{key}: {value}**",
    "- {key}: {value}",
    "> `{key}`: `{value}`",
    "## **[{key}]**: **[{value}]**",
    "## {key}\n{value}",
    "## VERDICT\n\n{value}",
    "| field | value |\n|---|---|\n| {key} | {value} |",
    "<b>{key}</b>: {value}",
    "<strong>{key}:</strong> {value}",
    "<strong>{key}</strong>: {value}",
    "𝐕𝐞𝐫𝐝𝐢𝐜𝐭: {value}",
    "{key}：{value}",
    "{key} - {value}",
    "{key} {value}",
    "{key} (final): {value}",
)
PROSE_VERDICTS = ("APPROVE", "REQUEST CHANGES")
DECORATION_CASES = [
    pytest.param(
        decoration,
        value,
        present,
        record_first,
        id=(
            f"decoration-{decoration_index}-"
            f"{value.lower().replace(' ', '-')}-"
            f"{'record' if present else 'no-record'}-"
            f"{'record-first' if record_first else 'prose-first'}"
        ),
    )
    for (decoration_index, decoration), value, present, record_first in itertools.product(
        enumerate(DECORATIONS), PROSE_VERDICTS, (False, True), (False, True)
    )
]


@pytest.mark.parametrize(
    "decoration, prose_value, record_present, record_first",
    DECORATION_CASES,
)
def test_decorated_prose_never_supplies_and_can_only_veto_the_record(
    decoration, prose_value, record_present, record_first
):
    prose = decoration.format(key="Verdict", value=prose_value)
    parts = [_record(), prose] if record_first else [prose, _record()]
    text = "\n".join(parts) if record_present else prose
    result = review_phase.run_phase(_envelope(text))
    if not record_present:
        assert result["status"] == "failure"
        assert "ReviewRecordMissing" in result["reason"]
    elif prose_value == "APPROVE":
        assert result["status"] == "success", result["reason"]
        assert result["artifacts"]["findings.json"]["verdict"] == "approved"
    else:
        assert result["status"] == "failure"
        assert "VerdictConflict" in result["reason"]


# Neutral fixtures make the three prose cases explicit.
@pytest.mark.parametrize(
    "text, expected_status, reason_name",
    [
        pytest.param(
            "<strong>𝐕𝐞𝐫𝐝𝐢𝐜𝐭</strong>： **APPROVE**\n\n" + _record(),
            "success",
            None,
            id="matching-prose",
        ),
        pytest.param(_record(), "success", None, id="no-prose-verdict"),
        pytest.param(
            _record() + "\nVerdict: REQUEST CHANGES\n",
            "failure",
            "VerdictConflict",
            id="different-prose",
        ),
        pytest.param(
            "The résumé change is correct.\n\n" + _record(),
            "success",
            None,
            id="plain-non-ascii-prose",
        ),
    ],
)
def test_neutral_prose_fixtures(text, expected_status, reason_name):
    result = review_phase.run_phase(_envelope(text))
    assert result["status"] == expected_status, result["reason"]
    if reason_name:
        assert reason_name in result["reason"]


@pytest.mark.parametrize(
    "prose",
    [
        pytest.param("Καλημέρα, the change is correct.", id="greek"),
        pytest.param("Le résumé est correct.", id="french"),
        pytest.param("Die Änderung ist korrekt.", id="german"),
    ],
)
@pytest.mark.parametrize("record_first", [False, True], ids=["prose-first", "record-first"])
def test_safe_accented_prose_succeeds_in_both_orders(prose, record_first):
    parts = [_record(), prose] if record_first else [prose, _record()]
    result = review_phase.run_phase(_envelope("\n".join(parts)))
    assert result["status"] == "success", result["reason"]
    assert result["artifacts"]["findings.json"]["verdict"] == "approved"

UNSAFE_PROSE_CASES = [
    pytest.param("Ver\u034fdict: REQUEST CHANGES", id="mn-grapheme-joiner"),
    pytest.param("Ver\u0335dict: REQUEST CHANGES", id="mn-short-stroke-overlay"),
    pytest.param("Verdi\u0301ct: REQUEST CHANGES", id="mn-combining-acute"),
    pytest.param("Ve\u0301rdict: REQUEST CHANGES", id="mn-acute-after-e"),
    pytest.param("Ver\u0338dict: REQUEST CHANGES", id="mn-long-solidus-overlay"),
    pytest.param("Ver\u0488dict: REQUEST CHANGES", id="me-combining-sign"),
    pytest.param("\u202etcidreV\u202c: REQUEST CHANGES", id="cf-bidi-key"),
    pytest.param(
        "\u202eSEGNAHC TSEUQER :tcidreV\u202c",
        id="cf-bidi-whole-line",
    ),
    pytest.param("Ver\u00addict: REQUEST CHANGES", id="cf-soft-hyphen"),
    pytest.param("Verdict:\u200b\nREQUEST CHANGES", id="cf-zero-width-value"),
    pytest.param("Verdict: \u202eSEGNAHC TSEUQER\u202c", id="cf-bidi-value"),
    pytest.param("Ver\u2028dict: REQUEST CHANGES", id="zl-line-separator"),
    pytest.param("Safe prose.\u2029", id="zp-paragraph-separator"),
    pytest.param("Safe \ue000 prose.", id="co-private-use"),
    pytest.param("Safe \u0378 prose.", id="cn-unassigned"),
    pytest.param("Safe \ud800 prose.", id="cs-surrogate"),
    pytest.param("Ver\u2800dict: REQUEST CHANGES", id="braille-filler"),
    pytest.param("Ver\u3164dict: REQUEST CHANGES", id="hangul-filler-3164"),
    pytest.param("Ver\u115fdict: REQUEST CHANGES", id="hangul-filler-115f"),
    pytest.param("Ver\u1160dict: REQUEST CHANGES", id="hangul-filler-1160"),
    pytest.param("Ver\uffa0dict: REQUEST CHANGES", id="hangul-filler-ffa0"),
    pytest.param("Ver&shy;dict: REQUEST CHANGES", id="html-named-reference"),
    pytest.param("&#86;erdict: REQUEST CHANGES", id="html-numeric-reference"),
    # A lookalike letter inside an ASCII word is refused by name, never matched.
    pytest.param("V\u0435rdict: REQUEST CHANGES", id="cyrillic-e"),
]


@pytest.mark.parametrize("prose", UNSAFE_PROSE_CASES)
@pytest.mark.parametrize("record_first", [False, True], ids=["prose-first", "record-first"])
def test_unsafe_prose_is_refused_in_both_orders(prose, record_first):
    parts = [_record(), prose] if record_first else [prose, _record()]
    result = review_phase.run_phase(_envelope("\n".join(parts)))
    assert result["status"] == "failure", result["reason"]
    assert "ReviewProseUnsafe" in result["reason"], result["reason"]


@pytest.mark.parametrize(
    "prose",
    [
        pytest.param("VERDİCT: REQUEST CHANGES", id="turkish-dotted-i"),
        pytest.param("Verdicts: REQUEST CHANGES", id="plural"),
        pytest.param("Verdict_: REQUEST CHANGES", id="underscore"),
        pytest.param("Ver-dict: REQUEST CHANGES", id="hyphenated"),
    ],
)
@pytest.mark.parametrize("record_first", [False, True], ids=["prose-first", "record-first"])
def test_fuzzy_verdict_keys_conflict_in_both_orders(prose, record_first):
    parts = [_record(), prose] if record_first else [prose, _record()]
    reason = _reason("\n".join(parts))
    assert "VerdictConflict" in reason, reason


@pytest.mark.parametrize(
    "prose",
    [
        pytest.param("Ver_dict: REQUEST CHANGES", id="internal-underscore"),
        pytest.param("V_e_r_d_i_c_t: REQUEST CHANGES", id="fully-separated"),
    ],
)
@pytest.mark.parametrize("record_first", [False, True], ids=["prose-first", "record-first"])
def test_underscore_separated_verdict_keys_conflict_in_both_orders(
    prose, record_first
):
    parts = [_record(), prose] if record_first else [prose, _record()]
    reason = _reason("\n".join(parts))
    assert "VerdictConflict" in reason, reason


def test_key_only_verdict_heading_uses_the_following_record_as_its_value():
    result = review_phase.run_phase(_envelope("## Verdict\n\n" + _record()))
    assert result["status"] == "success", result["reason"]
    assert result["artifacts"]["findings.json"]["verdict"] == "approved"


# --- every refusal has its own stable name -----------------------------------


def test_zero_records_is_refused_by_name():
    assert "ReviewRecordMissing" in _reason("Verdict: APPROVE\n")


def test_a_prose_approve_without_a_record_proves_the_label_path_is_gone():
    result = review_phase.run_phase(_envelope("## Verdict: APPROVE\n"))
    assert result["status"] == "failure"
    assert "ReviewRecordMissing" in result["reason"]
    assert result["artifacts"]["findings.json"] is None


def test_two_records_are_refused_by_name():
    assert "ReviewRecordMultiple" in _reason(_record() + "\n" + _record())


def test_an_unterminated_second_record_counts_as_multiple():
    unterminated = _record("REQUEST CHANGES").rsplit("```", 1)[0]
    reason = _reason(_record() + "\n" + unterminated)
    assert "ReviewRecordMultiple" in reason, reason


def test_one_unterminated_record_is_refused_by_name():
    unterminated = _record().rsplit("```", 1)[0]
    reason = _reason(unterminated)
    assert "ReviewRecordUnterminated" in reason, reason


def test_invalid_json_is_refused_by_name():
    text = '```review-result\n{"verdict": "APPROVE",}\n```\n'
    assert "ReviewRecordInvalidJSON" in _reason(text)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "```review-result\n"
            '{"verdict":"APPROVE","bugs":[],"nits":[]}\n'
            "```\n",
            id="missing-field",
        ),
        pytest.param(_record(extra={"other": True}), id="extra-field"),
        pytest.param(
            "```review-result\n"
            '{"verdict":"REQUEST CHANGES","verdict":"APPROVE",'
            f'"bugs":[],"nits":[],"reviewed_head":"{HEAD}"'
            "}\n```\n",
            id="duplicate-field",
        ),
        pytest.param(_record(verdict=1), id="typed-verdict"),
        pytest.param(_record(verdict="CLEARED"), id="unknown-verdict"),
        pytest.param(_record(bugs="none"), id="non-list-bugs"),
        pytest.param(_record(nits={}), id="non-list-nits"),
        pytest.param(_record(bugs=[1]), id="typed-bug-entry"),
        pytest.param(_record(reviewed_head="abc123"), id="short-reviewed-head"),
        pytest.param(_record(reviewed_head="A" * 40), id="uppercase-reviewed-head"),
        pytest.param(_record(bugs=[""]), id="empty-bug-entry"),
        pytest.param(_record(bugs=["  "]), id="whitespace-bug-entry"),
        pytest.param(_record(verdict="approve"), id="lowercase-verdict"),
        pytest.param(_record(verdict="APPROVE "), id="trailing-space-verdict"),
        pytest.param(
            _record(bugs=["src/export.py:58 — broken"]),
            id="approve-with-bugs",
        ),
        pytest.param(
            _record(reviewed_head=HEAD + "\n"),
            id="newline-reviewed-head",
        ),
    ],
)
def test_schema_failures_are_refused_by_name(text):
    result = review_phase.run_phase(_envelope(text))
    assert result["status"] == "failure"
    assert "ReviewRecordSchema" in result["reason"]


@pytest.mark.parametrize(
    "prose",
    ["Verdict: REQUEST CHANGES", "Verdict: approved"],
)
def test_conflicting_prose_is_refused_by_name(prose):
    assert "VerdictConflict" in _reason(prose + "\n\n" + _record("APPROVE"))


def test_an_unparseable_verdict_mention_is_refused_by_name():
    reason = _reason("The verdict remains under discussion.\n\n" + _record())
    assert "VerdictConflict" in reason, reason


def test_a_plain_fence_cannot_hide_conflicting_prose():
    text = _record() + "\n```\nVerdict: REQUEST CHANGES\n"
    reason = _reason(text)
    assert "VerdictConflict" in reason, reason


@pytest.mark.parametrize("indent", ["> ", "    "])
def test_a_nested_second_record_cannot_bypass_the_conflict_guard(indent):
    nested = indent + _record("REQUEST CHANGES").replace("\n", "\n" + indent)
    reason = _reason(_record() + "\n" + nested)
    assert "VerdictConflict" in reason, reason


def test_clean_sentinel_never_upgrades_a_missing_record():
    text = "### Issues\nNo bugs or security issues found.\nVerdict: APPROVE\n"
    result = review_phase.run_phase(_envelope(text))
    assert result["status"] == "failure"
    assert "ReviewRecordMissing" in result["reason"]


def test_clean_sentinel_never_upgrades_a_schema_failure():
    text = (
        "No bugs or security issues found.\n\n"
        "```review-result\n"
        '{"verdict":"APPROVE","bugs":[],"nits":[],"reviewed_head":"bad"}\n'
        "```\n"
    )
    result = review_phase.run_phase(_envelope(text))
    assert result["status"] == "failure"
    assert "ReviewRecordSchema" in result["reason"]


@pytest.mark.parametrize(
    "text",
    [
        "```review-result extra\n"
        '{"verdict":"APPROVE","bugs":[],"nits":[],"reviewed_head":"'
        + HEAD
        + '"}\n```\n',
        "> ```review-result\n"
        '> {"verdict":"APPROVE","bugs":[],"nits":[],"reviewed_head":"'
        + HEAD
        + '"}\n> ```\n',
        "````text\n"
        "```review-result\n"
        '{"verdict":"APPROVE","bugs":[],"nits":[],"reviewed_head":"'
        + HEAD
        + '"}\n```\n````\n',
    ],
    ids=("info-string-variant", "blockquote", "nested-fence"),
)
def test_only_a_top_level_exact_review_result_fence_counts(text):
    assert "ReviewRecordMissing" in _reason(text)


@pytest.mark.parametrize("text", ["", "\n", OMP_NO_RECORD])
def test_adapter_raises_on_empty_or_missing_record(text):
    with pytest.raises(review_phase.ReviewRecordMissing):
        review_phase.parse_omp(text)


# --- attribution and command-line plumbing remain unchanged ------------------


def test_attribution_names_the_lane_model_and_thinking_level():
    result = review_phase.run_phase(_envelope(OMP_REQUEST_CHANGES))
    assert result["attribution"] == {
        "lane": "omp",
        "model": MODEL,
        "thinking": THINKING,
    }


@pytest.mark.parametrize(
    "model, thinking",
    [("other-model-2", "low"), ("vendor/model-x:latest", "high")],
)
def test_attribution_echoes_the_runner_parameters(model, thinking):
    result = review_phase.run_phase(
        _envelope(OMP_APPROVE, model=model, thinking=thinking)
    )
    assert result["status"] == "success", result["reason"]
    assert result["attribution"] == {
        "lane": "omp",
        "model": model,
        "thinking": thinking,
    }


def test_failed_parse_still_names_the_lane():
    result = review_phase.run_phase(_envelope(OMP_NO_RECORD))
    assert result["status"] == "failure"
    assert result["attribution"] == {
        "lane": "omp",
        "model": MODEL,
        "thinking": THINKING,
    }


@pytest.mark.parametrize(
    "params, fragment",
    [
        ({"model": None}, "inputs.model"),
        ({"model": ""}, "inputs.model"),
        ({"model": "two words"}, "inputs.model"),
        ({"thinking": None}, "inputs.thinking"),
        ({"thinking": ""}, "inputs.thinking"),
        ({"thinking": 3}, "inputs.thinking"),
        ({"thinking": "banana"}, "inputs.thinking"),
        ({"model": "model\n"}, "inputs.model"),
    ],
)
def test_missing_or_malformed_parameters_block_rather_than_default(params, fragment):
    result = review_phase.run_phase(_envelope(OMP_APPROVE, **params))
    assert result["status"] == "blocked"
    assert fragment in result["reason"]
    assert result["artifacts"] == {"review.md": None, "findings.json": None}
    assert "attribution" not in result


@pytest.mark.parametrize(
    "thinking", ["off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"]
)
def test_every_omp_thinking_level_is_accepted(thinking):
    result = review_phase.run_phase(_envelope(OMP_APPROVE, thinking=thinking))
    assert result["status"] == "success", result["reason"]
    assert result["attribution"]["thinking"] == thinking


def test_from_text_passes_model_and_thinking_through(tmp_path: Path):
    reply = tmp_path / "reply.md"
    reply.write_text(OMP_REQUEST_CHANGES + INTERVAL_NONE, encoding="utf-8")
    result = _run_cli(
        "--from-text",
        str(reply),
        "--backend",
        "omp",
        "--model",
        "cli-model-3",
        "--thinking",
        "low",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["attribution"] == {
        "lane": "omp",
        "model": "cli-model-3",
        "thinking": "low",
    }
    assert payload["artifacts"]["findings.json"]["verdict"] == "changes_requested"


def test_from_text_without_model_is_blocked(tmp_path: Path):
    reply = tmp_path / "reply.md"
    reply.write_text(OMP_REQUEST_CHANGES + INTERVAL_NONE, encoding="utf-8")
    result = _run_cli(
        "--from-text", str(reply), "--backend", "omp", "--thinking", "low"
    )
    assert result.returncode == 3, result.stderr
    assert "inputs.model" in json.loads(result.stdout)["reason"]


def test_jev_fallback_does_not_rescue_a_missing_record(monkeypatch):
    consulted = []

    def fake_consult(backend, reply, intervals):
        consulted.append((backend, reply))
        return review_phase._finish(
            backend, review_phase.APPROVED, [], reply, source="jev"
        )

    monkeypatch.setattr(review_phase, "_consult_jev", fake_consult)
    envelope = _envelope("Verdict: APPROVE\n")
    envelope["adapters"] = {"jev_fallback": True}
    result = review_phase.run_phase(envelope)
    assert result["status"] == "failure"
    assert "ReviewRecordMissing" in result["reason"]
    assert consulted == []

"""Tiny structural test-runner shim for the `review` skill.

DISCOVERED FACT: `review` ships as pure SKILL.md prose with no code, no
`config.json`, and no test suite of its own — and neither does `humanizer`,
the alternative leaf-skill candidate considered for this job. Sixteen of
claude-toolkit's twenty-five skills have no tests today; both `review` and
`humanizer` are in that set. This file is a small test-runner shim written
for exactly that situation — not a port of `scripts/coupling_gauge.py`. It
is deliberately independent of that gauge: a gauge that silently stopped
scanning skills/review/ would still be caught here, and a bug in this
file's own pattern would still be caught by the gauge. Neither can go green
covering for the other's blind spot.

Runs green under the clean-account job's own contract: HARNESS_ROOT unset,
no foundry checkout present, no network.
"""
import json
import os
import re
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SKILL_MD = SKILL_DIR / "SKILL.md"
CONFIG_JSON = SKILL_DIR / "config.json"

# Independent of coupling_gauge.py's literal categories on purpose (see the
# module docstring above) — a plain, readable pattern for the same families
# the gauge falsifies: a configured harness root env var, foundry's own
# standards/roadmap/decisions/topics tree, and the herald repo.
_COUPLING_PATTERN = re.compile(
    r"~/foundry"
    r"|\$HOME/foundry"
    r"|\$\{HOME\}/foundry"
    r"|/Users/\w+/foundry"
    r"|~/\.herald"
    r"|~/herald"
    r"|HARNESS_ROOT"
    r"|foundry/roadmaps"
    r"|foundry/standards"
    r"|foundry/decisions"
    r"|foundry/topics"
)


def test_harness_root_is_unset():
    # The clean-account job's own precondition for this step, re-asserted
    # here so a future direct `pytest skills/review/tests` (outside the
    # job) still proves it rather than assuming it.
    assert "HARNESS_ROOT" not in os.environ


def test_skill_md_exists_and_declares_kind_portable():
    assert SKILL_MD.is_file(), f"{SKILL_MD} missing"
    lines = SKILL_MD.read_text().splitlines()
    assert lines and lines[0] == "---", "SKILL.md must open with a frontmatter block"
    end = lines[1:].index("---") + 1
    frontmatter = "\n".join(lines[1:end])
    assert re.search(r"^kind:\s*portable\s*$", frontmatter, re.MULTILINE), (
        "review/SKILL.md must declare kind: portable"
    )
    assert re.search(r"^name:\s*review\s*$", frontmatter, re.MULTILINE), (
        "review/SKILL.md must declare name: review"
    )


def test_config_json_if_present_parses():
    # review ships no config.json today; this stays true if one is ever
    # added without anyone updating this file.
    if CONFIG_JSON.exists():
        json.loads(CONFIG_JSON.read_text())


def test_no_harness_coupling_literals_in_skill_md():
    text = SKILL_MD.read_text()
    match = _COUPLING_PATTERN.search(text)
    assert match is None, (
        f"review/SKILL.md contains a coupling literal: {match.group(0)!r}"
    )

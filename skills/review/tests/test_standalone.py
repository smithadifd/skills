"""Structural checks for a standalone `review` installation.

The skill instructions must work from an installed copy with no sibling
project checkout. These checks run with HARNESS_ROOT unset and no network.
"""
import json
import os
import re
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SKILL_MD = SKILL_DIR / "SKILL.md"
CONFIG_JSON = SKILL_DIR / "config.json"

# A configured root or a project data path outside the installed skill is a
# coupling literal. The sibling repository name is intentionally arbitrary.
_COUPLING_PATTERN = re.compile(
    r"HARNESS_ROOT"
    r"|(?:~|\$HOME|\$\{HOME\}|/Users/[^/\s]+)/[A-Za-z0-9_-]+/"
    r"(?:roadmaps|standards|decisions|topics)"
)


def test_generic_outside_reference_is_rejected():
    sentinel = "~/" + "private-sibling-repo" + "/roadmaps"
    assert _COUPLING_PATTERN.search(sentinel) is not None


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

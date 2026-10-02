"""Structural checks for a standalone `review` installation.

The skill instructions must work from an installed copy with no sibling
project checkout. These checks run with HARNESS_ROOT unset and no network.
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
SKILL_MD = SKILL_DIR / "SKILL.md"
CONFIG_JSON = SKILL_DIR / "config.json"
CHECK_SCRIPT = SKILL_DIR.parent.parent / "ci" / "check.sh"

@pytest.mark.parametrize(
    "reference",
    [
        "~/" + "private-sibling-repo" + "/scripts/run.sh",
        "$HOME/" + "private-sibling-repo" + "/standards/example.md",
        "${HOME}/" + "private-sibling-repo" + "/decisions/example.md",
        "/Users/example/" + "private-sibling-repo" + "/topics/example.md",
        "/Users/example/" + "private-sibling-repo" + "/.claude/example.md",
        "/home/example/" + "private-sibling-repo" + "/scripts/run.sh",
        "/tmp/" + "private-sibling-repo" + "/topics/example.md",
    ],
)
def test_installed_outside_reference_is_rejected(tmp_path, reference):
    (tmp_path / "example.md").write_text(reference, encoding="utf-8")
    result = subprocess.run(
        ["bash", str(CHECK_SCRIPT), "outside-references", str(tmp_path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "installed files contain an outside project reference" in result.stderr


@pytest.mark.parametrize(
    "reference",
    [
        "~/.claude/skills/example/SKILL.md",
        "$HOME/.claude/skill-config/example.json",
        "${HOME}/.claude/topics/example.md",
        "/Users/example/.claude/skills/example/SKILL.md",
        "/home/example/.claude/skills/example/SKILL.md",
    ],
)
def test_installed_generic_path_is_allowed(tmp_path, reference):
    (tmp_path / "example.md").write_text(reference, encoding="utf-8")
    result = subprocess.run(
        ["bash", str(CHECK_SCRIPT), "outside-references", str(tmp_path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


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
    match = re.search(r"HARNESS_ROOT", text)
    assert match is None, (
        f"review/SKILL.md contains a coupling literal: {match.group(0)!r}"
    )

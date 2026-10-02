"""Structural checks for a standalone `review` installation.

The skill instructions must work from an installed copy with no sibling
project checkout. These checks run with HARNESS_ROOT unset and no network.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
SKILL_MD = SKILL_DIR / "SKILL.md"
CONFIG_JSON = SKILL_DIR / "config.json"
CHECK_SCRIPT = SKILL_DIR.parent.parent / "ci" / "check.sh"


def private_reference_repo(tmp_path, include_tests=True):
    repo = tmp_path / "repo"
    directories = ["ci", "skills/example", "agents"]
    if include_tests:
        directories.append("skills/example/tests")
    for directory in directories:
        (repo / directory).mkdir(parents=True)
    shutil.copy2(CHECK_SCRIPT, repo / "ci/check.sh")
    shutil.copy2(CHECK_SCRIPT.parent.parent / "install.sh", repo / "install.sh")
    (repo / "ci/reference-allowlist.txt").write_text("", encoding="utf-8")
    (repo / "skills/example/SKILL.md").write_text("clean\n", encoding="utf-8")
    (repo / "agents/example.md").write_text("clean\n", encoding="utf-8")
    return repo


@pytest.mark.parametrize("name", ["foun" + "dry", "claude" + "-toolkit"])
@pytest.mark.parametrize("target", ["skills/example/SKILL.md", "agents/example.md"])
def test_installed_private_name_is_rejected(tmp_path, name, target):
    repo = private_reference_repo(tmp_path)
    (repo / target).write_text(f"See {name} for details.\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(repo / "ci/check.sh"), "references"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1, f"{target} private name was accepted: {result.stderr}"
    assert "installed file contains a private project name" in result.stderr


@pytest.mark.parametrize("name", ["foun" + "dry", "claude" + "-toolkit"])
def test_private_name_in_test_file_is_allowed(tmp_path, name):
    repo = private_reference_repo(tmp_path)
    (repo / "skills/example/tests/test_example.py").write_text(name, encoding="utf-8")
    result = subprocess.run(
        ["bash", str(repo / "ci/check.sh"), "references"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_installed_symlink_to_test_file_with_private_name_is_rejected(tmp_path):
    repo = private_reference_repo(tmp_path)
    skill = repo / "skills/example/SKILL.md"
    skill.unlink()
    skill.symlink_to("tests/test_example.py")
    (repo / "skills/example/tests/test_example.py").write_text(
        "See " + "foun" + "dry for details.\n", encoding="utf-8"
    )
    result = subprocess.run(
        ["bash", str(repo / "ci/check.sh"), "references"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1, f"installed symlink private name was accepted: {result.stderr}"
    assert "installed file contains a private project name" in result.stderr


@pytest.mark.parametrize("name", [
    "FOUN" + "DRY_ROOT",
    "foun" + "dryctl",
    "claude" + "_toolkit",
])
def test_private_name_substrings_are_rejected(tmp_path, name):
    repo = private_reference_repo(tmp_path)
    (repo / "skills/example/SKILL.md").write_text(name, encoding="utf-8")
    result = subprocess.run(
        ["bash", str(repo / "ci/check.sh"), "references"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1, f"private name substring was accepted: {result.stderr}"
    assert "installed file contains a private project name" in result.stderr


def test_private_name_case_is_ignored(tmp_path):
    repo = private_reference_repo(tmp_path)
    (repo / "skills/example/SKILL.md").write_text("FoUn" + "DrY", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(repo / "ci/check.sh"), "references"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1, f"mixed-case private name was accepted: {result.stderr}"
    assert "installed file contains a private project name" in result.stderr


@pytest.mark.parametrize("entry", ["symlink", "file", "nested"])
def test_non_test_directory_named_tests_is_scanned(tmp_path, entry):
    repo = private_reference_repo(tmp_path, include_tests=False)
    content = "foun" + "dry"
    if entry == "symlink":
        outside = tmp_path / "outside.md"
        outside.write_text(content, encoding="utf-8")
        (repo / "skills/example/tests").symlink_to(outside)
    elif entry == "file":
        (repo / "skills/example/tests").write_text(content, encoding="utf-8")
    else:
        nested = repo / "skills/example/references/tests"
        nested.mkdir(parents=True)
        (nested / "a.md").write_text(content, encoding="utf-8")
    result = subprocess.run(
        ["bash", str(repo / "ci/check.sh"), "references"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1, f"{entry} named tests was accepted: {result.stderr}"
    assert "installed file contains a private project name" in result.stderr


def test_unreadable_installed_symlink_is_rejected(tmp_path):
    repo = private_reference_repo(tmp_path)
    (repo / "skills/example/missing.md").symlink_to("absent.md")
    result = subprocess.run(
        ["bash", str(repo / "ci/check.sh"), "references"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1, f"unreadable symlink was accepted: {result.stderr}"
    assert "installed file contains an unreadable symlink" in result.stderr


def test_private_name_in_invalid_utf8_file_is_rejected(tmp_path):
    repo = private_reference_repo(tmp_path)
    (repo / "skills/example/SKILL.md").write_bytes(b"\xff" + b"foun" + b"dry")
    result = subprocess.run(
        ["bash", str(repo / "ci/check.sh"), "references"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1, f"invalid UTF-8 private name was accepted: {result.stderr}"
    assert "installed file contains a private project name" in result.stderr


def test_private_names_are_not_spelled_out_in_check_script():
    check = CHECK_SCRIPT.read_text(encoding="utf-8").lower()
    assert "foun" + "dry" not in check
    assert "claude" + "-toolkit" not in check


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

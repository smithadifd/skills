"""The skill discovery of ci/check.sh.

Invariant: every list of skills ci/check.sh builds from the skills directory holds exactly
the names install.sh's is_skill_name accepts. A cache directory, a dot-name directory or
any directory without a SKILL.md never enters it, so install.sh skipping such a directory
never fails an assertion; and an entry install.sh installs that the script did not
discover fails install-all, so the two copies of the rule cannot drift apart unseen.

Each test runs one ci/check.sh sub-check (install-all, install-one) against a throwaway
copy of this repository (with `.git` left out) that has one planted entry under skills/,
with a throwaway HOME and TMPDIR and the C locale (so the glob order is fixed). Nothing is
written to the real HOME or to this working tree. These sub-checks need no ledger clone.
Each planted entry isolates one condition of the rule: `__pycache__` and the dot-name
carry a SKILL.md, so only the name rule turns them away; the directory without a SKILL.md
has an ordinary name, so only the SKILL.md requirement turns it away.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# name -> files planted under skills/<name>/. The first name of each sorts before every
# real skill in the C locale, so install-one, which takes the first discovered skill,
# picks it when the rule lets it through.
PLANTED = {
    "__pycache__": {"SKILL.md": "---\nname: x\n---\n", "x.cpython-312.pyc": "bytes"},
    ".hidden-skill": {"SKILL.md": "---\nname: x\n---\n"},
    "0-notes": {"README.md": "not a skill\n"},
    ".pytest_cache": {"CACHEDIR.TAG": "cache\n"},
}


def make_copy(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(
        REPO_ROOT,
        root,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    return root


def plant(root, names):
    for name in names:
        for relative, content in PLANTED[name].items():
            target = root / "skills" / name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)


def run_check(root, tmp_path, check):
    home = tmp_path / "home"
    scratch = tmp_path / "tmpdir"
    home.mkdir(exist_ok=True)
    scratch.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key not in ("LC_ALL", "LANG")}
    env.update(HOME=str(home), TMPDIR=str(scratch), LC_ALL="C")
    return subprocess.run(
        ["bash", str(root / "ci" / "check.sh"), check],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def output(done):
    return "exit %d\n%s%s" % (done.returncode, done.stdout, done.stderr)


PASS_LINES = {
    "install-all": "PASS: install-all installed every discovered skill",
    "install-one": "PASS: install-one installed exactly one discovered skill",
}


@pytest.mark.parametrize("check", sorted(PASS_LINES))
def test_a_clean_tree_passes(tmp_path, check):
    root = make_copy(tmp_path)
    done = run_check(root, tmp_path, check)
    assert done.returncode == 0, output(done)
    assert PASS_LINES[check] in done.stdout, output(done)


@pytest.mark.parametrize("check", sorted(PASS_LINES))
@pytest.mark.parametrize("name", sorted(PLANTED))
def test_an_entry_that_is_not_a_skill_is_not_discovered(tmp_path, check, name):
    root = make_copy(tmp_path)
    plant(root, [name])
    done = run_check(root, tmp_path, check)
    assert done.returncode == 0, output(done)
    assert PASS_LINES[check] in done.stdout, output(done)


@pytest.mark.parametrize("check", sorted(PASS_LINES))
def test_every_kind_of_entry_that_is_not_a_skill_together(tmp_path, check):
    root = make_copy(tmp_path)
    plant(root, sorted(PLANTED))
    done = run_check(root, tmp_path, check)
    assert done.returncode == 0, output(done)
    assert PASS_LINES[check] in done.stdout, output(done)


@pytest.mark.parametrize("keep", [None, "_lib"])
def test_a_tree_with_no_skill_fails_closed(tmp_path, keep):
    # With nothing but the planted entries, and with `_lib` beside them: `_lib` is
    # discovered but is not a skill, so neither tree has a skill.
    root = make_copy(tmp_path)
    for entry in (root / "skills").iterdir():
        if entry.name != keep:
            shutil.rmtree(entry)
    plant(root, sorted(PLANTED))
    done = run_check(root, tmp_path, "install-all")
    assert done.returncode == 1, output(done)
    assert "ASSERTION FAILED: no skills were discovered" in done.stderr, output(done)


def test_an_entry_install_sh_installs_that_was_not_discovered_fails(tmp_path):
    # Simulated drift: this copy of install.sh no longer requires a SKILL.md, so it
    # installs a directory that ci/check.sh does not discover.
    root = make_copy(tmp_path)
    installer = root / "install.sh"
    text = installer.read_text()
    old = '*) [ -d "$skills_dir/$1" ] && [ -f "$skills_dir/$1/SKILL.md" ] ;;'
    assert text.count(old) == 1
    installer.write_text(text.replace(old, '*) [ -d "$skills_dir/$1" ] ;;'))
    plant(root, ["0-notes"])
    done = run_check(root, tmp_path, "install-all")
    assert done.returncode == 1, output(done)
    assert (
        "ASSERTION FAILED: install-all installed an entry that is not a discovered skill: 0-notes"
        in done.stderr
    ), output(done)

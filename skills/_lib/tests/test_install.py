"""install.sh reports the skill it installed, and installs the shared `_lib` beside it.

Grammar of a skill name: a directory under skills/ that holds a SKILL.md, or `_lib`.
A dot-name, `__pycache__` and a directory with no SKILL.md are not skills: `--all` and
`--uninstall --all` skip them, and naming one is refused as `Unknown skill: <name>`.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL = REPO_ROOT / "install.sh"


def _install(home, *args):
    env = {k: v for k, v in os.environ.items()}
    env["HOME"] = str(home)
    return subprocess.run(
        ["bash", str(INSTALL), *args], env=env, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("skill", ["shape", "topic", "sharpen"])
def test_installing_one_skill_prints_that_skill_not_the_shared_lib(tmp_path, skill):
    proc = _install(tmp_path, skill)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [f"Installed {skill}"]
    assert (tmp_path / ".claude" / "skills" / skill).is_dir()
    assert (tmp_path / ".claude" / "skills" / "_lib" / "ledger_cli.py").is_file()


def test_install_all_prints_each_skill_once_by_name(tmp_path):
    proc = _install(tmp_path, "--all")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    names = sorted(
        p.name
        for p in (REPO_ROOT / "skills").iterdir()
        if p.is_dir() and (p.name == "_lib" or (p / "SKILL.md").is_file())
    )
    assert sorted(lines) == sorted(f"Installed {n}" for n in names)


NOT_SKILLS = ("__pycache__", ".pytest_cache", ".hidden", "notes")


@pytest.fixture
def source(tmp_path):
    """A throwaway copy of the source tree with a cache, a dot-name and a non-skill directory.

    `__pycache__` and `.hidden` carry a SKILL.md so that only the name rule, not the
    SKILL.md rule, can refuse them; `notes` has none, so only the SKILL.md rule can.
    """
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy2(INSTALL, src / "install.sh")
    ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache")
    shutil.copytree(REPO_ROOT / "skills", src / "skills", ignore=ignore)
    shutil.copytree(REPO_ROOT / "agents", src / "agents", ignore=ignore)
    skills = src / "skills"
    (skills / "__pycache__").mkdir()
    (skills / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\0")
    (skills / "__pycache__" / "SKILL.md").write_text("not a skill\n")
    (skills / ".pytest_cache").mkdir()
    (skills / ".pytest_cache" / "README.md").write_text("cache\n")
    (skills / ".hidden").mkdir()
    (skills / ".hidden" / "SKILL.md").write_text("not a skill\n")
    (skills / "notes").mkdir()
    (skills / "notes" / "README.md").write_text("not a skill\n")
    return src


def _run(src, home, *args):
    env = {k: v for k, v in os.environ.items()}
    env["HOME"] = str(home)
    return subprocess.run(
        ["bash", str(src / "install.sh"), *args], env=env, capture_output=True, text=True, check=False
    )


def _real_skills(src):
    return sorted(
        p.name
        for p in (src / "skills").iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name not in NOT_SKILLS
    )


def test_install_all_and_uninstall_all_skip_what_is_not_a_skill(source, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    installed = home / ".claude" / "skills"

    proc = _run(source, home, "--all")
    assert proc.returncode == 0, proc.stderr
    assert sorted(proc.stdout.splitlines()) == sorted(f"Installed {n}" for n in _real_skills(source))
    assert sorted(p.name for p in installed.iterdir()) == _real_skills(source)
    for path in installed.rglob("*"):
        assert path.name != "__pycache__", path
        assert not (path.is_dir() and path.name.startswith(".")), path
    assert not [p for p in installed.iterdir() if p.name.startswith(".")]

    # The user's own directories that share a non-skill's name are not ours to remove.
    for name in NOT_SKILLS:
        (installed / name).mkdir()
        (installed / name / "keep").write_text("user data\n")

    proc = _run(source, home, "--uninstall", "--all")
    assert proc.returncode == 0, proc.stderr
    assert sorted(proc.stdout.splitlines()) == sorted(f"Uninstalled {n}" for n in _real_skills(source))
    assert sorted(p.name for p in installed.iterdir()) == sorted(NOT_SKILLS)
    for name in NOT_SKILLS:
        assert (installed / name / "keep").read_text() == "user data\n"


@pytest.mark.parametrize("name", NOT_SKILLS)
def test_naming_a_non_skill_is_refused_and_copies_nothing(source, tmp_path, name):
    home = tmp_path / "home"
    home.mkdir()
    installed = home / ".claude" / "skills"

    proc = _run(source, home, name)
    assert proc.returncode == 1
    assert proc.stderr == f"Unknown skill: {name}\n"
    assert not installed.exists()

    installed.mkdir(parents=True)
    (installed / name).mkdir()
    (installed / name / "keep").write_text("user data\n")
    proc = _run(source, home, "--uninstall", name)
    assert proc.returncode == 1
    assert proc.stderr == f"Unknown skill: {name}\n"
    assert (installed / name / "keep").read_text() == "user data\n"

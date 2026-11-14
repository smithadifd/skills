"""The grammar of ci/source_shape.py: the shape of the tree the ledger e2e installs from.

Accepted form: `skills/` and `agents/` are real directories, and every entry below them
(the roots included) is a regular file or a directory by lstat type. Each near miss
(a symlinked root, entry or nested file, a FIFO, a missing root) is refused by relative
path. Dot-names and a directory named like an agent are not refused and are not names.
"""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE = REPO_ROOT / "ci" / "source_shape.py"
_spec = importlib.util.spec_from_file_location("source_shape", MODULE)
shape = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shape)


@pytest.fixture
def tree(tmp_path):
    src = tmp_path / "src"
    (src / "skills" / "demo").mkdir(parents=True)
    (src / "skills" / "demo" / "SKILL.md").write_text("x")
    (src / "agents").mkdir()
    (src / "agents" / "a.md").write_text("x")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "f.md").write_text("x")
    return src


def test_a_plain_tree_passes_and_names_are_returned(tree):
    assert shape.violations(str(tree)) == []
    assert shape.names(str(tree)) == ({"demo"}, {"a.md"})


def test_a_symlinked_skills_root_is_refused(tree, tmp_path):
    (tree / "skills" / "demo" / "SKILL.md").unlink()
    (tree / "skills" / "demo").rmdir()
    (tree / "skills").rmdir()
    os.symlink(tmp_path / "outside", tree / "skills")
    assert shape.violations(str(tree)) == ["skills (not a real directory)"]


def test_a_symlinked_agents_root_is_refused(tree, tmp_path):
    (tree / "agents" / "a.md").unlink()
    (tree / "agents").rmdir()
    os.symlink(tmp_path / "outside", tree / "agents")
    assert shape.violations(str(tree)) == ["agents (not a real directory)"]


def test_a_symlinked_skill_directory_is_refused(tree, tmp_path):
    os.symlink(tmp_path / "outside", tree / "skills" / "x")
    assert shape.violations(str(tree)) == [os.path.join("skills", "x")]


def test_a_symlinked_agent_file_is_refused(tree, tmp_path):
    os.symlink(tmp_path / "outside" / "f.md", tree / "agents" / "x.md")
    assert shape.violations(str(tree)) == [os.path.join("agents", "x.md")]


def test_a_nested_file_symlink_and_a_nested_directory_symlink_are_each_refused_by_path(tree, tmp_path):
    sub = tree / "skills" / "demo" / "sub"
    sub.mkdir()
    os.symlink(tmp_path / "outside" / "f.md", sub / "link-file")
    # The directory link leads to a directory that itself holds a symlink: a walk that follows
    # links would report that inner link too, so the exact list below pins followlinks=False.
    (tmp_path / "outside2").mkdir()
    os.symlink(tmp_path / "outside" / "f.md", tmp_path / "outside2" / "inner")
    os.symlink(tmp_path / "outside2", sub / "link-dir")
    assert sorted(shape.violations(str(tree))) == [
        os.path.join("skills", "demo", "sub", "link-dir"),
        os.path.join("skills", "demo", "sub", "link-file"),
    ]


def test_a_fifo_inside_a_skill_is_refused(tree):
    os.mkfifo(tree / "skills" / "demo" / "pipe")
    assert shape.violations(str(tree)) == [os.path.join("skills", "demo", "pipe")]


def test_a_missing_skills_root_says_missing(tree):
    (tree / "skills" / "demo" / "SKILL.md").unlink()
    (tree / "skills" / "demo").rmdir()
    (tree / "skills").rmdir()
    assert shape.violations(str(tree)) == ["skills (missing)"]


def test_dot_names_and_a_directory_named_like_an_agent_pass_and_are_not_names(tree):
    (tree / "skills" / ".DS_Store").write_text("x")
    (tree / "skills" / ".pytest_cache").mkdir()
    (tree / "skills" / ".pytest_cache" / "v").write_text("x")
    (tree / "agents" / ".hidden.md").write_text("x")
    (tree / "agents" / "sub.md").mkdir()
    assert shape.violations(str(tree)) == []
    assert shape.names(str(tree)) == ({"demo"}, {"a.md"})


def test_a_venv_symlink_outside_the_two_roots_passes(tree, tmp_path):
    (tree / ".venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, tree / ".venv" / "bin" / "python")
    assert shape.violations(str(tree)) == []


def test_names_refuses_a_tree_that_violates_the_shape(tree, tmp_path):
    os.symlink(tmp_path / "outside" / "f.md", tree / "agents" / "x.md")
    with pytest.raises(ValueError):
        shape.names(str(tree))


def test_the_script_prints_the_violation_and_exits_one(tree, tmp_path):
    os.symlink(tmp_path / "outside" / "f.md", tree / "agents" / "x.md")
    done = subprocess.run([sys.executable, "-I", "-B", str(MODULE), str(tree)], capture_output=True, text=True)
    assert (done.returncode, done.stdout.splitlines()) == (1, [os.path.join("agents", "x.md")])


def test_the_real_repo_tree_passes():
    assert shape.violations(str(REPO_ROOT)) == []
    skills, agents = shape.names(str(REPO_ROOT))
    assert skills and agents


def test_check_sh_invokes_source_shape_exactly_twice():
    # One call refuses a bad tree before the installs; one loads names() for the footprint allowlist.
    lines = [
        line
        for line in (REPO_ROOT / "ci" / "check.sh").read_text(encoding="utf-8").splitlines()
        if re.search(r"source_shape\.py\"", line) and not line.lstrip().startswith("#")
    ]
    assert len(lines) == 2, lines
    assert any(re.search(r'-I -B "\$repo_dir/ci/source_shape\.py" "\$src"', line) for line in lines), lines
    assert not any("os.path.isfile" in line for line in (REPO_ROOT / "ci" / "check.sh").read_text().splitlines()), (
        "the agent name set must come from source_shape.names, not os.path.isfile"
    )

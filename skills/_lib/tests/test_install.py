"""install.sh reports the skill it installed, and installs the shared `_lib` beside it."""

import os
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
    names = sorted(p.name for p in (REPO_ROOT / "skills").iterdir() if p.is_dir())
    assert sorted(lines) == sorted(f"Installed {n}" for n in names)

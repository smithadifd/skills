"""The scratch cleanup of the ledger end-to-end step in ci/check.sh.

Invariant: when `ci/check.sh ledger-e2e` exits, by success or by a failing step, the one
scratch directory the step made under TMPDIR is gone, with everything under it (home, bin,
data, the source copy and the pinned ledger clone), and the exit status is the one the
step itself reached: 0 for a pass, 1 for a failing step.

Each run points TMPDIR at a throwaway directory and reads back what it holds. The runs
use a copy of this repository (with `.git` left out) that holds one read-only directory,
so removing the scratch directory needs the trap's `chmod -R u+w`. Run it with a
throwaway HOME, as CI does for the skill tests; the step itself never writes to HOME.
A passing run needs LEDGER_E2E_URL, the clone URL of the ledger repository; without it
the passing-run test fails with a message, it is never skipped.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PASS_LINE = "PASS: ledger-e2e search found the decision"


def make_writable(root):
    for base, dirs, files in os.walk(root):
        for name in [""] + dirs + files:
            path = os.path.join(base, name) if name else base
            if not os.path.islink(path):
                os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)


@pytest.fixture(scope="module")
def repo_copy(tmp_path_factory):
    """A copy of the repository with a read-only directory, which the step copies again."""
    root = tmp_path_factory.mktemp("repo-copy") / "repo"
    shutil.copytree(
        REPO_ROOT,
        root,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    readonly = root / "zz-readonly-fixture"
    (readonly / "inner").mkdir(parents=True)
    (readonly / "inner" / "file.txt").write_text("x")
    for directory in (readonly / "inner", readonly):
        directory.chmod(0o555)
    yield root
    make_writable(root)


def run_step(repo_copy, tmp_path, url):
    scratch_root = tmp_path / "tmpdir"
    scratch_root.mkdir()
    env = dict(os.environ, TMPDIR=str(scratch_root), LEDGER_E2E_URL=url)
    done = subprocess.run(
        ["bash", str(repo_copy / "ci" / "check.sh"), "ledger-e2e"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    left = sorted(entry.name for entry in scratch_root.iterdir())
    return done, left


def test_a_passing_run_leaves_no_scratch_directory(repo_copy, tmp_path):
    url = os.environ.get("LEDGER_E2E_URL", "")
    if not url:
        pytest.fail(
            "LEDGER_E2E_URL is unset: this test runs the passing ledger-e2e step and needs the "
            "ledger repository's clone URL (for a local run, a file:// URL of a ledger clone)"
        )
    done, left = run_step(repo_copy, tmp_path, url)
    assert done.returncode == 0, done.stdout + done.stderr
    assert PASS_LINE in done.stdout, done.stdout
    assert left == [], f"the passing run left scratch behind: {left}"


def test_a_failing_run_leaves_no_scratch_directory_and_keeps_its_status(repo_copy, tmp_path):
    # The clone is the last step that can fail before the ledger runs: every install has
    # finished by then, so the scratch directory holds the whole source copy.
    url = (tmp_path / "no-such-ledger").as_uri()
    done, left = run_step(repo_copy, tmp_path, url)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "ledger-e2e step ledger-install: git clone of LEDGER_E2E_URL" in done.stderr, done.stderr
    assert PASS_LINE not in done.stdout, done.stdout
    assert left == [], f"the failing run left scratch behind: {left}"

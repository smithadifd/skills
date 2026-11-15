"""The install quiesce of the ledger end-to-end step in ci/check.sh.

Invariant: the checks that follow the two installs (the source-tree record, the decoys, the
footprint allowlist, the installed frontmatter) run only once every process each install
started in its own process group has exited. install.sh runs as the leader of a fresh process
group; once it exits, the group gets SIGTERM, then SIGKILL after a short grace, and must be
empty within a bounded deadline, or step sdlc-install fails naming the install before any later
check runs. A child that leaves the group on its own (setsid or setpgid) is outside this check.

The end-to-end tests run `ci/check.sh ledger-e2e` from a throwaway copy of this repository whose
install.sh is a wrapper: it runs the real installer (kept beside it as install-real.sh) and, for
`--all`, leaves a background process behind in the install's process group.

- In the first, that process writes `.claude/settings.json` under the throwaway home when it is
  sent SIGTERM, or else once the step has gone past the footprint check (the pinned ledger
  clone appears in the scratch directory). The step must fail its footprint check naming that
  file: only a kill-and-wait before the check lets the write land before it.
- In the second, that process ignores SIGTERM, and the copy's ci/check.sh has the SIGKILL send
  patched out, so the group cannot be emptied: the step must fail sdlc-install at the deadline,
  before the footprint check, which the planted settings file would otherwise fail.

The narrow tests run the helper script the step embeds, cut from ci/check.sh, against a
throwaway source tree whose install.sh is a small shell script: a well-behaved install passes
with no output, a leader that exits non-zero fails, a background child that ignores SIGTERM is
ended by the SIGKILL, and a leader that cannot be started is named.

A passing run of the whole step with the real install.sh is
test_check_ledger_e2e_cleanup.py::test_a_passing_run_leaves_no_scratch_directory.

The first end-to-end test needs LEDGER_E2E_URL, the clone URL of the ledger repository (for a
local run, a file:// URL of a ledger clone): without it it fails with a message, it is never
skipped. Run with a throwaway HOME, as CI does for the skill tests; nothing here writes to HOME
or to this repository.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECK_SH = REPO_ROOT / "ci" / "check.sh"
FOOTPRINT_FAILURE = (
    "ASSERTION FAILED: ledger-e2e step install: install left a path outside the footprint allowlist "
    "(exit 1): .claude/settings.json"
)
DEADLINE_FAILURE = (
    "ASSERTION FAILED: ledger-e2e step sdlc-install: install.sh --all left processes in its process "
    "group that were still there 5 seconds after SIGTERM and SIGKILL"
)
# The one SIGKILL send in the helper; the deadline test patches it out of a scratch copy.
SIGKILL_SEND = "            if send(pgid, signal.SIGKILL):\n"

WRAPPER = """\
#!/bin/bash
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
/bin/bash "$here/install-real.sh" "$@"
if [ "${1:-}" = --all ]; then
  %(python)s -I -B %(child)s "$HOME" %(out)s < /dev/null > /dev/null %(stderr)s &
  tries=0
  until [ -e %(out)s/ready ]; do
    tries=$((tries + 1))
    [ "$tries" -lt 600 ] || exit 1
    sleep 0.05
  done
fi
"""

# Writes the forbidden file on SIGTERM, or once the step is past the footprint check.
LATE_WRITER = """\
import os, signal, sys, time

home, out = sys.argv[1:3]
scratch = os.path.dirname(home)


def write(marker):
    with open(os.path.join(home, ".claude", "settings.json"), "w") as handle:
        handle.write("{}\\n")
    open(os.path.join(out, marker), "w").close()


def on_term(signum, frame):
    write("wrote-on-term")
    sys.exit(0)


signal.signal(signal.SIGTERM, on_term)
open(os.path.join(out, "ready"), "w").close()
deadline = time.monotonic() + 120
while not os.path.isdir(os.path.join(scratch, "ledger")):
    if not os.path.isdir(scratch) or time.monotonic() > deadline:
        open(os.path.join(out, "gave-up"), "w").close()
        sys.exit(1)
    time.sleep(0.01)
write("wrote-late")
"""

# Plants the forbidden file, then ignores SIGTERM and stays.
STAYER = """\
import os, signal, sys, time

home, out = sys.argv[1:3]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(os.path.join(home, ".claude", "settings.json"), "w") as handle:
    handle.write("{}\\n")
with open(os.path.join(out, "ready.tmp"), "w") as handle:
    handle.write(str(os.getpid()))
os.rename(os.path.join(out, "ready.tmp"), os.path.join(out, "ready"))
time.sleep(120)
"""


def shell_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def embedded_script(assignment):
    """The heredoc body of the `<assignment>=$(... <<'PY'` block in ci/check.sh."""
    lines = CHECK_SH.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.lstrip().startswith(assignment + "=$(")]
    assert len(starts) == 1, starts
    start = starts[0]
    assert lines[start].endswith("<<'PY'"), lines[start]
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "PY")
    return "\n".join(lines[start + 1 : end]) + "\n"


def ledger_url():
    url = os.environ.get("LEDGER_E2E_URL", "")
    if not url:
        pytest.fail(
            "LEDGER_E2E_URL is unset: this test runs the ledger-e2e step and needs the ledger "
            "repository's clone URL (for a local run, a file:// URL of a ledger clone)"
        )
    return url


def repo_with_wrapper(tmp_path, child_source, stderr):
    repo = tmp_path / "repo"
    shutil.copytree(
        REPO_ROOT,
        repo,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    out = tmp_path / "out"
    out.mkdir()
    child = tmp_path / "child.py"
    child.write_text(child_source, encoding="utf-8")
    (repo / "install.sh").rename(repo / "install-real.sh")
    wrapper = repo / "install.sh"
    wrapper.write_text(
        WRAPPER
        % {
            "python": shell_quote(sys.executable),
            "child": shell_quote(child),
            "out": shell_quote(out),
            "stderr": stderr,
        },
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return repo, out


def run_step(repo, tmp_path, url):
    scratch_root = tmp_path / "tmpdir"
    scratch_root.mkdir()
    env = dict(os.environ, TMPDIR=str(scratch_root), LEDGER_E2E_URL=url)
    done = subprocess.run(
        ["bash", str(repo / "ci" / "check.sh"), "ledger-e2e"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    return done, sorted(entry.name for entry in scratch_root.iterdir())


def test_a_child_the_install_leaves_behind_is_ended_before_the_footprint_check(tmp_path):
    url = ledger_url()
    # The child inherits the step's stderr, so the run returns only after it exits.
    repo, out = repo_with_wrapper(tmp_path, LATE_WRITER, "")
    done, left = run_step(repo, tmp_path, url)
    markers = sorted(entry.name for entry in out.iterdir())
    assert done.returncode == 1, (markers, done.stdout + done.stderr)
    assert FOOTPRINT_FAILURE in done.stderr.splitlines(), done.stderr
    assert markers == ["ready", "wrote-on-term"], markers
    assert left == []


def test_a_group_that_cannot_be_emptied_fails_sdlc_install_before_the_footprint_check(tmp_path):
    # The child's stderr is /dev/null so the run returns while it is still alive.
    repo, out = repo_with_wrapper(tmp_path, STAYER, "2> /dev/null")
    check = repo / "ci" / "check.sh"
    text = check.read_text(encoding="utf-8")
    assert text.count(SIGKILL_SEND) == 1
    check.write_text(text.replace(SIGKILL_SEND, "            if send(pgid, 0):\n"), encoding="utf-8")
    try:
        done, left = run_step(repo, tmp_path, (tmp_path / "no-such-ledger").as_uri())
        assert (out / "ready").exists(), done.stderr
        assert done.returncode == 1, done.stdout + done.stderr
        assert DEADLINE_FAILURE in done.stderr.splitlines(), done.stderr
        assert "ledger-e2e step install:" not in done.stderr, done.stderr
        assert left == []
    finally:
        if (out / "ready").exists():
            try:
                os.kill(int((out / "ready").read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def run_helper(src, argv, flag="--all"):
    return subprocess.run(
        [sys.executable, "-I", "-B", "-", str(src), flag, *argv],
        input=embedded_script("quiesce_out"),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def source_tree(tmp_path, body):
    src = tmp_path / "src"
    src.mkdir()
    install = src / "install.sh"
    install.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    install.chmod(0o755)
    return src


ENV = ["env", "-i", "PATH=/usr/bin:/bin"]


def test_a_well_behaved_install_passes_with_no_output(tmp_path):
    src = source_tree(tmp_path, '[ "$1" = --all ] || exit 9\nexit 0\n')
    done = run_helper(src, ENV)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_leader_that_exits_non_zero_fails_the_install(tmp_path):
    src = source_tree(tmp_path, "exit 1\n")
    done = run_helper(src, ENV)
    assert (done.returncode, done.stdout) == (1, "install.sh --all failed\n"), done.stderr


def test_a_child_that_ignores_sigterm_is_ended_by_sigkill(tmp_path):
    out = tmp_path / "pid"
    child = (
        "import os, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
        "os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
        "time.sleep(120)\n"
    )
    body = (
        "%s -I -c %s %s < /dev/null > /dev/null 2>&1 &\n"
        "until [ -e %s ]; do sleep 0.05; done\n"
        "exit 0\n"
    ) % (shell_quote(sys.executable), shell_quote(child), shell_quote(out), shell_quote(out))
    src = source_tree(tmp_path, body)
    started = time.monotonic()
    done = run_helper(src, ENV)
    pid = int(out.read_text())
    try:
        assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
        assert time.monotonic() - started < 5
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_a_leader_that_cannot_be_started_is_named(tmp_path):
    src = source_tree(tmp_path, "exit 0\n")
    done = run_helper(src, [str(tmp_path / "no-such-env")])
    assert (done.returncode, done.stdout) == (
        3,
        "install.sh --all could not be started as a process group leader: ENOENT\n",
    ), done.stderr

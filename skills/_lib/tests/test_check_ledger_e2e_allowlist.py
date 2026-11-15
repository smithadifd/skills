"""The footprint allowlist of the ledger end-to-end step in ci/check.sh.

Invariant: the skill and agent name sets the footprint check allows are the ones the source
copy held before any branch install code ran. The step reads them once, after the
record-before snapshot and before the first install, and hands them to the footprint check
as an argument; nothing the install does to the source copy can add a name to them.

The end-to-end tests run `ci/check.sh ledger-e2e` from a throwaway copy of this repository
whose install.sh is a wrapper: it runs the real installer (kept beside it as
install-real.sh), installs one extra skill, `planted`, into the throwaway home, and leaves a
background process behind in a session (and process group) of its own, which the step's
install quiesce does not end: it ends only the install's own process group, and a process
that leaves the group is the known limit this test covers. That process waits until the
step has recorded the source copy
after the installs (so the before/after record cannot see it), then changes the copy: it adds
skills/planted holding a regular SKILL.md and, in one case, first swaps a listed skill
directory for a symlink. The footprint check must fail naming exactly the two planted
entries. That holds only when the allowlist is the pre-install name set: with any other set
the list would also name a missing skill or an unlisted one, and a set read from the copy
after the change would accept planted (or raise on the symlink). Each test waits for the
background process to exit before it returns.

The narrow tests run the two scripts the step embeds, cut from ci/check.sh: the pre-install
name read must fail on a tree that breaks the source shape, and the footprint check must
refuse a missing or malformed name-set argument with its named allowlist error.

The end-to-end tests need LEDGER_E2E_URL, the clone URL of the ledger repository (for a
local run, a file:// URL of a ledger clone): without it they fail with a message, they are
never skipped. Run with a throwaway HOME, as CI does for the skill tests; nothing here
writes to HOME or to this repository.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECK_SH = REPO_ROOT / "ci" / "check.sh"
ALLOWLIST_ERROR = "allowlist error: the pre-install skill and agent name sets are missing or unreadable"
FOOTPRINT_FAILURE = (
    "ASSERTION FAILED: ledger-e2e step install: install left a path outside the footprint allowlist "
    "(exit 1): .claude/skills/planted; .claude/skills/planted/SKILL.md"
)

_spec = importlib.util.spec_from_file_location("source_shape", REPO_ROOT / "ci" / "source_shape.py")
shape = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shape)

PLANTED_SKILL_MD = "---\nname: planted\ndescription: A skill the source tree did not hold before the install.\n---\n"

WRAPPER = """\
#!/bin/bash
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
/bin/bash "$here/install-real.sh" "$@"
if [ "${1:-}" = --all ]; then
  mkdir -p "$HOME/.claude/skills/planted"
  printf '%%s' %(skill_md)s > "$HOME/.claude/skills/planted/SKILL.md"
  # A new session, entered before the launcher returns, so the child outlives the quiesce.
  %(python)s -I -c 'import subprocess, sys; subprocess.Popen(sys.argv[1:], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)' \
    %(python)s -I -B %(child)s "$here" "$HOME" %(mode)s %(victim)s %(out)s
fi
"""

CHILD = """\
import os, sys, time

src, home, mode, victim, out = sys.argv[1:6]
scratch = os.path.dirname(home)
after = os.path.join(scratch, "record-after.json")
deadline = time.monotonic() + 60
while not os.path.exists(after):
    if not os.path.isdir(scratch) or time.monotonic() > deadline:
        open(os.path.join(out, "gave-up"), "w").close()
        sys.exit(1)
    time.sleep(0.0005)
skills = os.path.join(src, "skills")
if mode == "swap-and-add":
    away = os.path.join(scratch, "swapped-away")
    os.rename(os.path.join(skills, victim), away)
    os.symlink(away, os.path.join(skills, victim))
os.mkdir(os.path.join(skills, "planted"))
with open(os.path.join(skills, "planted", "SKILL.md"), "w") as handle:
    handle.write(%(skill_md)r)
open(os.path.join(out, "changed"), "w").close()
"""


def shell_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def embedded_script(assignment):
    """The heredoc body of the `<assignment>=$(... <<'PY'` block in ci/check.sh."""
    lines = CHECK_SH.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.lstrip().startswith(assignment + "=$(")]
    assert len(starts) == 1, starts
    start = starts[0]
    assert lines[start].endswith("<<'PY'") or lines[start].endswith("<<'PY' 2>&1"), lines[start]
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


@pytest.mark.parametrize("mode", ["add", "swap-and-add"])
def test_an_entry_the_install_adds_to_the_source_copy_is_not_allowed(tmp_path, mode):
    url = ledger_url()
    repo = tmp_path / "repo"
    shutil.copytree(
        REPO_ROOT,
        repo,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    before_skills, _ = shape.names(str(repo))
    assert "planted" not in before_skills
    victim = sorted(before_skills - {"_lib", "ticket"})[0]
    out = tmp_path / "out"
    out.mkdir()
    child = tmp_path / "child.py"
    child.write_text(CHILD % {"skill_md": PLANTED_SKILL_MD}, encoding="utf-8")
    (repo / "install.sh").rename(repo / "install-real.sh")
    wrapper = repo / "install.sh"
    wrapper.write_text(
        WRAPPER
        % {
            "skill_md": shell_quote(PLANTED_SKILL_MD),
            "python": shell_quote(sys.executable),
            "child": shell_quote(child),
            "mode": shell_quote(mode),
            "victim": shell_quote(victim),
            "out": shell_quote(out),
        },
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    scratch_root = tmp_path / "tmpdir"
    scratch_root.mkdir()
    env = dict(os.environ, TMPDIR=str(scratch_root), LEDGER_E2E_URL=url)
    # The background process inherits the step's stderr, so this returns only after it exits.
    done = subprocess.run(
        ["bash", str(repo / "ci" / "check.sh"), "ledger-e2e"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert sorted(entry.name for entry in out.iterdir()) == ["changed"], (
        "the background process did not change the source copy after the installs",
        done.stdout + done.stderr,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert FOOTPRINT_FAILURE in done.stderr.splitlines(), done.stderr
    assert sorted(entry.name for entry in scratch_root.iterdir()) == []


def run_footprint(home, *args):
    return subprocess.run(
        [sys.executable, "-I", "-B", "-", str(home), *args],
        input=embedded_script("footprint"),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "home"
    (root / ".claude" / "skills" / "ticket").mkdir(parents=True)
    (root / ".claude" / "skills" / "ticket" / "SKILL.md").write_text("x")
    (root / ".claude" / "agents").mkdir()
    (root / ".claude" / "agents" / "a.md").write_text("x")
    return root


VALID = json.dumps({"skills": ["ticket"], "agents": ["a.md"]})


def test_the_footprint_check_passes_a_home_that_matches_the_passed_sets(home):
    done = run_footprint(home, VALID)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_the_footprint_check_names_an_installed_skill_the_passed_sets_lack(home):
    (home / ".claude" / "skills" / "planted").mkdir()
    done = run_footprint(home, VALID)
    assert (done.returncode, done.stdout.splitlines()) == (1, [".claude/skills/planted"])


@pytest.mark.parametrize(
    "args",
    [
        [],
        [""],
        ["not json"],
        ["[]"],
        ['{"skills": ["ticket"]}'],
        ['{"skills": ["ticket"], "agents": ["a.md"], "more": []}'],
        ['{"skills": [], "agents": ["a.md"]}'],
        ['{"skills": ["ticket"], "agents": []}'],
        ['{"skills": "ticket", "agents": ["a.md"]}'],
        ['{"skills": ["ticket"], "agents": [""]}'],
        ['{"skills": ["ticket", 1], "agents": ["a.md"]}'],
        [VALID, VALID],
    ],
    ids=[
        "missing",
        "empty",
        "not-json",
        "a-list",
        "no-agents-key",
        "an-extra-key",
        "empty-skills",
        "empty-agents",
        "skills-a-string",
        "an-empty-name",
        "a-number-name",
        "two-arguments",
    ],
)
def test_a_missing_or_malformed_name_set_is_an_allowlist_error(home, args):
    done = run_footprint(home, *args)
    assert (done.returncode, done.stdout.splitlines()) == (2, [ALLOWLIST_ERROR]), done.stderr


def test_the_pre_install_name_read_fails_on_a_tree_that_breaks_the_shape(tmp_path):
    tree = tmp_path / "tree"
    (tree / "skills" / "demo").mkdir(parents=True)
    (tree / "skills" / "demo" / "SKILL.md").write_text("x")
    (tree / "agents").mkdir()
    (tree / "agents" / "a.md").write_text("x")
    script = embedded_script("allow_names")
    argv = [sys.executable, "-I", "-B", "-", str(REPO_ROOT / "ci" / "source_shape.py"), str(tree)]
    done = subprocess.run(argv, input=script, capture_output=True, text=True, check=False)
    assert (done.returncode, json.loads(done.stdout)) == (0, {"skills": ["demo"], "agents": ["a.md"]})
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", tree / "skills" / "linked")
    done = subprocess.run(argv, input=script, capture_output=True, text=True, check=False)
    assert done.returncode != 0 and done.stdout == "", done.stdout
    assert "source tree shape: skills/linked" in done.stderr, done.stderr


def test_the_footprint_check_never_reads_the_source_copy():
    script = embedded_script("footprint")
    assert "source_shape" not in script and "names(" not in script, textwrap.shorten(script, 200)

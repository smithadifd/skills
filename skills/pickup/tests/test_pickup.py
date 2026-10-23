"""Stdlib-only tests: Verified commands are rerun and failing ones are demoted."""
import os
import subprocess
import sys
import time

import pickup

PY = '"%s"' % sys.executable
OK_CMD = PY + ' -c "import sys; sys.exit(0)"'
BAD_CMD = PY + ' -c "import sys; sys.exit(3)"'


def note(verified_lines, written="2020-01-01T00:00:00Z"):
    return "\n".join([
        "# Handoff: r / s", "", "Written: " + written, "",
        "## Goal", "", "Ship it.", "",
        "## Verified", "", *verified_lines, "",
        "## Claimed but not verified", "", "- CI is green upstream.", "",
        "## Open questions", "", "- Which backoff cap?", "",
        "## Skills to invoke next", "", "- review", "",
    ])


def test_failing_verified_command_is_demoted_to_claimed():
    text = note(["- `%s` => ok" % OK_CMD, "- `%s` => was passing" % BAD_CMD])
    result = pickup.pickup(text)
    assert [v["command"] for v in result["verified"]] == [OK_CMD]
    assert [d["command"] for d in result["demoted"]] == [BAD_CMD]
    assert any("failed on rerun" in c and "sys.exit(3)" in c for c in result["claimed"])
    assert "- CI is green upstream." in result["claimed"]


def test_unrunnable_command_is_demoted():
    result = pickup.pickup(note(["- `definitely-not-a-command-xyz` => ok"]))
    assert len(result["demoted"]) == 1 and not result["verified"]


def test_open_questions_and_skills_are_listed():
    result = pickup.pickup(note([]))
    assert result["open_questions"] == ["- Which backoff cap?"]
    assert result["skills"] == ["- review"]


def test_note_older_than_latest_commit_is_stale(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "c"], cwd=repo, check=True, env=env)
    assert pickup.pickup(note([]), cwd=str(repo))["stale"] is True
    assert pickup.pickup(note([], written="2999-01-01T00:00:00Z"), cwd=str(repo))["stale"] is False


def test_find_note_returns_newest_matching(private_tmp):
    old = private_tmp / "handoff-a-one.md"
    new = private_tmp / "handoff-a-two.md"
    other = private_tmp / "handoff-b-three.md"
    for p in (old, new, other):
        p.write_text("x")
    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now - 50, now - 50))
    os.utime(other, (now, now))
    assert pickup.find_note("a") == str(new)
    assert pickup.find_note() == str(other)


def test_cli_reports_demotion(private_tmp, capsys):
    path = private_tmp / "n.md"
    path.write_text(note(["- `%s` => was ok" % BAD_CMD]))
    assert pickup.main([str(path), "--cwd", str(private_tmp)]) == 0
    assert "Demoted to claimed (1)" in capsys.readouterr().out

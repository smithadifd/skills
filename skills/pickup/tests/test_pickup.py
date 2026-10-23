"""Stdlib-only tests: nothing runs unasked; rerun failures and unrun items are demoted."""
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


def touch_cmd(path):
    return PY + ' -c "open(%r, \'w\').close()"' % str(path)


def make_notes_dir(tmp):
    d = tmp / ("handoff-notes-%d" % os.getuid())
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    return d


def make_note(d, name, mtime=None):
    p = d / name
    p.write_text("x")
    os.chmod(p, 0o600)
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


def test_nothing_runs_without_rerun(tmp_path):
    marker = tmp_path / "ran"
    result = pickup.pickup(note(["- `%s` => ok" % touch_cmd(marker)]))
    assert not marker.exists()
    assert len(result["commands"]) == 1 and len(result["not_rerun"]) == 1
    assert any("not rerun" in c for c in result["claimed"])


def test_cli_default_run_executes_nothing_and_lists_numbered(tmp_path, capsys):
    marker = tmp_path / "ran"
    path = tmp_path / "n.md"
    path.write_text(note(["- `%s` => ok" % touch_cmd(marker)]))
    assert pickup.main([str(path), "--cwd", str(tmp_path)]) == 0
    assert not marker.exists()
    assert "1. `" in capsys.readouterr().out


def test_rerun_runs_only_listed_commands(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    text = note(["- `%s` => a" % touch_cmd(first), "- `%s` => b" % touch_cmd(second)])
    result = pickup.pickup(text, cwd=str(tmp_path), rerun_indices={2})
    assert not first.exists() and second.exists()
    assert len(result["verified"]) == 1 and len(result["not_rerun"]) == 1


def test_failing_rerun_command_is_demoted_to_claimed():
    text = note(["- `%s` => ok" % OK_CMD, "- `%s` => was passing" % BAD_CMD])
    result = pickup.pickup(text, rerun_indices={1, 2})
    assert [v["command"] for v in result["verified"]] == [OK_CMD]
    assert [d["command"] for d in result["demoted"]] == [BAD_CMD]
    assert any("failed on rerun" in c and "sys.exit(3)" in c for c in result["claimed"])
    assert "- CI is green upstream." in result["claimed"]


def test_unrunnable_command_is_demoted():
    result = pickup.pickup(note(["- `definitely-not-a-command-xyz` => ok"]), rerun_indices={1})
    assert len(result["demoted"]) == 1 and not result["verified"]


def test_cli_rerun_requires_explicit_path(private_tmp, capsys):
    assert pickup.main(["--rerun", "1"]) == 2
    assert "explicit note path" in capsys.readouterr().err


def test_cli_rerun_reports_demotion(private_tmp, capsys):
    path = private_tmp / "n.md"
    path.write_text(note(["- `%s` => was ok" % BAD_CMD]))
    assert pickup.main([str(path), "--rerun", "1", "--cwd", str(private_tmp)]) == 0
    assert "failed, demoted to claimed (1)" in capsys.readouterr().out


def test_cli_bad_rerun_spec_is_a_clean_error(private_tmp, capsys):
    path = private_tmp / "n.md"
    path.write_text(note([]))
    assert pickup.main([str(path), "--rerun", "one"]) == 1
    assert "pickup:" in capsys.readouterr().err


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


def test_malformed_written_date_does_not_crash(tmp_path, capsys):
    path = tmp_path / "n.md"
    path.write_text(note([], written="2020-13-45"))
    assert pickup.main([str(path), "--cwd", str(tmp_path)]) == 0
    assert "no readable Written date" in capsys.readouterr().out


def test_missing_note_is_a_clean_error(tmp_path, capsys):
    assert pickup.main([str(tmp_path / "nope.md")]) == 1
    assert "pickup:" in capsys.readouterr().err


def test_find_note_returns_newest_matching(private_tmp):
    d = make_notes_dir(private_tmp)
    now = time.time()
    make_note(d, "handoff-a-one.md", now - 100)
    new = make_note(d, "handoff-a-two.md", now - 50)
    other = make_note(d, "handoff-b-three.md", now)
    assert pickup.find_note("a") == str(new)
    assert pickup.find_note() == str(other)


def _as_other_owner(monkeypatch, suffix):
    """Make lstat report a different owner for paths ending in suffix."""
    real_lstat = os.lstat

    def fake(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if not str(path).endswith(suffix):
            return st
        fields = list(st)
        fields[4] = st.st_uid + 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "lstat", fake)


def test_find_note_ignores_files_not_owned_by_current_user(private_tmp, monkeypatch):
    d = make_notes_dir(private_tmp)
    make_note(d, "handoff-a-one.md")
    assert pickup.find_note() is not None
    _as_other_owner(monkeypatch, ".md")
    assert pickup.find_note() is None


def test_find_note_refuses_a_directory_owned_by_someone_else(private_tmp, monkeypatch):
    d = make_notes_dir(private_tmp)
    make_note(d, "handoff-a-one.md")
    _as_other_owner(monkeypatch, "handoff-notes-%d" % os.getuid())
    assert pickup.find_note() is None


def test_find_note_skips_writable_and_symlinked_files(private_tmp):
    d = make_notes_dir(private_tmp)
    loose = make_note(d, "handoff-a-loose.md")
    os.chmod(loose, 0o666)
    real = private_tmp / "real.md"
    real.write_text("x")
    (d / "handoff-a-link.md").symlink_to(real)
    assert pickup.find_note() is None


def test_find_note_refuses_an_unsafe_directory(private_tmp):
    d = make_notes_dir(private_tmp)
    make_note(d, "handoff-a-one.md")
    os.chmod(d, 0o755)
    assert pickup.find_note() is None

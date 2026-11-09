"""The plugin's SessionStart hook: reports ledger state without blocking startup."""

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK = REPO_ROOT / "hooks" / "session-start.py"
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"

FAKE = """#!__PYTHON__
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LEDGER_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(argv) + "\\n")
mode = os.environ.get("FAKE_LEDGER_MODE", "ok")
if argv == ["--version"]:
    if mode == "versionfail":
        sys.stderr.write("no version here\\n")
        raise SystemExit(2)
    sys.stdout.write("ledger 0.1.14\\n")
    raise SystemExit(0)
if mode == "fail":
    sys.stdout.write("PARTIAL OUTPUT\\n")
    sys.stderr.write("\\nboom\\nmore detail\\n")
    raise SystemExit(3)
if mode == "longfail":
    sys.stderr.write("x" * 400 + "\\n")
    raise SystemExit(4)
if mode == "timeout":
    import time
    time.sleep(1)
if argv[0] == "resume":
    if mode == "noresume":
        sys.stdout.write("usage: ledger <verb>\\n")
        sys.stderr.write("unknown verb: resume\\n")
        raise SystemExit(2)
    sys.stdout.write("BRIEF: 2 open items\\nnext: LC15\\n")
    raise SystemExit(0)
print("ok")
"""


class Rig:
    def __init__(self, tmp_path):
        self.fake_home = tmp_path / "home"
        self.fake_home.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "calls.log"
        fake = self.bin / "fakeledger"
        fake.write_text(FAKE.replace("__PYTHON__", sys.executable), encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.env_file = tmp_path / "claude-env"
        self.hook = HOOK

    def use_hook_copy(self, timeout):
        plugin = self.fake_home.parent / "plugin"
        (plugin / "hooks").mkdir(parents=True)
        source = HOOK.read_text(encoding="utf-8")
        source = source.replace("TIMEOUT_SECONDS = 20", f"TIMEOUT_SECONDS = {timeout}")
        (plugin / "hooks" / "session-start.py").write_text(source, encoding="utf-8")
        (plugin / "skills" / "_lib").mkdir(parents=True)
        shutil.copy(REPO_ROOT / "skills" / "_lib" / "ledger_cli.py", plugin / "skills" / "_lib" / "ledger_cli.py")
        self.hook = plugin / "hooks" / "session-start.py"

    def configure(self, value="fakeledger"):
        path = self.fake_home / ".claude" / "skill-config"
        path.mkdir(parents=True, exist_ok=True)
        (path / "topic.json").write_text(json.dumps({"ledger_cli": value}), encoding="utf-8")

    def run(self, stdin='{"session_id": "sess-1"}', mode="ok", env_file=True, extra_env=None, drop=()):
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("LEDGER_") and k != "CLAUDE_ENV_FILE"
        }
        env["HOME"] = str(self.fake_home)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_LEDGER_LOG"] = str(self.log)
        env["FAKE_LEDGER_MODE"] = mode
        if env_file:
            env["CLAUDE_ENV_FILE"] = str(self.env_file)
        env.update(extra_env or {})
        for key in drop:
            env.pop(key, None)
        return subprocess.run(
            [sys.executable, str(self.hook)],
            input=stdin,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_with_no_cli_configured_it_prints_nothing_calls_nothing_and_writes_nothing(rig):
    proc = rig.run()
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert rig.calls() == []
    assert not rig.env_file.exists()


@pytest.mark.parametrize("value", [None, "", "  ", ["fakeledger"]])
def test_an_unset_or_unusable_setting_is_the_same_as_none(rig, value):
    rig.configure(value)
    proc = rig.run()
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert rig.calls() == []
    assert not rig.env_file.exists()


def test_success_prints_the_version_line_then_the_resume_brief(rig):
    rig.configure()
    proc = rig.run()
    assert proc.returncode == 0
    assert proc.stdout == "ledger 0.1.14\nBRIEF: 2 open items\nnext: LC15\n"
    assert rig.calls() == [["resume", "--brief"], ["--version"]]


def test_a_failed_version_call_does_not_hide_the_brief(rig):
    rig.configure()
    proc = rig.run(mode="versionfail")
    assert proc.returncode == 0
    assert proc.stdout == "BRIEF: 2 open items\nnext: LC15\n"
    assert rig.calls() == [["resume", "--brief"], ["--version"]]


def test_a_cli_that_exits_nonzero_prints_one_bounded_failure_line_and_exits_zero(rig):
    rig.configure()
    proc = rig.run(mode="fail")
    assert proc.returncode == 0
    assert proc.stdout == "ledger session-start: fakeledger resume --brief exited 3: boom\n"
    assert rig.calls() == [["resume", "--brief"]]

    proc = rig.run(mode="longfail")
    assert proc.returncode == 0
    assert proc.stdout.startswith("ledger session-start: fakeledger resume --brief exited 4: ")
    assert proc.stdout.endswith("\n")
    assert len(proc.stdout.splitlines()) == 1
    assert len(proc.stdout) <= 320


def test_an_older_ledger_without_resume_names_the_failure_and_exits_zero(rig):
    rig.configure()
    proc = rig.run(mode="noresume")
    assert proc.returncode == 0
    assert proc.stdout == "ledger session-start: fakeledger resume --brief exited 2: unknown verb: resume\n"


def test_a_missing_binary_names_the_failure_and_exits_zero(rig):
    missing = str(rig.bin / "no-such-ledger")
    rig.configure(missing)
    proc = rig.run()
    assert proc.returncode == 0
    assert proc.stdout == f"ledger session-start: {missing} resume --brief could not run: No such file or directory\n"


def test_a_timeout_names_the_failure_and_exits_zero(rig):
    rig.use_hook_copy(0.01)
    rig.configure()
    proc = rig.run(mode="timeout")
    assert proc.returncode == 0
    assert proc.stdout == "ledger session-start: fakeledger resume --brief timed out after 0.01s\n"


def test_the_session_id_is_written_to_the_env_file_only_when_one_is_set(rig):
    rig.configure()
    before = sorted(str(q) for q in rig.fake_home.rglob("*"))
    proc = rig.run(env_file=False)
    assert proc.returncode == 0
    assert not rig.env_file.exists()
    assert sorted(str(q) for q in rig.fake_home.rglob("*")) == before, "no file may be written without CLAUDE_ENV_FILE"
    proc = rig.run(env_file=True)
    assert proc.returncode == 0
    lines = rig.env_file.read_text(encoding="utf-8").splitlines()
    assert "export LEDGER_SESSION=sess-1" in lines


def test_the_agent_and_harness_are_set_only_when_the_hook_environment_has_none(rig):
    rig.configure()
    rig.run()
    assert rig.env_file.read_text(encoding="utf-8").splitlines() == [
        "export LEDGER_SESSION=sess-1",
        "export LEDGER_AGENT=claude-code",
        "export LEDGER_HARNESS=claude-code",
    ]
    rig.env_file.unlink()
    rig.run(extra_env={"LEDGER_AGENT": "someone-else", "LEDGER_HARNESS": "other-harness"})
    assert rig.env_file.read_text(encoding="utf-8").splitlines() == [
        "export LEDGER_SESSION=sess-1",
    ]


def test_model_string_and_object_id_are_shell_quoted(rig):
    rig.configure()
    for model in ["model one'", {"id": "model two'"}]:
        rig.run(stdin=json.dumps({"session_id": "sess-1", "model": model}))
        lines = rig.env_file.read_text(encoding="utf-8").splitlines()
        model_id = model if isinstance(model, str) else model["id"]
        assert f"export LEDGER_MODEL={shlex.quote(model_id)}" in lines
        rig.env_file.unlink()


def test_model_is_omitted_when_absent_empty_invalid_or_already_set(rig):
    rig.configure()
    for model in [None, "", "  ", {}, {"id": ""}, {"id": 3}, 3]:
        payload = {"session_id": "sess-1"}
        if model is not None:
            payload["model"] = model
        rig.run(stdin=json.dumps(payload))
        assert "LEDGER_MODEL" not in rig.env_file.read_text(encoding="utf-8")
        rig.env_file.unlink()

    rig.run(
        stdin=json.dumps({"session_id": "sess-1", "model": "new-model"}),
        extra_env={"LEDGER_MODEL": "existing-model"},
    )
    assert "LEDGER_MODEL" not in rig.env_file.read_text(encoding="utf-8")


def test_the_session_id_is_shell_quoted(rig):
    rig.configure()
    nasty = "a b'c;$(x)"
    rig.run(stdin=json.dumps({"session_id": nasty}))
    first = rig.env_file.read_text(encoding="utf-8").splitlines()[0]
    assert first == f"export LEDGER_SESSION={shlex.quote(nasty)}"
    assert shlex.split(first) == ["export", f"LEDGER_SESSION={nasty}"]


def test_the_env_file_is_appended_to_not_replaced(rig):
    rig.configure()
    rig.env_file.write_text("export KEEP=1\n", encoding="utf-8")
    rig.run()
    assert rig.env_file.read_text(encoding="utf-8").splitlines()[0] == "export KEEP=1"


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"session_id": 5}', "{}"])
def test_bad_stdin_still_prints_the_brief_and_writes_no_session(rig, stdin):
    rig.configure()
    proc = rig.run(stdin=stdin)
    assert proc.returncode == 0
    assert proc.stdout == "ledger 0.1.14\nBRIEF: 2 open items\nnext: LC15\n"
    text = rig.env_file.read_text(encoding="utf-8")
    assert "LEDGER_SESSION" not in text


def test_a_cli_failure_still_tags_the_session(rig):
    rig.configure()
    proc = rig.run(mode="fail")
    assert proc.returncode == 0
    assert "export LEDGER_SESSION=sess-1" in rig.env_file.read_text(encoding="utf-8")


def test_an_unwritable_env_file_does_not_fail_the_session(rig, tmp_path):
    rig.configure()
    proc = rig.run(extra_env={"CLAUDE_ENV_FILE": str(tmp_path / "no-dir" / "env")})
    assert proc.returncode == 0
    assert proc.stdout == "ledger 0.1.14\nBRIEF: 2 open items\nnext: LC15\n"


def test_it_writes_no_file_but_the_env_file(rig, tmp_path):
    """Run from a copy of the plugin layout and compare the whole tree afterwards."""
    plugin = tmp_path / "plugin"
    (plugin / "hooks").mkdir(parents=True)
    shutil.copy(HOOK, plugin / "hooks" / "session-start.py")
    shutil.copytree(
        REPO_ROOT / "skills" / "_lib",
        plugin / "skills" / "_lib",
        ignore=shutil.ignore_patterns("tests", "__pycache__"),
    )
    (plugin / "skills" / "topic").mkdir()
    shutil.copy(REPO_ROOT / "skills" / "topic" / "config.json", plugin / "skills" / "topic" / "config.json")
    rig.hook = plugin / "hooks" / "session-start.py"
    rig.configure()

    def tree(root):
        return sorted(str(p.relative_to(root)) for p in root.rglob("*"))

    before_plugin, before_home = tree(plugin), tree(rig.fake_home)
    proc = rig.run()
    assert proc.returncode == 0
    assert proc.stdout == "ledger 0.1.14\nBRIEF: 2 open items\nnext: LC15\n"
    assert tree(plugin) == before_plugin
    assert tree(rig.fake_home) == before_home
    assert rig.env_file.exists()


def test_hooks_json_declares_the_session_start_command():
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    entries = data["hooks"]["SessionStart"]
    commands = [h["command"] for entry in entries for h in entry["hooks"]]
    assert commands == ['python3 "${CLAUDE_PLUGIN_ROOT}/hooks/session-start.py"']
    assert all(h["type"] == "command" for entry in entries for h in entry["hooks"])
    assert HOOK.is_file()

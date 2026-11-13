"""The plugin's SessionStart hook: reports ledger state without blocking startup.

Whether the ledger is on is the shared rule's decision (`skills/_lib/ledger_cli.py`),
called with `path_rule=False`: an explicit `ledger_cli` setting turns it on and
`none` turns it off. With no setting the hook does not look on PATH, so a
`ledger` on PATH with `LEDGER_DB` set leaves it silent.
"""

import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
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
if argv[:1] == ["append-many"] and "--help" in argv:
    if mode == "nocapability":
        sys.stdout.write("usage: ledger append-many ITEM\\n")
    else:
        sys.stdout.write("usage: ledger append-many ITEM [--expect-revision N]\\n")
    raise SystemExit(0)
if mode == "fail":
    sys.stdout.write("PARTIAL OUTPUT\\n")
    sys.stderr.write("\\nboom\\nmore detail\\n")
    raise SystemExit(3)
if mode == "longfail":
    sys.stderr.write("x" * 400 + "\\n")
    raise SystemExit(4)
if mode == "emptybrief" and argv[0] == "resume":
    raise SystemExit(0)
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


EXPLICIT_LINE = "ledger on (explicit setting: ledger_cli in the topic config names it)\n"
BRIEF_OUTPUT = "ledger 0.1.14\nBRIEF: 2 open items\nnext: LC15\n"


def _load_rule():
    spec = importlib.util.spec_from_file_location(
        "session_start_test_rule", REPO_ROOT / "skills" / "_lib" / "ledger_cli.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    def path_ledger(self):
        """The same fake, installed as `ledger` on PATH; its path."""
        ledger = self.bin / "ledger"
        ledger.write_text(FAKE.replace("__PYTHON__", sys.executable), encoding="utf-8")
        ledger.chmod(ledger.stat().st_mode | stat.S_IXUSR)
        return ledger

    def database(self):
        """An existing database file for `LEDGER_DB`; its path."""
        db = self.fake_home.parent / "ledger.db"
        db.touch()
        return db

    def configure(self, value="fakeledger"):
        path = self.fake_home / ".claude" / "skill-config"
        path.mkdir(parents=True, exist_ok=True)
        (path / "topic.json").write_text(json.dumps({"ledger_cli": value}), encoding="utf-8")

    def run(self, stdin='{"session_id": "sess-1"}', mode="ok", env_file=True, extra_env=None, drop=(), cwd=None):
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
            cwd=cwd,
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
    assert proc.stdout == EXPLICIT_LINE + BRIEF_OUTPUT
    assert rig.calls() == [["resume", "--brief"], ["--version"]]


def test_an_empty_brief_prints_nothing_and_skips_the_version_call(rig):
    rig.configure()
    proc = rig.run(mode="emptybrief")
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""
    assert rig.calls() == [["resume", "--brief"]]


def test_a_failed_version_call_does_not_hide_the_brief(rig):
    rig.configure()
    proc = rig.run(mode="versionfail")
    assert proc.returncode == 0
    assert proc.stdout == EXPLICIT_LINE + "BRIEF: 2 open items\nnext: LC15\n"
    assert rig.calls() == [["resume", "--brief"], ["--version"]]


def test_version_probe_shares_the_resume_deadline_and_cannot_hide_the_brief(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("session_start_deadline", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    timeouts = []

    def run(args, **kwargs):
        timeouts.append(kwargs["timeout"])
        if args[1:] == ["resume", "--brief"]:
            time.sleep(0.01)
            return subprocess.CompletedProcess(args, 0, "BRIEF: ready\n", "")
        return subprocess.CompletedProcess(args, 2, "", "version unavailable\n")

    rule = _load_rule()
    decision = rule.Decision("fakeledger", rule.RULE_EXPLICIT, "set for the test")
    monkeypatch.setattr(module, "_input_data", lambda: {})
    monkeypatch.setattr(module, "_decide", lambda deadline: decision)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)

    assert module.main() == 0
    assert capsys.readouterr().out == "ledger on (explicit setting: set for the test)\nBRIEF: ready\n"
    assert timeouts[1] < timeouts[0], "version probe must use only the deadline remaining after resume"


def test_version_is_not_run_once_the_deadline_has_passed(monkeypatch):
    spec = importlib.util.spec_from_file_location("session_start_expired", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    invocations = []

    def run(args, **kwargs):
        invocations.append(args)
        return subprocess.CompletedProcess(args, 0, "ledger 0.1.14\n", "")

    monkeypatch.setattr(module.subprocess, "run", run)

    assert module._version("fakeledger", time.monotonic() - 1) == ""
    assert invocations == [], "no --version call once the shared deadline has passed"


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


def test_the_session_id_is_written_even_when_the_hook_environment_already_has_one(rig):
    rig.configure()
    rig.run(extra_env={"LEDGER_SESSION": "stale-session"})
    assert rig.env_file.read_text(encoding="utf-8").splitlines() == [
        "export LEDGER_SESSION=sess-1",
        "export LEDGER_AGENT=claude-code",
        "export LEDGER_HARNESS=claude-code",
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
    assert proc.stdout == EXPLICIT_LINE + BRIEF_OUTPUT
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
    assert proc.stdout == EXPLICIT_LINE + BRIEF_OUTPUT


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
    assert proc.stdout == EXPLICIT_LINE + BRIEF_OUTPUT
    assert tree(plugin) == before_plugin
    assert tree(rig.fake_home) == before_home
    assert rig.env_file.exists()


# --- The shared rule: a ledger on PATH with LEDGER_DB, and `none` ------------------------


def test_with_only_a_ledger_on_path_and_ledger_db_the_hook_stays_silent_and_runs_nothing(rig):
    # A compatible `ledger` on PATH and a LEDGER_DB that names a file turn the
    # ledger on for the skills a person invokes, but the hook uses only an
    # explicit setting: it never looks the `ledger` up, so nothing runs.
    rig.path_ledger()
    proc = rig.run(extra_env={"LEDGER_DB": str(rig.database())})
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""
    assert not rig.log.exists(), "the hook ran the ledger on PATH"
    assert not rig.env_file.exists()


@pytest.mark.parametrize("value", ["none", "NONE", " None "])
def test_none_keeps_the_hook_silent_though_a_ledger_is_on_path_with_ledger_db(rig, value):
    rig.path_ledger()
    rig.configure(value)
    proc = rig.run(extra_env={"LEDGER_DB": str(rig.database())})
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert rig.calls() == []
    assert not rig.env_file.exists()


@pytest.mark.parametrize("database", ["unset", "empty", "missing"])
def test_a_ledger_on_path_without_a_usable_ledger_db_is_silent_and_never_run(rig, tmp_path, database):
    rig.path_ledger()
    extra = {"empty": {"LEDGER_DB": ""}, "missing": {"LEDGER_DB": str(tmp_path / "absent.db")}}.get(database, {})
    proc = rig.run(extra_env=extra)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert rig.calls() == []
    assert not rig.env_file.exists()


def test_an_incompatible_ledger_on_path_is_silent_and_never_checked(rig):
    rig.path_ledger()
    proc = rig.run(mode="nocapability", extra_env={"LEDGER_DB": str(rig.database())})
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert rig.calls() == [], "the hook ran the compatibility check on a ledger from PATH"
    assert not rig.env_file.exists()


def test_an_unreadable_setting_never_falls_through_to_the_ledger_on_path(rig):
    rig.path_ledger()
    override = rig.fake_home / ".claude" / "skill-config" / "topic.json"
    override.parent.mkdir(parents=True)
    override.write_text("{not json", encoding="utf-8")
    proc = rig.run(extra_env={"LEDGER_DB": str(rig.database())})
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""
    assert rig.calls() == []


# --- A ledger a checkout plants on PATH: the hook never runs it ------------------------

# A compatible fake that appends each call to a marker file named in its own text.
PLANTED = """#!__PYTHON__
import sys
with open(__MARKER__, "a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["append-many"]:
    sys.stdout.write("usage: ledger append-many ITEM [--expect-revision N]\\n")
elif sys.argv[1:2] == ["resume"]:
    sys.stdout.write("BRIEF: planted\\n")
"""


def _plant(folder, marker):
    """A compatible fake `ledger` in `folder` that appends each call to `marker`; its path."""
    folder.mkdir(parents=True, exist_ok=True)
    ledger = folder / "ledger"
    ledger.write_text(
        PLANTED.replace("__PYTHON__", sys.executable).replace("__MARKER__", repr(str(marker))),
        encoding="utf-8",
    )
    ledger.chmod(ledger.stat().st_mode | stat.S_IXUSR)
    return ledger


def _payload(path, marker):
    """A shell script at `path` that appends to `marker` whenever anything runs it."""
    path.write_text(f"#!/bin/sh\necho ran >> {shlex.quote(str(marker))}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _first_on_path(folder):
    return f"{folder}{os.pathsep}{os.environ.get('PATH', '')}"


def _silent_and_never_ran(proc, marker, rig):
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""
    assert not marker.exists(), "the hook ran a program a checkout put on PATH"
    assert not rig.env_file.exists()


def test_a_ledger_planted_in_a_checkout_bin_on_path_is_never_run_from_the_checkout_root(rig, tmp_path):
    checkout = tmp_path / "checkout"
    marker = tmp_path / "planted-ran.log"
    _plant(checkout / "bin", marker)
    proc = rig.run(
        cwd=checkout,
        extra_env={"PATH": _first_on_path(checkout / "bin"), "LEDGER_DB": str(rig.database())},
    )
    _silent_and_never_ran(proc, marker, rig)


def test_a_ledger_planted_in_a_checkout_bin_on_path_is_never_run_from_a_subfolder(rig, tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "sub").mkdir(parents=True)
    marker = tmp_path / "planted-ran.log"
    _plant(checkout / "bin", marker)
    proc = rig.run(
        cwd=checkout / "sub",
        extra_env={"PATH": _first_on_path(checkout / "bin"), "LEDGER_DB": str(rig.database())},
    )
    _silent_and_never_ran(proc, marker, rig)


@pytest.mark.parametrize("where", ["root", "subfolder"])
@pytest.mark.parametrize("target", ["/bin/sh", "/usr/bin/env"])
def test_a_planted_ledger_linked_to_a_system_program_never_runs_its_payload(rig, tmp_path, target, where):
    # `bin/ledger` is a link to a program outside the checkout. Run for the
    # compatibility check, `sh append-many --help` would run ./append-many from
    # the working directory, and `env append-many --help` the bin/append-many
    # first on PATH. Both payloads are the checkout's own.
    checkout = tmp_path / "checkout"
    (checkout / "bin").mkdir(parents=True)
    cwd = checkout if where == "root" else checkout / "sub"
    cwd.mkdir(exist_ok=True)
    marker = tmp_path / "payload-ran.log"
    (checkout / "bin" / "ledger").symlink_to(target)
    if target == "/bin/sh":
        _payload(cwd / "append-many", marker)
    else:
        _payload(checkout / "bin" / "append-many", marker)
    proc = rig.run(
        cwd=cwd,
        extra_env={"PATH": _first_on_path(checkout / "bin"), "LEDGER_DB": str(rig.database())},
    )
    _silent_and_never_ran(proc, marker, rig)


@pytest.mark.parametrize("shape", ["dot", "relative bin", "empty entry", "sibling checkout", "project below home"])
def test_no_path_entry_shape_makes_the_hook_run_a_planted_ledger(rig, tmp_path, shape):
    system = os.environ.get("PATH", "")
    checkout = tmp_path / "checkout"
    marker = tmp_path / "planted-ran.log"
    if shape == "dot":
        _plant(checkout / "bin", marker)
        cwd, path = checkout / "bin", f".{os.pathsep}{system}"
    elif shape == "relative bin":
        _plant(checkout / "bin", marker)
        cwd, path = checkout, f"bin{os.pathsep}{system}"
    elif shape == "empty entry":
        # An empty PATH entry is the working directory.
        _plant(checkout / "bin", marker)
        cwd, path = checkout / "bin", f"{os.pathsep}{system}"
    elif shape == "sibling checkout":
        sibling_bin = tmp_path / "sibling" / "bin"
        _plant(sibling_bin, marker)
        checkout.mkdir()
        cwd, path = checkout, _first_on_path(sibling_bin)
    else:
        # The session starts in a temporary HOME; a checkout below it has its bin on PATH.
        project_bin = rig.fake_home / "code" / "project" / "bin"
        _plant(project_bin, marker)
        cwd, path = rig.fake_home, _first_on_path(project_bin)
    proc = rig.run(cwd=cwd, extra_env={"PATH": path, "LEDGER_DB": str(rig.database())})
    _silent_and_never_ran(proc, marker, rig)


def _plugin_with_shipped(root, shipped):
    """A copy of the plugin layout whose shipped topic config is `shipped`; the hook's path."""
    plugin = root / "plugin"
    (plugin / "hooks").mkdir(parents=True)
    shutil.copy(HOOK, plugin / "hooks" / "session-start.py")
    shutil.copytree(
        REPO_ROOT / "skills" / "_lib",
        plugin / "skills" / "_lib",
        ignore=shutil.ignore_patterns("tests", "__pycache__"),
    )
    (plugin / "skills" / "topic").mkdir()
    (plugin / "skills" / "topic" / "config.json").write_text(json.dumps(shipped), encoding="utf-8")
    return plugin / "hooks" / "session-start.py"


@pytest.mark.parametrize("broken", ["{not json", "[1, 2]", "directory"])
def test_a_broken_override_keeps_the_hook_silent_though_the_shipped_config_names_a_cli(rig, tmp_path, broken):
    rig.hook = _plugin_with_shipped(tmp_path, {"ledger_cli": str(rig.bin / "fakeledger")})
    # Control: with no override file, the shipped CLI runs.
    proc = rig.run()
    assert proc.stdout == EXPLICIT_LINE + BRIEF_OUTPUT
    rig.log.unlink()
    rig.env_file.unlink()
    override = rig.fake_home / ".claude" / "skill-config" / "topic.json"
    override.parent.mkdir(parents=True)
    if broken == "directory":
        override.mkdir()
    else:
        override.write_text(broken, encoding="utf-8")
    proc = rig.run()
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""
    assert rig.calls() == [], "the shipped CLI ran beneath a broken override"
    assert not rig.env_file.exists()


def test_an_explicit_setting_wins_over_the_ledger_on_path(rig):
    rig.path_ledger()
    rig.configure()
    proc = rig.run(extra_env={"LEDGER_DB": str(rig.database())})
    assert proc.stdout == EXPLICIT_LINE + BRIEF_OUTPUT
    assert rig.calls() == [["resume", "--brief"], ["--version"]], "an explicit CLI is not probed"


def test_the_decision_and_the_brief_share_one_deadline(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("session_start_shared_deadline", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen = {}
    real = module._decide

    def decide(deadline):
        seen["deadline"] = deadline
        return real(deadline)

    def brief(cli, deadline):
        seen["brief"] = deadline
        return "", ""

    monkeypatch.setattr(module, "_decide", decide)
    monkeypatch.setattr(module, "_brief", brief)
    monkeypatch.setattr(module, "_input_data", lambda: {})
    (tmp_path / "rig").mkdir()
    rig = Rig(tmp_path / "rig")
    rig.configure()
    monkeypatch.setenv("HOME", str(rig.fake_home))
    monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
    assert module.main() == 0
    assert seen["brief"] == seen["deadline"]


def test_hooks_json_declares_the_session_start_command():
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    entries = data["hooks"]["SessionStart"]
    commands = [h["command"] for entry in entries for h in entry["hooks"]]
    assert commands == ['python3 "${CLAUDE_PLUGIN_ROOT}/hooks/session-start.py"']
    assert all(h["type"] == "command" for entry in entries for h in entry["hooks"])
    assert HOOK.is_file()

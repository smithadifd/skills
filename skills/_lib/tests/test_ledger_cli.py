"""The shared rule that decides whether the ledger is on: one rule, four named outcomes, never raises."""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LIB_SCRIPT = REPO_ROOT / "skills" / "_lib" / "ledger_cli.py"

EXPLICIT = "explicit setting"
OFF = "off by setting"
ON_PATH = "ledger on PATH with LEDGER_DB"
NOT_CONFIGURED = "not configured"

# A `ledger` that logs every call and answers the compatibility check by mode.
FAKE = """#!__PYTHON__
import json, os, sys, time
argv = sys.argv[1:]
with open(__LOG__, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(argv) + "\\n")
mode = __MODE__
if argv[:1] == ["append-many"] and "--help" in argv:
    if mode == "hang":
        time.sleep(30)
    if mode == "exit2":
        sys.stderr.write("usage: ledger <verb>\\n")
        raise SystemExit(2)
    if mode == "noflag":
        sys.stdout.write("usage: ledger append-many ITEM\\n")
        raise SystemExit(0)
    if mode == "nonutf8":
        sys.stdout.buffer.write(b"\\xff\\xfe usage\\n")
        raise SystemExit(0)
    sys.stdout.write("usage: ledger append-many ITEM [--expect-revision N]\\n")
    raise SystemExit(0)
print("ok")
"""


def _load():
    spec = importlib.util.spec_from_file_location("ledger_cli_under_test", LIB_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _layout(tmp_path, shipped):
    """A copy of the plugin layout: `_lib` and the topic skill's shipped config."""
    skills = tmp_path / "skills"
    shutil.copytree(
        REPO_ROOT / "skills" / "_lib",
        skills / "_lib",
        ignore=shutil.ignore_patterns("tests", "__pycache__"),
    )
    (skills / "topic").mkdir()
    (skills / "topic" / "config.json").write_text(json.dumps(shipped), encoding="utf-8")
    return skills / "_lib" / "ledger_cli.py"


def _env(home, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env["HOME"] = str(home)
    env.update(extra)
    return env


def _run(script, home, *args, cwd=None, **extra):
    return subprocess.run(
        [sys.executable, str(script), *args],
        env=_env(home, **extra),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _out(proc):
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _cli(proc):
    return _out(proc)["ledger_cli"]


def _override(home, **keys):
    path = home / ".claude" / "skill-config"
    path.mkdir(parents=True, exist_ok=True)
    (path / "topic.json").write_text(json.dumps(keys), encoding="utf-8")


class Machine:
    """A `ledger` on a PATH of its own, a database file, and a clean HOME."""

    def __init__(self, tmp_path, mode="ok"):
        self.home_dir = tmp_path / "home"
        self.home_dir.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "calls.log"
        self.ledger = self.bin / "ledger"
        self.ledger.write_text(
            FAKE.replace("__PYTHON__", sys.executable)
            .replace("__LOG__", repr(str(self.log)))
            .replace("__MODE__", repr(mode)),
            encoding="utf-8",
        )
        self.ledger.chmod(self.ledger.stat().st_mode | stat.S_IXUSR)
        self.db = tmp_path / "ledger.db"
        self.db.touch()
        self.path = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"

    def run(self, *args, script=LIB_SCRIPT, cwd=None, **extra):
        extra.setdefault("PATH", self.path)
        extra.setdefault("LEDGER_DB", str(self.db))
        return _run(script, self.home_dir, *args, cwd=cwd, **extra)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def use(self, monkeypatch, **extra):
        """The same machine in this process, for `decide` called directly."""
        monkeypatch.setenv("HOME", str(self.home_dir))
        monkeypatch.setenv("PATH", extra.pop("PATH", self.path))
        monkeypatch.setenv("LEDGER_DB", extra.pop("LEDGER_DB", str(self.db)))
        for name, value in extra.items():
            monkeypatch.setenv(name, value)


@pytest.fixture
def machine(tmp_path):
    return Machine(tmp_path)


# --- The grammar of the decision and its line --------------------------------------


def test_the_four_rules_are_fixed_phrases():
    module = _load()
    assert module.RULES == (EXPLICIT, OFF, ON_PATH, NOT_CONFIGURED)
    assert (module.RULE_EXPLICIT, module.RULE_OFF, module.RULE_PATH, module.RULE_NOT_CONFIGURED) == module.RULES


@pytest.mark.parametrize(
    "cli, rule, line",
    [
        ("/bin/x", EXPLICIT, "ledger on (explicit setting: why)"),
        (None, OFF, "ledger off (off by setting: why)"),
        ("/bin/x", ON_PATH, "ledger on (ledger on PATH with LEDGER_DB: why)"),
        (None, NOT_CONFIGURED, "ledger off (not configured: why)"),
    ],
)
def test_the_ledger_line_names_the_rule_in_one_fixed_form(cli, rule, line):
    decision = _load().Decision(cli, rule, "why")
    assert decision.line == line
    assert decision.reason == f"{rule}: why"
    assert decision.on is (cli is not None)


@pytest.mark.parametrize(
    "rule",
    ["Explicit setting", "explicit setting ", " explicit setting", "explicit-setting", "explicit", "- explicit setting", ""],
)
def test_a_near_miss_rule_phrase_is_refused_by_name(rule):
    with pytest.raises(ValueError, match="unknown ledger rule"):
        _load().Decision("/bin/x", rule, "why")


@pytest.mark.parametrize(
    "cli, rule",
    [(None, EXPLICIT), (None, ON_PATH), ("/bin/x", OFF), ("/bin/x", NOT_CONFIGURED)],
)
def test_a_rule_that_does_not_match_on_or_off_is_refused(cli, rule):
    with pytest.raises(ValueError, match="does not match"):
        _load().Decision(cli, rule, "why")


# --- Rule 1: an explicit setting ----------------------------------------------------


def test_the_shipped_config_leaves_it_off_without_a_ledger_database(tmp_path):
    out = _out(_run(LIB_SCRIPT, tmp_path))
    assert (out["ledger_cli"], out["rule"]) == (None, NOT_CONFIGURED)
    assert out["line"].startswith("ledger off (not configured: ")


def test_the_override_file_sets_it(tmp_path):
    _override(tmp_path, ledger_cli="myledger")
    assert _out(_run(LIB_SCRIPT, tmp_path)) == {
        "ledger_cli": "myledger",
        "rule": EXPLICIT,
        "line": "ledger on (explicit setting: ledger_cli in the topic config names it)",
    }


def test_a_tilde_path_is_expanded(tmp_path):
    _override(tmp_path, ledger_cli="~/.claude/bin/ledger")
    assert _cli(_run(LIB_SCRIPT, tmp_path)) == str(tmp_path / ".claude" / "bin" / "ledger")


def test_the_shipped_value_applies_when_the_override_is_silent(tmp_path):
    script = _layout(tmp_path / "plugin", {"ledger_cli": "shipped-ledger"})
    home = tmp_path / "home"
    home.mkdir()
    assert _cli(_run(script, home)) == "shipped-ledger"


def test_the_override_wins_over_the_shipped_value_and_null_clears_it(tmp_path):
    script = _layout(tmp_path / "plugin", {"ledger_cli": "shipped-ledger"})
    home = tmp_path / "home"
    home.mkdir()
    _override(home, ledger_cli="mine")
    assert _cli(_run(script, home)) == "mine"
    # null in the override clears the shipped value: no explicit setting, so the
    # PATH rule decides, and this machine has no ledger database.
    _override(home, ledger_cli=None)
    out = _out(_run(script, home))
    assert (out["ledger_cli"], out["rule"]) == (None, NOT_CONFIGURED)


@pytest.mark.parametrize("value", ["none", "NONE", "None", "  none  "])
def test_none_turns_it_off_though_a_compatible_ledger_is_on_path(machine, value):
    _override(machine.home_dir, ledger_cli=value)
    assert _out(machine.run()) == {
        "ledger_cli": None,
        "rule": OFF,
        "line": "ledger off (off by setting: ledger_cli in the topic config is none)",
    }
    assert machine.calls() == [], "an opted-out machine never runs the ledger"


@pytest.mark.parametrize("value", ["nonee", "no ne", "off", "false", "'none'", "none.", "-none"])
def test_a_near_miss_of_none_is_a_cli_name_not_the_off_switch(machine, value):
    # The off switch is exactly `none` in any case, surrounding blanks ignored.
    # Anything else is an explicit CLI name, reported by the call that runs it.
    _override(machine.home_dir, ledger_cli=value)
    out = _out(machine.run())
    assert (out["ledger_cli"], out["rule"]) == (value, EXPLICIT)
    assert machine.calls() == []


def test_an_explicit_setting_wins_over_the_ledger_on_path_and_is_not_probed(machine, tmp_path):
    other = tmp_path / "elsewhere" / "my-ledger"
    _override(machine.home_dir, ledger_cli=str(other))
    out = _out(machine.run())
    assert (out["ledger_cli"], out["rule"]) == (str(other), EXPLICIT)
    assert machine.calls() == []


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_unset_key_is_no_explicit_setting_so_the_path_rule_decides(machine, value):
    _override(machine.home_dir, ledger_cli=value)
    out = _out(machine.run())
    assert (out["ledger_cli"], out["rule"]) == (str(machine.ledger), ON_PATH)


@pytest.mark.parametrize("value", [["ledger"], 7, True, {"a": 1}])
def test_a_key_that_is_not_a_string_is_off_and_never_falls_through_to_path(machine, value):
    _override(machine.home_dir, ledger_cli=value)
    assert _out(machine.run()) == {
        "ledger_cli": None,
        "rule": NOT_CONFIGURED,
        "line": "ledger off (not configured: ledger_cli in the topic config is not a string)",
    }
    assert machine.calls() == []


def test_strict_mode_refuses_a_non_string_the_way_topic_does(tmp_path):
    module = _load()
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"ledger_cli": ["x"]}), encoding="utf-8")
    with pytest.raises(module.ConfigError):
        module.decide(path, strict=True)
    with pytest.raises(module.ConfigError):
        module.resolve(path, strict=True)
    assert module.resolve(path) is None


@pytest.mark.parametrize("broken", ["{not json", "[1, 2]"])
def test_a_config_that_cannot_be_read_is_off_and_never_falls_through_to_path(machine, broken):
    override = machine.home_dir / ".claude" / "skill-config" / "topic.json"
    override.parent.mkdir(parents=True)
    override.write_text(broken, encoding="utf-8")
    proc = machine.run()
    out = _out(proc)
    assert (out["ledger_cli"], out["rule"]) == (None, NOT_CONFIGURED)
    assert out["line"] == (
        f"ledger off (not configured: {override} could not be read, so no ledger on PATH is used)"
    )
    assert len(proc.stderr.splitlines()) == 1 and str(override) in proc.stderr
    assert machine.calls() == []


@pytest.mark.parametrize("broken", ["{not json", "[1, 2]", "directory"])
def test_a_broken_override_is_off_though_the_shipped_config_names_a_cli(machine, tmp_path, broken):
    # Each config source keeps its own read status: the override comes first,
    # and a value in the shipped config beneath a broken override is never used.
    script = _layout(tmp_path / "plugin", {"ledger_cli": str(machine.ledger)})
    override = machine.home_dir / ".claude" / "skill-config" / "topic.json"
    override.parent.mkdir(parents=True)
    if broken == "directory":
        override.mkdir()
    else:
        override.write_text(broken, encoding="utf-8")
    assert _out(machine.run(script=script)) == {
        "ledger_cli": None,
        "rule": NOT_CONFIGURED,
        "line": f"ledger off (not configured: {override} could not be read, so no ledger on PATH is used)",
    }
    assert machine.calls() == []


def test_a_broken_shipped_config_beneath_an_override_value_keeps_the_override(machine, tmp_path):
    script = _layout(tmp_path / "plugin", {})
    (tmp_path / "plugin" / "skills" / "topic" / "config.json").write_text("{not json", encoding="utf-8")
    _override(machine.home_dir, ledger_cli="mine")
    out = _out(machine.run(script=script))
    assert (out["ledger_cli"], out["rule"]) == ("mine", EXPLICIT)
    # An override that clears the key leaves the broken shipped file standing: off, not PATH.
    _override(machine.home_dir, ledger_cli=None)
    out = _out(machine.run(script=script))
    assert (out["ledger_cli"], out["rule"]) == (None, NOT_CONFIGURED)
    assert machine.calls() == []


def test_an_explicit_config_file_that_is_missing_is_off_and_never_falls_through_to_path(machine, tmp_path):
    out = _out(machine.run("--config", str(tmp_path / "absent.json")))
    assert (out["ledger_cli"], out["rule"]) == (None, NOT_CONFIGURED)
    assert machine.calls() == []


def test_a_layout_without_the_topic_skill_is_silent_and_still_finds_the_ledger_on_path(machine, tmp_path):
    skills = tmp_path / "plugin" / "skills"
    shutil.copytree(
        REPO_ROOT / "skills" / "_lib",
        skills / "_lib",
        ignore=shutil.ignore_patterns("tests", "__pycache__"),
    )
    script = skills / "_lib" / "ledger_cli.py"
    off = machine.run(script=script, PATH=os.environ.get("PATH", ""), LEDGER_DB="")
    assert off.stderr == ""
    assert _cli(off) is None
    on = machine.run(script=script)
    assert on.stderr == ""
    assert (_out(on)["ledger_cli"], _out(on)["rule"]) == (str(machine.ledger), ON_PATH)


def test_the_environment_is_not_a_second_switch_for_the_key(tmp_path):
    proc = _run(LIB_SCRIPT, tmp_path, LEDGER_CLI="from-env", ledger_cli="from-env")
    assert _cli(proc) is None


# --- A caller's own explicit setting (the ticket skill's variable) ------------------


def test_a_named_variable_comes_before_the_config_key(machine, monkeypatch):
    module = _load()
    _override(machine.home_dir, ledger_cli="/opt/config-ledger")
    machine.use(monkeypatch, SKILL_LEDGER_CLI="NONE")
    assert module.decide(selector_env="SKILL_LEDGER_CLI") == module.Decision(
        None, OFF, "SKILL_LEDGER_CLI=none"
    )
    monkeypatch.setenv("SKILL_LEDGER_CLI", "/opt/env-ledger")
    assert module.decide(selector_env="SKILL_LEDGER_CLI") == module.Decision(
        "/opt/env-ledger", EXPLICIT, "SKILL_LEDGER_CLI names it"
    )
    monkeypatch.setenv("SKILL_LEDGER_CLI", "   ")
    assert module.decide(selector_env="SKILL_LEDGER_CLI").cli == "/opt/config-ledger"
    # Only a caller that names the variable reads it.
    monkeypatch.setenv("SKILL_LEDGER_CLI", "none")
    assert module.decide().cli == "/opt/config-ledger"
    assert machine.calls() == []


def test_a_named_variable_that_is_unset_leaves_the_path_rule_to_decide(machine, monkeypatch):
    module = _load()
    machine.use(monkeypatch)
    monkeypatch.delenv("SKILL_LEDGER_CLI", raising=False)
    decision = module.decide(selector_env="SKILL_LEDGER_CLI")
    assert (decision.cli, decision.rule) == (str(machine.ledger), ON_PATH)


# --- Rule 2: a compatible ledger on PATH with a usable LEDGER_DB ----------------------


def test_a_compatible_ledger_on_path_with_a_database_turns_it_on(machine):
    assert _out(machine.run()) == {
        "ledger_cli": str(machine.ledger),
        "rule": ON_PATH,
        "line": (
            f"ledger on (ledger on PATH with LEDGER_DB: {machine.ledger} found on PATH, "
            f"LEDGER_DB names {machine.db})"
        ),
    }
    assert machine.calls() == [["append-many", "--help"]]


@pytest.mark.parametrize("database", ["unset", "empty", "missing", "directory"])
def test_without_a_usable_database_the_ledger_on_path_is_never_run(machine, tmp_path, database):
    if database == "unset":
        proc = _run(LIB_SCRIPT, machine.home_dir, PATH=machine.path)
        detail = "is on PATH but LEDGER_DB is not set, and the ledger picks no default database"
    elif database == "empty":
        proc = machine.run(LEDGER_DB="")
        detail = "is on PATH but LEDGER_DB is not set, and the ledger picks no default database"
    else:
        target = tmp_path / "absent.db"
        if database == "directory":
            target.mkdir()
        proc = machine.run(LEDGER_DB=str(target))
        detail = f"is on PATH but LEDGER_DB names {target}, which is not a file"
    assert _out(proc) == {
        "ledger_cli": None,
        "rule": NOT_CONFIGURED,
        "line": f"ledger off (not configured: {machine.ledger} {detail})",
    }
    assert machine.calls() == []


@pytest.mark.parametrize("mode", ["exit2", "noflag", "nonutf8"])
def test_an_incompatible_ledger_on_path_leaves_it_off(tmp_path, mode):
    machine = Machine(tmp_path, mode)
    assert _out(machine.run()) == {
        "ledger_cli": None,
        "rule": NOT_CONFIGURED,
        "line": (
            f"ledger off (not configured: {machine.ledger} on PATH lacks append-many or "
            "--expect-revision, or does not run)"
        ),
    }
    assert machine.calls() == [["append-many", "--help"]]


def test_a_ledger_on_path_whose_check_hangs_is_off_within_the_timeout(tmp_path, monkeypatch):
    machine = Machine(tmp_path, "hang")
    module = _load()
    machine.use(monkeypatch)
    decision = module.decide(probe_timeout=0.5)
    assert (decision.cli, decision.rule) == (None, NOT_CONFIGURED)
    assert module.probe(str(machine.ledger), 0.5) == "append-many --help did not finish within 0.5s"


def test_no_ledger_on_path_is_off(tmp_path, monkeypatch):
    module = _load()
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    db = tmp_path / "ledger.db"
    db.touch()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("LEDGER_DB", str(db))
    assert module.decide() == module.Decision(
        None, NOT_CONFIGURED, "no ledger CLI on PATH and no setting names one"
    )


INSIDE = "ledger off (not configured: the ledger CLI on PATH lies inside the working directory, so it is not run)"


def test_a_ledger_on_path_inside_the_working_directory_is_never_run(tmp_path):
    # A checkout that is the working directory and whose bin folder is on PATH
    # must not choose the program the rule runs: the planted `ledger` is never
    # run, not even for the compatibility check. Its log is the side effect.
    project = tmp_path / "project"
    project.mkdir()
    machine = Machine(project)
    out = _out(machine.run(cwd=project))
    assert out == {"ledger_cli": None, "rule": NOT_CONFIGURED, "line": INSIDE}
    assert not machine.log.exists(), "the ledger inside the working directory was run"
    # The same ledger with the working directory elsewhere is on.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out = _out(machine.run(cwd=elsewhere))
    assert (out["ledger_cli"], out["rule"]) == (str(machine.ledger), ON_PATH)
    assert machine.calls() == [["append-many", "--help"]]


@pytest.mark.parametrize("entry", ["symlink", "relative", "nested"])
def test_a_ledger_reached_through_any_path_entry_inside_the_working_directory_is_never_run(tmp_path, entry):
    project = tmp_path / "project"
    project.mkdir()
    machine = Machine(project)
    cwd = project
    if entry == "symlink":
        # A PATH folder outside the project whose `ledger` links into it.
        outer = tmp_path / "outer-bin"
        outer.mkdir()
        (outer / "ledger").symlink_to(machine.ledger)
        path = str(outer)
    elif entry == "relative":
        path = "bin"
    else:
        # The working directory is the project's parent: the project is inside it.
        path = str(machine.bin)
        cwd = tmp_path
    out = _out(machine.run(cwd=cwd, PATH=path))
    assert out == {"ledger_cli": None, "rule": NOT_CONFIGURED, "line": INSIDE}
    assert not machine.log.exists(), "the ledger inside the working directory was run"


def test_a_working_directory_that_cannot_be_examined_leaves_the_ledger_off(machine, tmp_path, monkeypatch):
    # The ledger lies outside the working directory, but the working directory
    # is gone, so containment cannot be told: the rule fails closed.
    module = _load()
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    machine.use(monkeypatch)
    assert module.decide() == module.Decision(
        None,
        NOT_CONFIGURED,
        "the ledger CLI on PATH lies inside the working directory, so it is not run",
    )
    assert machine.calls() == []


def test_an_explicit_setting_naming_a_ledger_inside_the_working_directory_is_still_used(tmp_path):
    # The person chose it: the working-directory skip belongs to the PATH rule only.
    project = tmp_path / "project"
    project.mkdir()
    machine = Machine(project)
    _override(machine.home_dir, ledger_cli=str(machine.ledger))
    out = _out(machine.run(cwd=project))
    assert (out["ledger_cli"], out["rule"]) == (str(machine.ledger), EXPLICIT)


def test_resolve_is_the_cli_the_rule_chose(machine, monkeypatch):
    module = _load()
    machine.use(monkeypatch)
    assert module.resolve() == module.decide().cli == str(machine.ledger)


def test_link_argv_is_the_documented_call():
    module = _load()
    assert module.link_argv("topic-x", "/a/b.md") == [
        "link", "topic-x", "--rel", "url", "--target", "/a/b.md",
    ]

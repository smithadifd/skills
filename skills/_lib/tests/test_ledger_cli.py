"""The shared rule that decides whether the ledger is on: one rule, four named outcomes, never raises."""

import errno
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
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


def _run(script, home, *args, **extra):
    return subprocess.run(
        [sys.executable, str(script), *args],
        env=_env(home, **extra),
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

    def run(self, *args, script=LIB_SCRIPT, **extra):
        extra.setdefault("PATH", self.path)
        extra.setdefault("LEDGER_DB", str(self.db))
        return _run(script, self.home_dir, *args, **extra)

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


def test_resolve_is_the_cli_the_rule_chose(machine, monkeypatch):
    module = _load()
    machine.use(monkeypatch)
    assert module.resolve() == module.decide().cli == str(machine.ledger)


def test_link_argv_is_the_documented_call():
    module = _load()
    assert module.link_argv("topic-x", "/a/b.md") == [
        "link", "topic-x", "--rel", "url", "--target", "/a/b.md",
    ]


# --- Setup: pin the ledger CLI's absolute path ---------------------------------------

PLANT = """#!__PYTHON__
import sys
with open(__MARK__, "a", encoding="utf-8") as handle:
    handle.write("planted ledger ran: " + " ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["append-many", "--help"]:
    sys.stdout.write("usage: ledger append-many ITEM [--expect-revision N]\\n")
print("ok")
"""

SETUP_TOKENS = (
    "ledger_cli_setup_not_found",
    "ledger_cli_setup_incompatible",
    "ledger_cli_setup_config_unreadable",
    "ledger_cli_setup_exists",
    "ledger_cli_setup_config_changed",
    "ledger_cli_setup_no_hardlinks",
    "ledger_cli_setup_write_failed",
    "ledger_cli_setup_lock_failed",
)


LOCK_NAME = "topic.json.lock"
BACKUP_NAME = "topic.json.setup-backup"
PREVIOUS_NAME = "topic.json.setup-previous"
# The grammar of a kept name: the prefix, a UTC timestamp, the pid, and an optional collision counter.
KEPT_GRAMMAR = re.compile(r"^topic\.json\.setup-previous\.\d{8}T\d{6}Z-\d+(-\d+)?$")


def _plant(directory, marker):
    """A `ledger` that writes `marker` whenever it is run with any argument; returns its folder."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "ledger"
    script.write_text(
        PLANT.replace("__PYTHON__", sys.executable).replace("__MARK__", repr(str(marker))),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return directory


def _override_file(home):
    return home / ".claude" / "skill-config" / "topic.json"


def _put_override(home, text):
    path = _override_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def _pin(machine):
    """Run setup on the machine and return the path it pinned."""
    proc = machine.run("setup")
    assert proc.returncode == 0, proc.stderr
    return json.loads(_override_file(machine.home_dir).read_text(encoding="utf-8"))["ledger_cli"]


def _refusal(proc, token):
    """The one stderr line of a refusal: it starts with `token:`; nothing on stdout; exit 1."""
    assert proc.returncode == 1, (proc.returncode, proc.stderr)
    assert proc.stdout == ""
    lines = proc.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith(f"{token}: "), proc.stderr
    return lines[0]


def _no_temp_files(home):
    folder = _override_file(home).parent
    if folder.is_dir():
        # The kept files of displaced versions are left on purpose after a commit.
        leftovers = [
            p.name
            for p in folder.iterdir()
            if p.name not in ("topic.json", LOCK_NAME) and not p.name.startswith(PREVIOUS_NAME + ".")
        ]
        assert leftovers == []


def _nothing_written(home):
    """Setup refused: no override file, and at most the empty lock file beside where it would be."""
    assert not _override_file(home).exists()
    folder = _override_file(home).parent
    if folder.is_dir():
        assert [p.name for p in folder.iterdir()] in ([], [LOCK_NAME])
        lock = folder / LOCK_NAME
        assert not lock.exists() or lock.read_bytes() == b""


@pytest.mark.parametrize(
    "before",
    [None, {}, {"topics_dir": "/somewhere", "ledger_cli": None, "extra": [1, {"a": "b"}]}, {"ledger_cli": "", "z": 1}],
    ids=["no-file", "empty-object", "other-keys-and-null", "blank-value"],
)
def test_setup_writes_the_absolute_path_and_keeps_every_other_key(machine, before):
    if before is not None:
        _put_override(machine.home_dir, json.dumps(before))
    proc = machine.run("setup")
    assert proc.returncode == 0 and proc.stderr == ""
    path = _override_file(machine.home_dir)
    kept = (
        ""
        if before is None
        else f"; the version it replaced is kept at {_previous(machine.home_dir)}; remove it once no editor has it open"
    )
    assert proc.stdout.splitlines() == [f"ledger_cli set to {machine.ledger} in {path}{kept}"]
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {**(before or {}), "ledger_cli": str(machine.ledger)}
    assert list(written)[: len(before or {})] == list(before or {}), "the key order is kept"
    assert machine.calls() == [["append-many", "--help"]], "the one run is the compatibility check"
    _no_temp_files(machine.home_dir)
    # The shared rule now names that path through the explicit setting.
    out = _out(machine.run())
    assert (out["ledger_cli"], out["rule"]) == (str(machine.ledger), EXPLICIT)


def test_setup_keeps_a_symlink_on_path_unresolved(tmp_path):
    machine = Machine(tmp_path)
    link_dir = tmp_path / "linkbin"
    link_dir.mkdir()
    (link_dir / "ledger").symlink_to(machine.ledger)
    proc = machine.run("setup", PATH=f"{link_dir}{os.pathsep}{machine.path}")
    assert proc.returncode == 0, proc.stderr
    pinned = json.loads(_override_file(machine.home_dir).read_text(encoding="utf-8"))["ledger_cli"]
    assert pinned == str(link_dir / "ledger")


def test_setup_through_a_symlinked_override_rewrites_its_target_and_keeps_the_link(machine, tmp_path):
    real = tmp_path / "dotfiles" / "topic.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"keep": 1}), encoding="utf-8")
    link = _override_file(machine.home_dir)
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    assert machine.run("setup").returncode == 0
    assert link.is_symlink() and link.resolve() == real.resolve()
    assert json.loads(real.read_text(encoding="utf-8")) == {"keep": 1, "ledger_cli": str(machine.ledger)}


def test_after_setup_a_ledger_planted_earlier_on_path_is_never_run(machine, tmp_path, monkeypatch):
    marker = tmp_path / "planted.marker"
    evil = _plant(tmp_path / "evil", marker)
    pinned = _pin(machine)
    machine.log.unlink()
    path = f"{evil}{os.pathsep}{machine.path}"
    # A call through the decided CLI runs the pinned one: the plant stays silent.
    module = _load()
    machine.use(monkeypatch, PATH=path)
    decision = module.decide()
    module.call(decision.cli, ["note", "x"])
    assert not marker.exists(), marker.read_text() if marker.exists() else ""
    assert machine.calls() == [["note", "x"]]
    # The decision names the pinned path, by the explicit setting, with the plant first on PATH.
    assert decision.cli == pinned and decision.rule == EXPLICIT
    out = _out(machine.run(PATH=path))
    assert (out["ledger_cli"], out["rule"]) == (pinned, EXPLICIT)
    assert not marker.exists()


def test_nothing_reads_path_while_a_setting_names_the_cli(machine, tmp_path, monkeypatch):
    pinned = _pin(machine)
    module = _load()
    machine.use(monkeypatch)

    def refuse_lookup(*_args, **_kwargs):
        raise AssertionError("PATH was read while ledger_cli names a CLI")

    monkeypatch.setattr(module.shutil, "which", refuse_lookup)
    assert module.decide().cli == pinned
    module.call(pinned, ["note", "x"])
    os.unlink(pinned)
    with pytest.raises(module.LedgerCallError, match=r"^ledger_cli_moved: "):
        module.call(pinned, ["note", "x"])


def test_without_setup_the_path_rule_still_runs_the_first_ledger_on_path(machine, tmp_path):
    # The owner's call: an invoked skill with no setting keeps the PATH rule. This pins the cost of
    # not running setup, and is what the moved and pinned tests above and below are measured against.
    marker = tmp_path / "planted.marker"
    evil = _plant(tmp_path / "evil", marker)
    out = _out(machine.run(PATH=f"{evil}{os.pathsep}{machine.path}"))
    assert (out["ledger_cli"], out["rule"]) == (str(evil / "ledger"), ON_PATH)
    assert marker.exists()


def test_a_moved_cli_is_refused_by_name_and_setup_replaces_the_dead_path(machine, tmp_path, monkeypatch):
    marker = tmp_path / "planted.marker"
    evil = _plant(tmp_path / "evil", marker)
    pinned = _pin(machine)
    os.unlink(pinned)
    module = _load()
    machine.use(monkeypatch, PATH=f"{evil}{os.pathsep}{machine.path}")
    # The decision still names the pinned path: nothing falls back to PATH.
    decision = module.decide()
    assert (decision.cli, decision.rule) == (pinned, EXPLICIT)
    with pytest.raises(module.LedgerCallError) as excinfo:
        module.call(decision.cli, ["note", "x"])
    line = str(excinfo.value)
    assert line.startswith("ledger_cli_moved: ") and "\n" not in line
    assert pinned in line
    assert f"`{module.setup_command()}`" in line
    assert not marker.exists()
    # A new compatible CLI elsewhere on PATH; rerunning setup replaces the dead path.
    (tmp_path / "second").mkdir()
    other = Machine(tmp_path / "second")
    proc = machine.run("setup", PATH=other.path)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(_override_file(machine.home_dir).read_text(encoding="utf-8"))["ledger_cli"] == str(
        other.ledger
    )
    assert not marker.exists()


def test_the_moved_line_names_the_exact_setup_command(machine, tmp_path):
    pinned = _pin(machine)
    os.unlink(pinned)
    module = _load()
    with pytest.raises(module.LedgerCallError) as excinfo:
        module.call(pinned, ["note"])
    assert module.setup_command() in str(excinfo.value)
    # The command in the line is runnable as printed: it is `python3 <this module> setup`.
    import shlex

    words = shlex.split(module.setup_command())
    assert words == ["python3", str(LIB_SCRIPT), "setup"]
    (tmp_path / "second").mkdir()
    other = Machine(tmp_path / "second")
    proc = subprocess.run(
        [sys.executable, *words[1:]], env=_env(machine.home_dir, PATH=other.path),
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(_override_file(machine.home_dir).read_text(encoding="utf-8"))["ledger_cli"] == str(other.ledger)


@pytest.mark.parametrize("kind", ["missing", "not-executable", "directory", "dead-tilde"])
def test_every_way_an_absolute_cli_stops_being_an_executable_file_is_moved(tmp_path, monkeypatch, kind):
    module = _load()
    monkeypatch.setenv("HOME", str(tmp_path))
    if kind == "missing":
        cli = str(tmp_path / "gone" / "ledger")
    elif kind == "not-executable":
        path = tmp_path / "ledger"
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o644)
        cli = str(path)
    elif kind == "directory":
        (tmp_path / "ledger").mkdir()
        cli = str(tmp_path / "ledger")
    else:
        cli = "~/.claude/gone/ledger"
    with pytest.raises(module.LedgerCallError, match=r"^ledger_cli_moved: "):
        module.call(cli, ["note"])
    with pytest.raises(module.LedgerCallError, match=r"^ledger_cli_moved: "):
        module.require_runnable(cli)


def test_a_bare_name_is_not_a_moved_cli(tmp_path):
    module = _load()
    module.require_runnable("ledger")
    module.require_runnable("./ledger")


@pytest.mark.parametrize(
    "value",
    ["LIVE", "~/.claude/bin/ledger", "none", "NONE", " None ", "ledger", "./ledger", "5", "true", "[1]", "{\"a\": 1}", "0"],
)
def test_setup_leaves_an_existing_setting_alone(machine, tmp_path, value):
    live = machine.home_dir / ".claude" / "bin" / "ledger"
    live.parent.mkdir(parents=True)
    shutil.copy(machine.ledger, live)
    live.chmod(0o755)
    if value == "LIVE":
        raw = json.dumps({"keep": [1], "ledger_cli": str(live)})
    elif value in ("5", "true", "[1]", '{"a": 1}', "0"):
        raw = f'{{"ledger_cli": {value}, "keep": 1}}'
    else:
        raw = json.dumps({"ledger_cli": value, "keep": 1})
    path = _put_override(machine.home_dir, raw)
    before = path.read_bytes()
    proc = machine.run("setup")
    line = _refusal(proc, "ledger_cli_setup_exists")
    shown = {"LIVE": str(live), "true": "true", "[1]": "[1]", '{"a": 1}': '{"a": 1}'}.get(value, value.strip())
    assert f"already set to {shown};" in line, line
    assert path.read_bytes() == before, "the file is byte-identical"
    assert machine.calls() == [], "a refusal on an existing setting runs no program"
    _no_temp_files(machine.home_dir)


def test_setup_replaces_a_dead_absolute_path_only(machine, tmp_path):
    dead = tmp_path / "gone" / "ledger"
    _put_override(machine.home_dir, json.dumps({"ledger_cli": str(dead), "keep": 1}))
    assert machine.run("setup").returncode == 0
    assert json.loads(_override_file(machine.home_dir).read_text(encoding="utf-8")) == {
        "ledger_cli": str(machine.ledger),
        "keep": 1,
    }
    _put_override(machine.home_dir, json.dumps({"ledger_cli": "~/.claude/gone/ledger"}))
    # The first run's displaced version sits under a kept name, which never blocks a later run and is
    # never replaced: the second commit keeps the version it displaced under another name.
    first_kept = _previous(machine.home_dir)
    first_bytes = first_kept.read_bytes()
    assert machine.run("setup").returncode == 0
    kept = _kept(machine.home_dir)
    assert len(kept) == 2 and first_kept in kept and first_kept.read_bytes() == first_bytes
    assert [p for p in kept if p != first_kept and json.loads(p.read_text(encoding="utf-8")) == {"ledger_cli": "~/.claude/gone/ledger"}]
    assert _override_file(machine.home_dir).read_text(encoding="utf-8").count(str(machine.ledger)) == 1


def test_a_second_setup_after_success_refuses_and_changes_nothing(machine):
    _pin(machine)
    path = _override_file(machine.home_dir)
    before = path.read_bytes()
    _refusal(machine.run("setup"), "ledger_cli_setup_exists")
    assert path.read_bytes() == before


def test_setup_with_no_ledger_on_path_writes_nothing(machine, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    line = _refusal(machine.run("setup", PATH=str(empty)), "ledger_cli_setup_not_found")
    assert "no ledger CLI on PATH" in line
    _nothing_written(machine.home_dir)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    _refusal(machine.run("setup", PATH=str(empty)), "ledger_cli_setup_not_found")
    assert path.read_bytes() == b'{"keep": 1}'


@pytest.mark.parametrize("mode", ["exit2", "noflag", "nonutf8"])
def test_setup_with_an_incompatible_ledger_writes_nothing_and_names_what_failed(tmp_path, mode):
    machine = Machine(tmp_path, mode)
    line = _refusal(machine.run("setup"), "ledger_cli_setup_incompatible")
    assert str(machine.ledger) in line and "append-many --help exited" in line
    _nothing_written(machine.home_dir)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    _refusal(machine.run("setup"), "ledger_cli_setup_incompatible")
    assert path.read_bytes() == b'{"keep": 1}'
    assert machine.calls() == [["append-many", "--help"]] * 2


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[1, 2]", b'"text"', b"", b"\xff\xfe\x00", b"null", b'{"ledger_cli": '],
    ids=["invalid", "array", "string", "empty", "non-utf8", "null", "truncated"],
)
def test_setup_with_an_unreadable_override_writes_nothing_and_runs_no_program(machine, content):
    path = _put_override(machine.home_dir, content)
    _refusal(machine.run("setup"), "ledger_cli_setup_config_unreadable")
    assert path.read_bytes() == content
    assert machine.calls() == []
    _no_temp_files(machine.home_dir)


def test_setup_with_a_directory_where_the_override_belongs_is_unreadable(machine):
    path = _override_file(machine.home_dir)
    path.mkdir(parents=True)
    _refusal(machine.run("setup"), "ledger_cli_setup_config_unreadable")
    assert path.is_dir() and list(path.iterdir()) == []
    assert machine.calls() == []


def test_setup_that_cannot_create_the_directory_refuses_by_name_and_writes_nothing(machine):
    claude = machine.home_dir / ".claude"
    claude.mkdir()
    claude.chmod(0o500)
    try:
        line = _refusal(machine.run("setup"), "ledger_cli_setup_write_failed")
    finally:
        claude.chmod(0o700)
    assert "nothing was written" in line
    assert list(claude.iterdir()) == []


def test_a_writer_that_changes_the_override_while_setup_runs_is_never_lost(machine, monkeypatch, capsys):
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    real_probe = module.probe

    def probe_while_another_writer_edits(cli, timeout=module.PROBE_TIMEOUT_SECONDS):
        path.write_text('{"keep": 1, "written_by": "someone else"}', encoding="utf-8")
        return real_probe(cli, timeout)

    monkeypatch.setattr(module, "probe", probe_while_another_writer_edits)
    assert module.setup() == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("ledger_cli_setup_config_changed: ")
    assert len(captured.err.splitlines()) == 1
    assert path.read_text(encoding="utf-8") == '{"keep": 1, "written_by": "someone else"}'
    _no_temp_files(machine.home_dir)
    # A rerun on the new content writes, keeping the other writer's key.
    monkeypatch.setattr(module, "probe", real_probe)
    assert module.setup() == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "keep": 1,
        "written_by": "someone else",
        "ledger_cli": str(machine.ledger),
    }


def test_a_file_created_while_setup_runs_is_never_replaced(machine, monkeypatch, capsys):
    module = _load()
    machine.use(monkeypatch)
    path = _override_file(machine.home_dir)
    real_probe = module.probe

    def probe_while_another_writer_creates(cli, timeout=module.PROBE_TIMEOUT_SECONDS):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"made": "meanwhile"}', encoding="utf-8")
        return real_probe(cli, timeout)

    monkeypatch.setattr(module, "probe", probe_while_another_writer_creates)
    assert module.setup() == 1
    assert capsys.readouterr().err.startswith("ledger_cli_setup_config_changed: ")
    assert path.read_text(encoding="utf-8") == '{"made": "meanwhile"}'


def test_two_setup_runs_that_race_end_with_one_pin_and_the_other_refusing_exists(
    machine, tmp_path, monkeypatch, capsys
):
    module = _load()
    machine.use(monkeypatch)
    other_bin = tmp_path / "other-bin"
    other_bin.mkdir()
    other = other_bin / "ledger"
    shutil.copy2(machine.ledger, other)
    # Run "a" finds one compatible ledger and run "b" another, so a silent overwrite shows in the file.
    found = {"setup-a": str(machine.ledger), "setup-b": str(other)}
    monkeypatch.setattr(module.shutil, "which", lambda name, *args, **kwargs: found[threading.current_thread().name])
    paused = threading.Event()
    release = threading.Event()
    real_link = os.link

    def link_after_a_pause_in_run_a(source, destination, *args, **kwargs):
        # Run "a" has read the absent file and written its temp file; hold it just before its commit.
        if threading.current_thread().name == "setup-a":
            paused.set()
            release.wait(30)
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(module.os, "link", link_after_a_pause_in_run_a)
    results = {}

    def run(name):
        results[name] = module.setup()

    first = threading.Thread(target=run, args=("setup-a",), name="setup-a")
    second = threading.Thread(target=run, args=("setup-b",), name="setup-b")
    first.start()
    assert paused.wait(30), "run a never reached its commit"
    second.start()
    second.join(1)
    still_waiting = second.is_alive()
    release.set()
    first.join(30)
    second.join(30)
    assert not first.is_alive() and not second.is_alive()
    captured = capsys.readouterr()
    assert still_waiting, f"run b did not wait for run a's lock; results {results}, stderr {captured.err!r}"
    assert results == {"setup-a": 0, "setup-b": 1}
    pinned = json.loads(_override_file(machine.home_dir).read_text(encoding="utf-8"))
    assert pinned == {"ledger_cli": str(machine.ledger)}
    assert captured.out == f"ledger_cli set to {machine.ledger} in {_override_file(machine.home_dir)}\n"
    assert len(captured.err.splitlines()) == 1
    assert captured.err.startswith("ledger_cli_setup_exists: ledger_cli in ")
    assert f"is already set to {machine.ledger}; setup does not change it" in captured.err
    _no_temp_files(machine.home_dir)


def test_setup_that_cannot_open_the_lock_refuses_by_name_and_writes_nothing(machine):
    folder = _override_file(machine.home_dir).parent
    folder.mkdir(parents=True)
    (folder / LOCK_NAME).mkdir()
    line = _refusal(machine.run("setup"), "ledger_cli_setup_lock_failed")
    assert LOCK_NAME in line and "nothing was written" in line
    assert not _override_file(machine.home_dir).exists()
    assert machine.calls() == []


def test_setup_that_cannot_take_the_lock_refuses_by_name_and_runs_no_program(machine, monkeypatch, capsys):
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')

    def refuse_to_lock(descriptor, operation):
        raise OSError(35, "Resource temporarily unavailable")

    monkeypatch.setattr(module.fcntl, "flock", refuse_to_lock)
    assert module.setup() == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("ledger_cli_setup_lock_failed: ")
    assert len(captured.err.splitlines()) == 1 and "nothing was written" in captured.err
    assert path.read_text(encoding="utf-8") == '{"keep": 1}'
    assert machine.calls() == []
    _no_temp_files(machine.home_dir)


def test_the_lock_file_stays_after_setup_and_a_later_run_takes_it_again(machine):
    path = _override_file(machine.home_dir)
    lock = path.parent / LOCK_NAME
    _pin(machine)
    assert lock.is_file() and lock.read_bytes() == b""
    written = path.read_bytes()
    _refusal(machine.run("setup"), "ledger_cli_setup_exists")
    assert lock.is_file() and path.read_bytes() == written


def test_the_lock_descriptor_is_not_inherited_by_the_program_setup_runs(machine, monkeypatch):
    module = _load()
    machine.use(monkeypatch)
    _override_file(machine.home_dir).parent.mkdir(parents=True)
    descriptor = module._take_lock(_override_file(machine.home_dir))
    try:
        assert not os.get_inheritable(descriptor)
    finally:
        os.close(descriptor)


def test_every_setup_refusal_token_is_a_distinct_fixed_name():
    module = _load()
    assert (
        module.SETUP_NOT_FOUND,
        module.SETUP_INCOMPATIBLE,
        module.SETUP_CONFIG_UNREADABLE,
        module.SETUP_EXISTS,
        module.SETUP_CONFIG_CHANGED,
        module.SETUP_NO_HARDLINKS,
        module.SETUP_WRITE_FAILED,
        module.SETUP_LOCK_FAILED,
    ) == SETUP_TOKENS
    assert module.MOVED_TOKEN == "ledger_cli_moved:"


def test_setup_takes_no_config_flag_and_the_no_argument_form_is_unchanged(machine, tmp_path):
    proc = machine.run("--config", str(tmp_path / "x.json"), "setup")
    assert proc.returncode == 2 and "setup takes no --config" in proc.stderr
    assert not (machine.home_dir / ".claude").exists()
    assert machine.run("nonsense").returncode == 2
    out = _out(machine.run("--config", str(tmp_path / "missing.json")))
    assert (out["ledger_cli"], out["rule"]) == (None, NOT_CONFIGURED)


# --- Setup's commit: capture the displaced file, verify it, create no-clobber ----------


def _same_path(one, two):
    return os.path.realpath(os.fspath(one)) == os.path.realpath(os.fspath(two))


def _backup(home):
    """The in-flight name: the only one crash recovery reads."""
    return _override_file(home).parent / BACKUP_NAME


def _kept(home):
    """Every kept file beside the override: where finished commits keep the displaced versions."""
    folder = _override_file(home).parent
    return sorted(p for p in folder.iterdir() if p.name.startswith(PREVIOUS_NAME + "."))


def _previous(home):
    """The one kept file after a single displacing commit."""
    files = _kept(home)
    assert len(files) == 1, files
    assert KEPT_GRAMMAR.match(files[0].name), files[0].name
    return files[0]


def test_an_editor_save_in_the_commit_window_is_the_file_left_at_the_path(machine, monkeypatch, capsys):
    """A non-locking editor saves (write a sibling, rename it over the path) at the first step of
    setup's commit that touches the path, whichever primitive that is; its save must survive."""
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    editor = b'{"keep": 1, "saved_by": "editor"}'
    real = {"rename": os.rename, "replace": os.replace, "link": os.link}
    fired = []

    def editor_save():
        sibling = path.parent / ".editor-save.tmp"
        sibling.write_bytes(editor)
        real["replace"](sibling, path)

    def wrap(name):
        def call(source, destination, *args, **kwargs):
            if not fired and (_same_path(source, path) or _same_path(destination, path)):
                fired.append(name)
                editor_save()
            return real[name](source, destination, *args, **kwargs)

        return call

    for name in real:
        monkeypatch.setattr(module.os, name, wrap(name))
    result = module.setup()
    for name, function in real.items():
        monkeypatch.setattr(module.os, name, function)
    captured = capsys.readouterr()
    assert result == 1, f"setup exited {result}; the file now holds {path.read_bytes()!r}"
    assert path.read_bytes() == editor, "the editor's save is the file at the path"
    assert captured.out == "" and len(captured.err.splitlines()) == 1
    assert captured.err.startswith("ledger_cli_setup_config_changed: ")
    assert fired == ["rename"], "the first step that touches the path is the move aside"
    _no_temp_files(machine.home_dir)
    # A rerun reads the editor's version and keeps its key.
    assert module.setup() == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "keep": 1,
        "saved_by": "editor",
        "ledger_cli": str(machine.ledger),
    }
    _no_temp_files(machine.home_dir)


def test_an_editor_that_creates_the_absent_file_just_before_the_link_keeps_its_file(
    machine, monkeypatch, capsys
):
    module = _load()
    machine.use(monkeypatch)
    path = _override_file(machine.home_dir)
    editor = b'{"made": "by the editor"}'
    real_link = os.link

    def create_then_link(source, destination, *args, **kwargs):
        if _same_path(destination, path) and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(editor)
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(module.os, "link", create_then_link)
    result = module.setup()
    monkeypatch.setattr(module.os, "link", real_link)
    captured = capsys.readouterr()
    assert result == 1, f"setup exited {result}; the file now holds {path.read_bytes()!r}"
    assert path.read_bytes() == editor
    assert captured.err.startswith("ledger_cli_setup_config_changed: ") and len(captured.err.splitlines()) == 1
    _no_temp_files(machine.home_dir)


def test_a_file_that_reappears_before_the_commit_link_is_left_and_the_read_version_kept(
    machine, monkeypatch, capsys
):
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    editor = b'{"keep": 1, "saved_by": "editor"}'
    real_link = os.link

    def save_then_link(source, destination, *args, **kwargs):
        if _same_path(destination, path) and not path.exists():
            path.write_bytes(editor)
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(module.os, "link", save_then_link)
    result = module.setup()
    monkeypatch.setattr(module.os, "link", real_link)
    captured = capsys.readouterr()
    assert result == 1, f"setup exited {result}; the file now holds {path.read_bytes()!r}"
    assert path.read_bytes() == editor
    backup = _backup(machine.home_dir)
    assert backup.read_bytes() == b'{"keep": 1}', "the version setup read is kept"
    assert captured.err.startswith("ledger_cli_setup_config_changed: ")
    assert str(backup) in captured.err


def test_crash_recovery_puts_the_backup_back_before_setup_reads(machine, monkeypatch):
    # A run that stopped while the path was absent left the file's version in the backup.
    backup = _backup(machine.home_dir)
    backup.parent.mkdir(parents=True)
    backup.write_text(json.dumps({"keep": 1, "x": [2]}), encoding="utf-8")
    proc = machine.run("setup")
    assert proc.returncode == 0, proc.stderr
    written = json.loads(_override_file(machine.home_dir).read_text(encoding="utf-8"))
    assert written == {"keep": 1, "x": [2], "ledger_cli": str(machine.ledger)}
    _no_temp_files(machine.home_dir)


def test_a_run_that_dies_between_the_move_aside_and_the_link_is_recovered_by_the_next(machine, monkeypatch):
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    real_link = os.link

    def die_before_the_commit(source, destination, *args, **kwargs):
        if _same_path(destination, path):
            raise SystemExit("killed")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(module.os, "link", die_before_the_commit)
    with pytest.raises(SystemExit):
        module.setup()
    monkeypatch.setattr(module.os, "link", real_link)
    assert not path.exists()
    assert _backup(machine.home_dir).read_bytes() == b'{"keep": 1}'
    proc = machine.run("setup")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(path.read_text(encoding="utf-8")) == {"keep": 1, "ledger_cli": str(machine.ledger)}
    _no_temp_files(machine.home_dir)


def test_a_backup_left_beside_a_present_file_is_never_moved_over(machine):
    path = _put_override(machine.home_dir, '{"keep": 1}')
    backup = _backup(machine.home_dir)
    backup.write_bytes(b'{"older": "version"}')
    line = _refusal(machine.run("setup"), "ledger_cli_setup_config_changed")
    assert str(backup) in line
    assert path.read_bytes() == b'{"keep": 1}'
    assert backup.read_bytes() == b'{"older": "version"}'


def test_a_backup_that_is_the_same_file_as_the_present_one_is_dropped(machine):
    # A run that stopped after putting the backup back left two links to one file.
    path = _put_override(machine.home_dir, '{"keep": 1}')
    os.link(path, _backup(machine.home_dir))
    proc = machine.run("setup")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(path.read_text(encoding="utf-8")) == {"keep": 1, "ledger_cli": str(machine.ledger)}
    _no_temp_files(machine.home_dir)


def test_an_in_place_edit_after_the_compare_lands_in_the_kept_file(machine, monkeypatch, capsys):
    """The residual: a writer holding a descriptor opened before the move writes after the compare."""
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    edited = b'{"keep": 1, "edited": "in place"}'
    handle = open(path, "r+b")
    real_link = os.link

    def edit_in_place_then_link(source, destination, *args, **kwargs):
        if _same_path(destination, path) and not path.exists():
            handle.seek(0)
            handle.write(edited)
            handle.truncate()
            handle.flush()
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(module.os, "link", edit_in_place_then_link)
    try:
        result = module.setup()
    finally:
        monkeypatch.setattr(module.os, "link", real_link)
        handle.close()
    captured = capsys.readouterr()
    backup = _previous(machine.home_dir)
    assert result == 1, f"setup exited {result}; stderr {captured.err!r}"
    assert not _backup(machine.home_dir).exists(), "a completed commit leaves no in-flight file"
    assert backup.read_bytes() == edited, "the in-place edit is kept"
    assert captured.err.startswith("ledger_cli_setup_config_changed: ") and str(backup) in captured.err
    assert json.loads(path.read_text(encoding="utf-8")) == {"keep": 1, "ledger_cli": str(machine.ledger)}


def test_a_successful_commit_keeps_the_displaced_bytes_under_a_kept_name(
    machine, capsys, monkeypatch
):
    """Setup never unlinks the displaced version after a commit, and leaves no in-flight file."""
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    result = module.setup()
    captured = capsys.readouterr()
    previous = _previous(machine.home_dir)
    assert result == 0, captured.err
    assert not _backup(machine.home_dir).exists() and not os.path.lexists(_backup(machine.home_dir))
    assert previous.read_bytes() == b'{"keep": 1}', "the displaced version is kept"
    assert not os.path.samefile(previous, path)
    assert captured.out == (
        f"ledger_cli set to {machine.ledger} in {path}; the version it replaced is kept at {previous}; "
        "remove it once no editor has it open\n"
    )
    # A rerun finds the pin and leaves both files as they are.
    written = path.read_bytes()
    rerun = module.setup()
    assert rerun == 1 and path.read_bytes() == written and previous.read_bytes() == b'{"keep": 1}'
    capsys.readouterr()


def test_deleting_the_override_after_a_commit_does_not_bring_the_displaced_keys_back(machine, capsys, monkeypatch):
    """A kept file is never read: setup treats a deleted override as a first run."""
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": "deleted-state"}')
    assert module.setup() == 0
    capsys.readouterr()
    previous = _previous(machine.home_dir)
    assert previous.read_bytes() == b'{"keep": "deleted-state"}'
    path.unlink()
    assert module.setup() == 0
    captured = capsys.readouterr()
    assert json.loads(path.read_text(encoding="utf-8")) == {"ledger_cli": str(machine.ledger)}
    assert captured.out == f"ledger_cli set to {machine.ledger} in {path}\n"
    assert not _backup(machine.home_dir).exists()
    assert previous.read_bytes() == b'{"keep": "deleted-state"}', "the record is left as it was"


def test_a_link_to_a_kept_name_that_fails_leaves_the_in_flight_file_and_exits_0(machine, capsys, monkeypatch):
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    real_link = os.link

    def refuse_the_kept_link(source, destination, *args, **kwargs):
        if os.path.basename(os.fspath(destination)).startswith(PREVIOUS_NAME + "."):
            raise PermissionError(13, "Permission denied")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(module.os, "link", refuse_the_kept_link)
    result = module.setup()
    monkeypatch.setattr(module.os, "link", real_link)
    captured = capsys.readouterr()
    backup = _backup(machine.home_dir)
    assert result == 0, captured.err
    assert backup.read_bytes() == b'{"keep": 1}' and _kept(machine.home_dir) == [], "nothing is unlinked or lost"
    assert str(backup) in captured.out and "left at" in captured.out and f"{PREVIOUS_NAME}.*" in captured.out
    assert json.loads(path.read_text(encoding="utf-8")) == {"keep": 1, "ledger_cli": str(machine.ledger)}
    # With the override present the in-flight file is not restored; a rerun finds the pin.
    assert module.setup() == 1 and backup.read_bytes() == b'{"keep": 1}'
    capsys.readouterr()


def test_an_in_place_edit_after_setups_last_read_is_in_the_named_file_and_setup_exits_0(
    machine, monkeypatch, capsys
):
    """The editor holds a descriptor opened before the move and writes after setup's last read of the
    backup (at the success line): setup still exits 0, and the edit is in the backup the line names."""
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    edited = b'{"keep": 1, "edited": "in place"}'
    handle = open(path, "r+b")
    lines = []

    def edit_then_report(text, *args, **kwargs):
        handle.seek(0)
        handle.write(edited)
        handle.truncate()
        handle.flush()
        lines.append(text)

    monkeypatch.setattr(module, "print", edit_then_report, raising=False)
    try:
        result = module.setup()
    finally:
        handle.close()
    captured = capsys.readouterr()
    backup = _previous(machine.home_dir)
    assert result == 0, f"setup exited {result}; stderr {captured.err!r}"
    assert not _backup(machine.home_dir).exists(), "a completed commit leaves no in-flight file"
    assert backup.read_bytes() == edited, "the in-place edit is in the named completed-version file"
    assert lines == [
        f"ledger_cli set to {machine.ledger} in {path}; the version it replaced is kept at {backup}; "
        "remove it once no editor has it open"
    ]
    assert json.loads(path.read_text(encoding="utf-8")) == {"keep": 1, "ledger_cli": str(machine.ledger)}


def test_the_moved_pin_is_still_replaced_with_its_keys_and_mode_kept(machine, tmp_path):
    dead = tmp_path / "gone" / "ledger"
    path = _put_override(machine.home_dir, json.dumps({"ledger_cli": str(dead), "keep": 1}))
    path.chmod(0o600)
    proc = machine.run("setup")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(path.read_text(encoding="utf-8")) == {"ledger_cli": str(machine.ledger), "keep": 1}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    _no_temp_files(machine.home_dir)


def _edit_during_probe(module, monkeypatch, path, content):
    """A non-locking writer edits the file while setup runs its compatibility check."""
    real_probe = module.probe

    def probe_while_another_writer_edits(cli, timeout=module.PROBE_TIMEOUT_SECONDS):
        path.write_bytes(content)
        return real_probe(cli, timeout)

    monkeypatch.setattr(module, "probe", probe_while_another_writer_edits)
    return real_probe


def test_a_file_that_reappears_before_the_restore_link_is_left_and_the_moved_version_kept(
    machine, monkeypatch, capsys
):
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    first = b'{"keep": 1, "first": "editor"}'
    second = b'{"keep": 1, "second": "editor"}'
    _edit_during_probe(module, monkeypatch, path, first)
    backup = _backup(machine.home_dir)
    real_link = os.link

    def save_then_restore(source, destination, *args, **kwargs):
        if _same_path(source, backup) and not path.exists():
            path.write_bytes(second)
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(module.os, "link", save_then_restore)
    result = module.setup()
    monkeypatch.setattr(module.os, "link", real_link)
    captured = capsys.readouterr()
    assert result == 1, f"setup exited {result}; stderr {captured.err!r}"
    assert path.read_bytes() == second, "the file at the path is the one the writer left"
    assert backup.read_bytes() == first, "the version setup moved aside is kept"
    assert captured.err.startswith("ledger_cli_setup_config_changed: ") and str(backup) in captured.err


def test_a_restore_that_fails_keeps_the_backup_and_the_next_run_puts_it_back(machine, monkeypatch, capsys):
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    edited = b'{"keep": 1, "edited": "meanwhile"}'
    real_probe = _edit_during_probe(module, monkeypatch, path, edited)
    backup = _backup(machine.home_dir)
    real_link = os.link

    def refuse_the_restore(source, destination, *args, **kwargs):
        if _same_path(source, backup):
            raise PermissionError(13, "Permission denied")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(module.os, "link", refuse_the_restore)
    result = module.setup()
    monkeypatch.setattr(module.os, "link", real_link)
    monkeypatch.setattr(module, "probe", real_probe)
    captured = capsys.readouterr()
    assert result == 1, f"setup exited {result}; stderr {captured.err!r}"
    assert captured.err.startswith("ledger_cli_setup_write_failed: ") and str(backup) in captured.err
    assert not path.exists() and backup.read_bytes() == edited
    assert module.setup() == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "keep": 1,
        "edited": "meanwhile",
        "ledger_cli": str(machine.ledger),
    }
    _no_temp_files(machine.home_dir)


def test_a_save_between_the_restore_and_the_backup_removal_keeps_the_backup(machine, monkeypatch, capsys):
    """After a restore, setup drops the backup only while it is still the file at the path."""
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    first = b'{"keep": 1, "first": "editor"}'
    second = b'{"keep": 1, "second": "editor"}'
    _edit_during_probe(module, monkeypatch, path, first)
    backup = _backup(machine.home_dir)
    real_link = os.link
    real_replace = os.replace

    def restore_then_editor_saves(source, destination, *args, **kwargs):
        result = real_link(source, destination, *args, **kwargs)
        if _same_path(source, backup):
            sibling = path.parent / ".editor-save.tmp"
            sibling.write_bytes(second)
            real_replace(sibling, path)
        return result

    monkeypatch.setattr(module.os, "link", restore_then_editor_saves)
    result = module.setup()
    monkeypatch.setattr(module.os, "link", real_link)
    captured = capsys.readouterr()
    assert result == 1, f"setup exited {result}; stderr {captured.err!r}"
    assert path.read_bytes() == second
    assert backup.exists(), "the first writer's version, no longer at the path, is kept"
    assert backup.read_bytes() == first
    assert captured.err.startswith("ledger_cli_setup_config_changed: ") and str(backup) in captured.err


def test_setup_never_renames_or_replaces_onto_the_override_path(machine, monkeypatch):
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    seen = []
    real = {"rename": os.rename, "replace": os.replace}

    def watch(name):
        def call(source, destination, *args, **kwargs):
            seen.append((name, os.path.basename(os.fspath(source)), os.path.basename(os.fspath(destination))))
            return real[name](source, destination, *args, **kwargs)

        return call

    for name in real:
        monkeypatch.setattr(module.os, name, watch(name))
    result = module.setup()
    for name, function in real.items():
        monkeypatch.setattr(module.os, name, function)
    assert result == 0
    assert seen == [("rename", "topic.json", BACKUP_NAME)], "the kept name is linked to, never renamed onto"


def _no_hard_links(code):
    """A stand-in for `os.link` on a mount that takes no hard links."""

    def refuse(source, destination, *args, **kwargs):
        raise OSError(code, os.strerror(code))

    return refuse


@pytest.mark.parametrize(
    "code",
    [errno.EPERM, errno.ENOTSUP, errno.EXDEV, errno.EMLINK, errno.EIO],
    ids=["EPERM", "ENOTSUP", "EXDEV", "EMLINK", "EIO"],
)
def test_setup_on_a_folder_without_hard_links_leaves_the_override_in_place_and_refuses_by_name(
    machine, monkeypatch, capsys, code
):
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1, "z": [1, 2]}')
    original = path.read_bytes()
    real_link = os.link
    monkeypatch.setattr(module.os, "link", _no_hard_links(code))
    result = module.setup()
    monkeypatch.setattr(module.os, "link", real_link)
    captured = capsys.readouterr()
    assert path.exists(), "the override was moved aside and never put back"
    assert path.read_bytes() == original
    assert result == 1, f"setup exited {result}; stderr {captured.err!r}"
    assert captured.out == "" and len(captured.err.splitlines()) == 1
    assert captured.err.startswith("ledger_cli_setup_no_hardlinks: "), captured.err
    assert "nothing was moved or written" in captured.err
    # No backup, probe or temp file is left; only the override and the lock stand in the folder.
    assert sorted(p.name for p in path.parent.iterdir()) == sorted(["topic.json", LOCK_NAME])
    # The same folder with working links then pins normally.
    assert module.setup() == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "keep": 1,
        "z": [1, 2],
        "ledger_cli": str(machine.ledger),
    }


def test_setup_with_no_override_on_a_folder_without_hard_links_refuses_by_name_and_creates_nothing(
    machine, monkeypatch, capsys
):
    module = _load()
    machine.use(monkeypatch)
    real_link = os.link
    monkeypatch.setattr(module.os, "link", _no_hard_links(errno.EPERM))
    result = module.setup()
    monkeypatch.setattr(module.os, "link", real_link)
    captured = capsys.readouterr()
    assert not _override_file(machine.home_dir).exists(), "the override was created"
    assert result == 1, f"setup exited {result}; stderr {captured.err!r}"
    assert captured.out == "" and len(captured.err.splitlines()) == 1
    assert captured.err.startswith("ledger_cli_setup_no_hardlinks: "), captured.err
    _nothing_written(machine.home_dir)


def test_a_link_probe_that_cannot_be_removed_refuses_write_failed_naming_it_and_moves_nothing(
    machine, monkeypatch, capsys
):
    module = _load()
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    original = path.read_bytes()
    real_unlink = os.unlink

    def refuse_to_remove_the_probe(name, *args, **kwargs):
        if os.fspath(name).endswith(module.PROBE_SUFFIX):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(module.os, "unlink", refuse_to_remove_the_probe)
    result = module.setup()
    monkeypatch.setattr(module.os, "unlink", real_unlink)
    captured = capsys.readouterr()
    assert result == 1, f"setup exited {result}; stderr {captured.err!r}"
    assert captured.out == "" and len(captured.err.splitlines()) == 1
    assert captured.err.startswith("ledger_cli_setup_write_failed: "), captured.err
    probes = [p for p in path.parent.iterdir() if p.name.endswith(module.PROBE_SUFFIX)]
    assert len(probes) == 1, "the probe file that could not be removed is left, and named"
    assert str(probes[0]) in captured.err
    assert path.read_bytes() == original
    assert not _backup(machine.home_dir).exists(), "nothing was moved aside"
    # Only the probe is left besides the override and the lock: the temp file is removed.
    assert sorted(p.name for p in path.parent.iterdir()) == sorted(["topic.json", LOCK_NAME, probes[0].name])


def test_a_restore_that_cannot_link_does_not_promise_that_a_rerun_puts_it_back(tmp_path, monkeypatch):
    module = _load()
    target = tmp_path / "topic.json"
    backup = tmp_path / "topic.json.setup-backup"
    backup.write_text('{"keep": 1}', encoding="utf-8")
    monkeypatch.setattr(module.os, "link", _no_hard_links(errno.EPERM))
    with pytest.raises(module._Refusal) as raised:
        module._restore(target, backup)
    assert raised.value.token == module.SETUP_WRITE_FAILED
    detail = raised.value.detail
    assert str(backup) in detail, "the line names where the version is kept"
    assert "rerunning setup puts it back" not in detail
    assert "move it back by hand" in detail
    assert backup.read_text(encoding="utf-8") == '{"keep": 1}' and not target.exists()


def _spare_cli(tmp_path, machine, name):
    """A second live `ledger` in a directory of its own, for the run after the first pin goes dead."""
    directory = tmp_path / name
    directory.mkdir()
    shutil.copy(machine.ledger, directory / "ledger")
    return directory


def test_a_second_setup_never_replaces_the_file_an_editor_still_holds(machine, monkeypatch, capsys, tmp_path):
    """An editor holds a descriptor on the override across two displacing setups and saves late: its
    bytes stay in a kept file, and the second displaced version is kept byte for byte beside it."""
    module = _load()
    spare = _spare_cli(tmp_path, machine, "spare")
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    handle = open(path, "r+b")
    held = os.fstat(handle.fileno()).st_ino
    late = b'{"keep": 1, "late": "editor save"}'
    try:
        assert module.setup() == 0
        second_displaced = path.read_bytes()
        machine.ledger.unlink()  # the pinned CLI is dead, so the next setup replaces the pin
        monkeypatch.setenv("PATH", f"{spare}{os.pathsep}{machine.path}")
        assert module.setup() == 0
        handle.seek(0)
        handle.write(late)
        handle.truncate()
        handle.flush()
    finally:
        handle.close()
    captured = capsys.readouterr()
    assert captured.err == ""
    kept = _kept(machine.home_dir)
    assert len(kept) == 2, [p.name for p in kept]
    assert all(KEPT_GRAMMAR.match(p.name) for p in kept), [p.name for p in kept]
    holding = [p for p in kept if p.stat().st_ino == held]
    others = [p for p in kept if p.stat().st_ino != held]
    assert len(holding) == 1 and len(others) == 1, "the editor's file and the second displaced file are distinct"
    assert holding[0].read_bytes() == late, "the late save is in the file the editor held"
    assert others[0].read_bytes() == second_displaced, "the second displaced version is kept byte for byte"
    assert not _backup(machine.home_dir).exists()
    assert json.loads(path.read_text(encoding="utf-8"))["ledger_cli"] == str(spare / "ledger")


def test_every_file_that_has_been_at_the_override_path_keeps_a_name_after_three_setups(
    machine, monkeypatch, capsys, tmp_path
):
    """Setup never removes the last name of a file that was at the override path."""
    module = _load()
    spares = [_spare_cli(tmp_path, machine, "spare1"), _spare_cli(tmp_path, machine, "spare2")]
    machine.use(monkeypatch)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    recorded = []
    held = []  # open descriptors keep a lost file's inode from being reused by a later file

    def record():
        recorded.append(path.stat().st_ino)
        held.append(open(path, "rb"))

    try:
        record()
        assert module.setup() == 0
        for dead, spare in ((machine.ledger, spares[0]), (spares[0] / "ledger", spares[1])):
            dead.unlink()  # the pinned CLI is dead, so the next setup replaces the pin
            monkeypatch.setenv("PATH", f"{spare}{os.pathsep}{machine.path}")
            record()
            assert module.setup() == 0
        names = [p for p in path.parent.iterdir()]
        reachable = {os.stat(p).st_ino for p in names}
        missing = [ino for ino in recorded if ino not in reachable]
        assert missing == [], f"inodes with no name left in {sorted(p.name for p in names)}: {missing}"
        assert len(_kept(machine.home_dir)) == 3
    finally:
        for handle in held:
            handle.close()
    capsys.readouterr()


def test_a_taken_kept_name_is_left_byte_identical_and_another_name_is_used(machine, monkeypatch, capsys):
    """The first candidate names exist already: setup links to the next free name, never over them."""
    module = _load()
    machine.use(monkeypatch)
    monkeypatch.setattr(module, "_utc_stamp", lambda: "20000101T000000Z")
    monkeypatch.setattr(module.os, "getpid", lambda: 4242)
    path = _put_override(machine.home_dir, '{"keep": 1}')
    folder = path.parent
    first = folder / f"{PREVIOUS_NAME}.20000101T000000Z-4242"
    second = folder / f"{PREVIOUS_NAME}.20000101T000000Z-4242-1"
    first.write_bytes(b"precious one")
    second.write_bytes(b"precious two")
    inodes = (first.stat().st_ino, second.stat().st_ino)
    result = module.setup()
    captured = capsys.readouterr()
    third = folder / f"{PREVIOUS_NAME}.20000101T000000Z-4242-2"
    assert result == 0, captured.err
    assert first.read_bytes() == b"precious one" and second.read_bytes() == b"precious two"
    assert (first.stat().st_ino, second.stat().st_ino) == inodes
    assert third.read_bytes() == b'{"keep": 1}', "the displaced version is kept under the next free name"
    assert not _backup(machine.home_dir).exists()
    assert str(third) in captured.out

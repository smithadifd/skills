"""The shared ledger CLI resolver: one key, one grammar, never raises."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LIB_SCRIPT = REPO_ROOT / "skills" / "_lib" / "ledger_cli.py"


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


def _run(script, home, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _override(home, **keys):
    path = home / ".claude" / "skill-config"
    path.mkdir(parents=True, exist_ok=True)
    (path / "topic.json").write_text(json.dumps(keys), encoding="utf-8")


def test_shipped_config_leaves_it_off(tmp_path):
    proc = _run(LIB_SCRIPT, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"ledger_cli": None}


def test_the_override_file_sets_it(tmp_path):
    _override(tmp_path, ledger_cli="myledger")
    proc = _run(LIB_SCRIPT, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"ledger_cli": "myledger"}


def test_a_tilde_path_is_expanded(tmp_path):
    _override(tmp_path, ledger_cli="~/.claude/bin/ledger")
    proc = _run(LIB_SCRIPT, tmp_path)
    assert json.loads(proc.stdout) == {"ledger_cli": str(tmp_path / ".claude" / "bin" / "ledger")}


def test_the_shipped_value_applies_when_the_override_is_silent(tmp_path):
    script = _layout(tmp_path / "plugin", {"ledger_cli": "shipped-ledger"})
    home = tmp_path / "home"
    home.mkdir()
    proc = _run(script, home)
    assert json.loads(proc.stdout) == {"ledger_cli": "shipped-ledger"}


def test_the_override_wins_over_the_shipped_value_including_null(tmp_path):
    script = _layout(tmp_path / "plugin", {"ledger_cli": "shipped-ledger"})
    home = tmp_path / "home"
    home.mkdir()
    _override(home, ledger_cli="mine")
    assert json.loads(_run(script, home).stdout) == {"ledger_cli": "mine"}
    _override(home, ledger_cli=None)
    assert json.loads(_run(script, home).stdout) == {"ledger_cli": None}


@pytest.mark.parametrize("value", [None, "", "   ", ["ledger"], 7, True, {"a": 1}])
def test_anything_but_a_non_empty_string_means_off(tmp_path, value):
    _override(tmp_path, ledger_cli=value)
    proc = _run(LIB_SCRIPT, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"ledger_cli": None}


def test_strict_mode_refuses_a_non_string_the_way_topic_does(tmp_path):
    module = _load()
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"ledger_cli": ["x"]}), encoding="utf-8")
    with pytest.raises(module.ConfigError):
        module.resolve(path, strict=True)
    assert module.resolve(path) is None


def test_a_missing_or_broken_config_is_off_and_exits_zero(tmp_path):
    (tmp_path / ".claude" / "skill-config").mkdir(parents=True)
    (tmp_path / ".claude" / "skill-config" / "topic.json").write_text("{not json", encoding="utf-8")
    proc = _run(LIB_SCRIPT, tmp_path)
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"ledger_cli": None}
    proc = _run(LIB_SCRIPT, tmp_path, "--config", str(tmp_path / "absent.json"))
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"ledger_cli": None}


def test_a_layout_without_the_topic_skill_is_off_and_silent(tmp_path):
    skills = tmp_path / "skills"
    shutil.copytree(
        REPO_ROOT / "skills" / "_lib",
        skills / "_lib",
        ignore=shutil.ignore_patterns("tests", "__pycache__"),
    )
    home = tmp_path / "home"
    home.mkdir()
    proc = _run(skills / "_lib" / "ledger_cli.py", home)
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert json.loads(proc.stdout) == {"ledger_cli": None}


def test_the_environment_is_not_a_second_switch(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env["HOME"] = str(tmp_path)
    env["LEDGER_CLI"] = "from-env"
    env["ledger_cli"] = "from-env"
    proc = subprocess.run(
        [sys.executable, str(LIB_SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    assert json.loads(proc.stdout) == {"ledger_cli": None}


def test_link_argv_is_the_documented_call():
    module = _load()
    assert module.link_argv("topic-x", "/a/b.md") == [
        "link", "topic-x", "--rel", "url", "--target", "/a/b.md",
    ]

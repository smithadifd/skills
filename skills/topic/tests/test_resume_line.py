"""topic's close prints the ledger's resume output as its last line, when a CLI is set."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RESUME = REPO_ROOT / "skills" / "topic" / "resume_line.py"
SKILL_DOC = REPO_ROOT / "skills" / "topic" / "SKILL.md"

FAKE = """#!__PYTHON__
import json, os, sys
with open(os.environ["FAKE_LEDGER_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
mode = os.environ.get("FAKE_LEDGER_MODE", "ok")
if mode == "fail":
    sys.stdout.write("usage: ledger <verb>\\n")
    sys.stderr.write("unknown verb: resume\\n")
    raise SystemExit(2)
sys.stdout.write("topic-demo: 1 open, next step LC15\\n  (second line)\\n")
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

    def configure(self, value="fakeledger"):
        config = self.fake_home / ".claude" / "skill-config"
        config.mkdir(parents=True, exist_ok=True)
        (config / "topic.json").write_text(json.dumps({"ledger_cli": value}), encoding="utf-8")

    def run(self, mode="ok"):
        env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
        env["HOME"] = str(self.fake_home)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_LEDGER_LOG"] = str(self.log)
        env["FAKE_LEDGER_MODE"] = mode
        return subprocess.run(
            [sys.executable, str(RESUME), "topic-demo"],
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


def test_it_prints_the_literal_resume_output(rig):
    rig.configure()
    proc = rig.run()
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "topic-demo: 1 open, next step LC15\n  (second line)\n"
    assert rig.calls() == [["resume", "--item", "topic-demo"]]


def test_with_the_cli_unset_the_close_is_unchanged(rig):
    for value in (None, "", "  "):
        rig.configure(value)
        proc = rig.run()
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""
    assert rig.calls() == []


def test_a_cli_without_resume_is_one_stderr_line_and_no_stdout(rig):
    rig.configure()
    proc = rig.run(mode="fail")
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert len(proc.stderr.splitlines()) == 1
    assert "resume" in proc.stderr


@pytest.mark.parametrize("value", [["fakeledger"], 7, True, {"a": 1}])
def test_a_non_string_key_is_one_stderr_line_exit_one_and_no_stdout(rig, value):
    rig.configure(value)
    proc = rig.run()
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert len(proc.stderr.splitlines()) == 1
    assert "ledger_cli" in proc.stderr
    assert rig.calls() == []


def test_a_missing_binary_is_one_stderr_line(rig):
    rig.configure(str(rig.bin / "no-such-ledger"))
    proc = rig.run()
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert len(proc.stderr.splitlines()) == 1


def test_skill_text_names_the_last_line_step():
    text = SKILL_DOC.read_text(encoding="utf-8")
    assert "resume_line.py" in text
    assert "resume --item" in text
    assert "last line" in text

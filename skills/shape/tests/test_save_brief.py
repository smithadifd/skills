"""shape saves its brief to sharpen's roadmaps directory and links it when the ledger is on."""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SAVE = REPO_ROOT / "skills" / "shape" / "save_brief.py"
SKILL_DOC = REPO_ROOT / "skills" / "shape" / "SKILL.md"

FAKE = """#!__PYTHON__
import json, os, sys
with open(os.environ["FAKE_LEDGER_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == os.environ.get("FAKE_FAIL_VERB"):
    sys.stderr.write("refused by the fake\\n")
    raise SystemExit(3)
print("ok")
"""

BRIEF = "# Shape brief\n\nVerdict: Proceed with changes.\n"


class Rig:
    def __init__(self, tmp_path):
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.roadmaps = tmp_path / "roadmaps"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "calls.log"
        fake = self.bin / "fakeledger"
        fake.write_text(FAKE.replace("__PYTHON__", sys.executable), encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        config = self.home / ".claude" / "skill-config"
        config.mkdir(parents=True)
        (config / "sharpen.json").write_text(
            json.dumps({"notes_root": {"roadmaps": str(self.roadmaps)}}), encoding="utf-8"
        )
        self.script = SAVE

    def configure(self, value="fakeledger"):
        config = self.home / ".claude" / "skill-config"
        (config / "topic.json").write_text(json.dumps({"ledger_cli": value}), encoding="utf-8")

    def run(self, *args, stdin=BRIEF, extra_env=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
        env["HOME"] = str(self.home)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_LEDGER_LOG"] = str(self.log)
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, str(self.script), *args],
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

    def files(self):
        return sorted(p.name for p in self.roadmaps.glob("*")) if self.roadmaps.exists() else []


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_a_shape_run_leaves_a_dated_file_under_the_roadmaps_directory(rig):
    proc = rig.run("--slug", "new-cache", "--date", "2026-11-04")
    assert proc.returncode == 0, proc.stderr
    target = rig.roadmaps / "shape-new-cache-2026-11-04.md"
    assert proc.stdout.strip() == str(target.resolve())
    assert target.read_text(encoding="utf-8") == BRIEF
    assert rig.files() == ["shape-new-cache-2026-11-04.md"]


def test_the_date_defaults_to_today(rig):
    import datetime

    proc = rig.run("--slug", "x")
    assert proc.returncode == 0, proc.stderr
    assert rig.files() == [f"shape-x-{datetime.date.today().isoformat()}.md"]


def test_with_the_ledger_on_it_links_the_saved_file_once(rig):
    rig.configure()
    proc = rig.run("--slug", "new-cache", "--date", "2026-11-04", "--item", "topic-new-cache")
    assert proc.returncode == 0, proc.stderr
    target = (rig.roadmaps / "shape-new-cache-2026-11-04.md").resolve()
    assert rig.calls() == [
        ["link", "topic-new-cache", "--rel", "url", "--target", str(target)]
    ]


def test_with_the_ledger_off_the_file_is_written_and_nothing_is_called(rig):
    for value in (None, "", "  "):
        rig.configure(value)
        proc = rig.run("--slug", f"s{len(str(value))}", "--date", "2026-11-04", "--item", "topic-x")
        assert proc.returncode == 0, proc.stderr
    assert len(rig.files()) == 3
    assert rig.calls() == []


def test_without_an_item_no_ledger_call_is_made(rig):
    rig.configure()
    proc = rig.run("--slug", "x", "--date", "2026-11-04")
    assert proc.returncode == 0
    assert rig.calls() == []


def test_a_failing_link_keeps_the_file_and_says_where_it_is(rig):
    rig.configure()
    proc = rig.run(
        "--slug", "x", "--date", "2026-11-04", "--item", "topic-x",
        extra_env={"FAKE_FAIL_VERB": "link"},
    )
    assert proc.returncode == 1
    assert rig.files() == ["shape-x-2026-11-04.md"]
    assert "shape-x-2026-11-04.md" in proc.stderr
    assert len(proc.stderr.splitlines()) == 1


def test_an_existing_brief_is_never_overwritten(rig):
    first = rig.run("--slug", "x", "--date", "2026-11-04")
    assert first.returncode == 0
    second = rig.run("--slug", "x", "--date", "2026-11-04", stdin="# different\n")
    assert second.returncode == 2
    assert "already exists" in second.stderr
    assert (rig.roadmaps / "shape-x-2026-11-04.md").read_text(encoding="utf-8") == BRIEF


@pytest.mark.parametrize(
    "args",
    [
        ("--slug", "Bad Slug"),
        ("--slug", "../escape"),
        ("--slug", "trailing-"),
        ("--slug", "x", "--date", "2026-1-4"),
        ("--slug", "x", "--date", "2026-13-40"),
    ],
)
def test_a_bad_slug_or_date_is_refused_and_writes_nothing(rig, args):
    proc = rig.run(*args)
    assert proc.returncode == 2
    assert rig.files() == []


def test_an_empty_brief_is_refused(rig):
    proc = rig.run("--slug", "x", "--date", "2026-11-04", stdin="  \n")
    assert proc.returncode == 2
    assert rig.files() == []


def test_it_reuses_sharpens_resolver_and_degrades_when_sharpen_is_absent(rig, tmp_path):
    layout = tmp_path / "skills"
    (layout / "shape").mkdir(parents=True)
    shutil.copy(SAVE, layout / "shape" / "save_brief.py")
    shutil.copytree(
        REPO_ROOT / "skills" / "_lib", layout / "_lib",
        ignore=shutil.ignore_patterns("tests", "__pycache__"),
    )
    rig.script = layout / "shape" / "save_brief.py"
    proc = rig.run("--slug", "x", "--date", "2026-11-04")
    assert proc.returncode == 2
    assert "sharpen" in proc.stderr
    assert rig.files() == []


def test_skill_text_always_saves_and_names_the_helper():
    text = SKILL_DOC.read_text(encoding="utf-8")
    assert "save_brief.py" in text
    assert "shape-<slug>-<YYYY-MM-DD>.md" in text
    assert "../_lib/ledger_cli.py" in text
    assert "mutates nothing" not in text

"""sharpen's ledger capture: link the brief, record the ratification as an election."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CAPTURE = REPO_ROOT / "skills" / "sharpen" / "ledger_capture.py"
SKILL_DOC = REPO_ROOT / "skills" / "sharpen" / "SKILL.md"

FAKE = """#!__PYTHON__
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LEDGER_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(argv) + "\\n")
if argv[0] == os.environ.get("FAKE_FAIL_VERB"):
    sys.stderr.write("refused by the fake\\n")
    raise SystemExit(3)
if argv[0] == "append":
    print(json.dumps({"ok": True, "data": {"event": {"id": int(os.environ.get("FAKE_EVENT_ID", "7"))}}}))
else:
    print("ok")
"""

WORDS = "Looks right to me. Ratified as written, go ahead and build it."
QUOTE = "Ratified as written"


class Rig:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "calls.log"
        fake = self.bin / "fakeledger"
        fake.write_text(FAKE.replace("__PYTHON__", sys.executable), encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.brief = tmp_path / "roadmaps" / "sharpen-demo-2026-11-04.md"
        self.brief.parent.mkdir()
        self.brief.write_text("# Sharpening brief — demo\n", encoding="utf-8")

    def configure(self, value="fakeledger"):
        path = self.home / ".claude" / "skill-config"
        path.mkdir(parents=True, exist_ok=True)
        (path / "topic.json").write_text(json.dumps({"ledger_cli": value}), encoding="utf-8")

    def run(self, *args, extra_env=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
        env["HOME"] = str(self.home)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_LEDGER_LOG"] = str(self.log)
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, str(CAPTURE), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def link(self, **kw):
        return self.run("link", "--item", "topic-demo", "--brief", str(self.brief), **kw)

    def ratify(self, words=WORDS, quote=QUOTE, *more, **kw):
        args = ["ratify", "--item", "topic-demo", "--brief", str(self.brief), "--quote", quote]
        if words is not None:
            args += ["--words", words]
        return self.run(*args, *more, **kw)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def test_a_brief_produces_one_link_call_with_the_absolute_brief_path(rig):
    rig.configure()
    proc = rig.link()
    assert proc.returncode == 0, proc.stderr
    assert rig.calls() == [
        ["link", "topic-demo", "--rel", "url", "--target", str(rig.brief.resolve())]
    ]


def test_a_relative_brief_path_is_made_absolute(rig):
    rig.configure()
    proc = subprocess.run(
        [sys.executable, str(CAPTURE), "link", "--item", "topic-demo", "--brief", rig.brief.name],
        cwd=rig.brief.parent,
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")},
            "HOME": str(rig.home),
            "PATH": f"{rig.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_LEDGER_LOG": str(rig.log),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert rig.calls()[0][-1] == str(rig.brief.resolve())


def test_ratifying_captures_the_words_then_elects_quoting_them(rig):
    rig.configure()
    proc = rig.ratify()
    assert proc.returncode == 0, proc.stderr
    calls = rig.calls()
    assert [c[0] for c in calls] == ["append", "elect"]
    append, elect = calls

    assert _flag(append, "--kind") == "utterance.captured"
    payload = json.loads(_flag(append, "--payload"))
    assert payload["text"] == WORDS
    assert payload["origin"] == "person"
    assert _flag(append, "--item") == "topic-demo"

    assert elect[1] == "topic-demo"
    quote = _flag(elect, "--quote")
    assert quote == QUOTE
    assert quote in payload["text"], "the quote must be a verbatim substring of the captured words"
    assert _flag(elect, "--quote-ref") == "7", "the election cites the captured utterance's event id"
    assert _flag(elect, "--question") == "Ratify sharpen-demo-2026-11-04.md"
    assert _flag(elect, "--chosen") == "ratified"
    assert _flag(elect, "--rejected") == "not-ratified"
    assert _flag(elect, "--scope") == str(rig.brief.resolve())
    assert _flag(elect, "--chosen-description")
    assert not any(c[0] == "note" for c in calls), "a ratification is an election, not a note"


def test_the_session_id_rides_on_the_captured_utterance_when_known(rig):
    rig.configure()
    rig.ratify(extra_env={"LEDGER_SESSION": "sess-9"})
    payload = json.loads(_flag(rig.calls()[0], "--payload"))
    assert payload["session_id"] == "sess-9"


def test_a_quote_that_is_not_in_the_words_records_nothing(rig):
    rig.configure()
    proc = rig.ratify(quote="ratified as written")  # other case
    assert proc.returncode == 2
    assert "substring" in proc.stderr
    assert rig.calls() == []


@pytest.mark.parametrize(
    "words, quote",
    [(None, QUOTE), ("", QUOTE), (WORDS, "")],
)
def test_missing_words_or_quote_records_nothing(rig, words, quote):
    rig.configure()
    proc = rig.ratify(words=words, quote=quote)
    assert proc.returncode == 2
    assert rig.calls() == []


def test_a_brief_that_is_not_a_file_records_nothing(rig):
    rig.configure()
    rig.brief.unlink()
    assert rig.link().returncode == 2
    assert rig.ratify().returncode == 2
    assert rig.calls() == []


def test_with_the_cli_unset_nothing_is_called_and_nothing_printed(rig):
    for value in (None, "", "  "):
        rig.configure(value)
        for proc in (rig.link(), rig.ratify()):
            assert proc.returncode == 0, proc.stderr
            assert proc.stdout == ""
            assert proc.stderr == ""
    assert rig.calls() == []


def test_with_no_config_file_at_all_nothing_is_called(rig):
    assert rig.link().returncode == 0
    assert rig.ratify().returncode == 0
    assert rig.calls() == []


def test_a_failing_link_is_one_line_and_exit_one(rig):
    rig.configure()
    proc = rig.link(extra_env={"FAKE_FAIL_VERB": "link"})
    assert proc.returncode == 1
    assert len(proc.stderr.splitlines()) == 1
    assert "link failed" in proc.stderr


def test_a_failing_capture_stops_before_any_election(rig):
    rig.configure()
    proc = rig.ratify(extra_env={"FAKE_FAIL_VERB": "append"})
    assert proc.returncode == 1
    assert [c[0] for c in rig.calls()] == ["append"]


def test_a_failing_election_names_the_captured_event_and_a_retry_does_not_capture_again(rig):
    rig.configure()
    proc = rig.ratify(extra_env={"FAKE_FAIL_VERB": "elect", "FAKE_EVENT_ID": "41"})
    assert proc.returncode == 1
    assert "--quote-ref 41" in proc.stderr
    assert [c[0] for c in rig.calls()] == ["append", "elect"]

    retry = rig.run(
        "ratify", "--item", "topic-demo", "--brief", str(rig.brief),
        "--quote", QUOTE, "--quote-ref", "41",
    )
    assert retry.returncode == 0, retry.stderr
    calls = rig.calls()
    assert [c[0] for c in calls] == ["append", "elect", "elect"]
    assert _flag(calls[-1], "--quote-ref") == "41"


def test_the_capture_carries_a_stable_idempotency_key(rig):
    rig.configure()
    rig.ratify()
    rig.ratify()
    appends = [c for c in rig.calls() if c[0] == "append"]
    assert len(appends) == 2
    key = _flag(appends[0], "--idempotency-key")
    assert key == _flag(appends[1], "--idempotency-key")
    other = rig.ratify(words=WORDS + " Also fine.", quote=QUOTE)
    assert other.returncode == 0
    last = [c for c in rig.calls() if c[0] == "append"][-1]
    assert _flag(last, "--idempotency-key") != key


def test_skill_text_documents_the_commands_and_the_off_switch():
    text = SKILL_DOC.read_text(encoding="utf-8")
    assert "ledger_capture.py link" in text
    assert "ledger_capture.py ratify" in text
    assert "../_lib/ledger_cli.py" in text
    assert "null" in text

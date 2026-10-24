"""ledger_item suite — the `ledger_cli` route of `/topic`.

Seam under test: `ledger_item.py --open <slug> --ask <text>` run as a
program. With `ledger_cli` set it opens or resumes a `topic` item through the
configured executable; unset, it behaves as `topics_dir.py --open`.

The ledger is a fake executable the test writes to `tmp_path`. It records
every argv, keeps its items in a JSON file, and accepts only the verbs and
flags the real `ledger` CLI defines (`query`, `new`, `note`, `link`, `card`,
`export-item`), exits 2 on anything else, and like the real one refuses writes
without `LEDGER_HARNESS` and runs nothing without `LEDGER_DB`.

Two state keys let a test stage what a real store does under failure and
concurrency, at the CLI boundary: `fail_notes` (1-based `note` call numbers
that exit 1 without writing) and `inject_on_new` (an item another process
creates just before this process's `new` is evaluated).
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
HELPER = SKILL_DIR / "ledger_item.py"
ASK = "the ask in the asker's words"

FAKE_LEDGER = """#!__PYTHON__
import json
import os
import sys

STATE = __STATE__
ITEM_TYPES = ("topic", "ticket", "initiative", "decision", "gate", "guidance")
LINK_RELS = ("pr", "branch", "session", "item", "url", "blocks")
# verb -> (value flags, positional names)
VERBS = {
    "query": (("--type", "--item", "--status", "--limit", "--db"), ()),
    "new": (("--type", "--title", "--id", "--status", "--db"), ()),
    "note": (("--text", "--expect-revision", "--db"), ("item",)),
    "link": (("--rel", "--target", "--db"), ("item",)),
    "card": (("--md", "--db"), ("item",)),
    "export-item": (("--db",), ("item",)),
}
WRITES = ("new", "note", "link")

with open(STATE, encoding="utf-8") as handle:
    state = json.load(handle)
argv = sys.argv[1:]
state["calls"].append(argv)


def save():
    with open(STATE, "w", encoding="utf-8") as handle:
        json.dump(state, handle)


def finish(code, ok, data=None, error_code=None, message=None):
    save()
    print(json.dumps({
        "data": data,
        "error": None if ok else {"code": error_code, "message": message},
        "ok": ok,
    }, sort_keys=True))
    raise SystemExit(code)


if not argv or argv[0] not in VERBS:
    finish(2, False, error_code="usage", message="unknown verb")
verb = argv[0]
flags, positional = VERBS[verb]
values = {}
bare = []
rest = argv[1:]
index = 0
while index < len(rest):
    token = rest[index]
    if token == "--json":
        index += 1
    elif token in flags:
        if index + 1 >= len(rest):
            finish(2, False, error_code="usage", message=token + " needs a value")
        values[token] = rest[index + 1]
        index += 2
    elif token.startswith("-"):
        finish(2, False, error_code="usage", message="unrecognized argument " + token)
    else:
        bare.append(token)
        index += 1
if len(bare) != len(positional):
    finish(2, False, error_code="usage", message="wrong positional arguments")
if not (values.get("--db") or os.environ.get("LEDGER_DB")):
    finish(2, False, error_code="usage", message="pass --db or set LEDGER_DB")
if verb in WRITES and not os.environ.get("LEDGER_HARNESS"):
    finish(3, False, error_code="missing_harness", message="LEDGER_HARNESS is required for writes")

items = state["items"]
if verb == "query":
    if not state.get("db", True):
        finish(1, False, error_code="missing_db", message="database does not exist")
    matched = [
        item for item in items.values()
        if values.get("--item", item["item_id"]) == item["item_id"]
        and values.get("--type", item["type"]) == item["type"]
        and values.get("--status", item["status"]) == item["status"]
    ]
    if not matched:
        finish(1, False, error_code="empty", message="no matching rows")
    finish(0, True, data={"items": matched, "limit": 50, "truncated": False})
if verb == "new":
    if values.get("--type") not in ITEM_TYPES or "--title" not in values:
        finish(2, False, error_code="usage", message="--type and --title are required")
    item_id = values.get("--id") or "it-generated"
    if state.get("inject_on_new"):
        racer = state.pop("inject_on_new")
        items[racer["item_id"]] = racer
    if item_id in items:
        finish(3, False, error_code="conflict", message="item already exists: " + item_id)
    state["db"] = True
    items[item_id] = {
        "item_id": item_id,
        "type": values["--type"],
        "title": values["--title"],
        "status": values.get("--status", "open"),
        "notes": [],
        "links": [],
    }
    finish(0, True, data={"item_id": item_id, "event": {"id": len(items)}})
item_id = bare[0]
if item_id not in items:
    finish(1, False, error_code="not_found", message="item not found: " + item_id)
if verb == "card":
    item = items[item_id]
    finish(0, True, data={
        "item_id": item_id,
        "type": item["type"],
        "title": item["title"],
        "status": item["status"],
        "notes": [
            {"event_id": number + 2, "text": text}
            for number, text in enumerate(item["notes"])
        ],
        "footer": "This card is a projection, not authority",
    })
if verb == "export-item":
    item = items[item_id]
    events = [{
        "id": 1, "kind": "item.created", "item_id": item_id,
        "payload": {"type": item["type"], "title": item["title"]},
    }]
    events += [
        {"id": number + 2, "kind": "item.note", "item_id": item_id,
         "payload": {"text": text}}
        for number, text in enumerate(item["notes"])
    ]
    finish(0, True, data={"item_id": item_id, "events": events})
if verb == "note":
    if "--text" not in values:
        finish(2, False, error_code="usage", message="--text is required")
    state["note_calls"] = state.get("note_calls", 0) + 1
    if state["note_calls"] in state.get("fail_notes", []):
        finish(1, False, error_code="store_busy", message="database is locked")
    items[item_id]["notes"].append(values["--text"])
if verb == "link":
    if values.get("--rel") not in LINK_RELS or "--target" not in values:
        finish(2, False, error_code="usage", message="--rel and --target are required")
    items[item_id]["links"].append([values["--rel"], values["--target"]])
finish(0, True, data={"item_id": item_id, "event": {"id": 1}})
"""


class Rig:
    """A fake ledger, a config naming it, and a clean HOME."""

    def __init__(self, tmp_path, monkeypatch):
        self.home_dir = tmp_path / "home"
        self.home_dir.mkdir()
        self.topics = tmp_path / "topics"
        self.state = tmp_path / "fake-state.json"
        self.state.write_text(json.dumps({"items": {}, "calls": []}), encoding="utf-8")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.fake = self.bin / "ledger"
        self.fake.write_text(
            FAKE_LEDGER.replace("__PYTHON__", sys.executable).replace(
                "__STATE__", repr(str(self.state))
            ),
            encoding="utf-8",
        )
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.config = tmp_path / "config.json"
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("LEDGER_")
        }
        self.env["HOME"] = str(self.home_dir)
        self.env["LEDGER_DB"] = str(tmp_path / "ledger.db")
        self.env["LEDGER_HARNESS"] = "test"

    def write_config(self, **keys):
        keys.setdefault("topics_dir", str(self.topics))
        self.config.write_text(json.dumps(keys), encoding="utf-8")

    def run(self, slug="demo", ask=ASK, *, use_config=True):
        argv = [sys.executable, str(HELPER), "--open", slug, "--ask", ask]
        if use_config:
            argv[2:2] = ["--config", str(self.config)]
        return subprocess.run(
            argv, env=self.env, capture_output=True, text=True, check=False
        )

    def snapshot(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def stage(self, **keys):
        state = self.snapshot()
        state.update(keys)
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def card_notes(self, item_id="topic-demo"):
        """The item's notes as the ledger CLI's own card shows them."""
        proc = subprocess.run(
            [str(self.fake), "card", item_id, "--json"],
            env=self.env, capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        return [row["text"] for row in json.loads(proc.stdout)["data"]["notes"]]

    def note_files(self):
        if not self.topics.exists():
            return []
        return sorted(p.name for p in self.topics.rglob("*") if p.is_file())


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


RAILS = "topic-rails\nstaged_drafts: none\npromote_or_park: open"
QUERY = ["query", "--type", "topic", "--item", "topic-demo", "--json"]
NEW = ["new", "--type", "topic", "--title", "demo", "--id", "topic-demo", "--json"]
NOTE = ["note", "topic-demo", "--text", "Ask: " + ASK, "--json"]
RAILS_NOTE = ["note", "topic-demo", "--text", RAILS, "--json"]
EXPORT = ["export-item", "topic-demo", "--json"]


def rails_of(notes):
    """The current rails, parsed from the newest `topic-rails` note."""
    blocks = [text for text in notes if text.split("\n", 1)[0] == "topic-rails"]
    assert blocks, f"no topic-rails note among {notes}"
    fields = {}
    for line in blocks[-1].split("\n")[1:]:
        key, _, value = line.partition(": ")
        fields[key] = value
    return fields


def test_first_call_creates_exactly_one_topic_item_and_no_note_file(rig):
    rig.write_config(ledger_cli=str(rig.fake))

    proc = rig.run()

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"topic-demo\tcreated\t{rig.fake}\n"
    state = rig.snapshot()
    assert list(state["items"]) == ["topic-demo"]
    item = state["items"]["topic-demo"]
    assert (item["type"], item["title"], item["status"]) == ("topic", "demo", "open")
    assert item["notes"] == ["Ask: " + ASK, RAILS]
    assert [call[0] for call in state["calls"]].count("new") == 1
    assert rig.note_files() == [], "the ledger route must not write a markdown note"


def test_second_call_resumes_the_same_item_and_creates_none(rig):
    rig.write_config(ledger_cli=str(rig.fake))
    first = rig.run()
    after_first = rig.snapshot()["items"]
    calls_before = len(rig.snapshot()["calls"])

    second = rig.run(ask="a different ask on the second touch")

    assert second.returncode == 0, second.stderr
    assert second.stdout == f"topic-demo\tresumed\t{rig.fake}\n"
    state = rig.snapshot()
    assert state["items"] == after_first, "a resume must not change or duplicate the item"
    assert state["calls"][calls_before:] == [QUERY, EXPORT], "a resume only reads the item"
    assert [call[0] for call in state["calls"]].count("new") == 1
    assert first.stdout.split("\t")[0] == second.stdout.split("\t")[0]
    assert rig.note_files() == []


def test_first_call_against_a_store_that_does_not_exist_yet_creates_the_item(rig):
    state = rig.snapshot()
    state["db"] = False
    rig.state.write_text(json.dumps(state), encoding="utf-8")
    rig.write_config(ledger_cli=str(rig.fake))

    proc = rig.run()

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"topic-demo\tcreated\t{rig.fake}\n"
    assert rig.snapshot()["calls"] == [QUERY, NEW, NOTE, RAILS_NOTE]
    assert list(rig.snapshot()["items"]) == ["topic-demo"]


def test_a_different_slug_is_a_different_item(rig):
    rig.write_config(ledger_cli=str(rig.fake))

    assert rig.run("demo").returncode == 0
    assert rig.run("other").returncode == 0

    assert sorted(rig.snapshot()["items"]) == ["topic-demo", "topic-other"]


@pytest.mark.parametrize("setting", ["absent", None, ""])
def test_key_unset_makes_no_cli_call_and_writes_the_note_file(rig, setting):
    if setting == "absent":
        rig.write_config()
    else:
        rig.write_config(ledger_cli=setting)
    # A working `ledger` on PATH must not be picked up when the key is unset.
    rig.env["PATH"] = str(rig.bin) + os.pathsep + rig.env["PATH"]

    proc = rig.run()

    assert proc.returncode == 0, proc.stderr
    assert rig.snapshot()["calls"] == []
    assert proc.stdout.strip() == str(rig.topics / "demo.md")
    assert rig.note_files() == ["demo.md"]
    assert ASK in (rig.topics / "demo.md").read_text(encoding="utf-8")
    assert rig.snapshot()["items"] == {}


def test_shipped_config_leaves_the_key_unset():
    shipped = json.loads((SKILL_DIR / "config.json").read_text(encoding="utf-8"))
    assert "ledger_cli" in shipped
    assert shipped["ledger_cli"] is None


def test_missing_executable_fails_closed_with_one_line_and_no_note(rig):
    missing = rig.bin / "no-such-ledger"
    rig.write_config(ledger_cli=str(missing))

    proc = rig.run()

    assert rig.note_files() == [], "a missing CLI must not fall back to a note file"
    assert not rig.topics.exists()
    assert proc.returncode != 0
    assert proc.stdout == ""
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert str(missing) in lines[0]


def test_a_cli_that_fails_fails_closed_with_one_line_and_no_note(rig):
    rig.write_config(ledger_cli=str(rig.fake))
    del rig.env["LEDGER_HARNESS"]  # the CLI refuses the write

    proc = rig.run()

    assert rig.note_files() == [], "a failing CLI must not fall back to a note file"
    assert proc.returncode != 0
    assert proc.stdout == ""
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert str(rig.fake) in lines[0]
    assert "LEDGER_HARNESS" in lines[0]
    assert rig.snapshot()["items"] == {}


def test_a_cli_that_cannot_be_executed_fails_closed(rig):
    not_executable = rig.bin / "plain-file"
    not_executable.write_text("not a program\n", encoding="utf-8")
    rig.write_config(ledger_cli=str(not_executable))

    proc = rig.run()

    assert proc.returncode != 0
    assert len(proc.stderr.splitlines()) == 1
    assert str(not_executable) in proc.stderr
    assert rig.note_files() == []


def test_a_non_string_key_fails_closed_and_writes_no_note(rig):
    rig.write_config(ledger_cli=["ledger"])

    proc = rig.run()

    assert proc.returncode != 0
    assert len(proc.stderr.splitlines()) == 1
    assert "ledger_cli" in proc.stderr
    assert rig.note_files() == []


def test_an_item_of_another_type_under_the_same_id_is_not_taken_over(rig):
    state = rig.snapshot()
    state["items"]["topic-demo"] = {
        "item_id": "topic-demo", "type": "ticket", "title": "x",
        "status": "open", "notes": [], "links": [],
    }
    rig.state.write_text(json.dumps(state), encoding="utf-8")
    rig.write_config(ledger_cli=str(rig.fake))

    proc = rig.run()

    assert proc.returncode != 0
    assert len(proc.stderr.splitlines()) == 1
    assert "already exists" in proc.stderr
    assert rig.snapshot()["items"]["topic-demo"]["notes"] == []
    # The conflict was re-queried by type, found nothing, and was re-raised.
    assert rig.snapshot()["calls"] == [QUERY, NEW, QUERY]


def test_argv_sent_to_the_cli_matches_the_real_verbs_and_flags(rig):
    rig.write_config(ledger_cli=str(rig.fake))

    rig.run()
    created = rig.snapshot()["calls"]
    rig.run()
    resumed = rig.snapshot()["calls"][len(created):]

    assert created == [QUERY, NEW, NOTE, RAILS_NOTE]
    assert resumed == [QUERY, EXPORT]


def test_the_per_skill_override_file_turns_the_route_on(rig):
    override = rig.home_dir / ".claude" / "skill-config" / "topic.json"
    override.parent.mkdir(parents=True)
    override.write_text(
        json.dumps({"topics_dir": str(rig.topics), "ledger_cli": str(rig.fake)}),
        encoding="utf-8",
    )

    proc = rig.run(use_config=False)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"topic-demo\tcreated\t{rig.fake}\n"
    assert list(rig.snapshot()["items"]) == ["topic-demo"]
    assert rig.note_files() == []


@pytest.mark.parametrize("slug", ["../escaped", "a/b", ".."])
def test_a_slug_that_leaves_the_topics_dir_is_rejected_before_any_call(rig, slug):
    rig.write_config(ledger_cli=str(rig.fake))

    proc = rig.run(slug)

    assert proc.returncode != 0
    assert rig.snapshot()["calls"] == []


def test_skill_text_names_the_key_and_the_helper():
    text = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
    assert "ledger_cli" in text
    assert "${CLAUDE_SKILL_DIR}/ledger_item.py" in text
    assert "writes no markdown note" in text


def test_skill_text_states_the_rails_grammar_and_how_to_update_it():
    text = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
    assert "topic-rails staged_drafts: none promote_or_park: open" in text
    assert "append a newer note of the same grammar" in text
    assert "Never free text" in text


def test_a_fresh_item_exposes_both_rails_through_the_cli_card(rig):
    rig.write_config(ledger_cli=str(rig.fake))

    proc = rig.run()

    assert proc.returncode == 0, proc.stderr
    notes = rig.card_notes()
    assert notes == ["Ask: " + ASK, RAILS]
    assert rails_of(notes) == {"staged_drafts": "none", "promote_or_park": "open"}


@pytest.mark.parametrize("failing_note", [1, 2], ids=["ask-note-fails", "rails-note-fails"])
def test_a_create_that_dies_after_new_is_completed_by_the_next_call_exactly_once(
    rig, failing_note
):
    rig.write_config(ledger_cli=str(rig.fake))
    rig.stage(fail_notes=[failing_note])

    first = rig.run()

    assert first.returncode != 0
    assert len(first.stderr.splitlines()) == 1
    assert "note failed" in first.stderr
    assert list(rig.snapshot()["items"]) == ["topic-demo"], "the item was created"
    assert len(rig.card_notes()) == failing_note - 1, "and is missing a note"

    second = rig.run()

    assert second.returncode == 0, second.stderr
    assert second.stdout == f"topic-demo\tresumed\t{rig.fake}\n"
    notes = rig.card_notes()
    assert notes == ["Ask: " + ASK, RAILS], "exactly one Ask note and one rails note"
    assert rails_of(notes) == {"staged_drafts": "none", "promote_or_park": "open"}

    calls_before = len(rig.snapshot()["calls"])
    third = rig.run()

    assert third.stdout == f"topic-demo\tresumed\t{rig.fake}\n"
    assert [c[0] for c in rig.snapshot()["calls"][calls_before:]] == ["query", "export-item"]
    assert rig.card_notes() == notes, "a further retry adds nothing"


def _racer(notes):
    return {
        "item_id": "topic-demo", "type": "topic", "title": "demo",
        "status": "open", "notes": notes, "links": [],
    }


@pytest.mark.parametrize(
    "winner_notes, extra_calls",
    [
        (["Ask: the winner's ask", RAILS], []),
        ([], [NOTE, RAILS_NOTE]),
    ],
    ids=["winner-finished", "winner-still-between-new-and-note"],
)
def test_losing_the_create_race_resumes_the_winners_item(rig, winner_notes, extra_calls):
    # `query` finds nothing; another process then creates the item just before
    # this process's `new` is evaluated, so `new` answers `conflict`.
    rig.write_config(ledger_cli=str(rig.fake))
    rig.stage(inject_on_new=_racer(winner_notes))

    proc = rig.run()

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"topic-demo\tresumed\t{rig.fake}\n"
    state = rig.snapshot()
    assert state["calls"] == [QUERY, NEW, QUERY, EXPORT, *extra_calls]
    assert list(state["items"]) == ["topic-demo"]
    notes = rig.card_notes()
    assert len([n for n in notes if n.startswith("Ask: ")]) == 1
    assert len([n for n in notes if n.split("\n", 1)[0] == "topic-rails"]) == 1

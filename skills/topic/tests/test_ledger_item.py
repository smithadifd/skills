"""ledger_item suite — the `ledger_cli` route of `/topic`.

Seam under test: `ledger_item.py --open <slug> --ask <text>` run as a
program. With `ledger_cli` set it opens or resumes a `topic` item through the
configured executable; unset, it behaves as `topics_dir.py --open`.

The ledger is a fake executable the test writes to `tmp_path`. It records
every argv, keeps its items in a JSON file, and accepts only the verbs and
flags the real `ledger` CLI defines (`query`, `new`, `note`, `link`, `card`,
`export-item`), exits 2 on anything else, and like the real one refuses writes
without `LEDGER_HARNESS` and runs nothing without `LEDGER_DB`. Every write takes
the next id from one event counter, and `note` honours `--expect-revision` as the
real one does: it appends only when the item's newest event id equals the value,
and otherwise exits 5 with `stale_revision` and writes nothing.

Three state keys let a test stage what a real store does under failure and
concurrency, at the CLI boundary: `fail_notes` (1-based `note` call numbers
that exit 1 without writing), `stale_notes` (1-based `note` call numbers
refused as `stale_revision` without writing) and `inject_on_new` (an item
another process creates just before this process's `new` is evaluated).
"""

import importlib.util
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
state.setdefault("last_event", 0)


def next_event():
    state["last_event"] += 1
    return state["last_event"]


def stamp(item):
    # Give an item staged without event ids its ids, in order.
    if "rev" not in item:
        item["created_id"] = next_event()
        item["note_ids"] = [next_event() for _ in item["notes"]]
        item["rev"] = item["note_ids"][-1] if item["note_ids"] else item["created_id"]
        # `hidden`: session events the item has recorded that `export-item`
        # omits once it is closed. They take event ids and move the revision.
        for _ in range(item.get("hidden", 0)):
            item["rev"] = next_event()
    return item


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
    rows = [dict(stamp(item), revision=item["rev"]) for item in matched]
    finish(0, True, data={"items": rows, "limit": 50, "truncated": False})
if verb == "new":
    if values.get("--type") not in ITEM_TYPES or "--title" not in values:
        finish(2, False, error_code="usage", message="--type and --title are required")
    item_id = values.get("--id") or "it-generated"
    if state.get("inject_on_new"):
        racer = state.pop("inject_on_new")
        items[racer["item_id"]] = stamp(racer)
    if item_id in items:
        finish(3, False, error_code="conflict", message="item already exists: " + item_id)
    state["db"] = True
    event_id = next_event()
    items[item_id] = {
        "item_id": item_id,
        "type": values["--type"],
        "title": values["--title"],
        "status": values.get("--status", "open"),
        "notes": [],
        "links": [],
        "created_id": event_id,
        "note_ids": [],
        "rev": event_id,
    }
    finish(0, True, data={"item_id": item_id, "event": {"id": event_id}})
item_id = bare[0]
if item_id not in items:
    finish(1, False, error_code="not_found", message="item not found: " + item_id)
item = stamp(items[item_id])
if verb == "card":
    finish(0, True, data={
        "item_id": item_id,
        "type": item["type"],
        "title": item["title"],
        "status": item["status"],
        "notes": [
            {"event_id": note_id, "text": text}
            for note_id, text in zip(item["note_ids"], item["notes"])
        ],
        "footer": "This card is a projection, not authority",
    })
if verb == "export-item":
    events = [{
        "id": item["created_id"], "kind": "item.created", "item_id": item_id,
        "payload": {"type": item["type"], "title": item["title"]},
    }]
    events += [
        {"id": note_id, "kind": "item.note", "item_id": item_id,
         "payload": {"text": text}}
        for note_id, text in zip(item["note_ids"], item["notes"])
    ]
    finish(0, True, data={"item_id": item_id, "events": events})
if verb == "note":
    if "--text" not in values:
        finish(2, False, error_code="usage", message="--text is required")
    state["note_calls"] = state.get("note_calls", 0) + 1
    if state["note_calls"] in state.get("fail_notes", []):
        finish(1, False, error_code="store_busy", message="database is locked")
    if state["note_calls"] in state.get("stale_notes", []):
        finish(5, False, error_code="stale_revision", message="stale revision for item " + item_id)
    expect = values.get("--expect-revision")
    if expect is not None:
        if expect != "none" and not (expect.isdigit() and int(expect) >= 1):
            finish(2, False, error_code="usage", message="--expect-revision must be an event id or none")
        if expect == "none" or int(expect) != item["rev"]:
            finish(5, False, error_code="stale_revision", message=(
                "stale revision for item " + item_id + ": expected " + expect
                + ", current is " + str(item["rev"])
            ))
    event_id = next_event()
    item["notes"].append(values["--text"])
    item["note_ids"].append(event_id)
    item["rev"] = event_id
    finish(0, True, data={"item_id": item_id, "event": {"id": event_id}})
if verb == "link":
    if values.get("--rel") not in LINK_RELS or "--target" not in values:
        finish(2, False, error_code="usage", message="--rel and --target are required")
    event_id = next_event()
    item["links"].append([values["--rel"], values["--target"]])
    item["rev"] = event_id
    finish(0, True, data={"item_id": item_id, "event": {"id": event_id}})
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
NOTE = ["note", "topic-demo", "--text", "Ask: " + ASK, "--expect-revision", "1", "--json"]
RAILS_NOTE = ["note", "topic-demo", "--text", RAILS, "--expect-revision", "2", "--json"]
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
    assert state["calls"][calls_before:] == [QUERY, QUERY, EXPORT], "a resume only reads the item"
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
    assert resumed == [QUERY, QUERY, EXPORT]


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
    assert [c[0] for c in rig.snapshot()["calls"][calls_before:]] == ["query", "query", "export-item"]
    assert rig.card_notes() == notes, "a further retry adds nothing"


def test_successful_appends_after_stale_refusals_do_not_count_toward_the_retry_limit(rig):
    # Six stale refusals leave two attempts of the eight; the two appends that
    # then succeed complete the item, so the open ends in success, not a raise.
    rig.write_config(ledger_cli=str(rig.fake))
    rig.stage(stale_notes=[1, 2, 3, 4, 5, 6])

    proc = rig.run()

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"topic-demo\tcreated\t{rig.fake}\n"
    notes = rig.card_notes()
    assert notes == ["Ask: " + ASK, RAILS], "exactly one Ask note and one rails note"
    assert [c[0] for c in rig.snapshot()["calls"]].count("note") == 8


def test_stale_refusals_up_to_the_retry_limit_in_a_row_still_fail_closed(rig):
    rig.write_config(ledger_cli=str(rig.fake))
    rig.stage(stale_notes=list(range(1, 9)))

    proc = rig.run()

    assert proc.returncode == 1
    assert len(proc.stderr.splitlines()) == 1
    assert "kept being refused as stale" in proc.stderr
    assert rig.card_notes() == [], "nothing was written"
    assert [c[0] for c in rig.snapshot()["calls"]].count("note") == 8


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
    assert state["calls"] == [QUERY, NEW, QUERY, QUERY, EXPORT, *extra_calls]
    assert list(state["items"]) == ["topic-demo"]
    notes = rig.card_notes()
    assert len([n for n in notes if n.startswith("Ask: ")]) == 1
    assert len([n for n in notes if n.split("\n", 1)[0] == "topic-rails"]) == 1


UPDATE = "topic-rails\nstaged_drafts: notes-2026.md\npromote_or_park: parked"
SECOND_ASK = "the second opener's ask"


def load_helper():
    spec = importlib.util.spec_from_file_location("ledger_item_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Interleaver:
    """Runs `between` just before the first opener's call number `boundary`.

    No threads: the second opener runs to completion, in this thread, at that
    boundary, and then the first opener carries on. The seam is the helper's one
    subprocess entry, `_call`, the same boundary the fake ledger sits behind;
    the second opener's own calls pass straight through and are not counted.
    """

    def __init__(self, module, boundary, between):
        self.real_call = module._call
        self.boundary = boundary
        self.between = between
        self.count = 0
        self.fired = False
        self.armed = True

    def __call__(self, cli, verb_argv):
        if self.armed:
            index = self.count
            self.count += 1
            if index == self.boundary:
                self.fired = True
                self.armed = False
                try:
                    self.between()
                finally:
                    self.armed = True
        return self.real_call(cli, verb_argv)


def use_cli_env(rig, monkeypatch):
    monkeypatch.setenv("LEDGER_DB", rig.env["LEDGER_DB"])
    monkeypatch.setenv("LEDGER_HARNESS", rig.env["LEDGER_HARNESS"])


def user_appends_rails(rig, text):
    """A person's rails update, written straight through the CLI with no guard."""
    proc = subprocess.run(
        [str(rig.fake), "note", "topic-demo", "--text", text, "--json"],
        env=rig.env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout


def rails_notes(notes):
    return [n for n in notes if n.split("\n", 1)[0] == "topic-rails"]


def assert_first_notes_written_once(notes, update_landed, where):
    assert len([n for n in notes if n.startswith("Ask: ")]) == 1, (where, notes)
    rails = rails_notes(notes)
    if update_landed:
        # A person's rails update stays the newest; the default never follows it.
        assert rails.count(UPDATE) == 1, (where, notes)
        assert rails[-1] == UPDATE, (where, notes)
        assert rails.count(RAILS) <= 1, (where, notes)
    else:
        assert rails == [RAILS], (where, notes)


@pytest.mark.parametrize(
    "update_in_the_gap", [False, True], ids=["no-update", "rails-update-in-the-gap"]
)
def test_the_winner_resuming_after_the_loser_finished_adds_nothing_and_buries_nothing(
    rig, monkeypatch, update_in_the_gap
):
    # The winner has run `new` and stalls before its first note. A second opener
    # finds the item, writes the Ask and rails notes, and a person may then
    # update the rails. The winner resumes and runs the rest of its calls.
    rig.write_config(ledger_cli=str(rig.fake))
    use_cli_env(rig, monkeypatch)
    cli = str(rig.fake)
    module = load_helper()
    second = {}

    def second_opener_then_the_gap():
        second["outcome"] = module.open_item(cli, "demo", SECOND_ASK)
        if update_in_the_gap:
            user_appends_rails(rig, UPDATE)

    # The winner's calls are query (0), new (1), then its first note (2).
    hook = Interleaver(module, 2, second_opener_then_the_gap)
    monkeypatch.setattr(module, "_call", hook)

    winner = module.open_item(cli, "demo", ASK)

    assert hook.fired
    assert winner == ("topic-demo", "created")
    assert second["outcome"] == ("topic-demo", "resumed")
    calls = rig.snapshot()["calls"]
    assert NOTE in calls, "the winner did try its Ask note after the second opener"
    notes = rig.card_notes()
    assert "Ask: " + ASK not in notes, "and the ledger refused it as stale"
    assert notes[0] == "Ask: " + SECOND_ASK
    assert len([n for n in notes if n.startswith("Ask: ")]) == 1
    if update_in_the_gap:
        assert rails_notes(notes) == [RAILS, UPDATE], "the update is still the newest rails"
        assert rails_of(notes) == {"staged_drafts": "notes-2026.md", "promote_or_park": "parked"}
    else:
        assert rails_notes(notes) == [RAILS]


@pytest.mark.parametrize(
    "update",
    [None, "before-the-second-opener", "after-the-second-opener"],
    ids=["no-update", "update-before-second-opener", "update-after-second-opener"],
)
def test_two_openers_interleaved_at_every_step_boundary_write_the_first_notes_once(
    rig, monkeypatch, update
):
    rig.write_config(ledger_cli=str(rig.fake))
    use_cli_env(rig, monkeypatch)
    cli = str(rig.fake)
    boundary = 0
    while True:
        rig.state.write_text(json.dumps({"items": {}, "calls": []}), encoding="utf-8")
        module = load_helper()
        outcomes = {}
        landed = []

        def user_update_if_the_item_exists():
            if "topic-demo" in rig.snapshot()["items"]:
                user_appends_rails(rig, UPDATE)
                landed.append(True)

        def second_opener():
            if update == "before-the-second-opener":
                user_update_if_the_item_exists()
            outcomes["second"] = module.open_item(cli, "demo", SECOND_ASK)
            if update == "after-the-second-opener":
                user_update_if_the_item_exists()

        hook = Interleaver(module, boundary, second_opener)
        monkeypatch.setattr(module, "_call", hook)

        outcomes["first"] = module.open_item(cli, "demo", ASK)
        past_the_end = not hook.fired
        hook.armed = False
        if past_the_end:
            second_opener()  # the boundary after the first opener's last call

        where = f"boundary {boundary}"
        assert sorted(state for _, state in outcomes.values()) == ["created", "resumed"], where
        assert list(rig.snapshot()["items"]) == ["topic-demo"], where
        assert_first_notes_written_once(rig.card_notes(), bool(landed), where)
        if past_the_end:
            break
        boundary += 1
    assert boundary >= 4, "the first opener makes at least query, new and two notes"


@pytest.mark.parametrize(
    "update",
    [None, "before-the-winner", "after-the-winner"],
    ids=["no-update", "update-before-winner", "update-after-winner"],
)
def test_a_resumer_interrupted_at_every_step_boundary_while_the_winner_finishes_writes_the_first_notes_once(
    rig, monkeypatch, update
):
    # The winner has run `new` and holds the revision it returned. A second
    # opener finds the item and reconciles it; at each boundary of that
    # reconcile the winner runs the rest of its calls (`_complete` is exactly
    # what `open_item` runs after `new`), so the resumer reads, the winner
    # writes, and only then the resumer writes.
    rig.write_config(ledger_cli=str(rig.fake))
    use_cli_env(rig, monkeypatch)
    cli = str(rig.fake)
    boundary = 0
    while True:
        rig.state.write_text(json.dumps({"items": {}, "calls": []}), encoding="utf-8")
        created = subprocess.run(
            [str(rig.fake), "new", "--type", "topic", "--title", "demo",
             "--id", "topic-demo", "--json"],
            env=rig.env, capture_output=True, text=True, check=False,
        )
        assert created.returncode == 0, created.stdout
        revision = json.loads(created.stdout)["data"]["event"]["id"]
        module = load_helper()

        def winner_finishes():
            if update == "before-the-winner":
                user_appends_rails(rig, UPDATE)
            module._complete(cli, "topic-demo", ASK, revision, [])
            if update == "after-the-winner":
                user_appends_rails(rig, UPDATE)

        hook = Interleaver(module, boundary, winner_finishes)
        monkeypatch.setattr(module, "_call", hook)

        outcome = module.open_item(cli, "demo", SECOND_ASK)
        past_the_end = not hook.fired
        hook.armed = False
        if past_the_end:
            winner_finishes()  # the boundary after the resumer's last call

        where = f"boundary {boundary}"
        assert outcome == ("topic-demo", "resumed"), where
        assert_first_notes_written_once(rig.card_notes(), update is not None, where)
        if past_the_end:
            break
        boundary += 1
    assert boundary >= 3, "the resumer makes at least query, export and two notes"


def _racer_closed_with_session_events():
    """A closed topic with no notes whose revision is above its export's newest id."""
    return {
        "item_id": "topic-demo", "type": "topic", "title": "demo",
        "status": "closed", "notes": [], "links": [], "hidden": 1,
    }


def test_repair_of_a_closed_item_takes_its_token_from_query_not_the_export(rig):
    # The closed item's session event takes id 2 and moves the revision to 2,
    # but `export-item` omits it and shows only id 1. The token must be 2.
    rig.write_config(ledger_cli=str(rig.fake))
    rig.stage(items={"topic-demo": _racer_closed_with_session_events()})

    proc = rig.run()

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"topic-demo\tresumed\t{rig.fake}\n"
    state = rig.snapshot()
    assert state["items"]["topic-demo"]["notes"] == ["Ask: " + ASK, RAILS]
    notes = [call for call in state["calls"] if call[0] == "note"]
    assert [call[call.index("--expect-revision") + 1] for call in notes] == ["2", "3"]
    verbs = [call[0] for call in state["calls"]]
    assert verbs.index("export-item") > 1, "the revision is queried before the export"


def test_each_stale_refusal_repeats_the_query_then_export_pair(rig):
    rig.write_config(ledger_cli=str(rig.fake))
    rig.stage(
        items={"topic-demo": _racer_closed_with_session_events()},
        stale_notes=[1],
    )

    proc = rig.run()

    assert proc.returncode == 0, proc.stderr
    verbs = [call[0] for call in rig.snapshot()["calls"]]
    assert verbs == [
        "query",  # find the item
        "query", "export-item", "note",  # refused stale
        "query", "export-item", "note",  # the pair is read again, then lands
        "note",
    ]


@pytest.mark.skipif(
    not os.environ.get("LEDGER_CLI"),
    reason="set LEDGER_CLI to the real ledger executable to run this test",
)
def test_repair_of_a_closed_item_succeeds_against_the_real_ledger_cli(tmp_path):
    cli = os.environ["LEDGER_CLI"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    env["LEDGER_DB"] = str(tmp_path / "real.db")
    env["LEDGER_HARNESS"] = "test"

    def ledger(*argv):
        proc = subprocess.run(
            [cli, *argv, "--json"], env=env, capture_output=True, text=True, check=False
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(proc.stdout)["data"]

    ledger("new", "--type", "topic", "--title", "demo", "--id", "topic-demo")
    ledger("state", "topic-demo", "--status", "closed")
    ledger(
        "append", "--kind", "item.progress", "--item", "topic-demo",
        "--payload", json.dumps({"text": "a session event after close"}),
    )
    export = ledger("export-item", "topic-demo")
    query = ledger("query", "--type", "topic", "--item", "topic-demo")
    export_max = max(event["id"] for event in export["events"])
    revision = query["items"][0]["revision"]
    assert revision > export_max, "the setup must reproduce the closed-item gap"

    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"topics_dir": str(tmp_path / "topics"), "ledger_cli": cli}),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(HELPER), "--config", str(config), "--open", "demo", "--ask", ASK],
        env=env, capture_output=True, text=True, check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"topic-demo\tresumed\t{cli}\n"
    notes = [
        event["payload"]["text"]
        for event in ledger("export-item", "topic-demo")["events"]
        if event["kind"] == "item.note"
    ]
    assert notes == ["Ask: " + ASK, RAILS]

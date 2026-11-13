"""Design 6.6: on the ledger store, an open records the tracker or JSON-file ticket it pulled as the item's spec.

The open sends one ``spec set`` with the ticket's title, description and criteria and the source
``ticket:<key> <url>``, before it reads the card. An unchanged ticket writes nothing; a changed one
writes the next version and the open prints ``spec diff``. The tracker's status is never written.
The tests run against the fake ledger CLI, which answers ``spec set`` and ``spec diff`` as ledger
0.1.22 does; the shared rule turns the ledger on through that fake, first on ``PATH`` with
``LEDGER_DB`` naming a file, in a ``HOME`` the test builds.
"""

import json
import shlex

import pytest

import ticket
from ledger_support import FakeLedger, ReadHook, machine
from ticket_support import A, B, B_EDITED, EDIT_WHAT, F1, F7, KEY, TITLE, Source, go_outward, go_plan, passing, present, refusal

URL = "https://tracker.example/browse/EK-14"
SOURCE = f"ticket:{KEY} {URL}"
DESCRIPTION = "Run one ticket from live orientation through approved outward work."
EDITED = "Run one ticket, and record what it pulled in the ledger."


@pytest.fixture
def fake(tmp_path, monkeypatch):
    fake = FakeLedger(tmp_path / "bin")
    machine(monkeypatch, tmp_path, fake)
    return fake


@pytest.fixture
def path(tmp_path):
    return tmp_path / "EK-14.json"


def write_ticket(path, **change):
    """A JSON ticket file, as a tracker export or a fixture holds it; ``change`` edits its fields."""

    data = {
        "key": KEY, "title": TITLE, "status": "In Progress", "description": DESCRIPTION,
        "acceptance_criteria": [A, B], "updated_at": "2026-11-12T09:00:00Z", "url": URL, "links": [],
    }
    data.update(change)
    path.write_text(json.dumps(data), encoding="utf-8")
    return ticket.FileTicketSource(path)


def open_ticket(repo, src, pass_id=None):
    """Open a pass the way SKILL.md says: a fresh ``choose_store`` handle for every open."""

    return ticket.TicketPass.open(KEY, src, ticket.choose_store(repo, KEY, TITLE), pass_id=pass_id)


def spec_calls(fake, action):
    return [call for call in fake.calls("spec") if call.argv[2] == action]


def versions(fake):
    return [event["payload"]["version"] for event in fake.specs()]


def claims(fake):
    return [event for event in fake.events() if event["kind"] == "session.opened"]


def set_command(fake, source=SOURCE):
    return (shlex.join([str(fake.path), "spec", "set", KEY, f"--source={source}"])
            + " --title=<title> --body=<description> --acceptance=<criteria>")


# --- One version per content: the ticket's test ------------------------------


def test_a_fixture_ticket_opened_twice_writes_one_spec_version(repo, fake, path):
    src = write_ticket(path)
    first = open_ticket(repo, src)
    first.end()
    second = open_ticket(repo, src)
    assert versions(fake) == [1]
    payload = fake.specs()[0]["payload"]
    assert {name: value for name, value in payload.items() if name != "content_hash"} == {
        "version": 1, "title": TITLE, "body_md": DESCRIPTION, "acceptance": f"- {A}\n- {B}", "source": SOURCE}
    # One spec set per open, the content as flags in the --name=value form, and no diff.
    assert [call.argv for call in spec_calls(fake, "set")] == [
        ["--json", "spec", "set", KEY, f"--title={TITLE}", f"--body={DESCRIPTION}", f"--acceptance=- {A}\n- {B}",
         f"--source={SOURCE}"]] * 2
    assert spec_calls(fake, "diff") == []
    # Each open confirms after its card read that the spec it recorded is still the item's current one.
    assert [call.verb for call in fake.calls()][-4:] == ["query", "export-item", "spec", "append-many"]
    assert [call.argv[1:4] for call in fake.calls("spec")] == [["spec", "set", KEY], ["spec", "show", KEY]] * 2
    assert first.reconciliation[1:] == () and second.reconciliation[1:] == ()
    assert second.live is True


def test_an_edited_description_writes_version_two_and_the_open_prints_the_diff_naming_the_field(repo, fake, path):
    src = write_ticket(path)
    open_ticket(repo, src).end()
    write_ticket(path, description=EDITED)
    session = open_ticket(repo, src)
    assert versions(fake) == [1, 2]
    assert fake.specs()[-1]["payload"]["body_md"] == EDITED
    (diff,) = spec_calls(fake, "diff")
    assert diff.argv == ["--json", "spec", "diff", KEY, "1", "2"]
    printed = session.reconciliation[1].splitlines()
    assert printed[0] == (f"ledger spec diff {KEY} 1 2: the ticket changed since spec v1, and spec v2 records it "
                          "(changed: body_md)")
    assert printed[1:] == ["## body_md", "--- v1", "+++ v2", "@@ -1 +1 @@", f"-{DESCRIPTION}", f"+{EDITED}"]
    assert session.reconciliation[2:] == ()
    # The spec version landed before the card read, so the claim landed under the item's newest revision.
    assert session.live is True and claims(fake)[-1]["id"] > fake.specs()[-1]["id"]


def test_the_source_is_byte_identical_on_every_open_and_names_the_key_and_url(repo, fake, path):
    src = write_ticket(path)
    for status, updated in (("In Progress", "2026-11-12T09:00:00Z"), ("Done", "2026-11-12T11:00:00Z"), ("Open", None)):
        write_ticket(path, status=status, updated_at=updated)
        open_ticket(repo, src).end()
    sources = [arg for call in spec_calls(fake, "set") for arg in call.argv if arg.startswith("--source=")]
    assert sources == [f"--source={SOURCE}"] * 3
    assert ticket.intake_source(src.pull(KEY)) == SOURCE
    assert versions(fake) == [1]


@pytest.mark.parametrize(
    "url", ["", "https://tracker.example/browse/EK 14", " " + URL, URL + "\n", 14, [URL]],
    ids=["empty", "inner space", "leading space", "trailing newline", "number", "list"],
)
def test_a_url_that_is_not_one_word_of_text_is_refused_by_the_source(path, url):
    with pytest.raises(ticket.TicketError) as excinfo:
        write_ticket(path, url=url).pull(KEY)
    assert str(excinfo.value) == "a ticket's url must be non-empty text with no whitespace"


@pytest.mark.parametrize("given", ["absent", "null"])
def test_a_ticket_with_no_url_records_its_key_alone_as_the_source(repo, fake, path, given):
    src = write_ticket(path, url=None)
    if given == "absent":
        stored = json.loads(path.read_text(encoding="utf-8"))
        del stored["url"]
        path.write_text(json.dumps(stored), encoding="utf-8")
    assert src.pull(KEY).url is None
    open_ticket(repo, src)
    assert fake.specs()[0]["payload"]["source"] == f"ticket:{KEY}"


# --- The tracker's status is never the ledger's ------------------------------


def test_a_status_flip_alone_writes_no_version_and_no_status(repo, fake):
    src = Source()
    open_ticket(repo, src).end()
    src.status = "Won't Do"
    session = open_ticket(repo, src)
    assert versions(fake) == [1]
    assert session.reconciliation[1:] == ()
    assert [event for event in fake.events() if event["kind"] == "item.state"] == []
    # Neither status reaches any event the ledger holds (a host name has no space in it).
    assert all("In Progress" not in json.dumps(event["payload"]) and "Won't Do" not in json.dumps(event["payload"])
               for event in fake.events())


# --- The other sources and stores --------------------------------------------


def test_the_markdown_store_makes_no_ledger_call(repo, fake, path, monkeypatch):
    monkeypatch.setenv("TICKET_LEDGER_CLI", "none")
    store = ticket.choose_store(repo, KEY, TITLE)
    assert isinstance(store, ticket.MarkdownStore)
    session = ticket.TicketPass.open(KEY, write_ticket(path), store)
    assert session.live is True
    assert fake.calls() == []


def test_a_ledger_item_source_records_no_spec_of_its_own(repo, fake):
    fake.new_item()
    fake.set_spec(f"- {A}\n- {B}")
    src = ticket.ledger_item_source(KEY)
    session = open_ticket(repo, src)
    assert session.live is True
    assert spec_calls(fake, "set") == [] and spec_calls(fake, "diff") == []
    assert versions(fake) == [1]


def test_an_item_a_person_filed_is_not_created_again(repo, fake, path):
    fake.new_item()
    session = open_ticket(repo, write_ticket(path))
    assert [[element["kind"] for element in call.batch] for call in fake.appends()] == [["session.opened"]]
    assert versions(fake) == [1] and session.live is True


# --- Fail closed ----------------------------------------------------------------


@pytest.mark.parametrize(
    "answer, said",
    [
        ({"code": "validation", "message": "the spec is refused"}, "answered validation: the spec is refused"),
        ({"code": "validation", "message": "the spec is refused", "stderr": "ledger: database is busy\n"},
         "answered validation: the spec is refused (stderr: ledger: database is busy)"),
        ({"stdout": "Traceback (most recent call last): boom", "status": 1},
         "gave no envelope, exit status 1: Traceback (most recent call last): boom"),
        ({"stdout": "", "status": 0}, "gave no envelope, exit status 0: "),
        ({"stdout": json.dumps({"ok": True, "data": {"item_id": KEY, "version": 1}, "error": None}), "status": 0},
         "answered what cannot be read: it does not give this item's id and an unchanged flag"),
        ({"stdout": json.dumps({"ok": True, "data": {"item_id": "EK-15", "version": 1, "unchanged": False}, "error": None}),
          "status": 0},
         "answered what cannot be read: it does not give this item's id and an unchanged flag"),
        ({"stdout": json.dumps({"ok": True, "data": {"item_id": KEY, "version": 0, "unchanged": False}, "error": None}),
          "status": 0},
         "answered what cannot be read: version 0 is not a whole number of at least 1"),
        ({"stdout": json.dumps({"ok": True, "data": {"item_id": KEY, "version": 1, "unchanged": False}, "error": None}),
          "status": 0},
         "answered what cannot be read: its event is not spec v1"),
        ({"stdout": json.dumps({"ok": True, "data": {"item_id": KEY, "version": 1, "unchanged": False, "event": {
            "payload": {"version": 2, "content_hash": "a" * 64}}}, "error": None}), "status": 0},
         "answered what cannot be read: its event is not spec v1"),
        ({"stdout": json.dumps({"ok": True, "data": {"item_id": KEY, "version": 1, "unchanged": False, "event": {
            "payload": {"version": 1, "content_hash": "A" * 64}}}, "error": None}), "status": 0},
         "answered what cannot be read: its event gives no SHA-256 content hash for spec v1"),
    ],
    ids=["refused", "refused with stderr", "no envelope", "lost", "no unchanged flag", "another item", "version 0",
         "no event", "event of another version", "content hash not a digest"],
)
def test_a_failing_spec_set_stops_the_open_by_name_before_the_card_is_read(repo, fake, path, answer, said):
    src = write_ticket(path)
    if "code" in answer:
        message = json.dumps({"ok": False, "data": None, "error": {"code": answer["code"], "message": answer["message"]}})
        fake.script("spec", stdout=message + "\n", status=3, stderr=answer.get("stderr"))
    else:
        fake.script("spec", stdout=answer["stdout"], status=answer["status"])
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_ticket(repo, src)
    assert refusal(excinfo) == "spec_set_failed"
    assert f"`{set_command(fake)}` {said}." in str(excinfo.value)
    assert "the pass does not plan from a ticket the ledger did not record" in str(excinfo.value)
    assert fake.calls("export-item") == [] and claims(fake) == [] and spec_calls(fake, "diff") == []
    # The stop came after the item's creation: a rerun creates nothing more and records the spec once.
    again = open_ticket(repo, src)
    assert again.live is True and again.reconciliation[1:] == ()
    assert [event["kind"] for event in fake.events()].count("item.created") == 1
    assert len(spec_calls(fake, "set")) == 2
    assert versions(fake) == [1]


@pytest.mark.parametrize(
    "description, said",
    [("", "answered usage: spec set needs a non-empty body"), ("A body with a \x00 byte.", "could not run (embedded null byte)")],
    ids=["empty description", "null byte"],
)
def test_a_description_the_ledger_cannot_take_stops_the_open_and_is_never_altered(repo, fake, path, description, said):
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_ticket(repo, write_ticket(path, description=description))
    assert refusal(excinfo) == "spec_set_failed"
    assert f"`{set_command(fake)}` {said}." in str(excinfo.value)
    assert len(spec_calls(fake, "set")) == (1 if description == "" else 0)
    assert fake.specs() == [] and claims(fake) == []


@pytest.mark.parametrize(
    "answer, said",
    [
        ({"code": "error", "message": "the diff failed"}, "answered error: the diff failed"),
        ({"data": {"item_id": KEY, "from": 1, "to": 2, "changed": ["body_md"]}},
         "answered what cannot be read as the diff of those two versions"),
        ({"data": {"item_id": KEY, "from": 2, "to": 3, "changed": [], "fields": []}},
         "answered what cannot be read as the diff of those two versions"),
        ({"data": {"item_id": KEY, "from": 1, "to": 2, "changed": ["body_md"], "fields": [{"field": "body_md", "diff": "-a"}]}},
         "answered what cannot be read as the diff of those two versions"),
        # The right newer version, but not from the version before it: the header would name the wrong older version.
        ({"data": {"item_id": KEY, "from": 2, "to": 2, "changed": [], "fields": []}},
         "answered what cannot be read as the diff of those two versions"),
        ({"data": {"item_id": KEY, "from": "1", "to": 2, "changed": [], "fields": []}},
         "answered what cannot be read as the diff of those two versions"),
    ],
    ids=["refused", "no fields", "other versions", "diff not a list", "from not the older version", "from as text"],
)
def test_a_failing_spec_diff_stops_the_open_and_a_rerun_records_nothing_more(repo, fake, path, answer, said):
    src = write_ticket(path)
    open_ticket(repo, src).end()
    write_ticket(path, description=EDITED)
    fake.script("spec", commit=True)  # the spec set lands as the ledger answers it
    if "code" in answer:
        fake.answer("spec", answer["code"], message=answer["message"])
    else:
        fake.script("spec", stdout=json.dumps({"ok": True, "data": answer["data"], "error": None}) + "\n", status=0)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_ticket(repo, src)
    assert refusal(excinfo) == "spec_diff_failed"
    command = shlex.join([str(fake.path), "spec", "diff", KEY, "1", "2"])
    assert f"spec v2 of ledger item {KEY} records the ticket as pulled now, but `{command}` {said}." in str(excinfo.value)
    assert versions(fake) == [1, 2] and len(claims(fake)) == 1
    again = open_ticket(repo, src)
    assert again.live is True and again.reconciliation[1:] == ()
    assert versions(fake) == [1, 2]


# --- The resume rules still hold after the intake ----------------------------


def test_a_changed_criterion_still_sends_the_pass_back_to_plan_and_the_diff_names_acceptance(repo, fake, path):
    src = write_ticket(path)
    session = open_ticket(repo, src)
    present(session)
    session.end()
    write_ticket(path, acceptance_criteria=[A, B_EDITED])
    resumed = open_ticket(repo, src)
    assert resumed.phase == "plan"
    printed = resumed.reconciliation[1].splitlines()
    assert printed[0] == (f"ledger spec diff {KEY} 1 2: the ticket changed since spec v1, and spec v2 records it "
                          "(changed: acceptance)")
    assert f"-- {B}" in printed and f"+- {B_EDITED}" in printed
    assert resumed.reconciliation[2:] == (F1(1, EDIT_WHAT["text"]),)


def test_a_closed_ticket_edited_after_close_records_the_version_and_stays_closed(repo, fake, path):
    src = write_ticket(path)
    session = open_ticket(repo, src)
    present(session)
    go_plan(session)
    session.begin_build()
    passing(session)
    go_outward(session)
    session.close("Ticket pass complete.", refs=("example/repo#14",), next_skills=("ticket",))
    write_ticket(path, description=EDITED, acceptance_criteria=[A, B_EDITED])
    reopened = open_ticket(repo, src)
    assert reopened.phase == "closed" and reopened.live is False
    assert versions(fake) == [1, 2]
    assert reopened.reconciliation[1].splitlines()[0] == (
        f"ledger spec diff {KEY} 1 2: the ticket changed since spec v1, and spec v2 records it (changed: body_md, acceptance)")
    assert reopened.reconciliation[2] == F7(EDIT_WHAT["text"])


# --- Concurrent writers ----------------------------------------------------------


def test_a_held_open_still_records_the_ticket_and_the_live_pass_then_writes_stale(repo, fake, path):
    src = write_ticket(path)
    live = open_ticket(repo, src)
    write_ticket(path, description=EDITED)
    with pytest.raises(ticket.TicketHeld):
        open_ticket(repo, src)
    assert versions(fake) == [1, 2]
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        live.record_tried("Run the suite.")
    assert refusal(excinfo) == "stale"
    resumed = open_ticket(repo, src)
    assert resumed.live is True and versions(fake) == [1, 2]


class RecordHook(ticket.LedgerStore):
    """A ledger handle that runs ``after_record`` once, right after its ``record_spec`` returns: before the card read."""

    def __init__(self, cli, key, title, after_record):
        super().__init__(cli, key, title)
        self._after_record = after_record

    def record_spec(self, pulled):
        result = super().record_spec(pulled)
        after, self._after_record = self._after_record, None
        if after is not None:
            after()
        return result


def another_open_records(fake, path):
    """What a second open of the edited ticket writes first: its own ``spec set``, through a handle of its own."""

    def record():
        edited = write_ticket(path.with_name("EK-14-edited.json"), description=EDITED).pull(KEY)
        ticket.LedgerStore(fake.path, KEY, TITLE).record_spec(edited)

    return record


@pytest.mark.parametrize("hook", [RecordHook, ReadHook], ids=["between the record and the card read", "after the card read"])
def test_a_newer_spec_another_open_records_while_this_one_opens_refuses_it_stale(repo, fake, path, hook):
    src = write_ticket(path)
    handle = hook(fake.path, KEY, TITLE, another_open_records(fake, path))
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.TicketPass.open(KEY, src, handle)
    assert refusal(excinfo) == "stale"
    assert "its spec moved from v1 to v2 while the pass opened" in str(excinfo.value)
    # The other open's ticket is the item's current spec, and this open took no claim, so it plans nothing.
    assert versions(fake) == [1, 2] and fake.specs()[-1]["payload"]["body_md"] == EDITED
    assert claims(fake) == []
    # Uncontended, the same ticket opens: it records itself again as v3 and prints the diff from the other open's v2.
    again = open_ticket(repo, src)
    assert again.live is True and versions(fake) == [1, 2, 3]
    assert again.reconciliation[1].splitlines()[0] == (
        f"ledger spec diff {KEY} 2 3: the ticket changed since spec v2, and spec v3 records it (changed: body_md)")


@pytest.mark.parametrize(
    "answer, code, said",
    [
        ({"code": "error", "message": "the read failed"}, "ledger_refused",
         f"the ledger refused to read the spec of ticket {KEY} (error: the read failed)"),
        ({"stdout": "Traceback (most recent call last): boom", "status": 1}, "ledger_refused",
         f"the ledger refused to read the spec of ticket {KEY} (no envelope, exit status 1: "
         "Traceback (most recent call last): boom)"),
        ({"data": {"item_id": "EK-15", "version": 1, "content_hash": "a" * 64}}, "item_unreadable",
         f"the ledger's `spec show` answer for item {KEY} cannot be read: it is not the spec of item {KEY}"),
        ({"data": {"item_id": KEY, "version": 1, "content_hash": "not a digest"}}, "item_unreadable",
         f"the ledger's `spec show` answer for item {KEY} cannot be read: its content_hash 'not a digest' is not a "
         "SHA-256 hex digest"),
    ],
    ids=["refused", "no envelope", "another item", "content hash not a digest"],
)
def test_a_spec_read_after_the_card_that_fails_refuses_the_open_and_never_falls_back_to_the_recorded_spec(
        repo, fake, path, answer, code, said):
    src = write_ticket(path)
    fake.script("spec", commit=True)  # the spec set lands as the ledger answers it
    if "code" in answer:
        fake.answer("spec", answer["code"], message=answer["message"])
    elif "data" in answer:
        fake.script("spec", stdout=json.dumps({"ok": True, "data": answer["data"], "error": None}) + "\n", status=0)
    else:
        fake.script("spec", stdout=answer["stdout"], status=answer["status"])
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_ticket(repo, src)
    assert refusal(excinfo) == code
    assert said in str(excinfo.value)
    assert [call.argv[2] for call in fake.calls("spec")] == ["set", "show"]
    assert versions(fake) == [1] and claims(fake) == []
    again = open_ticket(repo, src)
    assert again.live is True and versions(fake) == [1]

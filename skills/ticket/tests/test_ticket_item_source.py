"""The ledger-item source (design 6.5): ``/ticket ITEM-ID`` works an item filed in the ledger.

The item's title and status, its current spec (body and acceptance criteria) and its links are read
again on every open, and the pass keeps its card on that same item. The tests run against the fake
ledger CLI, which answers ``query --item``, ``spec show``, ``links``, ``export-item`` and
``append-many`` as ledger 0.1.22 does. The shared rule turns the ledger on through a fake ``ledger``
first on ``PATH`` with ``LEDGER_DB`` naming a file, in a ``HOME`` the test builds.
"""

import json
import shlex

import pytest

import ticket
from ledger_support import FakeLedger, ReadHook, machine
from ticket_support import F1, F2, F7, F10, KEY, TITLE, Source, changes, go_outward, go_plan, new_text, passing, present, refusal

A = "The item opens as a ticket with the spec's criteria."
B = "A changed spec sends the pass back to plan."
C = "Each open reads the spec again."
BULLETS = f"- {A}\n- {B}"
CHANGED = "the ticket's spec changed: v1 -> v2"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    fake = FakeLedger(tmp_path / "bin")
    machine(monkeypatch, tmp_path, fake)
    return fake


def source_for(fake):
    src = ticket.ledger_item_source(KEY)
    assert isinstance(src, ticket.LedgerItemSource) and src.cli == str(fake.path)
    return src


def open_item(repo, src, pass_id=None):
    """Open a pass the way SKILL.md says: a fresh ``choose_store`` handle for every open."""

    return ticket.TicketPass.open(KEY, src, ticket.choose_store(repo, KEY, TITLE), pass_id=pass_id)


def plan_records(fake):
    texts = [event["payload"]["text"] for event in fake.events() if event["kind"] == "item.note"]
    return [json.loads(text.split(" ", 1)[1]) for text in texts if text.startswith("ticket-plan:v2 ")]


def spec_data(fake, **change):
    """What ``spec show`` answers for the current spec, with ``change`` applied."""

    spec = [event for event in fake.events() if event["item_id"] == KEY and event["kind"] == "item.spec"][-1]
    data = {
        "item_id": KEY, "version": spec["payload"]["version"], "event_id": spec["id"],
        "recorded_at": spec["recorded_at"], "content_hash": spec["payload"]["content_hash"],
        "title": spec["payload"]["title"], "body_md": spec["payload"]["body_md"],
        "acceptance": spec["payload"].get("acceptance"), "size": None, "merge_class": None,
        "source": None, "source_rev": None,
    }
    data.update(change)
    return json.dumps({"data": data, "error": None, "ok": True}, sort_keys=True) + "\n"


# --- Opens, plans and resumes on the item's own events ---------------------


def test_an_item_with_a_spec_opens_plans_and_resumes_on_its_own_events(repo, fake):
    fake.new_item()
    fake.set_spec(BULLETS, body="Work the item from the ledger.")
    fake.link("url", "https://example.com/design")
    fake.new_item(title="The epic", item="EPIC-1")
    fake.link("blocks", KEY, item="EPIC-1")
    src = source_for(fake)

    session = open_item(repo, src)
    assert session.ticket == ticket.Ticket(
        key=KEY, title=TITLE, status="open", description="Work the item from the ledger.",
        acceptance_criteria=(A, B),
        links=({"kind": "url", "url": "https://example.com/design"}, {"kind": "blocks (incoming)", "url": "EPIC-1"}),
        updated_at="2026-11-12T10:00:01Z", spec=ticket.SpecVersion(1, fake.spec_hash()),
    )
    assert session.reconciliation[0].startswith("store: ledger (ledger on PATH with LEDGER_DB: ")
    present(session)
    assert [record["ticket_spec"] for record in plan_records(fake)] == [{"version": 1, "content_hash": fake.spec_hash()}]

    joined = open_item(repo, src, pass_id=session.pass_id)
    assert joined.phase == "plan_go" and joined.reconciliation[1:] == ()
    go_plan(joined)
    joined.begin_build()
    changes(joined)
    joined.end()

    resumed = open_item(repo, src)
    assert resumed.phase == "build" and resumed.reconciliation[1:] == ()
    assert resumed.plan.acceptance_criteria == (A, B)

    created = [event["item_id"] for event in fake.events() if event["kind"] == "item.created"]
    assert created == [KEY, "EPIC-1"]
    written = [event for event in fake.events() if event["payload"].get("source") == "ticket"]
    assert written and {event["item_id"] for event in written} == {KEY}
    # Every write went to the item itself, under its revision: the item is never created again.
    assert fake.appends() and all(call.argv[2] == KEY and call.argv[-1] != "none" for call in fake.appends())
    # Every open read the spec twice (the pull, then the check after the card read); nothing was cached.
    assert len(fake.calls("spec")) == 6


def test_an_item_with_no_spec_is_refused_by_name_with_the_command_that_gives_it_one(repo, fake):
    fake.new_item()
    src = source_for(fake)
    before = fake.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_item(repo, src)
    assert refusal(excinfo) == "no_spec"
    command = shlex.join([str(fake.path), "spec", "set", KEY, "--title", TITLE]) + " --body-file <body.md> --acceptance <criteria>"
    assert ticket.spec_set_command(str(fake.path), KEY, TITLE) == command
    assert f"`{command}`" in str(excinfo.value)
    assert fake.events() == before and fake.appends() == []
    assert fake.calls("export-item") == []


@pytest.mark.parametrize("acceptance", [None, "  \n "])
def test_a_spec_with_no_acceptance_criteria_is_refused_by_name_with_the_command(repo, fake, acceptance):
    fake.new_item()
    fake.set_spec(acceptance)
    src = source_for(fake)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_item(repo, src)
    assert refusal(excinfo) == "no_acceptance"
    assert ticket.spec_set_command(str(fake.path), KEY, TITLE) in str(excinfo.value)
    assert fake.appends() == []


# --- A changed spec sends the pass back to plan ------------------------------


@pytest.mark.parametrize("edit", ["acceptance", "body", "source only"])
def test_a_spec_changed_between_passes_sends_the_pass_back_to_plan(repo, fake, edit):
    fake.new_item()
    fake.set_spec(BULLETS)
    src = source_for(fake)  # one source object for every open: it must keep nothing between pulls
    session = open_item(repo, src)
    present(session)
    go_plan(session)
    session.begin_build()
    changes(session)
    session.end()
    unchanged = open_item(repo, src)
    assert unchanged.phase == "build"
    unchanged.end()

    if edit == "acceptance":
        fake.set_spec(f"{BULLETS}\n- {C}")
    elif edit == "body":
        fake.set_spec(BULLETS, body="The same criteria with another body.")
    else:
        fake.set_spec(BULLETS, source="roadmap.md#ITEM")
    if edit == "source only":
        assert fake.spec_hash() == plan_records(fake)[0]["ticket_spec"]["content_hash"]

    resumed = open_item(repo, src)
    assert resumed.phase == "plan"
    what = f"{new_text(C)}; {CHANGED}" if edit == "acceptance" else CHANGED
    assert resumed.reconciliation[1:] == (F1(1, what), F10)
    present(resumed)
    assert resumed.plan_revision == 2
    assert plan_records(fake)[-1]["ticket_spec"] == {"version": 2, "content_hash": fake.spec_hash()}
    go_plan(resumed)
    resumed.begin_build()
    assert resumed.review_round == 1


def test_a_spec_whose_content_changed_under_the_same_version_sends_the_pass_back_to_plan(repo, fake):
    """The ledger reports version 1 again with another content hash: the plan's snapshot no longer matches."""

    fake.new_item()
    fake.set_spec(BULLETS)
    src = source_for(fake)
    session = open_item(repo, src)
    present(session)
    go_plan(session)
    session.begin_build()
    changes(session)
    session.end()
    stored = plan_records(fake)[0]["ticket_spec"]
    other = "0" * 64
    assert stored == {"version": 1, "content_hash": fake.spec_hash()} and other != stored["content_hash"]

    for _ in range(2):  # the pull, then the check after the card read: both report the same version under another hash
        fake.script("spec", stdout=spec_data(fake, content_hash=other), status=0)
    resumed = open_item(repo, src)
    assert resumed.ticket.spec == ticket.SpecVersion(1, other)
    assert resumed.phase == "plan"
    # No F10 line: the item's update time did not move, only the spec's content under its version.
    assert resumed.reconciliation[1:] == (F1(1, "the ticket's spec v1 now holds other content"),)


@pytest.mark.parametrize("stage, phase", [("S3", "plan"), ("S6", "closed")])
def test_a_spec_changed_after_a_passing_round_or_the_close_flags_as_a_criteria_edit_does(repo, fake, stage, phase):
    fake.new_item()
    fake.set_spec(BULLETS)
    src = source_for(fake)
    session = open_item(repo, src)
    present(session)
    go_plan(session)
    session.begin_build()
    passing(session)
    if stage == "S6":
        go_outward(session)
        session.close("Ticket pass complete.", refs=("example/repo#14",), next_skills=("ticket",))
    else:
        session.end()
    fake.set_spec(BULLETS, body="Edited after the round.")
    before = len(fake.appends())
    resumed = open_item(repo, src)
    assert resumed.phase == phase
    flag = F2(1, 1, CHANGED) if stage == "S3" else F7(CHANGED)
    assert resumed.reconciliation[1] == flag
    if stage == "S6":
        assert len(fake.appends()) == before and not resumed.live


def test_a_plan_stored_without_a_spec_and_one_stored_with_a_spec_each_differ_from_the_other_source(repo, fake):
    tracker = Source(criteria=(A, B))
    session = open_item(repo, tracker)
    present(session)
    session.end()
    fake.set_spec(BULLETS)
    src = source_for(fake)
    resumed = open_item(repo, src)
    assert resumed.phase == "plan"
    assert F1(1, "the ticket now has spec v1 and the plan was stored without one") in resumed.reconciliation
    present(resumed)
    resumed.end()
    back = open_item(repo, tracker)
    assert back.phase == "plan"
    assert F1(2, "the plan was stored against spec v1 and the ticket source has no spec") in back.reconciliation


# --- Concurrent writers: a spec set while the pass opens or runs -------------


def test_a_spec_set_while_the_pass_opens_is_refused_stale_and_writes_nothing(repo, fake):
    fake.new_item()
    fake.set_spec(BULLETS)
    src = source_for(fake)
    handle = ReadHook(fake.path, KEY, TITLE, lambda: fake.set_spec(BULLETS, body="Edited while the pass opened."))
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.TicketPass.open(KEY, src, handle)
    assert refusal(excinfo) == "stale"
    assert "its spec moved from v1 to v2 while the pass opened" in str(excinfo.value)
    assert fake.appends() == []
    again = open_item(repo, src)
    assert again.ticket.spec.version == 2


class PullHook(ticket.LedgerItemSource):
    """A ledger-item source that runs ``after_pull`` once, right after its first pull returns."""

    def __init__(self, cli, after_pull):
        super().__init__(cli)
        self._after_pull = after_pull

    def pull(self, key):
        pulled = super().pull(key)
        after, self._after_pull = self._after_pull, None
        if after is not None:
            after()
        return pulled


def test_a_spec_set_between_the_pull_and_the_card_read_is_refused_stale(repo, fake):
    """The window the item's revision cannot see: the card read's token already holds the new spec."""

    fake.new_item()
    fake.set_spec(BULLETS)
    src = PullHook(source_for(fake).cli, lambda: fake.set_spec(BULLETS, body="Edited between the pull and the read."))
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_item(repo, src)
    assert refusal(excinfo) == "stale"
    assert "its spec moved from v1 to v2 while the pass opened" in str(excinfo.value)
    assert fake.appends() == []


def test_a_spec_set_during_a_live_pass_refuses_its_next_write_stale(repo, fake):
    fake.new_item()
    fake.set_spec(BULLETS)
    src = source_for(fake)
    session = open_item(repo, src)
    fake.set_spec(BULLETS, body="Edited during the pass.")
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        present(session)
    assert refusal(excinfo) == "stale"
    assert plan_records(fake) == []
    resumed = open_item(repo, src)
    assert resumed.phase == "orient" and resumed.ticket.spec.version == 2


# --- A spec read that fails never falls back ---------------------------------


@pytest.mark.parametrize("at", ["pull", "after the card read"])
@pytest.mark.parametrize(
    "answer, code",
    [
        ({"stdout": "", "status": 0}, "ledger_refused"),
        ({"stdout": "not an envelope\n", "status": 1}, "ledger_refused"),
        ({"code": "integrity"}, "ledger_refused"),
        ({"data": {"version": "2"}}, "item_unreadable"),
        ({"data": {"version": 0}}, "item_unreadable"),
        ({"data": {"version": True}}, "item_unreadable"),
        ({"data": {"version": None}}, "item_unreadable"),
        ({"data": {"content_hash": "abc"}}, "item_unreadable"),
        ({"data": {"item_id": "OTHER-1"}}, "item_unreadable"),
        ({"data": {"body_md": None}}, "item_unreadable"),
        ({"data": {"acceptance": 5}}, "item_unreadable"),
        ({"code": "usage"}, "ledger_incompatible"),
    ],
    ids=[
        "lost-answer", "no-envelope", "integrity", "version-text", "version-zero", "version-bool", "version-null",
        "hash-not-sha256", "other-item", "body-null", "acceptance-number", "usage",
    ],
)
def test_a_spec_read_that_fails_or_cannot_be_parsed_never_falls_back_to_an_earlier_spec(repo, fake, at, answer, code):
    fake.new_item()
    fake.set_spec(BULLETS)
    src = source_for(fake)
    session = open_item(repo, src)
    present(session)
    session.end()
    fake.set_spec(BULLETS, body="A second version.")
    if at != "pull":
        fake.script("spec", commit=True)  # the pull's read answers as the ledger does
    if "code" in answer:
        fake.answer("spec", answer["code"])
    elif "data" in answer:
        fake.script("spec", stdout=spec_data(fake, **answer["data"]), status=0)
    else:
        fake.script("spec", **answer)
    before = fake.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_item(repo, src)
    assert refusal(excinfo) == code
    assert fake.events() == before


# --- Which source /ticket ITEM-ID gets ---------------------------------------


def test_ledger_item_source_names_only_an_existing_item_while_the_ledger_is_on(repo, fake, monkeypatch):
    assert ticket.ledger_item_source(KEY) is None  # no such item: the key names no ticket, as before
    assert [call.argv for call in fake.calls("query")] == [["--json", "query", "--item", KEY]]
    fake.new_item()
    assert isinstance(ticket.ledger_item_source(KEY), ticket.LedgerItemSource)  # with or without a spec
    monkeypatch.setenv("TICKET_LEDGER_CLI", "none")
    calls = len(fake.calls())
    assert ticket.ledger_item_source(KEY) is None  # the ledger is off: its CLI is never run
    assert len(fake.calls()) == calls


def test_ledger_item_source_with_no_ledger_installed_is_not_found(tmp_path, monkeypatch):
    machine(monkeypatch, tmp_path)
    assert ticket.ledger_item_source(KEY) is None


@pytest.mark.parametrize("code, refused", [("missing_db", "ledger_refused"), ("usage", "ledger_incompatible")])
def test_ledger_item_source_refuses_a_ledger_that_cannot_answer(fake, code, refused):
    fake.new_item()
    fake.answer("query", code)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.ledger_item_source(KEY)
    assert refusal(excinfo) == refused


def test_ledger_item_source_refuses_an_unsafe_key(fake):
    with pytest.raises(ticket.TicketError):
        ticket.ledger_item_source("../EK-14")
    assert fake.calls("query") == []


def test_a_ledger_item_is_worked_only_through_the_ledger_store_of_that_item(repo, fake, tmp_path):
    fake.new_item()
    fake.set_spec(BULLETS)
    src = source_for(fake)
    other = FakeLedger(tmp_path / "other")
    for handle in (
        ticket.MarkdownStore(repo, KEY, TITLE),
        ticket.LedgerStore(fake.path, "OTHER-1", TITLE),
        ticket.LedgerStore(other.path, KEY, TITLE),
    ):
        calls = len(fake.calls())
        with pytest.raises(ticket.StoreUnavailable) as excinfo:
            ticket.TicketPass.open(KEY, src, handle)
        assert refusal(excinfo) == "source_store_mismatch"
        assert len(fake.calls()) == calls and other.calls() == []
    assert not (repo / ".ticket").exists()


# --- The acceptance grammar --------------------------------------------------


@pytest.mark.parametrize(
    "text, criteria",
    [
        (BULLETS, (A, B)),
        (f"- {A}\n\n- {B}\n", (A, B)),
        (f"-   {A}  \n- {B}", (A, B)),
        ("Two runs print the same bytes.", ("Two runs print the same bytes.",)),
        ("A first line\nand its second line.", ("A first line\nand its second line.",)),
        ("-A dash with no space is text.", ("-A dash with no space is text.",)),
        ("3.5 seconds is the limit.", ("3.5 seconds is the limit.",)),
    ],
)
def test_acceptance_text_in_either_form_gives_its_criteria(repo, fake, text, criteria):
    fake.new_item()
    fake.set_spec(text)
    assert open_item(repo, source_for(fake)).ticket.acceptance_criteria == criteria


@pytest.mark.parametrize(
    "text, reason",
    [
        (f"Criteria:\n- {A}\n- {B}", "line 1 is not a `- ` bullet at the start of the line, but other lines are bullets"),
        (f"- {A}\n  and it wraps", "line 2 is not a `- ` bullet at the start of the line, but other lines are bullets"),
        (f"-{A}\n- {B}", "line 1 is not a `- ` bullet at the start of the line, but other lines are bullets"),
        (f"- {A}\n+ {B}", "line 2 is not a `- ` bullet at the start of the line, but other lines are bullets"),
        (f"```\n- {A}\n```", "line 1 is not a `- ` bullet at the start of the line, but other lines are bullets"),
        (f"- {A}\n-   ", "line 2 is a bullet with no text"),
        (f"  - {A}\n  - {B}", "line 1 starts like a list item, but not with `- ` at the start of the line"),
        (f"* {A}\n* {B}", "line 1 starts like a list item, but not with `- ` at the start of the line"),
        (f"1. {A}\n2. {B}", "line 1 starts like a list item, but not with `- ` at the start of the line"),
        (f"1) {A}", "line 1 starts like a list item, but not with `- ` at the start of the line"),
        (f"-\t{A}", "line 1 starts like a list item, but not with `- ` at the start of the line"),
        ("- ", "line 1 is a bullet with no text"),
    ],
    ids=[
        "intro-line", "wrapped-continuation", "dash-no-space", "plus-among-bullets", "fence", "empty-bullet",
        "indented-bullet", "star-marker", "numbered-dot", "numbered-paren", "dash-tab", "only-an-empty-bullet",
    ],
)
def test_acceptance_text_near_a_form_is_refused_by_name(repo, fake, text, reason):
    fake.new_item()
    fake.set_spec(text)
    src = source_for(fake)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_item(repo, src)
    assert refusal(excinfo) == "acceptance_unparseable"
    assert reason in str(excinfo.value)
    assert fake.appends() == []


# --- The stored spec version is read strictly --------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        {"version": "1", "content_hash": "h"},
        {"version": 0, "content_hash": "h"},
        {"version": 1},
        {"version": 1, "content_hash": ""},
        {"version": 1, "content_hash": "h", "item": KEY},
        "v1",
    ],
)
def test_a_stored_plan_whose_spec_version_cannot_be_read_is_refused(repo, fake, stored):
    fake.new_item()
    fake.set_spec(BULLETS)
    src = source_for(fake)
    session = open_item(repo, src)
    present(session)
    session.end()
    record = {**plan_records(fake)[-1], "ticket_spec": stored}
    text = "ticket-plan:v2 " + json.dumps(record, sort_keys=True, separators=(",", ":"))
    fake.outside("item.note", {"text": text, "source": "ticket"})
    before = fake.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_item(repo, src)
    assert refusal(excinfo) == "card_unreadable"
    assert fake.events() == before

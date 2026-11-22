"""Matrix section N: the intake adapters of a ticket pass (an ad-hoc ask, and the tracker's address as a link).

Every source leaves the same thing on the ledger: one ticket item with a current spec that has acceptance
criteria, its links, and one lease claim held for this pass. An ad-hoc ask lands on the item its text names
(``ask-<12 hex>``, spec source ``ask:<12 hex>``), a ticket with a ``url`` carries one ``url`` link to it, and the
refusals are by name and write nothing. The rows the matrix marks (real) run against the fake ledger CLI always and
also against a real ledger CLI when ``LEDGER_REAL_CLI`` names one; every other row runs against the fake.
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

import ticket
from ledger_support import FakeLedger
from ticket_support import A, B, KEY, STATUS, TITLE, go_plan, present, refusal

LEDGER_REAL_CLI = os.environ.get("LEDGER_REAL_CLI", "")
BACKENDS = ["fake", *(["real"] if LEDGER_REAL_CLI else [])]

URL = "https://tracker.example/browse/EK-14"
OTHER_URL = "https://tracker.example/browse/EK-99"
ASK = "Add a retry to the sync job so one failed pull does not stop the run."
# Measured by hand: the first 12 hex digits of the SHA-256 of ASK, written out here and never computed from the code.
ASK_DIGEST = "22d2a3fa925c"
ASK_KEY = f"ask-{ASK_DIGEST}"
ASK_SOURCE = f"ask:{ASK_DIGEST}"
ASK_TITLE = "Retry a failed pull in the sync job"
ASK_BODY = "One failed pull stops the whole sync run; retry the pull before giving up."
ASK_CRITERIA = ["A failed pull is retried before the run stops.", "A pull that keeps failing still stops the run."]
DRAFTED = f"Drafted from the ad-hoc ask {ASK_SOURCE}: these criteria are a draft and bind only once the plan go approves them."


class Fake:
    def __init__(self, tmp_path):
        self.fake = FakeLedger(tmp_path / "ledger")
        self.path = self.fake.path

    def events(self, item):
        return [event for event in self.fake.events() if event["item_id"] == item]

    def new_item(self, item, kind="ticket"):
        self.fake.new_item(kind=kind, item=item)

    def link(self, item, rel, target):
        self.fake.link(rel, target, item=item)

    def spec(self, item, acceptance):
        self.fake.set_spec(acceptance, item=item)


class Real:
    """A real ledger CLI over a database of this test's own (``LEDGER_DB``)."""

    def __init__(self, tmp_path, monkeypatch):
        self.db = tmp_path / "ledger.db"
        monkeypatch.setenv("LEDGER_DB", str(self.db))
        monkeypatch.setenv("LEDGER_HARNESS", "ticket-tests")
        self.path = LEDGER_REAL_CLI
        # A read never creates the database, so the ledger holds another item first.
        self._append("EK-1", [{"kind": "item.created", "payload": {"type": "ticket", "title": "Another ticket"}}])

    def _run(self, *args, stdin=""):
        subprocess.run([self.path, "--json", *args], input=stdin, text=True, check=True, capture_output=True)

    def _append(self, item, batch):
        self._run("append-many", item, stdin=json.dumps(batch))

    def events(self, item):
        with sqlite3.connect(self.db) as database:
            rows = database.execute(
                "SELECT id, kind, payload, idempotency_key FROM events WHERE item_id = ? ORDER BY id", (item,)
            ).fetchall()
        return [{"id": i, "kind": k, "payload": json.loads(p), "idempotency_key": key} for i, k, p, key in rows]

    def new_item(self, item, kind="ticket"):
        self._append(item, [{"kind": "item.created", "payload": {"type": kind, "title": TITLE}}])

    def link(self, item, rel, target):
        self._run("link", item, "--rel", rel, "--target", target)

    def spec(self, item, acceptance):
        self._run("spec", "set", item, f"--title={TITLE}", "--body=Build the change the criteria describe.",
                  f"--acceptance={acceptance}")


@pytest.fixture(params=BACKENDS)
def backend(request, tmp_path, monkeypatch):
    chosen = Fake(tmp_path) if request.param == "fake" else Real(tmp_path, monkeypatch)
    monkeypatch.setenv("TICKET_LEDGER_CLI", str(chosen.path))
    return chosen


@pytest.fixture
def fake(tmp_path, monkeypatch):
    chosen = Fake(tmp_path)
    monkeypatch.setenv("TICKET_LEDGER_CLI", str(chosen.path))
    return chosen


class Tracker:
    """A tracker connector's stand-in: the ticket it pulls carries the tracker's address."""

    def __init__(self, url=URL):
        self.url = url

    def pull(self, key):
        return ticket.Ticket.from_mapping(
            {
                "key": key, "title": TITLE, "status": STATUS, "description": "Run one ticket.",
                "acceptance_criteria": [A, B], "url": self.url, "links": [],
            }
        )


def json_ticket(tmp_path, url=URL):
    path = tmp_path / "EK-14.json"
    path.write_text(json.dumps({
        "key": KEY, "title": TITLE, "status": STATUS, "description": "Run one ticket.",
        "acceptance_criteria": [A, B], "url": url,
    }), encoding="utf-8")
    return ticket.FileTicketSource(path)


def ask_source(ask=ASK, **change):
    draft = {"title": ASK_TITLE, "description": ASK_BODY, "acceptance": ASK_CRITERIA, **change}
    return ticket.AskTicketSource(ask, **draft)


def open_pass(repo, src, key=None, pass_id=None, holder=None):
    """Open a pass the way SKILL.md says: a fresh ``choose_store`` handle for every open."""

    key = key or src.key
    return ticket.TicketPass.open(key, src, ticket.choose_store(repo, key, TITLE), pass_id=pass_id, holder=holder)


def of_kind(events, kind):
    return [event for event in events if event["kind"] == kind]


def url_links(events):
    return [event["payload"] for event in events if event["kind"] == "item.linked" and event["payload"]["rel"] == "url"]


def leases(events):
    return [event["payload"] for event in events if event["kind"] == "item.state" and event["payload"].get("status") == "claimed"]


def pass_records(events):
    return [json.loads(event["payload"]["text"].split(" ", 1)[1]) for event in events
            if event["kind"] == "item.note" and event["payload"]["text"].startswith("ticket-pass:")]


def link_key(url):
    return f"ticket-link/{KEY}/url/{hashlib.sha256(url.encode()).hexdigest()[:16]}"


# --- N1-N2: the ask's identity ----------------------------------------------------------------------------


def test_N1_an_asks_key_and_source_are_the_first_12_hex_of_the_sha256_of_its_text_as_written():
    assert ticket.ask_key(ASK) == ASK_KEY and ticket.ask_spec_source(ASK) == ASK_SOURCE
    assert hashlib.sha256(ASK.encode("utf-8")).hexdigest()[:12] == ASK_DIGEST
    assert re.fullmatch(r"ask-[0-9a-f]{12}", ticket.ask_key(ASK)) and re.fullmatch(r"ask:[0-9a-f]{12}", ASK_SOURCE)
    # The text as written: a trailing newline, another case and another character are other asks.
    others = {ticket.ask_key(text) for text in (ASK + "\n", ASK.upper(), ASK + ".", ASK.replace("a", "а"))}
    assert len(others) == 4 and ASK_KEY not in others
    source = ask_source()
    assert (source.key, source.ask_source) == (ASK_KEY, ASK_SOURCE)
    assert ticket.intake_source(source.pull(ASK_KEY)) == ASK_SOURCE


@pytest.mark.parametrize("given", [ASK_KEY.upper(), "ask-" + "0" * 12, KEY, ASK_SOURCE, ""], ids=[
    "upper case", "another ask's key", "a tracker key", "the source as the key", "empty"])
def test_N1_the_ask_is_pulled_only_under_the_key_its_text_names(given):
    with pytest.raises(ticket.TicketError, match="is item"):
        ask_source().pull(given)


def test_N2_the_same_ask_lands_on_the_same_item_on_every_try(repo, backend):
    first = open_pass(repo, ask_source())
    first.end()
    second = open_pass(repo, ask_source())
    events = backend.events(ASK_KEY)
    assert len(of_kind(events, "item.created")) == 1
    assert [event["payload"]["version"] for event in of_kind(events, "item.spec")] == [1]
    assert first.key == second.key == ASK_KEY and second.live is True
    # Another ask is another item.
    other = open_pass(repo, ticket.AskTicketSource(ASK + " Also log each retry.", title=ASK_TITLE, description=ASK_BODY,
                                                   acceptance=ASK_CRITERIA))
    assert other.key != ASK_KEY and len(of_kind(backend.events(other.key), "item.created")) == 1
    assert len(of_kind(backend.events(ASK_KEY), "item.created")) == 1


# --- N3-N4: every adapter leaves the same thing -----------------------------------------------------------


@pytest.mark.parametrize("adapter", ["tracker key", "json file", "ledger id", "ad-hoc ask"])
def test_N3_every_adapter_leaves_one_ticket_item_a_current_spec_its_links_and_one_lease_claim(repo, tmp_path, backend, adapter):
    """The invariant (real): the same facts on the item whichever adapter ran."""

    links = [URL]
    if adapter == "tracker key":
        src, key = Tracker(), KEY
    elif adapter == "json file":
        src, key = json_ticket(tmp_path), KEY
    elif adapter == "ledger id":
        backend.new_item(KEY)
        backend.spec(KEY, f"- {A}\n- {B}")
        src, key, links = ticket.ledger_item_source(KEY), KEY, []
    else:
        src, key, links = ask_source(), ASK_KEY, []
    session = open_pass(repo, src, key)
    events = backend.events(key)
    (created,) = of_kind(events, "item.created")
    assert created["payload"]["type"] == "ticket"
    spec = of_kind(events, "item.spec")[-1]["payload"]
    assert spec["acceptance"].strip() and ticket._parse_acceptance(spec["acceptance"]) == list(session.ticket.acceptance_criteria)
    assert [link["target"] for link in url_links(events)] == links
    (lease,) = leases(events)
    holder = f"ticket-pass/{session.pass_id}"
    assert (lease["owner"], lease["claim"]) == (holder, holder)
    (record,) = pass_records(events)
    assert (record["id"], record["holder"], record["lease"]) == (session.pass_id, holder, "taken")
    assert session.live is True


def test_N4_the_spec_of_an_ask_holds_the_draft_and_the_ask_source_alone(repo, backend):
    open_pass(repo, ask_source())
    (spec,) = of_kind(backend.events(ASK_KEY), "item.spec")
    payload = spec["payload"]
    assert (payload["version"], payload["title"], payload["body_md"], payload["source"]) == (1, ASK_TITLE, ASK_BODY, ASK_SOURCE)
    assert payload["acceptance"] == "- " + "\n- ".join(ASK_CRITERIA)
    assert of_kind(backend.events(ASK_KEY), "item.linked") == []


# --- N5-N6: the drafted acceptance binds only through the plan go -----------------------------------------


def test_N5_the_drafted_acceptance_is_shown_on_the_plan_card_and_binds_only_through_the_plan_go(repo, fake):
    session = open_pass(repo, ask_source())
    present(session)
    card = session.plan.render()
    assert DRAFTED in card.splitlines()
    assert all(f"- [ ] passes: false - {text}" in card.splitlines() for text in ASK_CRITERIA)
    assert session.approved("plan") is False
    with pytest.raises(ticket.GateRequired):
        session.begin_build()
    go_plan(session)
    session.begin_build()
    assert session.approved("plan") is True
    # A later session shows the same mark on the stored plan, and no other ticket's plan carries it.
    session.end()
    resumed = open_pass(repo, ask_source())
    assert DRAFTED in resumed.plan.render().splitlines()
    plain = ticket.PlanCard(Tracker().pull(KEY)).render()
    assert "Drafted from" not in plain


def test_N6_a_redrafted_acceptance_is_not_covered_by_the_earlier_plan_go(repo, fake):
    session = open_pass(repo, ask_source())
    present(session)
    go_plan(session)
    session.begin_build()
    session.end()
    redrafted = ask_source(acceptance=[ASK_CRITERIA[0], "A pull that keeps failing is reported with its last error."])
    again = open_pass(repo, redrafted)
    assert [event["payload"]["version"] for event in of_kind(fake.events(ASK_KEY), "item.spec")] == [1, 2]
    assert again.phase == "plan"
    with pytest.raises(ticket.TicketError):
        again.begin_build()
    # The redraft is a new plan revision: it binds only through a plan go of its own.
    present(again)
    assert again.plan_revision == 2 and again.phase == "plan_go" and again.approved("plan") is False
    with pytest.raises(ticket.GateRequired):
        again.begin_build()
    go_plan(again)
    again.begin_build()


# --- N7-N10: refused by name, nothing written -------------------------------------------------------------


@pytest.mark.parametrize("ask, code", [
    ("", "ask_empty"), (" \n\t ", "ask_empty"), (None, "ask_empty"), (7, "ask_empty"), (b"ask", "ask_empty"),
    ("\ud800 lone surrogate", "ask_not_utf8"),
], ids=["empty", "blank", "none", "number", "bytes", "not utf-8"])
def test_N7_an_ask_that_is_empty_or_has_no_identity_is_refused_by_name_and_writes_nothing(fake, ask, code):
    before = fake.fake.calls()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.AskTicketSource(ask, title=ASK_TITLE, description=ASK_BODY, acceptance=ASK_CRITERIA)
    assert refusal(excinfo) == code
    with pytest.raises(ticket.StoreUnavailable) as again:
        ticket.ask_key(ask)
    assert refusal(again) == code
    assert fake.fake.calls() == before and fake.fake.events() == []


@pytest.mark.parametrize("change", [
    {"title": ""}, {"title": "   "}, {"title": "two\nlines"}, {"title": None},
    {"description": ""}, {"description": "  \n"}, {"description": None},
    {"acceptance": []}, {"acceptance": ()}, {"acceptance": "one criterion as a string"}, {"acceptance": None},
    {"acceptance": [ASK_CRITERIA[0], ""]}, {"acceptance": [ASK_CRITERIA[0], "  "]},
    {"acceptance": [ASK_CRITERIA[0], "two\nlines"]}, {"acceptance": [ASK_CRITERIA[0], "two\rlines"]},
    {"acceptance": [ASK_CRITERIA[0], 7]},
], ids=lambda change: "-".join(f"{name}={value!r}" for name, value in change.items()))
def test_N8_a_draft_that_cannot_be_a_ticket_is_refused_by_name_and_writes_nothing(fake, change):
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ask_source(**change)
    assert refusal(excinfo) == "ask_draft_invalid" and ASK_SOURCE in str(excinfo.value)
    assert fake.fake.calls() == [] and fake.fake.events() == []


@pytest.mark.parametrize("kind", ["initiative", "decision", "topic"])
def test_N9_an_item_with_the_asks_id_that_is_not_a_ticket_is_refused_and_nothing_is_written(repo, fake, kind):
    fake.new_item(ASK_KEY, kind=kind)
    before = fake.events(ASK_KEY)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_pass(repo, ask_source())
    assert refusal(excinfo) == "ask_item_not_ticket" and ASK_SOURCE in str(excinfo.value) and kind in str(excinfo.value)
    assert fake.events(ASK_KEY) == before
    assert fake.fake.calls("spec") == [] and fake.fake.appends() == []


def test_N9_an_item_with_the_asks_id_that_is_a_ticket_is_taken(repo, fake):
    fake.new_item(ASK_KEY)
    session = open_pass(repo, ask_source())
    assert session.live is True and len(of_kind(fake.events(ASK_KEY), "item.created")) == 1


def test_N10_an_ask_opens_only_on_the_store_of_its_own_item(repo, fake):
    src = ask_source()
    other = ticket.choose_store(repo, KEY, TITLE)
    before = fake.fake.calls()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.TicketPass.open(src.key, src, other)
    assert refusal(excinfo) == "source_store_mismatch"
    with pytest.raises(ticket.TicketError, match="is item"):
        ticket.TicketPass.open(KEY, src, other)
    assert fake.fake.calls() == before and fake.fake.events() == []


def test_N11_an_ask_runs_on_the_markdown_store_with_no_ledger_call(repo, fake, monkeypatch):
    monkeypatch.setenv("TICKET_LEDGER_CLI", "none")
    session = open_pass(repo, ask_source())
    assert isinstance(session.store, ticket.MarkdownStore) and session.live is True
    assert (repo / ".ticket" / f"{ASK_KEY}.md").exists()
    present(session)
    assert DRAFTED in session.plan.render().splitlines()
    assert fake.fake.calls() == []


def test_N12_a_second_pass_on_the_same_ask_is_refused_while_the_first_is_live(repo, fake):
    first = open_pass(repo, ask_source())
    before = fake.events(ASK_KEY)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_pass(repo, ask_source())
    assert refusal(excinfo) == "held" and first.pass_id in str(excinfo.value)
    assert fake.events(ASK_KEY) == before and first.live is True
    # Once the first pass ends, another pass opens on the same item: still one item.
    first.end()
    again = open_pass(repo, ask_source())
    assert again.live is True and len(of_kind(fake.events(ASK_KEY), "item.created")) == 1


# --- N13-N19: the tracker's address as a url link ---------------------------------------------------------


@pytest.mark.parametrize("adapter", ["tracker key", "json file"])
def test_N13_a_tickets_url_is_written_once_as_a_url_link_beside_the_spec_source(repo, tmp_path, backend, adapter):
    """(real) The link lands in the item's creation batch, as ``item.linked`` rel url with the address as target, once."""

    src = Tracker() if adapter == "tracker key" else json_ticket(tmp_path)
    open_pass(repo, src, KEY).end()
    open_pass(repo, src, KEY).end()
    events = backend.events(KEY)
    (link,) = of_kind(events, "item.linked")
    assert (link["payload"]["rel"], link["payload"]["target"]) == ("url", URL)
    assert link["idempotency_key"] == link_key(URL)
    assert of_kind(events, "item.spec")[-1]["payload"]["source"] == f"ticket:{KEY} {URL}"
    if isinstance(backend, Fake):
        first = [call for call in backend.fake.appends() if "item.linked" in call.stdin][0]
        assert [element["kind"] for element in first.batch] == ["item.created", "item.linked"]
        assert first.argv[-2:] == ["--expect-revision", "none"]
        links = [call for call in backend.fake.calls("links")]
        assert [call.argv for call in links] == [["--json", "links", KEY]]


def test_N14_an_item_filed_without_the_link_gets_it_under_the_revision_the_open_read(repo, fake):
    fake.new_item(KEY)
    revision = str(fake.events(KEY)[-1]["id"])
    open_pass(repo, Tracker(), KEY)
    (link_call,) = [call for call in fake.fake.appends() if "item.linked" in call.stdin]
    assert [element["kind"] for element in link_call.batch] == ["item.linked"]
    assert link_call.argv[-2:] == ["--expect-revision", revision]
    assert url_links(fake.events(KEY)) == [{"rel": "url", "target": URL, "source": "ticket"}]


def test_N15_an_item_that_already_links_the_address_gets_no_second_link(repo, backend):
    backend.new_item(KEY)
    backend.link(KEY, "url", URL)
    open_pass(repo, Tracker(), KEY)
    assert [link["target"] for link in url_links(backend.events(KEY))] == [URL]
    assert len(of_kind(backend.events(KEY), "item.linked")) == 1


def test_N16_a_url_link_to_another_address_is_refused_by_name_and_writes_nothing(repo, backend):
    """(real) Not the creation, the link, the spec nor the claim: the item is byte for byte as it was."""

    backend.new_item(KEY)
    backend.link(KEY, "url", OTHER_URL)
    before = backend.events(KEY)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_pass(repo, Tracker(), KEY)
    assert refusal(excinfo) == "url_link_conflict" and OTHER_URL in str(excinfo.value) and URL in str(excinfo.value)
    assert backend.events(KEY) == before


def test_N16_a_link_of_another_relation_to_the_address_is_not_a_url_link(repo, backend):
    backend.new_item(KEY)
    backend.link(KEY, "pr", "https://github.com/owner/repo/pull/12")
    open_pass(repo, Tracker(), KEY)
    assert [link["target"] for link in url_links(backend.events(KEY))] == [URL]


def test_N17_a_writer_racing_the_link_makes_it_stale_and_the_next_open_writes_it_once(repo, fake):
    fake.new_item(KEY)
    fake.fake.script("append-many", before=[
        {"kind": "item.note", "payload": {"text": "an outsider writes first"}, "item_id": KEY, "key": "outsider-1"}])
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_pass(repo, Tracker(), KEY)
    assert refusal(excinfo) == "stale"
    assert url_links(fake.events(KEY)) == [] and of_kind(fake.events(KEY), "item.spec") == []
    open_pass(repo, Tracker(), KEY)
    assert len(url_links(fake.events(KEY))) == 1


def test_N18_a_stop_after_the_link_leaves_it_and_the_rerun_writes_it_once(repo, fake):
    fake.fake.answer("spec", "validation")
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_pass(repo, Tracker(), KEY)
    assert refusal(excinfo) == "spec_set_failed"
    assert len(url_links(fake.events(KEY))) == 1 and of_kind(fake.events(KEY), "item.spec") == []
    session = open_pass(repo, Tracker(), KEY)
    events = fake.events(KEY)
    assert session.live is True and len(url_links(events)) == 1 and len(of_kind(events, "item.created")) == 1
    assert len(of_kind(events, "item.spec")) == 1


def test_N19_a_lost_answer_of_the_link_write_is_replayed_by_its_key(repo, fake):
    fake.new_item(KEY)
    fake.fake.script("append-many", stdout="", status=0, commit=True)
    session = open_pass(repo, Tracker(), KEY)
    assert session.live is True
    assert len(url_links(fake.events(KEY))) == 1
    assert [call.status for call in fake.fake.appends() if "item.linked" in call.stdin] == [0, 0]


def test_N20_a_ticket_with_no_url_and_a_ledger_id_write_no_link_and_ask_no_links(repo, tmp_path, fake):
    open_pass(repo, Tracker(url=None), KEY).end()
    assert of_kind(fake.events(KEY), "item.linked") == [] and fake.fake.calls("links") == []
    fake.new_item("EK-15")
    fake.spec("EK-15", f"- {A}\n- {B}")
    session = open_pass(repo, ticket.ledger_item_source("EK-15"), "EK-15")
    assert session.live is True and of_kind(fake.events("EK-15"), "item.linked") == []
    assert [call.argv[1] for call in fake.fake.calls("links")] == ["links"]
    assert [call.argv[2] for call in fake.fake.calls("links")] == ["EK-15"]


def test_N21_a_ledger_id_keeps_todays_refusals_and_writes_nothing_but_the_claim(repo, fake):
    fake.new_item("EK-15")
    before = fake.events("EK-15")
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_pass(repo, ticket.ledger_item_source("EK-15"), "EK-15")
    assert refusal(excinfo) == "no_spec" and fake.events("EK-15") == before
    fake.spec("EK-15", "")
    before = fake.events("EK-15")
    with pytest.raises(ticket.StoreUnavailable) as again:
        open_pass(repo, ticket.ledger_item_source("EK-15"), "EK-15")
    assert refusal(again) == "no_acceptance" and fake.events("EK-15") == before
    # An item with a spec: the open writes its claim and nothing else, no spec version and no link.
    fake.new_item("EK-16")
    fake.spec("EK-16", f"- {A}\n- {B}")
    before = fake.events("EK-16")
    session = open_pass(repo, ticket.ledger_item_source("EK-16"), "EK-16")
    assert [event["kind"] for event in fake.events("EK-16")[len(before):]] == ["item.state", "item.note"]
    assert len(of_kind(fake.events("EK-16"), "item.spec")) == 1 and session.live is True


# --- N22: the skill's text ---------------------------------------------------------------------------------


def test_N22_skill_md_takes_an_ad_hoc_ask_and_no_longer_sends_a_change_with_no_ticket_elsewhere():
    text = (Path(ticket.__file__).parent / "SKILL.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    front = " ".join(text.split("---")[1].split())
    assert "NOT for a change that has no ticket" not in flat and "use /tdd or /debug" not in front
    assert "NOT for a throwaway question" in front and "NOT for a session resume note (use /handoff)" in front
    assert "### An ad-hoc ask as the ticket" in text
    for needle in ("ticket.AskTicketSource(", "`ask-<12 hex>`", "`ask:<12 hex>`", "ask_empty", "ask_draft_invalid",
                   "ask_item_not_ticket", "url_link_conflict", "Drafted from the ad-hoc ask", "bind only through the plan go",
                   "A throwaway question", "one ticket item with a current spec that has acceptance criteria"):
        assert needle in flat, needle

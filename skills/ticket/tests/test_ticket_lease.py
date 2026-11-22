"""Matrix section M: the ledger lease a ticket pass takes or joins, and the old claim shape beside it.

A new pass on the ledger store takes the item's lease as its holder, or joins it when the item is already
leased to that holder, in the one claim batch that also carries its pass record. The rows the matrix marks
``(real)`` run against the fake ledger CLI always and also against a real ledger CLI when ``LEDGER_REAL_CLI``
names one (the backend list below then gains it); every other row runs against the fake.
"""

import json
import os
import re
import sqlite3
import subprocess
import threading
import time

import pytest

import ticket
from ledger_support import FakeLedger, build_ledger, open_ledger
from ticket_support import KEY, TITLE, Source, build, refusal

LEDGER_REAL_CLI = os.environ.get("LEDGER_REAL_CLI", "")
# The backends a (real) row runs on: the fake always, and the real ledger CLI when LEDGER_REAL_CLI names one.
BACKENDS = ["fake", *(["real"] if LEDGER_REAL_CLI else [])]
# Lease stamps far from any clock this suite runs under: one still live, one long expired.
LIVE = "2999-01-01T00:00:00.000Z"
PAST = "2000-01-01T00:00:00.000Z"
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


class Fake:
    def __init__(self, tmp_path):
        self.fake = FakeLedger(tmp_path / "ledger")
        self.path = self.fake.path

    def events(self):
        return [event for event in self.fake.events() if event["item_id"] == KEY]

    def write(self, kind, payload):
        """An outsider's event on the ticket's item."""

        self.fake.outside(kind, payload)

    def new_item(self):
        self.fake.new_item()


class Real:
    """A real ledger CLI over a database of this test's own (``LEDGER_DB``)."""

    def __init__(self, tmp_path, monkeypatch):
        self.db = tmp_path / "ledger.db"
        monkeypatch.setenv("LEDGER_DB", str(self.db))
        monkeypatch.setenv("LEDGER_HARNESS", "ticket-tests")
        self.path = LEDGER_REAL_CLI
        # A read never creates the database, so the ledger holds another item first.
        self._append("EK-1", [{"kind": "item.created", "payload": {"type": "ticket", "title": "Another ticket"}}])

    def _append(self, item, batch):
        subprocess.run([self.path, "--json", "append-many", item], input=json.dumps(batch), text=True, check=True,
                       capture_output=True)

    def events(self):
        with sqlite3.connect(self.db) as database:
            rows = database.execute(
                "SELECT id, kind, payload, idempotency_key FROM events WHERE item_id = ? ORDER BY id", (KEY,)
            ).fetchall()
        return [{"id": i, "kind": k, "payload": json.loads(p), "idempotency_key": key} for i, k, p, key in rows]

    def write(self, kind, payload):
        self._append(KEY, [{"kind": kind, "payload": payload}])

    def new_item(self):
        self.write("item.created", {"type": "ticket", "title": TITLE})


@pytest.fixture(params=BACKENDS)
def backend(request, tmp_path, monkeypatch):
    return Fake(tmp_path) if request.param == "fake" else Real(tmp_path, monkeypatch)


@pytest.fixture
def fake(tmp_path):
    return Fake(tmp_path)


def holder_of(session):
    return f"ticket-pass/{session.pass_id}"


def states(events):
    return [event["payload"] for event in events if event["kind"] == "item.state"]


def pass_records(events):
    return [event for event in events if event["kind"] == "item.note" and event["payload"]["text"].startswith("ticket-pass:")]


def record_of(event):
    return json.loads(event["payload"]["text"].split(" ", 1)[1])


def row(backend):
    answer = subprocess.run([str(backend.path), "--json", "query", "--item", KEY], capture_output=True, text=True,
                            check=True)
    return json.loads(answer.stdout)["data"]["items"][0]


def lease(holder, expires=LIVE, owner=True):
    """A lease of ``holder``: the shape the ledger's claim verb writes, or with ``owner=False`` the loop's take."""

    return {"status": "claimed", **({"owner": holder} if owner else {}), "claim": holder, "expires_at": expires}


def old_shape_pass(repo, cli, stage, rounds=1):
    """A pass of the old claim shape, live on the ledger at ``stage``: built on the markdown card, then imported.

    The import is the one path that still writes a ``session.opened`` claim. The item's spec is then recorded,
    as an open records it, so a later open of the same ticket records nothing more.
    """

    session = build(repo, Source(), stage, rounds)
    assert ticket.import_repo(repo, str(cli), say=lambda line: None) == 0
    ticket.LedgerStore(cli, KEY, TITLE).record_spec(Source().pull(KEY))
    return session.pass_id


# --- M1-M4: a new pass takes the lease, races, meets another holder, names a holder -------------------------


@pytest.mark.parametrize("named", [None, "seat-a"], ids=["default holder", "named holder"])
def test_M1_a_new_pass_takes_the_lease_with_its_pass_record_in_one_batch(backend, named):
    before = len(backend.events())
    session = open_ledger(backend.path, Source(), holder=named)
    holder = named or holder_of(session)
    claim = backend.events()[before:][-2:]
    assert [event["kind"] for event in claim] == ["item.state", "item.note"]
    assert claim[1]["id"] == claim[0]["id"] + 1
    taken = claim[0]["payload"]
    assert (taken["status"], taken["owner"], taken["claim"]) == ("claimed", holder, holder)
    assert STAMP.fullmatch(taken["expires_at"]) and "prior_holder" not in taken
    assert claim[1]["idempotency_key"] == f"ticket-pass/{KEY}/{session.pass_id}/record"
    record = record_of(claim[1])
    assert set(record) == {"id", "host", "started_at", "holder", "lease"}
    assert (record["id"], record["holder"], record["lease"]) == (session.pass_id, holder, "taken")
    assert record["host"] == ticket._host() and record["started_at"] <= time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    assert "session.opened" not in [event["kind"] for event in backend.events()]
    current = row(backend)
    assert (current["status"], current.get("owner"), current["revision"]) == ("claimed", holder, claim[1]["id"])
    if isinstance(backend, Fake):
        (sent,) = [call for call in backend.fake.appends() if "item.note" in call.stdin and "ticket-pass:" in call.stdin]
        assert [element["kind"] for element in sent.batch] == ["item.state", "item.note"]
        assert sent.argv[-2] == "--expect-revision"


def test_M3_another_holders_live_lease_refuses_the_open_and_writes_nothing(backend):
    src = Source()
    open_ledger(backend.path, src).end()
    backend.write("item.state", lease("seat-b"))
    before = backend.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_ledger(backend.path, src)
    assert refusal(excinfo) == "lease_held"
    assert "seat-b" in str(excinfo.value)
    assert backend.events() == before
    assert (row(backend)["status"], row(backend).get("owner")) == ("claimed", "seat-b")


def test_M3_a_claim_sent_under_a_revision_the_handle_did_not_read_is_stale(fake):
    open_ledger(fake.path, Source()).end()
    handle = fake.fake.store()
    handle.read()
    fake.write("item.note", {"text": "see PR 12"})
    newer = str(fake.events()[-1]["id"])
    before, appends = fake.events(), len(fake.fake.appends())
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        handle.apply(newer, [("claim", {"id": "0123456789abcdef", "host": "here", "started_at": "2026-11-22T00:00:00Z"})])
    assert refusal(excinfo) == "stale"
    assert fake.events() == before and len(fake.fake.appends()) == appends


@pytest.mark.parametrize("holder", ["", "   ", "seat a", "seat\nb", "x" * 201, 7], ids=[
    "empty", "blank", "a space", "a line break", "too long", "not text"])
def test_M4_a_holder_that_is_not_a_name_is_refused_before_any_cli_call(fake, holder):
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_ledger(fake.path, Source(), holder=holder)
    assert refusal(excinfo) == "holder_invalid"
    assert fake.fake.calls() == []


# --- M5: a holder that already holds the item joins its lease ---------------------------------------------


def test_M5_a_pass_joins_its_holders_lease_and_leaves_it_held_at_the_end(backend):
    backend.new_item()
    backend.write("item.state", lease("seat-h", owner=False))
    before = len(backend.events())
    session = open_ledger(backend.path, Source(), holder="seat-h")
    opened = backend.events()[before:]
    assert states(opened) == []
    (record,) = [record_of(event) for event in pass_records(opened)]
    assert (record["holder"], record["lease"]) == ("seat-h", "joined")
    session.end()
    ended = backend.events()[before + len(opened):]
    assert [event["kind"] for event in ended] == ["session.closed"]
    current = row(backend)
    assert (current["status"], current.get("claim")) == ("claimed", "seat-h")


# --- M6-M8: the old claim shape and the lease refuse each other ------------------------------------------


@pytest.mark.parametrize("holder", [None, "seat-x"], ids=["default holder", "named holder"])
def test_M6_a_live_old_shape_claim_holds_the_item_against_every_new_pass(fake, holder):
    # The old shape's claim event, written straight into the ledger as an earlier release of this skill wrote it.
    pass_id = "0123456789abcdef"
    fake.new_item()
    fake.fake.outside("session.opened", {"purpose": f"ticket pass {pass_id} on here at 2026-11-01T00:00:00Z", "source": "ticket"},
                      key=f"ticket-pass/{KEY}/{pass_id}/claimed")
    ticket.LedgerStore(fake.path, KEY, TITLE).record_spec(Source().pull(KEY))
    before = fake.events()
    with pytest.raises(ticket.TicketHeld) as excinfo:
        open_ledger(fake.path, Source(), holder=holder)
    assert excinfo.value.holder["id"] == pass_id
    assert ticket.release_command(KEY, pass_id) in str(excinfo.value)
    assert fake.events() == before


def test_M7_another_holders_lease_refuses_the_resume_of_an_old_shape_pass(repo, backend):
    pass_id = old_shape_pass(repo, backend.path, "S4", 1)
    backend.write("item.state", lease("seat-b"))
    before = backend.events()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_ledger(backend.path, Source(), pass_id=pass_id)
    assert refusal(excinfo) == "lease_held"
    assert "seat-b" in str(excinfo.value)
    assert backend.events() == before
    card, _ = ticket.LedgerStore(backend.path, KEY, TITLE).read()
    assert (card["status"], card["pass"]["id"]) == ("open", pass_id)
    assert row(backend)["status"] == "claimed"


def test_M8_an_old_shape_pass_with_no_lease_resumes_takes_no_lease_and_closes(repo, fake):
    pass_id = old_shape_pass(repo, fake.path, "S4", 1)
    before = len(fake.events())
    resumed = open_ledger(fake.path, Source(), pass_id=pass_id)
    assert (resumed.phase, resumed.live) == ("outward_go", True)
    assert fake.events()[before:] == []
    resumed.close("Ticket pass complete.", refs=("example/repo#14",), next_skills=("ticket",))
    assert resumed.phase == "closed"
    assert states(fake.events()) == [{"status": "closed", "source": "ticket"}]


# --- M9: a resume of a new-shape pass keeps, renews or refuses the lease ----------------------------------


@pytest.mark.parametrize("case", ["live and own", "expired", "released", "another holder", "a holder the record does not name"])
def test_M9_a_resume_reads_the_lease_of_its_pass_record(fake, monkeypatch, case):
    src = Source()
    with monkeypatch.context() as clock:
        if case == "expired":
            clock.setattr(ticket, "_now", lambda: "2000-01-01T00:00:00Z")
        first = build_ledger(fake.path, src, "S2", 0)
    holder = holder_of(first)
    if case == "released":
        fake.write("item.state", {"status": "open"})
    elif case == "another holder":
        fake.write("item.state", lease("seat-b"))
    before = fake.events()
    if case.startswith("a"):
        named = case.endswith("name")
        with pytest.raises(ticket.StoreUnavailable) as excinfo:
            open_ledger(fake.path, src, pass_id=first.pass_id, holder="seat-z" if named else None)
        assert refusal(excinfo) == ("holder_mismatch" if named else "lease_held")
        assert ("seat-z" if named else "seat-b") in str(excinfo.value)
        assert fake.events() == before
        return
    resumed = open_ledger(fake.path, src, pass_id=first.pass_id)
    assert (resumed.live, resumed.pass_id) == (True, first.pass_id)
    written = fake.events()[len(before):]
    if case == "live and own":
        assert written == []
    else:
        assert [event["kind"] for event in written] == ["item.state"]
        renewed = written[0]
        assert renewed["payload"]["owner"] == renewed["payload"]["claim"] == holder
        assert renewed["payload"]["expires_at"] > time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        assert "prior_holder" not in renewed["payload"]
    resumed.record_tried("The resumed pass writes.")
    assert fake.events()[-1]["kind"] == "item.tried"


# --- M10: the end and the release command leave another holder's lease alone -----------------------------


@pytest.mark.parametrize("how", ["end", "release command"])
def test_M10_the_pass_end_lands_alone_once_another_holder_took_the_expired_lease(fake, monkeypatch, how):
    src = Source()
    with monkeypatch.context() as clock:
        clock.setattr(ticket, "_now", lambda: "2000-01-01T00:00:00Z")
        session = build_ledger(fake.path, src, "S2", 0)
    fake.write("item.state", {**lease("seat-b"), "prior_holder": holder_of(session)})
    before = len(fake.events())
    if how == "end":
        session.end()
    else:
        monkeypatch.setenv("TICKET_LEDGER_CLI", str(fake.path))
        assert ticket.main(["release", KEY, session.pass_id]) == 0
    ended = fake.events()[before:]
    assert [(event["kind"], event["idempotency_key"]) for event in ended] == [
        ("session.closed", f"ticket-pass/{KEY}/{session.pass_id}/released")]
    current = row(fake)
    assert (current["status"], current["owner"]) == ("claimed", "seat-b")


# --- M11-M12: a stopped, closed or blocked item takes no lease; a closed item shows no pass ----------------


@pytest.mark.parametrize("stage", ["S5", "S6", "blocked"])
def test_M11_a_capped_closed_or_blocked_item_takes_no_lease(fake, stage):
    src = Source()
    if stage == "blocked":
        build_ledger(fake.path, src, "S2", 0).end()
        fake.write("item.state", {"status": "blocked", "blocked_by": ["EK-1"]})
    else:
        build_ledger(fake.path, src, stage).end()
    before = fake.events()
    if stage == "blocked":
        with pytest.raises(ticket.StoreUnavailable) as excinfo:
            open_ledger(fake.path, src)
        assert refusal(excinfo) == "item_not_claimable"
        assert "blocked" in str(excinfo.value)
    else:
        resumed = open_ledger(fake.path, src)
        assert (resumed.phase, resumed.live) == ({"S5": "stopped", "S6": "closed"}[stage], False)
    assert fake.events() == before


def test_M12_a_closed_item_reads_no_pass_though_its_pass_record_is_there(backend):
    session = build_ledger(backend.path, Source(), "S6")
    records = pass_records(backend.events())
    assert [record_of(event)["id"] for event in records] == [session.pass_id]
    card, _ = ticket.LedgerStore(backend.path, KEY, TITLE).read()
    assert (card["status"], card["pass"]) == ("closed", None)


# --- M14: a pass that takes over another holder's expired lease names that holder ----------------------------


@pytest.mark.parametrize("named", [None, "seat-a"], ids=["default holder", "named holder"])
def test_M14_a_pass_that_takes_over_an_expired_lease_names_its_prior_holder(backend, named):
    backend.new_item()
    backend.write("item.state", lease("seat-b", expires=PAST))
    assert (row(backend)["status"], row(backend).get("expired_holder")) == ("open", "seat-b")
    before = len(backend.events())
    session = open_ledger(backend.path, Source(), holder=named)
    holder = named or holder_of(session)
    (taken,) = states(backend.events()[before:])
    assert (taken["status"], taken["owner"], taken["claim"]) == ("claimed", holder, holder)
    assert taken["prior_holder"] == "seat-b"
    (record,) = [record_of(event) for event in pass_records(backend.events()[before:])]
    assert (record["holder"], record["lease"]) == (holder, "taken")
    current = row(backend)
    assert (current["status"], current.get("owner")) == ("claimed", holder)


# --- M15: a joined pass whose resume takes the lease again releases it at its end ------------------------


@pytest.mark.parametrize("how", ["end", "release command"])
def test_M15_a_resumed_joined_pass_that_takes_the_lease_again_releases_it_at_its_end(backend, monkeypatch, how):
    src = Source()
    backend.new_item()
    backend.write("item.state", lease("seat-h", owner=False))
    first = open_ledger(backend.path, src, holder="seat-h")
    (record,) = [record_of(event) for event in pass_records(backend.events())]
    assert (record["holder"], record["lease"]) == ("seat-h", "joined")
    # The holder releases its own lease; the joined pass is then resumed and takes the lease again as seat-h.
    backend.write("item.state", {"status": "open"})
    before = len(backend.events())
    resumed = open_ledger(backend.path, src, pass_id=first.pass_id)
    (retaken,) = states(backend.events()[before:])
    assert (retaken["status"], retaken["owner"], retaken["claim"]) == ("claimed", "seat-h", "seat-h")
    before = len(backend.events())
    if how == "end":
        resumed.end()
    else:
        monkeypatch.setenv("TICKET_LEDGER_CLI", str(backend.path))
        assert ticket.main(["release", KEY, first.pass_id]) == 0
    ended = backend.events()[before:]
    assert [event["kind"] for event in ended] == ["session.closed", "item.state"]
    assert ended[1]["payload"]["status"] == "open"
    assert row(backend)["status"] == "open"
    other = open_ledger(backend.path, src, holder="seat-z")
    assert other.live and row(backend).get("owner") == "seat-z"


# --- M16: two opens racing on one handle each claim as the holder they named -------------------------------


class Interleave(ticket.LedgerStore):
    """A handle shared by two opens: open-a's card read waits until open-b has read the card too, and open-b then
    waits until open-a's open is over, so both have named their holders before either claims."""

    def __init__(self, cli):
        super().__init__(cli, KEY, TITLE)
        self.b_read, self.a_done = threading.Event(), threading.Event()
        self.start_b = None

    def read(self):
        result = super().read()
        name = threading.current_thread().name
        if name == "open-a" and self.start_b is not None:
            start, self.start_b = self.start_b, None
            start()
            assert self.b_read.wait(30)
        elif name == "open-b" and not self.b_read.is_set():
            self.b_read.set()
            assert self.a_done.wait(30)
        return result


@pytest.mark.parametrize("held", [False, True], ids=["no lease", "seat-b holds a live lease"])
def test_M16_two_opens_racing_on_one_handle_each_claim_as_the_holder_they_named(fake, held):
    src = Source()
    open_ledger(fake.path, src).end()
    if held:
        fake.write("item.state", lease("seat-b"))
    before = len(fake.events())
    handle = Interleave(fake.path)
    results = {}

    def run(name, holder):
        try:
            results[name] = open_ledger(fake.path, src, handle=handle, holder=holder)
        except Exception as exc:
            results[name] = exc
        finally:
            if name == "open-a":
                handle.a_done.set()

    b = threading.Thread(target=run, args=("open-b", "seat-b"), name="open-b")
    handle.start_b = b.start
    a = threading.Thread(target=run, args=("open-a", "seat-a"), name="open-a")
    a.start()
    a.join(60)
    b.join(60)
    (record,) = [record_of(event) for event in pass_records(fake.events()[before:])]
    if held:
        assert isinstance(results["open-a"], ticket.StoreUnavailable) and results["open-a"].code == "lease_held"
        assert "seat-b" in str(results["open-a"])
        assert results["open-b"].live and results["open-b"].pass_id == record["id"]
        assert (record["holder"], record["lease"]) == ("seat-b", "joined")
        assert states(fake.events()[before:]) == []
    else:
        assert results["open-a"].live and results["open-a"].pass_id == record["id"]
        assert (record["holder"], record["lease"]) == ("seat-a", "taken")
        (taken,) = states(fake.events()[before:])
        assert (taken["owner"], taken["claim"]) == ("seat-a", "seat-a")
        assert isinstance(results["open-b"], ticket.StoreUnavailable) and results["open-b"].code == "handle_used"

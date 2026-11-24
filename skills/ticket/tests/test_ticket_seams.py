"""Matrix section L: the named seams of a ticket pass (build, review, outward) and their defaults.

A seam is a name with the name of its default implementation. A card with no seam record resolves to the
defaults, and a default pass writes no seam record: the card bytes, the card token and the ledger events are
the ones a pass wrote before the seams were named.
"""

import hashlib
import itertools
import json

import pytest

import ticket
from ledger_support import FakeLedger, build_ledger
from ticket_support import Source, build, card_bytes, read_card, store

DEFAULTS = {"build": "builder-core", "review": "reviewer", "outward": "push-and-open-draft"}

# The sha256 of what a default pass to S6 wrote, measured on the code before the seams were named, with the
# host, the clock, the random ids and the caller's ledger identity held fixed (``_fixed``). A change to what a
# default pass writes moves it. The ledger digest was measured again once when the pass began to take the
# ledger lease: its claim became the lease and a pass record where it had been one ``session.opened``. It was
# measured again when each recorded decision began to get a ``decided:`` item note beside its ticket-decision
# note (the two go decisions of S6 each add one event); the markdown digests did not move.
MARKDOWN_CARD_SHA256 = "2d29ff9ebc6efce9a28ec5a21454d7380fbb3f0ec0ba606cfb90bdf4d254b98f"
MARKDOWN_TOKEN = "4ef4daeed5681b20467d17f6ba895c6a04e42a40c12eb2472cf79c165ed1872d"
LEDGER_EVENTS_SHA256 = "15f20da436faddfc2bedc18f1486653e30d35f6b813564a6c7495a6d7030b7ee"


@pytest.fixture
def _fixed(monkeypatch):
    """The host, the clock, every random id and the caller's ledger identity fixed (the identity unset, which
    the fake ledger stamps onto each event), so a pass writes the same bytes on every run and in every
    environment."""

    monkeypatch.delenv("LEDGER_SESSION", raising=False)
    monkeypatch.delenv("LEDGER_AGENT", raising=False)
    counter = itertools.count(1)
    monkeypatch.setattr(ticket, "_host", lambda: "test-host")
    monkeypatch.setattr(ticket, "_now", lambda: "2026-11-22T09:00:00Z")
    monkeypatch.setattr(ticket.secrets, "token_hex", lambda n=16: f"{next(counter):0{2 * n}x}")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def test_L1_a_card_with_no_seam_record_resolves_to_the_three_defaults(repo):
    card = read_card(repo)
    assert "seams" not in card
    assert ticket.seam_set(card) == DEFAULTS
    assert list(ticket.seam_set(card)) == ["build", "review", "outward"]
    assert ticket.seam_set(card, "review") == {"review": "reviewer"}
    assert dict(ticket.SEAMS) == DEFAULTS


def test_L2_a_default_pass_writes_no_seam_record_on_either_store(repo, tmp_path, _fixed):
    build(repo, Source(), "S6")
    written = card_bytes(repo)
    assert b"seam" not in written
    assert hashlib.sha256(written).hexdigest() == MARKDOWN_CARD_SHA256
    assert store(repo).read()[1] == MARKDOWN_TOKEN
    fake = FakeLedger(tmp_path / "ledger")
    build_ledger(fake, Source(), "S6")
    events = fake.events()
    assert "seam" not in json.dumps(events)
    assert _digest(events) == LEDGER_EVENTS_SHA256


@pytest.mark.parametrize("name", ["", "Build", "build ", "outward_go", "close", "merge"])
def test_L3_an_unknown_seam_name_is_refused_by_name(repo, name):
    with pytest.raises(ticket.TicketError, match="unknown seam") as excinfo:
        ticket.seam_set(read_card(repo), "review", name)
    assert repr(name) in str(excinfo.value)

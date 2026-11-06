"""Design 6.4: importing markdown cards into the ledger, finding them, and the leftover-card notice.

Every test runs on temporary repositories and the fake ledger CLI; none touches a real repository.
"""

import fcntl
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

import ticket
from ledger_support import FakeLedger, open_ledger
from ticket_support import KEY, TITLE, Source, build, card_bytes, card_path, read_card

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "IC-ZOD-BUMP.md"
CLOSED = "IC-ZOD-BUMP"
START = ticket._BLOCK_START


@pytest.fixture
def fake(tmp_path, monkeypatch):
    ledger = FakeLedger(tmp_path / "ledger")
    monkeypatch.setenv("TICKET_LEDGER_CLI", str(ledger.path))
    return ledger


def make_repo(path, ignore=".ticket/\n"):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / ".gitignore").write_text(ignore, encoding="utf-8")
    return path


def run(capsys, *argv):
    code = ticket.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.splitlines(), out.err


def put_fixture(repo, name=CLOSED):
    (repo / ".ticket").mkdir(exist_ok=True)
    shutil.copy(FIXTURE, repo / ".ticket" / f"{name}.md")


def fixture_card():
    text = FIXTURE.read_text(encoding="utf-8")
    start = text.index(START)
    return json.loads(text[start + len(START) : text.index(ticket._BLOCK_END, start + 1)])


def put_card(repo, name, card):
    (repo / ".ticket").mkdir(exist_ok=True)
    (repo / ".ticket" / f"{name}.md").write_text(ticket._render_card(card), encoding="utf-8")


def snapshot(repo):
    return {str(p.relative_to(repo)): p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file() and ".git" not in p.parts}


def ledger_card(fake, key, title):
    return ticket.LedgerStore(fake.path, key, title).read()[0]


# --- The replay: a card imported whole reads back as the same card ------------------------------


def test_a_closed_card_imports_as_history_and_replays_equal(repo, fake, capsys):
    put_fixture(repo)
    before = fixture_card()
    code, lines, _ = run(capsys, "import-cards", str(repo))
    assert (code, lines) == (0, [f"imported {CLOSED}"])
    after = ledger_card(fake, CLOSED, before["title"])
    assert after == before
    assert after["status"] == "closed" and after["stamps"][-1]["close"] is True
    assert not (repo / ".ticket" / f"{CLOSED}.md").exists()
    assert (repo / ".ticket" / "imported" / f"{CLOSED}.md").read_bytes() == FIXTURE.read_bytes()
    assert len(fake.appends()) == 1


def test_an_open_card_with_a_live_pass_replays_equal_and_resumes_on_the_ledger(repo, fake, capsys):
    session = build(repo, Source(), "S2", rounds=1)
    before = read_card(repo)
    assert before["pass"] is not None and len(before["reviews"]) == 1
    code, lines, _ = run(capsys, "import-cards", str(repo))
    assert (code, lines) == (0, [f"imported {KEY}"])
    assert ledger_card(fake, KEY, TITLE) == before
    resumed = open_ledger(fake, Source(), pass_id=session.pass_id)
    assert (resumed.phase, resumed.pass_id, resumed.review_round) == (session.phase, session.pass_id, 1)
    assert resumed.reconciliation == ()
    resumed.record_tried("resumed on the ledger", outcome="the import carried the pass")
    resumed.end()


def test_a_closed_pass_with_no_close_stamp_still_replays_equal(repo, fake, capsys):
    card = fixture_card()
    card["stamps"][-1]["close"] = False
    put_card(repo, CLOSED, card)
    assert run(capsys, "import-cards", str(repo))[:2] == (0, [f"imported {CLOSED}"])
    assert ledger_card(fake, CLOSED, card["title"]) == card


# --- Idempotency, and what a stop between the write and the move leaves -----------------------------


def test_a_second_run_writes_nothing(repo, fake, capsys):
    build(repo, Source(), "S1")
    assert run(capsys, "import-cards", str(repo))[:2] == (0, [f"imported {KEY}"])
    events, appends = fake.events(), len(fake.appends())
    # The card is moved aside, so a plain second run has nothing to do.
    assert run(capsys, "import-cards", str(repo))[:2] == (0, [])
    # The card comes back (a restored backup): the same content is the same keys, a full replay.
    os.replace(repo / ".ticket" / "imported" / f"{KEY}.md", card_path(repo))
    assert run(capsys, "import-cards", str(repo))[:2] == (0, [f"already imported {KEY}"])
    assert fake.events() == events
    assert len(fake.appends()) == appends
    assert not card_path(repo).exists()


def test_a_stop_after_the_ledger_write_is_finished_by_a_rerun(repo, fake, capsys, monkeypatch):
    build(repo, Source(), "S1")
    before = card_bytes(repo)

    def refuse_link(*args, **kwargs):
        raise OSError("disk went away")

    with monkeypatch.context() as patch:
        patch.setattr(ticket.os, "link", refuse_link)
        code, lines, _ = run(capsys, "import-cards", str(repo))
    assert code == 1 and lines[0].startswith(f"skipped {KEY}: the card is in the ledger but could not be moved aside")
    assert card_bytes(repo) == before
    events = fake.events()
    assert len(events) > 0
    code, lines, _ = run(capsys, "import-cards", str(repo))
    assert (code, lines) == (0, [f"already imported {KEY}"])
    assert fake.events() == events
    assert card_bytes(repo) is None and (repo / ".ticket" / "imported" / f"{KEY}.md").read_bytes() == before


def test_a_destination_created_between_the_check_and_the_publish_is_not_replaced(repo, fake, capsys, monkeypatch):
    build(repo, Source(), "S1")
    before = card_bytes(repo)
    real_check = ticket._imported_directory

    def check_then_race(directory, name, **kwargs):
        moved = real_check(directory, name, **kwargs)  # the absence check passes ...
        if moved is not None:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=moved)  # ... then the name appears
            os.write(fd, b"someone else's file\n")
            os.close(fd)
        return moved

    with monkeypatch.context() as patch:
        patch.setattr(ticket, "_imported_directory", check_then_race)
        code, lines, _ = run(capsys, "import-cards", str(repo))
    assert (code, lines) == (1, [f"skipped {KEY}: imported copy already exists"])
    assert (repo / ".ticket" / "imported" / f"{KEY}.md").read_bytes() == b"someone else's file\n"
    assert card_bytes(repo) == before


# --- Cards that are refused stay where they are -------------------------------------------------------


def test_malformed_cards_are_reported_by_name_and_left_while_the_others_import(repo, fake, capsys):
    build(repo, Source(), "S1")
    head = FIXTURE.read_text(encoding="utf-8").partition(START)[0]
    block = json.dumps(fixture_card())
    (repo / ".ticket" / "BADJSON.md").write_text(head + START + "{not json\n-->\n", encoding="utf-8")
    (repo / ".ticket" / "BADFRAME.md").write_text(head + "<!-- ticket-card:v1\n" + block + "\n-->\n", encoding="utf-8")
    card = fixture_card()
    card["key"], card["plan"]["key"], card["stamps"][0]["phase"] = "BADRECORD", "BADRECORD", "nonsense"
    put_card(repo, "BADRECORD", card)
    (repo / ".ticket" / "NOTES.md").write_text("just some notes\n", encoding="utf-8")
    kept = snapshot(repo)
    code, lines, _ = run(capsys, "import-cards", str(repo))
    assert code == 1
    assert [line.split(":")[0] for line in lines] == [
        "skipped BADFRAME", "skipped BADJSON", "skipped BADRECORD", f"imported {KEY}", "skipped NOTES",
    ]
    assert "unknown card frame version v1" in lines[0]
    assert lines[1].startswith("skipped BADJSON: malformed: ") and "not JSON" in lines[1]
    assert lines[2].startswith("skipped BADRECORD: malformed: ") and "phase" in lines[2]
    assert "no complete ticket-card block" in lines[4]
    for name in ("BADJSON", "BADFRAME", "BADRECORD", "NOTES"):
        assert (repo / ".ticket" / f"{name}.md").read_bytes() == kept[f".ticket/{name}.md"]
    assert {event["item_id"] for event in fake.events()} == {KEY}


def test_a_repository_that_does_not_ignore_the_moved_card_is_refused_untouched(tmp_path, fake, capsys):
    repo = make_repo(tmp_path / "narrow", ignore=".ticket/*.md\n.ticket/*.lock\n")
    put_fixture(repo)
    kept = snapshot(repo)
    code, lines, _ = run(capsys, "import-cards", str(repo))
    assert code == 1 and len(lines) == 1
    assert lines[0].startswith(f"skipped {CLOSED}: .ticket/imported/ is not git-ignored")
    assert snapshot(repo) == kept and not (repo / ".ticket" / "imported").exists()
    assert fake.events() == []


def test_a_card_whose_lock_another_process_holds_is_skipped(repo, fake, capsys):
    put_fixture(repo)
    lock = repo / ".ticket" / f"{CLOSED}.lock"
    lock.write_text("", encoding="utf-8")
    with open(lock, "r+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        code, lines, _ = run(capsys, "import-cards", str(repo))
    assert code == 1 and lines[0].startswith(f"skipped {CLOSED}: the card's lock is held by another process")
    assert fake.events() == [] and (repo / ".ticket" / f"{CLOSED}.md").exists()
    assert run(capsys, "import-cards", str(repo))[:2] == (0, [f"imported {CLOSED}"])


def test_an_item_that_exists_with_different_history_is_never_merged(repo, fake, capsys):
    build(repo, Source(), "S1")
    fake.outside("item.created", {"type": "ticket", "title": "Another ticket entirely", "source": "other"})
    before, kept = fake.events(), card_bytes(repo)
    code, lines, _ = run(capsys, "import-cards", str(repo))
    assert (code, lines) == (1, [f"skipped {KEY}: ledger item exists with different history"])
    assert fake.events() == before and card_bytes(repo) == kept


def test_an_imported_directory_that_is_a_link_is_refused_before_any_write(repo, fake, capsys, tmp_path):
    put_fixture(repo)
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", repo / ".ticket" / "imported")
    code, lines, _ = run(capsys, "import-cards", str(repo))
    assert code == 1 and lines[0].startswith(f"skipped {CLOSED}: ")
    assert fake.events() == [] and (repo / ".ticket" / f"{CLOSED}.md").exists()
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_a_ticket_directory_that_is_a_link_is_refused(repo, fake, capsys, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    shutil.copy(FIXTURE, tmp_path / "elsewhere" / f"{CLOSED}.md")
    os.symlink(tmp_path / "elsewhere", repo / ".ticket")
    code, lines, _ = run(capsys, "import-cards", str(repo))
    assert code == 1 and lines[0].startswith("skipped .ticket: ")
    assert fake.events() == [] and (tmp_path / "elsewhere" / f"{CLOSED}.md").exists()


def test_a_refused_ledger_write_leaves_the_card_in_place(repo, fake, capsys):
    build(repo, Source(), "S1")
    kept = card_bytes(repo)
    fake.answer("append-many", "validation")
    code, lines, _ = run(capsys, "import-cards", str(repo))
    assert code == 1 and lines[0].startswith(f"skipped {KEY}: the ledger did not take the card (ledger_refused)")
    assert card_bytes(repo) == kept and fake.events() == [] and not (repo / ".ticket" / "imported" / f"{KEY}.md").exists()


def test_a_name_already_taken_in_imported_blocks_the_card_before_any_write(repo, fake, capsys):
    put_fixture(repo)
    (repo / ".ticket" / "imported").mkdir()
    (repo / ".ticket" / "imported" / f"{CLOSED}.md").write_text("an earlier card\n", encoding="utf-8")
    code, lines, _ = run(capsys, "import-cards", str(repo))
    assert code == 1 and f"skipped {CLOSED}: .ticket/imported/{CLOSED}.md already exists" in lines[0]
    assert fake.events() == [] and (repo / ".ticket" / f"{CLOSED}.md").exists()
    assert (repo / ".ticket" / "imported" / f"{CLOSED}.md").read_text(encoding="utf-8") == "an earlier card\n"


def test_cards_the_ledger_mapping_cannot_replay_are_skipped_not_approximated(repo, fake, capsys):
    held = fixture_card()
    held["pass"] = {"id": "abc123", "host": "studio", "started_at": "2026-11-05T10:00:00Z"}
    put_card(repo, "CLOSEDPASS", {**held, "key": "CLOSEDPASS", "plan": {**held["plan"], "key": "CLOSEDPASS"}})
    misplaced = fixture_card()
    misplaced["status"] = "open"
    put_card(repo, "OPENCLOSE", {**misplaced, "key": "OPENCLOSE", "plan": {**misplaced["plan"], "key": "OPENCLOSE"}})
    spaced = fixture_card()
    spaced["status"], spaced["stamps"][-1]["close"] = "open", False
    spaced["pass"] = {"id": "abc123", "host": "studio", "started_at": "2026-11-05 10:00"}
    put_card(repo, "SPACED", {**spaced, "key": "SPACED", "plan": {**spaced["plan"], "key": "SPACED"}})
    code, lines, _ = run(capsys, "import-cards", str(repo))
    assert code == 1
    assert lines[0].startswith("skipped CLOSEDPASS: a closed card holds a live pass")
    assert lines[1].startswith("skipped OPENCLOSE: a close stamp is not the last stamp of a closed card")
    assert lines[2].startswith("skipped SPACED: the card cannot be replayed through the ledger mapping: ")
    assert fake.events() == []


def test_the_imported_directory_is_opened_through_the_ticket_descriptor_without_following_a_link(tmp_path):
    # The ignore check refuses a link first in a real run; this is the race check behind it, a link swapped in after that check.
    (tmp_path / "dot-ticket").mkdir()
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", tmp_path / "dot-ticket" / "imported")
    directory = os.open(tmp_path / "dot-ticket", os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ticket._Skip, match="is not a plain directory"):
            ticket._imported_directory(directory, "X.md")
    finally:
        os.close(directory)
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_a_ledger_item_that_reads_back_differently_leaves_the_card_in_place(repo, fake, capsys):
    put_fixture(repo)
    kept = (repo / ".ticket" / f"{CLOSED}.md").read_bytes()
    # The write lands, then the read-back export omits the closing event: the item no longer replays as the card.
    fake.script("export-item", commit=True, drop_events=1)
    code, lines, _ = run(capsys, "import-cards", str(repo))
    assert code == 1
    assert lines == [f"skipped {CLOSED}: the ledger item reads differently from the card, so the card was left in place"]
    assert (repo / ".ticket" / f"{CLOSED}.md").read_bytes() == kept
    assert not (repo / ".ticket" / "imported" / f"{CLOSED}.md").exists()


def test_a_card_whose_replay_differs_before_the_write_writes_nothing(repo, fake, capsys, monkeypatch):
    # No real card reaches this guard (the mapping either replays equal or raises); the fold is patched to drift.
    put_fixture(repo)
    kept = (repo / ".ticket" / f"{CLOSED}.md").read_bytes()
    real = ticket._ledger_card

    def drifting(events, key, title):
        card, rest = real(events, key, title)
        return {**card, "title": card["title"] + " (drift)"}, rest

    monkeypatch.setattr(ticket, "_ledger_card", drifting)
    code, lines, _ = run(capsys, "import-cards", str(repo))
    assert code == 1
    assert lines == [f"skipped {CLOSED}: the card cannot be replayed through the ledger mapping (differs in title)"]
    assert fake.events() == [] and fake.appends() == []
    assert (repo / ".ticket" / f"{CLOSED}.md").read_bytes() == kept
    assert not (repo / ".ticket" / "imported").exists()


def test_a_dry_run_and_a_real_run_agree_when_the_moved_aside_name_is_taken(repo, fake, capsys):
    put_fixture(repo)
    (repo / ".ticket" / "imported").mkdir()
    (repo / ".ticket" / "imported" / f"{CLOSED}.md").write_text("an earlier card\n", encoding="utf-8")
    kept = snapshot(repo)
    dry_code, dry_lines, _ = run(capsys, "import-cards", str(repo), "--dry-run")
    assert snapshot(repo) == kept and fake.events() == []
    real_code, real_lines, _ = run(capsys, "import-cards", str(repo))
    assert (dry_code, dry_lines) == (real_code, real_lines) == (1, real_lines)
    assert real_lines[0].startswith(f"skipped {CLOSED}: .ticket/imported/{CLOSED}.md already exists")
    # The real run takes the card's lock, as it always does; nothing else changes.
    assert {k: v for k, v in snapshot(repo).items() if not k.endswith(".lock")} == kept and fake.events() == []


# --- Dry run, and the refusals before a card is read -----------------------------------------------------


def test_a_dry_run_writes_nothing_and_moves_nothing(repo, fake, capsys):
    put_fixture(repo)
    (repo / ".ticket" / "BAD.md").write_text("not a card\n", encoding="utf-8")
    kept = snapshot(repo)
    code, lines, _ = run(capsys, "import-cards", str(repo), "--dry-run")
    assert code == 1 and lines[0].startswith("skipped BAD: malformed: ") and "no complete ticket-card block" in lines[0]
    assert lines[1] == f"would import {CLOSED}"
    assert snapshot(repo) == kept and not (repo / ".ticket" / "imported").exists()
    assert fake.events() == [] and fake.appends() == []
    (repo / ".ticket" / "BAD.md").unlink()
    assert run(capsys, "import-cards", str(repo))[:2] == (0, [f"imported {CLOSED}"])
    os.replace(repo / ".ticket" / "imported" / f"{CLOSED}.md", repo / ".ticket" / f"{CLOSED}.md")
    before = fake.events()
    assert run(capsys, "import-cards", str(repo), "--dry-run")[:2] == (0, [f"already imported {CLOSED}"])
    assert fake.events() == before and (repo / ".ticket" / f"{CLOSED}.md").exists()


def test_without_a_ledger_cli_that_passes_the_probe_nothing_is_read(repo, fake, capsys, monkeypatch):
    put_fixture(repo)
    fake.help("no --expect-revision")
    code, lines, err = run(capsys, "import-cards", str(repo))
    assert (code, lines) == (2, []) and "lacks append-many or --expect-revision" in err
    assert [call.verb for call in fake.calls()] == ["append-many"] and fake.events() == []
    monkeypatch.delenv("TICKET_LEDGER_CLI")
    code, lines, err = run(capsys, "import-cards", str(repo))
    assert (code, lines) == (2, []) and "TICKET_LEDGER_CLI is not set" in err
    assert (repo / ".ticket" / f"{CLOSED}.md").exists()


def test_a_repository_that_is_not_a_directory_is_a_usage_error(repo, fake, capsys, tmp_path):
    code, lines, err = run(capsys, "import-cards", str(tmp_path / "missing"))
    assert (code, lines) == (2, []) and "not a directory" in err


# --- Discovery ----------------------------------------------------------------------------------------


def test_find_cards_lists_nested_repositories_and_skips_imported_and_links(tmp_path, capsys, monkeypatch):
    tree = tmp_path / "tree"
    shallow = make_repo(tree / "a")
    nested = make_repo(tree / "b" / "c" / "nested")
    done = make_repo(tree / "d")
    ignored = make_repo(tree / "node_modules" / "pkg")
    plain = tree / "e"
    for repo in (shallow, nested, ignored):
        put_fixture(repo)
    (done / ".ticket" / "imported").mkdir(parents=True)
    shutil.copy(FIXTURE, done / ".ticket" / "imported" / f"{CLOSED}.md")
    (plain / ".ticket").mkdir(parents=True)
    shutil.copy(FIXTURE, plain / ".ticket" / f"{CLOSED}.md")
    os.symlink(shallow, tree / "link")
    code, lines, _ = run(capsys, "find-cards", str(tree))
    assert (code, lines) == (0, sorted([str(shallow), str(nested)]))
    code, lines, _ = run(capsys, "find-cards", str(done))
    assert (code, lines) == (0, [])
    monkeypatch.chdir(shallow)
    assert run(capsys, "find-cards")[:2] == (0, [os.getcwd()])


def test_find_cards_skips_a_root_that_is_a_link(tmp_path, capsys):
    real = make_repo(tmp_path / "real")
    put_fixture(real)
    os.symlink(real, tmp_path / "link")
    assert run(capsys, "find-cards", str(tmp_path / "link"))[:2] == (0, [])
    assert run(capsys, "find-cards", str(tmp_path / "link"), str(real))[:2] == (0, [str(real)])
    for spelling in ("link/", "link/.", "link//"):
        assert run(capsys, "find-cards", str(tmp_path) + "/" + spelling)[:2] == (0, [])
    assert run(capsys, "find-cards", str(real) + "/")[:2] == (0, [str(real)])


# --- The notice /ticket prints on the ledger store ------------------------------------------------------


def import_line(repo):
    command = shlex.join(["python3", os.path.abspath(ticket.__file__), "import-cards", str(repo.resolve())])
    return (
        f"markdown ticket cards remain in {repo.resolve()}/.ticket and are not in the ledger; "
        f"import them with `{command}`"
    )


def test_an_open_on_the_ledger_store_prints_the_import_command_once(repo, fake, capsys):
    put_fixture(repo)
    put_fixture(repo, "OTHER-1")
    session = ticket.TicketPass.open(KEY, Source(), ticket.choose_store(repo, KEY, TITLE))
    assert session.reconciliation == (import_line(repo),)
    session.end()
    again = ticket.TicketPass.open(KEY, Source(), ticket.choose_store(repo, KEY, TITLE))
    assert again.reconciliation == (import_line(repo),)
    again.end()
    assert run(capsys, "import-cards", str(repo))[0] == 1  # OTHER-1 is the closed card under another key: refused
    (repo / ".ticket" / "OTHER-1.md").unlink()
    clean = ticket.TicketPass.open("EK-99", Source(), ticket.choose_store(repo, "EK-99", TITLE))
    assert clean.reconciliation == ()


def test_the_notice_is_absent_on_the_markdown_store(repo, monkeypatch):
    put_fixture(repo)
    monkeypatch.delenv("TICKET_LEDGER_CLI", raising=False)
    store = ticket.choose_store(repo, KEY, TITLE)
    assert isinstance(store, ticket.MarkdownStore)
    session = ticket.TicketPass.open(KEY, Source(), store)
    assert session.reconciliation == ()
    session.end()

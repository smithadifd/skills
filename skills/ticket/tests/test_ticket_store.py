"""Matrix section G: the markdown store."""

import contextlib
import errno
import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import ticket
from ticket_support import (
    A, B, KEY, TITLE, Source, build, card_bytes, card_path, open_pass, read_card, refusal, store,
)

SKILL_DIR = Path(__file__).resolve().parent.parent
ENV = dict(os.environ, PYTHONPATH=os.pathsep.join((str(SKILL_DIR), str(SKILL_DIR / "tests"))))


def _python(code, *args, **kwargs):
    return subprocess.Popen([sys.executable, "-c", code, *map(str, args)], env=ENV, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, **kwargs)


def _lock_path(repo):
    return card_path(repo).with_name(f"{KEY}.lock")


_OPEN_A_PASS = """
import json, sys
import ticket
from ticket_support import Source, open_pass
try:
    session = open_pass(sys.argv[1], Source())
    print(json.dumps({"pass": session.pass_id}))
except ticket.StoreUnavailable as exc:
    print(json.dumps({"code": exc.code, "message": str(exc)}))
"""


def test_G1_markdown_write_waits_for_the_lock_file_and_lands_after_release(repo):
    _lock_path(repo).parent.mkdir()
    with open(_lock_path(repo), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        child = _python(_OPEN_A_PASS, repo)
        time.sleep(1.0)
        assert child.poll() is None
        assert card_bytes(repo) is None
        fcntl.flock(lock, fcntl.LOCK_UN)
    out, err = child.communicate(timeout=30)
    assert child.returncode == 0, err
    assert read_card(repo)["pass"]["id"] == json.loads(out)["pass"]


_APPEND_TRIED = """
import sys, time
import ticket

class SlowSave(ticket.MarkdownStore):
    def _save(self, card):
        time.sleep(0.05)
        super()._save(card)

handle = SlowSave(sys.argv[1], "EK-14", "Add guarded ticket pass")
handle.pass_id = sys.argv[2]
for number in range(int(sys.argv[4])):
    while True:
        card, token = handle.read()
        try:
            handle.apply(token, [("tried", {"what": f"writer {sys.argv[3]} note {number}", "phase": "build"})])
            break
        except ticket.StoreUnavailable as exc:
            if exc.code != "stale":
                raise
"""


def test_G2_markdown_writers_in_parallel_lose_no_write(repo):
    session = open_pass(repo, Source())
    children = [_python(_APPEND_TRIED, repo, session.pass_id, writer, 3) for writer in range(4)]
    for child in children:
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err
    tried = {item["what"] for item in read_card(repo)["tried"]}
    assert tried == {f"writer {writer} note {number}" for writer in range(4) for number in range(3)}


def test_G3_two_threads_on_one_store_handle_lose_no_write(repo):
    session = open_pass(repo, Source())
    entered = threading.Event()

    class PausingSave(ticket.MarkdownStore):
        def _save(self, card):
            if not entered.is_set():
                entered.set()
                time.sleep(0.3)
            super()._save(card)

    handle = PausingSave(repo, KEY, TITLE)
    handle.pass_id = session.pass_id
    errors = []

    def append(what):
        try:
            while True:
                _, token = handle.read()
                try:
                    handle.apply(token, [("tried", {"what": what, "phase": "build"})])
                    return
                except ticket.StoreUnavailable as exc:
                    if exc.code != "stale":
                        raise
        except Exception as exc:  # reported to the main thread
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(f"thread {n}",)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert sorted(item["what"] for item in read_card(repo)["tried"]) == ["thread 0", "thread 1"]


def test_G4_markdown_store_refuses_a_lock_path_that_is_a_symlink(repo, tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep me\n", encoding="utf-8")
    _lock_path(repo).parent.mkdir()
    os.symlink(target, _lock_path(repo))
    with pytest.raises(ticket.StoreUnavailable, match="lock"):
        open_pass(repo, Source())
    assert target.read_text(encoding="utf-8") == "keep me\n"
    assert card_bytes(repo) is None


def test_G5_markdown_refuses_an_unignored_card_without_editing_the_ignore_file(repo):
    (repo / ".gitignore").unlink()
    with pytest.raises(ticket.StoreUnavailable, match=r"\.ticket/"):
        open_pass(repo, Source())
    assert not (repo / ".ticket").exists()
    assert not (repo / ".gitignore").exists()
    assert not (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").count(".ticket")


@pytest.mark.parametrize(
    "old, new",
    [
        ("Waiting at plan go", "waiting at plan go"),
        (json.dumps([A, B]), json.dumps([B, A])),
        ("plan go for plan", "plan go  or plan"),
    ],
    ids=["same length", "reorder", "whitespace in a point"],
)
def test_G6_a_hand_edit_between_steps_is_stale(repo, old, new):
    session = build(repo, Source(), "S2", 0)
    path = card_path(repo)
    body = path.read_text(encoding="utf-8")
    start = body.index("<!-- ticket-card:v2")
    machine_old = old.replace(", ", ",")
    machine_new = new.replace(", ", ",")
    assert len(machine_old) == len(machine_new) and machine_old in body[start:]
    edited = body[:start] + body[start:].replace(machine_old, machine_new, 1)
    stat = os.stat(path)
    path.write_text(edited, encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert os.stat(path).st_size == stat.st_size
    before = read_card(repo)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        session.record_tried("After the edit.")
    assert refusal(excinfo) == "stale"
    assert session.live is False
    assert read_card(repo) == {**before, "pass": None}


def _block(card):
    return "<!-- ticket-card:v2\n" + json.dumps(card) + "\n-->\n"


G7_BODIES = {
    "no block": lambda card: "# EK-14\n",
    "truncated block": lambda card: _block(card)[:60],
    "bad json": lambda card: "<!-- ticket-card:v2\n{\"schema\": \n-->\n",
    "stamps not a list": lambda card: _block({**card, "stamps": {}}),
    "status unknown": lambda card: _block({**card, "status": "done"}),
    "schema other": lambda card: _block({**card, "schema": "ticket-card/v1"}),
    "key other": lambda card: _block({**card, "key": "EK-99"}),
    "no title": lambda card: _block({**card, "title": ""}),
    "pass malformed": lambda card: _block({**card, "pass": {"id": "abc"}}),
    "pass id not hex": lambda card: _block(
        {**card, "pass": {"id": "deadbeef; printf PROBE_INJECTED", "host": "h", "started_at": "t"}}
    ),
    "plan field missing": lambda card: _block({name: value for name, value in card.items() if name != "plan"}),
    "reviews not a list": lambda card: _block({**card, "reviews": None}),
    "decision not an object": lambda card: _block({**card, "decisions": ["approved"]}),
}


@pytest.mark.parametrize("case", list(G7_BODIES))
def test_G7_an_unreadable_card_is_refused(repo, case):
    build(repo, Source(), "S2", 0).end()
    card_path(repo).write_text(G7_BODIES[case](read_card(repo)), encoding="utf-8")
    before = card_bytes(repo)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        open_pass(repo, Source())
    assert refusal(excinfo) == "card_unreadable"
    assert card_bytes(repo) == before


def test_G8_release_clears_only_this_handles_claim_and_needs_no_token(repo):
    src = Source()
    session = build(repo, src, "S2", 0)
    open_pass(repo, src, pass_id=session.pass_id).record_tried("An outside note.")
    stranger = store(repo)
    stranger.pass_id = "0000000000000000"
    stranger.release()
    assert read_card(repo)["pass"]["id"] == session.pass_id
    session.end()
    card = read_card(repo)
    assert card["pass"] is None
    assert card["tried"][-1]["what"] == "An outside note."


_HOLD_THE_LOCK = """
import fcntl, sys, time
handle = open(sys.argv[1], "w")
fcntl.flock(handle, fcntl.LOCK_EX)
print("locked", flush=True)
time.sleep(60)
"""


def test_G9_a_lock_held_by_a_dead_writer_is_released_by_the_system(repo):
    # Evidence only: the operating system drops a dead holder's flock.
    _lock_path(repo).parent.mkdir()
    holder = _python(_HOLD_THE_LOCK, _lock_path(repo))
    assert holder.stdout.readline().strip() == "locked"
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=30)
    session = open_pass(repo, Source())
    assert read_card(repo)["pass"]["id"] == session.pass_id


def test_G10_recorded_comment_markers_round_trip_and_leave_one_block(repo):
    session = open_pass(repo, Source())
    question = "Is --> or <!-- safe & is <b>bold</b> kept?"
    session.more_info("person", question)
    body = card_path(repo).read_text(encoding="utf-8")
    assert body.count("<!-- ticket-card:v2") == 1
    start = body.index("<!-- ticket-card:v2")
    assert body.index("-->", start) == body.rindex("\n-->") + 1
    assert read_card(repo)["stamps"][-1]["point"] == f"waiting on person: {question}"
    summary = body[:start]
    assert "--&gt;" in summary and "&lt;!--" in summary and "&amp;" in summary and "&lt;b&gt;" in summary
    assert "<b>" not in summary and "-->" not in summary


def test_G11_a_failed_save_leaves_the_old_card_and_no_temporary_file(repo, monkeypatch):
    session = build(repo, Source(), "S2", 0)
    before = card_bytes(repo)

    def refuse(*args, **kwargs):
        raise OSError("rename refused")

    monkeypatch.setattr(ticket.os, "replace", refuse)
    with pytest.raises(OSError, match="rename refused"):
        session.record_tried("Lost.")
    monkeypatch.undo()
    assert card_bytes(repo) == before
    assert [p.name for p in card_path(repo).parent.iterdir() if p.name.endswith(".tmp")] == []
    assert session.live is False


def test_G12_two_processes_racing_to_claim_get_one_pass(repo):
    children = [_python(_OPEN_A_PASS, repo) for _ in range(2)]
    answers = []
    for child in children:
        out, err = child.communicate(timeout=60)
        assert child.returncode == 0, err
        answers.append(json.loads(out))
    winners = [answer["pass"] for answer in answers if "pass" in answer]
    losers = [answer for answer in answers if "code" in answer]
    assert len(winners) == 1 and len(losers) == 1
    assert read_card(repo)["pass"]["id"] == winners[0]
    assert losers[0]["code"] == "held", losers[0]
    assert winners[0] in losers[0]["message"]


def test_G12_a_lock_create_that_reports_a_missing_file_is_retried(repo, monkeypatch):
    # Two first writers creating the lock through the directory at once can
    # see ENOENT from the create; the store opens it again.
    real_open = os.open
    failures = []

    def racing_open(path, flags, *args, **kwargs):
        if path == f"{KEY}.lock" and not failures:
            failures.append(path)
            raise FileNotFoundError(2, "No such file or directory", path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(ticket.os, "open", racing_open)
    session = open_pass(repo, Source())
    assert failures == [f"{KEY}.lock"]
    assert read_card(repo)["pass"]["id"] == session.pass_id


@pytest.mark.parametrize("winner", ["stays live", "ends at once"])
def test_G12_two_processes_racing_to_claim_get_one_pass_interleaved(tmp_path, winner):
    # The same race, interleaved: the winner claims between the loser's read
    # and its claim. The loser's claim is refused, never written; it reads
    # once more and names a winner that is still live.
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    (other / ".gitignore").write_text(".ticket/\n", encoding="utf-8")
    won = []

    class RacedStore(ticket.MarkdownStore):
        def read(self):
            result = super().read()
            if not won:
                won.append(open_pass(other, Source()))
                if winner == "ends at once":
                    won[0].end()
            return result

    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        ticket.TicketPass.open(KEY, Source(), RacedStore(other, KEY, TITLE))
    if winner == "stays live":
        assert refusal(excinfo) == "held"
        assert won[0].pass_id in str(excinfo.value)
        assert read_card(other)["pass"]["id"] == won[0].pass_id
    else:
        assert refusal(excinfo) == "stale"
        assert read_card(other)["pass"] is None


def test_G13_markdown_adapter_records_every_card_field(repo):
    handle = store(repo)
    claim = {"id": "a1b2c3d4e5f60718", "host": "builder", "started_at": "2026-10-17T14:30:00Z"}
    plan = {
        "schema": "ticket-plan-card/v2", "key": KEY, "title": TITLE, "revision": 1, "pass": claim["id"],
        "criteria": [A, B],
        "steps": [{"name": "orient", "status": "complete"}, {"name": "research", "status": "skipped", "skip": "Known."},
                  {"name": "plan", "status": "skipped", "skip": "One sentence covers it."},
                  {"name": "build", "status": "pending"}, {"name": "review", "status": "pending"}, {"name": "outward", "status": "pending"}],
        "diff_statement": "Add one guard.", "open_questions": ["Which flag?"],
        "ticket_status": "In Progress", "ticket_updated_at": "2026-10-17T14:30:00Z",
    }
    stamp = {"point": "Paused.", "phase": "build", "next_skills": ["ticket"], "refs": ["docs/a.md"], "close": False}
    decision = {
        "who": "the lead", "who_kind": "role", "relayed_by": "the person", "standing": "lean",
        "source": {"what": "reply", "ref": "conversation:12", "who": "the lead", "text": "Probably."},
        "effect": "Leans toward one flag.", "tags": [{"type": "team", "id": "platform"}], "verbatim": "Probably one.",
        "phase": "plan", "evidence": "conversation:12",
    }
    review = {"round": 1, "verdict": "changes", "plan_revision": 1, "criteria": [A, B], "passed": [1],
              "findings": ["Name the flag."], "reviewer": "reviewer", "evidence": "reviews/round-1.md"}
    tried = {"what": "Ran the suite.", "phase": "build", "outcome": "Green.", "evidence": "logs/run.txt"}
    _, token = handle.read()
    token = handle.apply(token, [("claim", claim)])
    handle.apply(token, [("plan", plan), ("stamp", stamp), ("decision", decision), ("review", review),
                         ("tried", tried), ("closed", None)])
    assert read_card(repo) == {
        "schema": "ticket-card/v2", "key": KEY, "title": TITLE, "status": "closed", "pass": claim, "plan": plan,
        "decisions": [decision], "reviews": [review], "tried": [tried], "stamps": [stamp],
    }


def test_G14_a_missing_card_reads_as_blank_with_token_none(repo):
    handle = store(repo)
    card, token = handle.read()
    assert (card, token) == (ticket._blank_card(KEY, TITLE), "none")
    assert card_bytes(repo) is None
    tried = [("tried", {"what": "First.", "phase": "orient"})]
    handle.apply("none", tried)
    assert read_card(repo)["tried"] == [tried[0][1]]
    before = card_bytes(repo)
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        handle.apply("none", tried)
    assert refusal(excinfo) == "stale"
    assert card_bytes(repo) == before


def test_G15_the_card_and_lock_are_inside_the_repository(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, repo / ".ticket")
    with pytest.raises(ticket.StoreUnavailable, match="outside the repository"):
        open_pass(repo, Source())
    assert list(outside.iterdir()) == []


# --- G16-G23: the binding events and the refusal codes ----------------------


def _tried(what="A note."):
    return ("tried", {"what": what, "phase": "build"})


CLAIM = {"id": "a1b2c3d4e5f60718", "host": "builder", "started_at": "2026-10-17T14:30:00Z"}


def test_G16_a_join_event_saves_nothing_and_returns_the_same_token(repo):
    first = open_pass(repo, Source(), handle=store(repo))
    before = card_bytes(repo)
    stat_before = os.stat(card_path(repo))
    handle = store(repo)
    _, token = handle.read()
    assert handle.apply(token, [("join", {"id": first.pass_id})]) == token
    assert card_bytes(repo) == before
    stat_after = os.stat(card_path(repo))
    assert (stat_after.st_ino, stat_after.st_mtime_ns) == (stat_before.st_ino, stat_before.st_mtime_ns)
    assert (handle.pass_id, handle.ended) == (first.pass_id, None)
    handle.apply(token, [_tried("Through the joined handle.")])
    assert read_card(repo)["tried"][-1]["what"] == "Through the joined handle."


@pytest.mark.parametrize("event", ["claim", "join"])
def test_G17_a_binding_event_travels_alone(repo, event):
    if event == "claim":
        binding = ("claim", CLAIM)
    else:
        first = open_pass(repo, Source(), handle=store(repo))
        binding = ("join", {"id": first.pass_id})
    handle = store(repo)
    before = card_bytes(repo)
    _, token = handle.read()
    with pytest.raises(ticket.TicketError, match="travels alone"):
        handle.apply(token, [binding, _tried()])
    assert card_bytes(repo) == before
    assert (handle.pass_id, handle.ended) == (None, None)


def test_G18_a_claim_binds_the_handle_before_the_lock_is_released(repo):
    seen = []

    class RecordingLock(ticket.MarkdownStore):
        @contextlib.contextmanager
        def _locked(self):
            with super()._locked():
                try:
                    yield
                finally:
                    seen.append((self.pass_id, self.ended))

    handle = RecordingLock(repo, KEY, TITLE)
    handle.apply("none", [("claim", CLAIM)])
    assert seen == [(CLAIM["id"], None)]
    assert (handle.pass_id, handle.ended) == (CLAIM["id"], None)


def test_G19_a_failed_save_during_a_claim_leaves_the_handle_unbound(repo):
    failures = []

    class FullDisk(ticket.MarkdownStore):
        def _save(self, card):
            if not failures:
                failures.append(card)
                raise OSError(errno.ENOSPC, "No space left on device")
            super()._save(card)

    handle = FullDisk(repo, KEY, TITLE)
    with pytest.raises(OSError, match="No space left"):
        open_pass(repo, Source(), handle=handle)
    assert (handle.pass_id, handle.ended) == (None, None)
    assert card_bytes(repo) is None
    retry = open_pass(repo, Source(), handle=handle)
    assert retry.live is True
    assert read_card(repo)["pass"]["id"] == retry.pass_id


def test_G20_a_card_outside_the_repository_is_refused_with_a_code(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, repo / ".ticket")
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        store(repo).apply("none", [_tried()])
    assert refusal(excinfo) == "outside_repository"
    assert list(outside.iterdir()) == []


def test_G21_an_unignored_card_is_refused_with_a_code(repo):
    (repo / ".gitignore").unlink()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        store(repo).apply("none", [_tried()])
    assert refusal(excinfo) == "not_ignored"
    assert not (repo / ".ticket").exists()
    assert not (repo / ".gitignore").exists()
    assert not (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").count(".ticket")


def test_G22_a_lock_that_is_not_a_regular_file_is_refused_with_a_code(repo):
    _lock_path(repo).parent.mkdir()
    os.mkfifo(_lock_path(repo))
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        store(repo).apply("none", [_tried()])
    assert refusal(excinfo) == "lock_not_regular"
    assert stat.S_ISFIFO(os.lstat(_lock_path(repo)).st_mode)
    assert card_bytes(repo) is None


def test_G23_an_unopenable_lock_is_refused_with_a_code(repo):
    _lock_path(repo).parent.mkdir()
    _lock_path(repo).mkdir()
    with pytest.raises(ticket.StoreUnavailable) as excinfo:
        store(repo).apply("none", [_tried()])
    assert refusal(excinfo) == "lock_unopenable"
    assert os.strerror(errno.EISDIR) in str(excinfo.value)
    assert _lock_path(repo).is_dir()
    assert card_bytes(repo) is None

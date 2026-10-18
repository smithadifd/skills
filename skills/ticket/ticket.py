#!/usr/bin/env python3
"""State and adapter helpers for one attended pass over a ticket."""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import functools
import getpass
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, Sequence

PHASES = ("orient", "plan", "build", "review", "close")
STEPS = ("orient", "research", "plan", "build", "review", "outward")
INFO_EXITS = ("person", "research", "someone_else")
OUTSIDE_EVENTS = ("merge", "reply", "ratification", "review", "release", "other")
TAG_TYPES = ("ticket", "initiative", "team", "project")
PLAN_SCHEMA = "ticket-plan-card/v1"
MAX_REVIEW_ROUNDS = 3
_SKILL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_JSON_START = "<!-- ticket-card:v1\n"
_JSON_END = "\n-->"
# A ledger note that carries a plan-card snapshot starts with this prefix.
_PLAN_NOTE = "ticket-plan-card/v1 "
# A ledger note that names the stop event confirmed as the close.
_CLOSE_NOTE = "ticket-close/v1 event:"
# What a stamp may point at: a path (absolute, or relative with a directory or
# an extension, optionally :line or :start-end), a PR or issue (owner/repo#N or
# #N), a branch (branch:<name>), a commit SHA, or a store event (event:<N>).
_SEGMENT = r"[A-Za-z0-9_.@+~-]+"
_REF_TARGET = re.compile(
    "|".join(
        (
            rf"/(?:{_SEGMENT}/)*{_SEGMENT}(?::\d+(?:-\d+)?)?",
            rf"(?:{_SEGMENT}/)+{_SEGMENT}(?::\d+(?:-\d+)?)?",
            r"[A-Za-z0-9_.@+~-]*\.[A-Za-z0-9]+(?::\d+(?:-\d+)?)?",
            r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[1-9][0-9]*",
            r"branch:[A-Za-z0-9_./-]+",
            r"[0-9a-f]{7,40}",
            r"event:[1-9][0-9]*",
        )
    )
)


class TicketError(ValueError):
    """The ticket pass cannot accept the requested transition or value."""


class GateRequired(TicketError):
    """A person has not explicitly approved the named gate."""


class StoreUnavailable(RuntimeError):
    """The configured store cannot be used safely."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class TicketHeld(StoreUnavailable):
    """Another writer holds the ticket; this write was refused, not queued."""

    def __init__(self, key: str, holder: str, *, detail: str | None = None):
        detail = detail or "This write was refused, not queued; retry after that writer finishes."
        super().__init__(f"ticket {key} already has a live writer: {holder}. {detail}", code="held")
        self.key = key
        self.holder = holder


def _writer_name(writer: str | None) -> str:
    """Name a store handle so a refused second writer can be told who holds the ticket.

    The name is the caller's writer label (``user@host`` by default) plus this
    process and a per-handle token, so two handles never share a name.
    """

    label = (writer or "").strip()
    if not label:
        try:
            user = getpass.getuser()
        except (KeyError, OSError):
            user = "unknown"
        label = f"{user}@{socket.gethostname()}"
    label = " ".join(label.split())[:120]
    return f"{label} (pid {os.getpid()}, handle {secrets.token_hex(3)})"


# One live pass per ticket. A pass takes the ticket's pass record when it
# starts and keeps it, across processes, until it ends. The record names the
# pass and the writer that started it; it is not the ticket's work claim.
_PASS_PREFIX = "ticket-pass/v1 "


def _release_command(key: str, pass_id: str) -> str:
    return f"python3 ticket.py release-pass {key} {pass_id}"


def _held_by_pass(key: str, live: Mapping[str, Any]) -> TicketHeld:
    return TicketHeld(
        key,
        str(live["holder"]),
        detail=(
            f"Its pass {live['id']} is live, so this write was refused, not queued; retry after that "
            "pass ends. If that pass crashed, a person clears its record with "
            f"`{_release_command(key, str(live['id']))}` (add `--repo <path>` for a markdown card)."
        ),
    )


def _check_pass(
    key: str, live: Mapping[str, Any] | None, mine: str | None, ended: str | None
) -> None:
    """Refuse a write unless this handle may write under the ticket's pass record.

    A handle bound to a pass writes only while that pass is the live one. An
    unbound handle writes only while no pass is live. A handle whose pass has
    ended writes nothing until it starts a new pass.
    """

    if ended is not None:
        raise StoreUnavailable(
            f"pass {ended} on ticket {key} has ended, so nothing was written. Start a new pass to write again.",
            code="pass_ended",
        )
    if mine is None:
        if live is not None:
            raise _held_by_pass(key, live)
        return
    if live is not None and live["id"] == mine:
        return
    if live is not None:
        raise _held_by_pass(key, live)
    raise StoreUnavailable(
        f"pass {mine} is no longer live on ticket {key}, so nothing was written. Start a new pass to write again.",
        code="pass_ended",
    )


def _new_pass_id() -> str:
    return secrets.token_hex(8)


@dataclass(frozen=True)
class Ticket:
    key: str
    title: str
    status: str
    description: str
    acceptance_criteria: tuple[str, ...]
    links: tuple[dict[str, str], ...] = ()
    updated_at: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Ticket":
        required = ("key", "title", "status", "description", "acceptance_criteria")
        missing = [name for name in required if name not in value]
        if missing:
            raise TicketError(f"ticket is missing: {', '.join(missing)}")
        criteria = value["acceptance_criteria"]
        if not isinstance(criteria, list) or not criteria or not all(
            isinstance(item, str) and item.strip() for item in criteria
        ):
            raise TicketError("acceptance_criteria must be a non-empty string list")
        links = value.get("links", [])
        if not isinstance(links, list):
            raise TicketError("links must be a list")
        normalized_links: list[dict[str, str]] = []
        for link in links:
            if not isinstance(link, Mapping) or not link.get("kind") or not link.get("url"):
                raise TicketError("each link needs kind and url")
            normalized_links.append({"kind": str(link["kind"]), "url": str(link["url"])})
        return cls(
            key=str(value["key"]),
            title=str(value["title"]),
            status=str(value["status"]),
            description=str(value["description"]),
            acceptance_criteria=tuple(item.strip() for item in criteria),
            links=tuple(normalized_links),
            updated_at=str(value["updated_at"]) if value.get("updated_at") else None,
        )


class TicketSource(Protocol):
    """A live source returns the current ticket on every pull."""

    def pull(self, key: str) -> Ticket: ...


class FileTicketSource:
    """Fixture and local-file source used by tests and dry runs."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def pull(self, key: str) -> Ticket:
        ticket = Ticket.from_mapping(json.loads(self.path.read_text(encoding="utf-8")))
        if ticket.key != key:
            raise TicketError(f"source returned {ticket.key!r}, expected {key!r}")
        return ticket


@dataclass(frozen=True)
class Criterion:
    text: str
    passes: bool = False
    review_evidence: str | None = None
    review_round: int | None = None


@dataclass
class PlanStep:
    name: str
    status: str = "pending"
    skip: str | None = None


class PlanCard:
    """Acceptance criteria and explicit skip records for one ticket.

    ``passes`` is not settable here: only ``TicketPass.record_review`` flips it,
    after the store has recorded the review round the evidence names.
    """

    def __init__(self, ticket: Ticket):
        self.key = ticket.key
        self.title = ticket.title
        self._criteria = tuple(Criterion(text) for text in ticket.acceptance_criteria)
        self.steps = [PlanStep(name) for name in STEPS]
        self.diff_statement: str | None = None
        self.open_questions: list[str] = []

    @property
    def acceptance_criteria(self) -> tuple[Criterion, ...]:
        return self._criteria

    def add_open_question(self, question: str) -> None:
        question = (question or "").strip()
        if not question:
            raise TicketError("an open question must be non-empty text")
        self.open_questions.append(question)

    def skip_step(
        self,
        name: str,
        reason: str | None,
        *,
        diff_statement: str | None = None,
    ) -> None:
        reason = (reason or "").strip()
        if not reason:
            raise TicketError("a skipped step requires skip: <reason>")
        try:
            step = next(item for item in self.steps if item.name == name)
        except StopIteration as exc:
            raise TicketError(f"unknown plan step: {name}") from exc
        if name == "plan":
            statement = (diff_statement or "").strip()
            if not _one_sentence(statement):
                raise TicketError("the plan may be skipped only for a one-sentence diff")
            self.diff_statement = statement
        step.status = "skipped"
        step.skip = reason

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": PLAN_SCHEMA,
            "key": self.key,
            "title": self.title,
            "acceptance_criteria": [asdict(item) for item in self._criteria],
            "steps": [
                {key: value for key, value in asdict(item).items() if value is not None}
                for item in self.steps
            ],
            "open_questions": list(self.open_questions),
        }
        if self.diff_statement is not None:
            result["diff_statement"] = self.diff_statement
        return result

    def render(self) -> str:
        lines = [f"# Plan — {self.key}: {self.title}", "", "## Acceptance criteria"]
        for item in self._criteria:
            line = f"- [ ] passes: {str(item.passes).lower()} — {item.text}"
            if item.review_evidence:
                line += f" (review round {item.review_round}: {item.review_evidence})"
            lines.append(line)
        lines.extend(("", "## Steps"))
        for step in self.steps:
            if step.status == "skipped":
                lines.append(f"- {step.name}: skip: {step.skip}")
            else:
                lines.append(f"- {step.name}: {step.status}")
        if self.diff_statement:
            lines.extend(("", f"Diff: {self.diff_statement}"))
        if self.open_questions:
            lines.extend(("", "## Open questions"))
            lines.extend(f"- {question}" for question in self.open_questions)
        return "\n".join(lines) + "\n"


def _one_sentence(text: str) -> bool:
    if not text or "\n" in text or "\r" in text:
        return False
    parts = [part for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    return len(parts) == 1


def _recorded_rounds(reviews: Sequence[Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
    return {review["round"]: review for review in reviews if type(review.get("round")) is int}


def _verified(criterion: Criterion, rounds: Mapping[int, Mapping[str, Any]]) -> bool:
    """A criterion passes only when its evidence names a round the store recorded."""

    if not criterion.passes or type(criterion.review_round) is not int:
        return False
    review = rounds.get(criterion.review_round)
    return review is not None and review.get("evidence") == criterion.review_evidence


@dataclass(frozen=True)
class DecisionSource:
    what: str
    ref: str
    who: str | None = None
    text: str | None = None

    def as_dict(self) -> dict[str, str]:
        value = {"what": self.what, "ref": self.ref}
        if self.who:
            value["who"] = self.who
        if self.text:
            value["text"] = self.text
        return value


@dataclass(frozen=True)
class DecisionRecord:
    who: str
    who_kind: str
    standing: str
    source: DecisionSource
    effect: str
    tags: tuple[dict[str, str], ...]
    verbatim: str
    phase: str
    evidence: str | None = None
    relayed_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["source"] = self.source.as_dict()
        result["tags"] = [dict(tag) for tag in self.tags]
        return {key: value for key, value in result.items() if value is not None}


@dataclass(frozen=True)
class ReviewRecord:
    round: int
    verdict: str
    findings: tuple[str, ...] = ()
    reviewer: str | None = None
    evidence: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["findings"] = list(self.findings)
        return {key: value for key, value in result.items() if value is not None}


@dataclass(frozen=True)
class TriedRecord:
    what: str
    phase: str
    outcome: str | None = None
    evidence: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


class StoreAdapter(Protocol):
    """The complete persistence boundary used by a ticket pass."""

    def write_stamp(
        self,
        point: str,
        phase: str,
        next_skills: Sequence[str] = (),
        refs: Sequence[str] = (),
    ) -> None: ...

    def confirm_close(self, position: int, point: str) -> None: ...

    def record_decision(self, decision: DecisionRecord) -> None: ...

    def record_review(self, review: ReviewRecord) -> None: ...

    def record_plan(self, plan: Mapping[str, Any]) -> str: ...

    def record_tried(self, tried: TriedRecord) -> None: ...

    def read_card(self) -> dict[str, Any]: ...

    def take_pass(self) -> str: ...

    def join_pass(self, pass_id: str) -> None: ...

    def release_pass(self, reason: str) -> None: ...

    def clear_pass(self, pass_id: str) -> None: ...


def _validate_phase(phase: str) -> None:
    if phase not in PHASES:
        raise TicketError(f"unknown phase: {phase}")


def _text(value: str | None, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise TicketError(f"{name} must be non-empty text")


def _ref(value: str | None, name: str) -> None:
    _text(value, name)
    if len(value) > 300 or "\n" in value or "\r" in value:
        raise TicketError(f"{name} must be a one-line reference of at most 300 characters")


def _ref_target(value: str | None, name: str) -> None:
    _ref(value, name)
    if not _REF_TARGET.fullmatch(value):
        raise TicketError(
            f"{name} must be a path, PR or issue id, branch:<name>, commit SHA, or event:<N>: {value!r}"
        )


def _validate_stamp(
    point: str, phase: str, next_skills: Sequence[str], refs: Sequence[str]
) -> None:
    _validate_phase(phase)
    _text(point, "point")
    if len(point) > 600:
        raise TicketError("point must be at most 600 characters")
    if len(next_skills) > 12 or any(
        not isinstance(skill, str) or len(skill) > 80 or not _SKILL.fullmatch(skill)
        for skill in next_skills
    ):
        raise TicketError("next_skills contains an invalid skill reference")
    if len(refs) > 20:
        raise TicketError("refs may contain at most 20 references")
    for ref in refs:
        _ref_target(ref, "ref")


def _validate_decision(decision: DecisionRecord) -> None:
    _validate_phase(decision.phase)
    _text(decision.who, "who")
    _text(decision.effect, "effect")
    _text(decision.verbatim, "verbatim")
    if decision.who_kind not in ("person", "role"):
        raise TicketError("who_kind must be person or role")
    if decision.who_kind == "role" and not decision.relayed_by:
        raise TicketError("a role decision requires relayed_by")
    if decision.standing not in ("lean", "settled"):
        raise TicketError("standing must be lean or settled")
    if decision.source.what not in OUTSIDE_EVENTS:
        raise TicketError("decision source has an unknown outside event")
    _ref(decision.source.ref, "decision source ref")
    if decision.evidence is not None:
        _ref(decision.evidence, "decision evidence")
    if not decision.tags:
        raise TicketError("a decision requires at least one tag")
    for tag in decision.tags:
        if set(tag) != {"type", "id"} or tag["type"] not in TAG_TYPES:
            raise TicketError("a decision tag requires a known type and id")
        _ref(tag["id"], "decision tag id")


def _validate_review(review: ReviewRecord) -> None:
    if type(review.round) is not int or review.round < 1 or review.verdict not in ("pass", "changes"):
        raise TicketError("invalid review record")
    if len(review.findings) > 20:
        raise TicketError("a review may record at most 20 findings")
    for finding in review.findings:
        _text(finding, "review finding")
    if review.reviewer is not None:
        _text(review.reviewer, "reviewer")
    if review.evidence is not None:
        _ref(review.evidence, "review evidence")


def _validate_plan(plan: Mapping[str, Any], key: str) -> None:
    if not isinstance(plan, Mapping) or plan.get("schema") != PLAN_SCHEMA:
        raise TicketError(f"a plan snapshot must carry schema {PLAN_SCHEMA}")
    if plan.get("key") != key:
        raise TicketError("plan snapshot belongs to another ticket")
    revision = plan.get("revision")
    if type(revision) is not int or revision < 1:
        raise TicketError("a plan snapshot requires a revision of at least 1")
    criteria = plan.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        raise TicketError("a plan snapshot requires its acceptance criteria")


def _validate_tried(tried: TriedRecord) -> None:
    _text(tried.what, "what")
    _validate_phase(tried.phase)
    if tried.outcome is not None:
        _text(tried.outcome, "outcome")
    if tried.evidence is not None:
        _ref(tried.evidence, "tried evidence")


def _require_pending_close(stamps: Sequence[Mapping[str, Any]], position: int, point: str) -> None:
    """Refuse unless the stamp at ``position`` is still the last stamp and that close.

    Stamps are only ever appended, so a position names one stamp in either store.
    """

    if (
        type(position) is not int
        or position < 0
        or position != len(stamps) - 1
        or stamps[position].get("point") != point
        or stamps[position].get("phase") != "close"
    ):
        raise StoreUnavailable("the last stamp is no longer the pending close; nothing was confirmed")


def _blank_card(key: str, title: str) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "status": "open",
        "stamps": [],
        "decisions": [],
        "reviews": [],
        "plan": None,
        "plan_ref": None,
        "tried": [],
        "pass": None,
    }


class MarkdownStore:
    """Ignored ``.ticket/<key>.md`` fallback with embedded structured data.

    Every write holds ``.ticket/<key>.md.lock`` for the whole read, change and
    replace. A writer prepares a new file that already holds its name and its
    exclusive ``flock``, then links it into place: owning the lock and naming
    the owner happen in one step. The node keeps that name until the owner
    removes it at release, so every refusal names the live holder. Nothing is
    ever written through the lock path, and a node there that is not a
    regular, singly-linked file is refused. The lock is taken without
    waiting. If the holding process dies, the operating system drops its
    ``flock`` and the next writer replaces the stale node.

    The card's ``pass`` field is the ticket's pass record: the live pass's id
    and the writer that started it. It is read and changed under the same
    lock as every other write, and every write checks it first.
    """

    def __init__(self, repo: str | Path, key: str, title: str, *, writer: str | None = None):
        if not _KEY.fullmatch(key):
            raise TicketError(f"unsafe ticket key: {key!r}")
        self.repo = Path(repo).resolve()
        self.key = key
        self.title = title
        self.relative_path = Path(".ticket") / f"{key}.md"
        self.path = self.repo / self.relative_path
        self.lock_path = self.path.with_name(f"{key}.md.lock")
        self.holder = _writer_name(writer)
        self._depth = 0
        # The pass this handle writes for, and the pass it ended, if any.
        self._pass: str | None = None
        self._ended: str | None = None

    @contextlib.contextmanager
    def _writing(self) -> Iterator[None]:
        """Hold this ticket's write lock for one write; refuse if it is held."""

        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._check_ignored()
        self._ensure_directory()
        try:
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise StoreUnavailable(f"cannot open .ticket as a directory for ticket {self.key}: {exc}") from exc
        try:
            fd = self._acquire(directory)
            try:
                self._depth = 1
                yield
            finally:
                self._depth = 0
                self._release(directory, fd)
        finally:
            os.close(directory)

    def _acquire(self, directory: int) -> int:
        """Link a named, locked node into place as the lock; return its descriptor."""

        name = self.lock_path.name
        temporary = f".{name}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
        except OSError as exc:
            raise StoreUnavailable(f"cannot create the write lock for ticket {self.key}: {exc}") from exc
        placed = False
        try:
            os.write(fd, self.holder.encode("utf-8"))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # A bounded number of tries: each retry means the node changed under
            # this writer (released, or replaced as stale) between two calls.
            for _ in range(8):
                try:
                    os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                except FileExistsError:
                    placed = self._replace_stale(directory, temporary, name)
                else:
                    placed = True
                if placed:
                    return fd
            raise StoreUnavailable(
                f"the write lock for ticket {self.key} kept changing while it was being taken; retry"
            )
        except OSError as exc:
            raise StoreUnavailable(f"cannot take the write lock for ticket {self.key}: {exc}") from exc
        finally:
            # After a link the temporary name is a second link to the lock;
            # after a replace it no longer exists. Either way it goes.
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=directory)
            if not placed:
                os.close(fd)

    def _replace_stale(self, directory: int, temporary: str, name: str) -> bool:
        """Refuse a live or unsafe lock node; replace a stale one. False means retry."""

        try:
            other = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return False
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise self._unsafe_node("a symbolic link") from None
            raise
        try:
            node = os.fstat(other)
            if not stat.S_ISREG(node.st_mode):
                raise self._unsafe_node("not a regular file")
            try:
                fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    # Only the owner ever holds this node's exclusive lock, and
                    # the node carried the owner's name before it was linked.
                    raise TicketHeld(self.key, _lock_holder(other)) from None
                raise
            node = os.fstat(other)
            if node.st_nlink == 0:
                # Its owner removed it at release after this writer opened it.
                return False
            if node.st_nlink != 1:
                raise self._unsafe_node(f"linked {node.st_nlink} times")
            # No live owner: the writer that left this node has exited. Replace
            # it under the directory's own lock, held only for the check and
            # the rename, so two writers cannot both replace the same node.
            # That lock is taken without waiting, like the ticket's own.
            self._lock_directory(directory, other)
            try:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (node.st_dev, node.st_ino):
                    return False
                os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                return True
            except FileNotFoundError:
                return False
            finally:
                fcntl.flock(directory, fcntl.LOCK_UN)
        finally:
            os.close(other)

    def _lock_directory(self, directory: int, stale: int) -> None:
        """Take the directory's lock without waiting; refuse after a few quick tries."""

        for attempt in range(_DIRECTORY_LOCK_TRIES):
            try:
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    raise
            if attempt + 1 < _DIRECTORY_LOCK_TRIES:
                time.sleep(_LOCK_PAUSE)
        raise TicketHeld(
            self.key,
            f"another writer replacing the stale lock left by {_lock_holder(stale)}",
            detail="This write was refused, not queued; retry after that writer finishes.",
        )

    def _unsafe_node(self, what: str) -> StoreUnavailable:
        return StoreUnavailable(
            f"{self.relative_path.as_posix()}.lock is {what}, so it was not used as a lock and nothing "
            "was written. Check what it points at, remove it, and retry.",
            code="unsafe_lock",
        )

    def _release(self, directory: int, fd: int) -> None:
        """Remove the lock node while still holding it, then drop the hold."""

        try:
            mine = os.fstat(fd)
            current = os.stat(self.lock_path.name, dir_fd=directory, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (mine.st_dev, mine.st_ino):
                os.unlink(self.lock_path.name, dir_fd=directory)
        except OSError:
            # A node left behind is unlocked once fd closes, so the next
            # writer replaces it as stale.
            pass
        finally:
            os.close(fd)

    def _ensure_directory(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.resolve().relative_to(self.repo)
        except ValueError as exc:
            raise StoreUnavailable(".ticket resolves outside the target repository") from exc

    def _check_ignored(self) -> None:
        # The card and its lock file must both be ignored, so neither can be committed.
        for relative in (self.relative_path, self.relative_path.with_name(self.lock_path.name)):
            result = subprocess.run(
                ["git", "-C", str(self.repo), "check-ignore", "--quiet", "--", str(relative)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise StoreUnavailable(
                    f"{relative.as_posix()} is not git-ignored; add `.ticket/` to "
                    "the target repository's ignore rules and retry. No ignore file was changed."
                )

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _blank_card(self.key, self.title)
        body = self.path.read_text(encoding="utf-8")
        # Only the block this adapter writes last counts. Recorded text is
        # rendered escaped above it, and the JSON encoding keeps it on one line.
        trimmed = body.rstrip("\n")
        start = trimmed.rfind(_JSON_START)
        if start < 0 or not trimmed.endswith(_JSON_END):
            raise StoreUnavailable(f"cannot read structured card data from {self.relative_path}")
        machine = trimmed[start + len(_JSON_START) : len(trimmed) - len(_JSON_END)]
        try:
            card = json.loads(machine)
        except json.JSONDecodeError as exc:
            raise StoreUnavailable(f"cannot read structured card data from {self.relative_path}") from exc
        for name, value in _blank_card(self.key, self.title).items():
            card.setdefault(name, value)
        return card

    def _save(self, card: Mapping[str, Any]) -> None:
        self._check_ignored()
        self._ensure_directory()
        body = _render_markdown_card(card)
        fd, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def write_stamp(
        self,
        point: str,
        phase: str,
        next_skills: Sequence[str] = (),
        refs: Sequence[str] = (),
    ) -> None:
        _validate_stamp(point, phase, next_skills, refs)
        with self._mutate() as card:
            card["stamps"].append(
                {
                    "point": point,
                    "phase": phase,
                    "next_skills": list(next_skills),
                    "refs": list(refs),
                    "close": False,
                }
            )

    def confirm_close(self, position: int, point: str) -> None:
        """Mark that pending close stamp as the close and the card closed.

        The check and the write run under one lock, so no stop can land between them.
        """

        with self._mutate() as card:
            _require_pending_close(card["stamps"], position, point)
            card["stamps"][position]["close"] = True
            card["status"] = "closed"

    def record_decision(self, decision: DecisionRecord) -> None:
        _validate_decision(decision)
        with self._mutate() as card:
            card["decisions"].append(decision.as_dict())

    def record_review(self, review: ReviewRecord) -> None:
        _validate_review(review)
        with self._mutate() as card:
            card["reviews"].append(review.as_dict())

    def record_plan(self, plan: Mapping[str, Any]) -> str:
        _validate_plan(plan, self.key)
        ref = self.relative_path.as_posix()
        with self._mutate() as card:
            card["plan"] = json.loads(json.dumps(plan))
            card["plan_ref"] = ref
        return ref

    def record_tried(self, tried: TriedRecord) -> None:
        _validate_tried(tried)
        with self._mutate() as card:
            card["tried"].append(tried.as_dict())

    def read_card(self) -> dict[str, Any]:
        return self._load()

    @contextlib.contextmanager
    def _mutate(self) -> Iterator[dict[str, Any]]:
        """Under the write lock: load the card, check the pass record, change and save it."""

        with self._writing():
            card = self._load()
            _check_pass(self.key, card.get("pass"), self._pass, self._ended)
            yield card
            self._save(card)

    def take_pass(self) -> str:
        """Start a pass: write the pass record, or refuse by the live pass's holder."""

        with self._writing():
            card = self._load()
            _check_pass(self.key, card.get("pass"), None, None)
            pass_id = _new_pass_id()
            card["pass"] = {"id": pass_id, "holder": self.holder}
            self._save(card)
        self._pass, self._ended = pass_id, None
        return pass_id

    def join_pass(self, pass_id: str) -> None:
        """Continue a live pass from this handle; refuse unless it is that pass."""

        _text(pass_id, "pass id")
        with self._writing():
            _check_pass(self.key, self._load().get("pass"), pass_id, None)
        self._pass, self._ended = pass_id, None

    def release_pass(self, reason: str) -> None:
        """End this handle's pass: clear the record while it is still that pass's."""

        pass_id = self._pass
        if pass_id is None:
            return
        self._pass, self._ended = None, pass_id
        # A refused writer holds the lock only for its own check, so a release
        # that meets one tries again a few times; it never waits on a lock.
        for attempt in range(_RELEASE_TRIES):
            try:
                with self._writing():
                    card = self._load()
                    live = card.get("pass")
                    if live is not None and live.get("id") == pass_id:
                        card["pass"] = None
                        self._save(card)
                return
            except TicketHeld:
                if attempt + 1 == _RELEASE_TRIES:
                    raise
                time.sleep(_LOCK_PAUSE)

    def clear_pass(self, pass_id: str) -> None:
        """A person's recovery: release the named pass, refusing unless it is the live one."""

        self.join_pass(pass_id)
        self.release_pass("cleared by a person")


# Quick, bounded tries: the directory lock and a release never wait longer.
_DIRECTORY_LOCK_TRIES = 10
_RELEASE_TRIES = 20
_LOCK_PAUSE = 0.01


def _lock_holder(fd: int) -> str:
    """The name the lock holder wrote into its node before linking it into place."""

    try:
        name = os.pread(fd, 4096, 0).decode("utf-8", "replace").strip()
    except OSError:
        name = ""
    return name or "a writer whose lock file carries no name"


def _inline(value: Any) -> str:
    """Render recorded text on one line with comment markers defused."""

    text = " / ".join(str(value).splitlines())
    return text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")


def _render_markdown_card(card: Mapping[str, Any]) -> str:
    lines = [
        f"# {_inline(card['key'])} — {_inline(card['title'])}",
        "",
        f"Status: {_inline(card['status'])}",
    ]
    live = card.get("pass")
    if live:
        lines.append(f"Live pass: {_inline(live['id'])}, started by {_inline(live['holder'])}")
    lines.extend(("", "## Stop and close stamps"))
    if card["stamps"]:
        for stamp in card["stamps"]:
            label = "close" if stamp["close"] else "stop"
            lines.append(f"- {label} [{stamp['phase']}]: {_inline(stamp['point'])}")
            if stamp["next_skills"]:
                lines.append(f"  next skills: {_inline(', '.join(stamp['next_skills']))}")
            if stamp["refs"]:
                lines.append(f"  refs: {_inline(', '.join(stamp['refs']))}")
    else:
        lines.append("(none)")
    lines.extend(("", "## Decision records"))
    if card["decisions"]:
        for decision in card["decisions"]:
            lines.append(
                f"- {decision['standing']} [{decision['phase']}], {_inline(decision['who'])}: "
                f"{_inline(decision['verbatim'])}"
            )
            lines.append(f"  effect: {_inline(decision['effect'])}")
    else:
        lines.append("(none)")
    lines.extend(("", "## Review rounds"))
    if card["reviews"]:
        for review in card["reviews"]:
            lines.append(f"- round {review['round']}: {review['verdict']}")
            lines.extend(f"  finding: {_inline(finding)}" for finding in review["findings"])
    else:
        lines.append("(none)")
    lines.extend(("", "## Plan card"))
    plan = card.get("plan")
    if plan:
        lines.append(f"Revision {plan['revision']}")
        for item in plan["acceptance_criteria"]:
            lines.append(f"- passes: {str(item['passes']).lower()} — {_inline(item['text'])}")
        for question in plan.get("open_questions", []):
            lines.append(f"- open question: {_inline(question)}")
    else:
        lines.append("(none)")
    lines.extend(("", "## Tried"))
    if card.get("tried"):
        for tried in card["tried"]:
            line = f"- [{tried['phase']}] {_inline(tried['what'])}"
            if tried.get("outcome"):
                line += f" — {_inline(tried['outcome'])}"
            lines.append(line)
    else:
        lines.append("(none)")
    # json.dumps escapes newlines, so recorded text cannot end this block early.
    machine = json.dumps(card, sort_keys=True, separators=(",", ":"))
    lines.extend(("", _JSON_START.rstrip("\n"), machine, _JSON_END.lstrip("\n"), ""))
    return "\n".join(lines)


class LedgerStore:
    """CLI-only adapter for an installed event ledger.

    The CLI may require a writer identity in its own environment for writes.
    This adapter never supplies one: the person or their wrapper exports it,
    and a refused write surfaces as ``StoreUnavailable`` with the CLI's message.

    One writer per ticket rests on a precondition on the ticket's revision,
    the newest ledger event of the ticket this handle has accounted for. The
    handle's first read pins it (a pass reads the card when it opens), and
    only the handle's own writes move it; later reads do not. Every write
    first checks that the ticket holds no newer event, and refuses, naming
    that event's writer, when it does. Every append then carries the
    idempotency key ``ticket-write/<key>/after/<revision>``. The CLI stores a
    key once and refuses a different event under it, so when two writers
    passed the check from the same revision, only the first append lands and
    the second is refused by the first one's name.

    A pass spans processes, so a handle's revision cannot carry it. The
    ticket's pass record does: a ``session.opened`` event whose purpose names
    the pass, written when the pass starts, and a ``session.closed`` event
    naming it when the pass ends. The check before every append also requires
    that the live pass is this handle's (or, for a handle outside any pass,
    that no pass is live).

    The CLI cannot make that check and the append one step: it has no
    expected-revision option, and the idempotency key only collides with
    another append under the same key. A write made through some other verb
    between the check and the append still lands. So after every append the
    adapter reads the ticket's events again, and if any event landed between
    the revision it checked and its own, it refuses, naming that event's
    writer and saying that its own event landed after it.

    The adapter claims nothing and never writes the ticket's status, owner,
    claim or blocked-by fields, except to mark the ticket closed; that write
    carries the owner, claim and blocked-by fields over unchanged.
    """

    def __init__(
        self,
        key: str,
        title: str,
        *,
        cli: str | Path | None = None,
        db: str | Path | None = None,
        writer: str | None = None,
    ):
        configured = str(cli) if cli is not None else os.environ.get("TICKET_LEDGER_CLI")
        self.cli = configured or shutil.which("ledger") or ""
        self.db = str(db) if db is not None else os.environ.get("LEDGER_DB")
        self.key = key
        self.title = title
        self.holder = _writer_name(writer)
        self._ensured = False
        # The ticket revision this handle has accounted for; None until it
        # first reads or writes the ticket.
        self._seen: int | None = None
        # The pass this handle writes for, and the pass it ended, if any.
        self._pass: str | None = None
        self._ended: str | None = None

    def is_installed(self) -> bool:
        return bool(self.cli) and os.path.isfile(self.cli) and os.access(self.cli, os.X_OK)

    def _invoke(self, *args: str) -> subprocess.CompletedProcess[str]:
        if not self.cli:
            raise StoreUnavailable("no ledger CLI is configured or available on PATH")
        command = [self.cli, "--json"]
        if self.db:
            command.extend(("--db", self.db))
        command.extend(args)
        try:
            return subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as exc:
            raise StoreUnavailable(f"cannot execute ledger CLI: {exc}") from exc

    def _call(self, *args: str) -> tuple[int, dict[str, Any]]:
        result = self._invoke(*args)
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            message = result.stderr.strip() or result.stdout.strip() or "invalid JSON response"
            raise StoreUnavailable(f"ledger CLI failed: {message}") from exc
        if not isinstance(envelope, dict):
            raise StoreUnavailable("ledger CLI returned a non-object envelope")
        return result.returncode, envelope

    def _run(self, *args: str) -> dict[str, Any]:
        returncode, envelope = self._call(*args)
        if returncode != 0 or not envelope.get("ok"):
            error = envelope.get("error") or {}
            raise StoreUnavailable(str(error.get("message") or "ledger error"), code=error.get("code"))
        return envelope["data"]

    def doctor(self) -> dict[str, Any]:
        return self._run("doctor")

    def is_available(self) -> bool:
        try:
            self.doctor()
        except StoreUnavailable:
            return False
        return True

    def _card_envelope(self) -> dict[str, Any] | None:
        """The CLI's card for this item, or None when the item does not exist yet."""

        returncode, envelope = self._call("card", self.key)
        if returncode == 0 and envelope.get("ok"):
            return envelope["data"]
        error = envelope.get("error") or {}
        if error.get("code") == "not_found":
            return None
        raise StoreUnavailable(str(error.get("message") or "ledger card failed"), code=error.get("code"))

    def _events(self) -> list[dict[str, Any]]:
        return self._run("export-item", self.key)["events"]

    def _ensure_item(self) -> None:
        if self._ensured:
            return
        if self._card_envelope() is None:
            try:
                created = self._run("new", "--id", self.key, "--type", "ticket", "--title", self.title)
            except StoreUnavailable as exc:
                # Another writer created the item since this handle looked; the
                # revision check that follows refuses this write by its name.
                if exc.code != "conflict":
                    raise
            else:
                if not self._seen:
                    self._seen = int((created.get("event") or {}).get("id") or 0)
        self._ensured = True

    def _current_events(self, *, taking: bool = False) -> list[dict[str, Any]]:
        """The ticket's events, refusing a newer event or a pass this handle may not write under.

        ``taking`` is the check for starting a pass: no pass may be live.
        """

        self._ensure_item()
        events = self._events()
        if self._seen is None:
            self._seen = _revision(events)
        newer = [event for event in events if int(event["id"]) > self._seen]
        if newer:
            raise self._stale(newer[-1])
        live = _live_pass(events)
        if taking:
            _check_pass(self.key, live, None, None)
        else:
            _check_pass(self.key, live, self._pass, self._ended)
        return events

    def take_pass(self) -> str:
        """Start a pass: append its pass record, or refuse by the live pass's holder."""

        pass_id = _new_pass_id()
        self._append("session.opened", {"purpose": f"{_PASS_PREFIX}{pass_id}"}, taking=True)
        self._pass, self._ended = pass_id, None
        return pass_id

    def join_pass(self, pass_id: str) -> None:
        """Continue a live pass from this handle; refuse unless it is that pass.

        The handle's revision is pinned here; the pass record, not the
        revision, is what kept other passes out between this pass's processes.
        """

        _text(pass_id, "pass id")
        events = self._events()
        _check_pass(self.key, _live_pass(events), pass_id, None)
        self._pass, self._ended = pass_id, None
        self._seen = _revision(events)
        self._ensured = True

    def release_pass(self, reason: str) -> None:
        """End this handle's pass: append the release while its record is still open.

        The release carries its own idempotency key and no revision
        precondition: ending a pass only gives up the ticket, so it is safe
        whatever landed since.
        """

        pass_id = self._pass
        if pass_id is None:
            return
        self._pass, self._ended, self._seen = None, pass_id, None
        if not any(item["id"] == pass_id for item in _open_passes(self._events())):
            return
        body = {"outcome": f"{_PASS_PREFIX}{pass_id} released: {reason}", "source": self.holder}
        self._run(
            "append",
            "--kind",
            "session.closed",
            "--item",
            self.key,
            "--idempotency-key",
            f"ticket-pass/{self.key}/{pass_id}/released",
            "--payload",
            json.dumps(body, sort_keys=True, separators=(",", ":")),
        )

    def clear_pass(self, pass_id: str) -> None:
        """A person's recovery: release the named pass, refusing unless it is the live one."""

        self.join_pass(pass_id)
        self.release_pass("cleared by a person")

    def _stale(self, event: Mapping[str, Any] | None) -> TicketHeld:
        if event is None:
            return TicketHeld(self.key, "another writer the ledger did not name")
        return TicketHeld(
            self.key,
            _event_writer(event),
            detail=(
                f"It wrote ledger event {int(event['id'])} after this pass read the ticket, so this "
                "write was refused, not queued. Start a new pass, which reads that writer's "
                "changes, before writing again."
            ),
        )

    def write_stamp(
        self,
        point: str,
        phase: str,
        next_skills: Sequence[str] = (),
        refs: Sequence[str] = (),
    ) -> None:
        _validate_stamp(point, phase, next_skills, refs)
        payload: dict[str, Any] = {"point": point, "phase": phase}
        if next_skills:
            payload["skills"] = list(next_skills)
        if refs:
            payload["refs"] = list(refs)
        self._append("item.stopped", payload)

    def confirm_close(self, position: int, point: str) -> None:
        """Name that pending close's stop event as the close, then mark the item closed.

        Both appends follow the revision the check read, so a stop that lands
        after the check refuses the close instead of following it.
        """

        events = self._current_events()
        stops = [event for event in events if event["kind"] == "item.stopped"]
        _require_pending_close([event["payload"] for event in stops], position, point)
        self._append("item.note", {"text": f"{_CLOSE_NOTE}{int(stops[position]['id'])}"})
        self._append("item.state", {**_carried_state(events), "status": "closed"})

    def record_decision(self, decision: DecisionRecord) -> None:
        _validate_decision(decision)
        source_data = self._append("outside.recorded", decision.source.as_dict())
        source_event_id = int(source_data["event"]["id"])
        payload = decision.as_dict()
        payload.pop("source")
        payload["source"] = decision.source.ref
        payload["source_event_id"] = source_event_id
        self._append("decision.recorded", payload)

    def record_review(self, review: ReviewRecord) -> None:
        _validate_review(review)
        self._append("review.recorded", review.as_dict())

    def record_plan(self, plan: Mapping[str, Any]) -> str:
        _validate_plan(plan, self.key)
        text = _PLAN_NOTE + json.dumps(plan, sort_keys=True, separators=(",", ":"))
        data = self._append("item.note", {"text": text})
        return f"event:{int(data['event']['id'])}"

    def record_tried(self, tried: TriedRecord) -> None:
        _validate_tried(tried)
        self._append("item.tried", tried.as_dict())

    def _append(self, kind: str, payload: Mapping[str, Any], *, taking: bool = False) -> dict[str, Any]:
        """Append one event only if the ticket is still at this handle's revision and pass.

        The check and the append are two CLI calls, so the append is followed
        by a read that refuses when anything landed between them.
        """

        self._current_events(taking=taking)
        seen = self._seen
        body = dict(payload)
        if kind in _NAMED_KINDS:
            # The writer's name rides on the event, so a refused writer can be
            # told who wrote first, and two writers never send the same event.
            body["source"] = self.holder
        idempotency_key = f"ticket-write/{self.key}/after/{seen}"
        try:
            data = self._run(
                "append",
                "--kind",
                kind,
                "--item",
                self.key,
                "--idempotency-key",
                idempotency_key,
                "--payload",
                json.dumps(body, sort_keys=True, separators=(",", ":")),
            )
        except StoreUnavailable as exc:
            if exc.code != "conflict":
                raise
            first = next(
                (event for event in self._events() if event.get("idempotency_key") == idempotency_key),
                None,
            )
            raise self._stale(first) from exc
        landed = int(data["event"]["id"])
        self._verify_landed(seen, landed)
        self._seen = landed
        return data

    def _verify_landed(self, seen: int | None, landed: int) -> None:
        """Refuse, by name, when another event landed between this write's check and its append."""

        between = [event for event in self._events() if (seen or 0) < int(event["id"]) < landed]
        if not between:
            return
        event = between[-1]
        raise TicketHeld(
            self.key,
            _event_writer(event),
            detail=(
                f"It wrote ledger event {int(event['id'])} between this write's revision check and its "
                f"append. The ledger CLI cannot make the two one step, so this write still landed after "
                f"it, as ledger event {landed}, and was not undone. Start a new pass, which reads both, "
                "and decide with the person whether either needs repairing."
            ),
        )

    def read_card(self) -> dict[str, Any]:
        card = self._card_envelope()
        if card is None:
            if self._seen is None:
                self._seen = 0
            return _blank_card(self.key, self.title)
        events = self._events()
        if self._seen is None:
            self._seen = _revision(events)
        return _normalize_ledger_card(card, events)


# Event kinds whose payload may carry a ``source``; the adapter puts the
# writer's name there. A decision's ``source`` is its outside event's ref.
_NAMED_KINDS = frozenset(
    (
        "item.stopped",
        "item.note",
        "item.tried",
        "item.state",
        "review.recorded",
        "outside.recorded",
        "session.opened",
        "session.closed",
    )
)
_HOLDER = re.compile(r".+ \(pid \d+, handle [0-9a-f]{6}\)")


def _revision(events: Sequence[Mapping[str, Any]]) -> int:
    return max((int(event["id"]) for event in events), default=0)


def _open_passes(events: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Pass records started and not yet released, oldest first."""

    open_: dict[str, dict[str, str]] = {}
    for event in events:
        payload = event.get("payload") or {}
        if event.get("kind") == "session.opened":
            text = str(payload.get("purpose") or "")
            if text.startswith(_PASS_PREFIX) and text[len(_PASS_PREFIX) :].strip():
                pass_id = text[len(_PASS_PREFIX) :].strip()
                open_.setdefault(pass_id, {"id": pass_id, "holder": _event_writer(event)})
        elif event.get("kind") == "session.closed":
            text = str(payload.get("outcome") or "")
            if text.startswith(_PASS_PREFIX) and text[len(_PASS_PREFIX) :].split():
                open_.pop(text[len(_PASS_PREFIX) :].split()[0], None)
    return list(open_.values())


def _live_pass(events: Sequence[Mapping[str, Any]]) -> dict[str, str] | None:
    """The ticket's live pass: the oldest pass record not yet released."""

    passes = _open_passes(events)
    return passes[0] if passes else None


def _event_writer(event: Mapping[str, Any]) -> str:
    """Name who wrote an event: the adapter's writer name, else the CLI's attribution."""

    payload = event.get("payload") or {}
    named = payload.get("source") if event.get("kind") in _NAMED_KINDS else None
    if isinstance(named, str) and _HOLDER.fullmatch(named):
        return named
    who = event.get("actor") or event.get("harness") or event.get("session")
    if who:
        return f"{who} (ledger event {int(event['id'])})"
    return f"the writer of ledger event {int(event['id'])}"


def _carried_state(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The owner, claim and blocked-by fields of the ticket's latest state event."""

    latest: Mapping[str, Any] = {}
    for event in events:
        if event["kind"] == "item.state":
            latest = event.get("payload") or {}
    return {name: latest[name] for name in ("owner", "claim", "blocked_by") if latest.get(name)}


def _normalize_ledger_card(card: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    # The adapter's writer name rides in ``source``; it is not card content.
    outside = {
        int(event["id"]): {name: value for name, value in event["payload"].items() if name != "source"}
        for event in events
        if event["kind"] == "outside.recorded"
    }
    decisions: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    stamps: list[dict[str, Any]] = []
    stop_ids: list[int] = []
    close_id: int | None = None
    tried: list[dict[str, Any]] = []
    plan: dict[str, Any] | None = None
    plan_ref: str | None = None
    for event in events:
        payload = dict(event["payload"])
        if event["kind"] == "decision.recorded":
            source_id = payload.pop("source_event_id", None)
            payload.pop("source", None)
            payload["source"] = dict(outside.get(int(source_id), {})) if source_id else {}
            decisions.append(payload)
        elif event["kind"] == "review.recorded":
            payload.pop("source", None)
            payload.setdefault("findings", [])
            reviews.append(payload)
        elif event["kind"] == "item.stopped":
            stamps.append(
                {
                    "point": payload["point"],
                    "phase": payload["phase"],
                    "next_skills": list(payload.get("skills") or ()),
                    "refs": list(payload.get("refs") or ()),
                    "close": False,
                }
            )
            stop_ids.append(int(event["id"]))
        elif event["kind"] == "item.tried":
            payload.pop("source", None)
            tried.append(payload)
        elif event["kind"] == "item.note" and str(payload.get("text", "")).startswith(_PLAN_NOTE):
            try:
                plan = json.loads(payload["text"][len(_PLAN_NOTE) :])
            except json.JSONDecodeError as exc:
                raise StoreUnavailable(f"ledger event {event['id']} holds an unreadable plan card") from exc
            plan_ref = f"event:{int(event['id'])}"
        elif event["kind"] == "item.note" and str(payload.get("text", "")).startswith(_CLOSE_NOTE):
            named = payload["text"][len(_CLOSE_NOTE) :]
            close_id = int(named) if named.isdigit() else None
    status = card["status"]
    # Only the stop the latest close note names is the close, and only while closed.
    if status == "closed" and close_id in stop_ids:
        stamps[stop_ids.index(close_id)]["close"] = True
    return {
        "key": card["item_id"],
        "title": card["title"],
        "status": status,
        "stamps": stamps,
        "decisions": decisions,
        "reviews": reviews,
        "plan": plan,
        "plan_ref": plan_ref,
        "tried": tried,
        "pass": _live_pass(events),
    }


def choose_store(
    repo: str | Path,
    key: str,
    title: str,
    *,
    cli: str | Path | None = None,
    db: str | Path | None = None,
    writer: str | None = None,
) -> StoreAdapter:
    """Use the ledger when its CLI is installed; markdown only when it is not.

    An installed CLI whose doctor check fails is reported, never replaced by the
    markdown card: a silent fallback would split one ticket across two stores.
    """

    ledger = LedgerStore(key, title, cli=cli, db=db, writer=writer)
    if not ledger.is_installed():
        return MarkdownStore(repo, key, title, writer=writer)
    try:
        ledger.doctor()
    except StoreUnavailable as exc:
        if exc.code == "missing_db":
            raise StoreUnavailable(
                f"the ledger database does not exist yet ({exc}). Initialise it by "
                "creating this ticket's item with the ledger CLI's `new` verb, then retry.",
                code="missing_db",
            ) from exc
        raise
    return ledger


@dataclass(frozen=True)
class MoreInfoExit:
    destination: str
    waiting: str
    drafted_ask: str | None = None


def _go_effect(gate: str, revision: int, pass_id: str | None = None) -> str:
    """The effect a go decision records; a plan go names the pass that presented the plan."""

    if gate == "plan" and pass_id:
        return f"The plan gate is approved for plan revision {revision} presented by pass {pass_id}."
    return f"The {gate} gate is approved for plan revision {revision}."


def _plan_pass(card: Mapping[str, Any]) -> str | None:
    plan = card.get("plan")
    value = plan.get("pass") if isinstance(plan, Mapping) else None
    return value if isinstance(value, str) and value else None


def _has_go(card: Mapping[str, Any], gate: str, revision: int, key: str) -> bool:
    """A gate is open only through a person's settled ratification in the store.

    A plan go counts only for the plan revision the stored plan's own pass presented.
    """

    if revision < 1:
        return False
    effect = _go_effect(gate, revision, _plan_pass(card) if gate == "plan" else None)
    tag = {"type": "ticket", "id": key}
    for decision in card.get("decisions") or ():
        source = decision.get("source") or {}
        if (
            decision.get("effect") == effect
            and decision.get("who_kind") == "person"
            and decision.get("standing") == "settled"
            and source.get("what") == "ratification"
            and tag in (decision.get("tags") or ())
        ):
            return True
    return False


def _plan_go_point(revision: int) -> str:
    return f"Waiting at plan go for plan revision {revision}."


def _outward_go_point(review_round: int) -> str:
    return f"Waiting at outward go after review round {review_round}."


def _has_stamp(card: Mapping[str, Any], point: str, phase: str) -> bool:
    return any(
        stamp.get("point") == point and stamp.get("phase") == phase for stamp in card.get("stamps") or ()
    )


def _close_stamp_problems(
    last: Mapping[str, Any], point: str, next_skills: Sequence[str], refs: Sequence[str]
) -> list[str]:
    problems: list[str] = []
    if last.get("point") != point or last.get("phase") != "close":
        problems.append("the last stamp is not this close point")
    if list(last.get("next_skills") or ()) != list(next_skills):
        problems.append("the next skills differ")
    if list(last.get("refs") or ()) != list(refs):
        problems.append("the references differ")
    return problems


def _close_card_problems(card: Mapping[str, Any], key: str, revision: int, rounds: int) -> list[str]:
    problems = [
        f"the {gate} go decision is missing"
        for gate in ("plan", "outward")
        if not _has_go(card, gate, revision, key)
    ]
    if len(card.get("reviews") or ()) != rounds:
        problems.append("the review rounds differ")
    return problems


def _reconcile(
    stored: Mapping[str, Any], ticket: Ticket, reviews: Sequence[Mapping[str, Any]]
) -> tuple[PlanCard, list[str], bool]:
    """Rebuild a plan from its snapshot against the fresh ticket.

    Returns the plan, a list of flagged differences, and whether the
    acceptance criteria changed (which needs a new plan and plan go).
    """

    plan = PlanCard(ticket)
    flags: list[str] = []
    changed = False
    rounds = _recorded_rounds(reviews)
    stored_criteria = {
        item.get("text"): item for item in stored.get("acceptance_criteria") or () if isinstance(item, Mapping)
    }
    restored: list[Criterion] = []
    for text in ticket.acceptance_criteria:
        item = stored_criteria.get(text)
        if item is None:
            flags.append(f"acceptance criterion is new or changed on the ticket: {text}")
            changed = True
            restored.append(Criterion(text))
            continue
        candidate = Criterion(
            text,
            item.get("passes") is True,
            item.get("review_evidence"),
            item.get("review_round"),
        )
        if candidate.passes and not _verified(candidate, rounds):
            flags.append(f"passes is not backed by a recorded review round, reset to false: {text}")
            candidate = Criterion(text)
        restored.append(candidate)
    for text in stored_criteria:
        if text not in ticket.acceptance_criteria:
            flags.append(f"acceptance criterion was removed from the ticket: {text}")
            changed = True
    plan._criteria = tuple(restored)
    for step_data in stored.get("steps") or ():
        step = next((item for item in plan.steps if item.name == step_data.get("name")), None)
        if step is None:
            continue
        if step_data.get("status") == "skipped" and step_data.get("skip"):
            step.status, step.skip = "skipped", str(step_data["skip"])
        elif step_data.get("status") in ("pending", "complete"):
            step.status = step_data["status"]
    if stored.get("diff_statement"):
        plan.diff_statement = str(stored["diff_statement"])
    plan.open_questions = [str(item) for item in stored.get("open_questions") or ()]
    if stored.get("ticket_status") not in (None, ticket.status):
        flags.append(f"ticket status changed: {stored['ticket_status']} -> {ticket.status}")
    if stored.get("ticket_updated_at") not in (None, ticket.updated_at):
        flags.append("ticket was updated since the plan card was written")
    return plan, flags, changed


def _ends_on_failure(method):
    """A failure past validation ends the pass: its record is released, then the error raises.

    A ``TicketError`` (an invalid value, a wrong phase, a gate not yet
    approved) changes nothing in the store, so the pass stays live.
    """

    @functools.wraps(method)
    def guarded(self: "TicketPass", *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except TicketError:
            raise
        except BaseException as exc:
            self._end_quietly(f"a store call failed: {type(exc).__name__}")
            raise

    return guarded


class TicketPass:
    """Guarded transitions for one pass; outward actions remain caller-owned.

    The phase, the review-round count and the gates are read-only. Gates are
    derived from go decisions the store recorded, never from session state.

    A pass holds the ticket's pass record from ``open`` until it ends, across
    processes: a later process continues it with ``open(..., pass_id=...)``.
    It ends, releasing the record, when the ticket is closed, at a
    more-information exit, at the review-round cap, when a store call fails
    or a write is refused, when an exception leaves a ``with`` block over the
    pass, or on ``end()``. The plan go and outward go stops keep it live, so
    the person's go is recorded by the pass that asked for it.
    """

    def __init__(self, ticket: Ticket, store: StoreAdapter):
        self._ticket = ticket
        self.store = store
        self.resume_card: dict[str, Any] | None = None
        self.reconciliation: tuple[str, ...] = ()
        self._phase = "orient"
        self._plan: PlanCard | None = None
        self._revision = 0
        self._plan_ref: str | None = None
        self._rounds = 0
        self._pass_id: str | None = None
        # The pass that presented the current plan revision.
        self._plan_pass: str | None = None
        self._ended = False

    @classmethod
    def open(
        cls,
        key: str,
        source: TicketSource,
        store: StoreAdapter,
        *,
        resume: bool = False,
        pass_id: str | None = None,
    ) -> "TicketPass":
        """Start a pass, or continue the live pass ``pass_id`` from a new process.

        Starting takes the ticket's pass record and is refused, by the holder's
        name, while another pass is live. Continuing presents the pass id this
        pass started with and is refused unless that pass is the live one; it
        always rebuilds the pass from the card.
        """

        ticket = source.pull(key)
        session = cls(ticket, store)
        if pass_id is None:
            session._pass_id = store.take_pass()
        else:
            store.join_pass(pass_id)
            session._pass_id = pass_id
            resume = True
        try:
            # Every open reads the card, which pins the store's view of the
            # ticket for this process; a store refuses a write from an older view.
            card = store.read_card()
            if resume:
                session._resume(card)
        except BaseException as exc:
            session._end_quietly(f"the pass could not be opened: {type(exc).__name__}")
            raise
        if session._phase in ("closed", "stopped"):
            # Nothing is left for this pass to write.
            session.end(f"resumed at {session._phase}")
        return session

    @property
    def pass_id(self) -> str | None:
        """The id a later process presents to continue this pass."""

        return self._pass_id

    @property
    def live(self) -> bool:
        return not self._ended

    def end(self, reason: str = "ended") -> None:
        """End the pass and release its record. Calling it again does nothing."""

        if self._ended:
            return
        self._ended = True
        self.store.release_pass(reason)

    def _end_quietly(self, reason: str) -> None:
        # On a failure path the original error wins. A release that fails too
        # leaves the record in place: the ticket stays refused until a person
        # clears it with the release-pass command, which fails closed.
        with contextlib.suppress(Exception):
            self.end(reason)

    def __enter__(self) -> "TicketPass":
        return self

    def __exit__(self, kind: Any, value: Any, traceback: Any) -> bool:
        if kind is not None:
            self._end_quietly(f"an exception left the pass: {kind.__name__}")
        return False

    @property
    def ticket(self) -> Ticket:
        return self._ticket

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def plan(self) -> PlanCard | None:
        return self._plan

    @property
    def review_round(self) -> int:
        return self._rounds

    @property
    def plan_revision(self) -> int:
        return self._revision

    @_ends_on_failure
    def approved(self, gate: str) -> bool:
        if gate not in ("plan", "outward"):
            raise TicketError(f"unknown gate: {gate}")
        return _has_go(self.store.read_card(), gate, self._revision, self._ticket.key)

    def _resume(self, card: dict[str, Any]) -> None:
        self.resume_card = card
        reviews = list(card.get("reviews") or ())
        self._rounds = len(reviews)
        stored = card.get("plan")
        if stored:
            plan, flags, changed = _reconcile(stored, self._ticket, reviews)
            self._plan = plan
            self._revision = int(stored.get("revision") or 0)
            self._plan_ref = card.get("plan_ref")
            self._plan_pass = _plan_pass(card)
            self.reconciliation = tuple(flags)
        stamps = card.get("stamps") or ()
        # A closed status counts only when the final stamp is the confirmed close
        # and the card still validates; otherwise the pass resumes before close.
        if card.get("status") == "closed":
            problems = self._closed_problems(card, stored, reviews)
            if not problems:
                self._phase = "closed"
                return
            self.reconciliation += (
                "the store is marked closed but the close does not validate: "
                + "; ".join(problems)
                + "; the ticket is not closed",
            )
        if any(stamp.get("phase") == "close" and not stamp.get("close") for stamp in stamps):
            self.reconciliation += ("a close stamp was written but never confirmed; the ticket is not closed",)
        if not stored:
            self._phase = "orient"
            return
        if changed:
            self._phase = "plan"
            return
        # A hard stop is re-entered only when the store holds its stop stamp;
        # otherwise the pass resumes at the step before the stop.
        if not _has_go(card, "plan", self._revision, self._ticket.key):
            if not _has_stamp(card, _plan_go_point(self._revision), "plan"):
                self.reconciliation += (
                    f"no plan go stop stamp for plan revision {self._revision}; resuming at plan",
                )
                self._phase = "plan"
            elif self._plan_pass != self._pass_id:
                # A plan go is recorded only by the pass that presented the
                # plan, so another pass presents it again for its own go.
                self.reconciliation += (
                    f"plan revision {self._revision} was presented by another pass and has no plan go; "
                    "resuming at plan to present it again",
                )
                self._phase = "plan"
            else:
                self._phase = "plan_go"
            return
        last = reviews[-1] if reviews else None
        passed = (
            last is not None
            and last.get("verdict") == "pass"
            and all(item.passes and item.review_round == last.get("round") for item in plan.acceptance_criteria)
        )
        if passed and not _has_stamp(card, _outward_go_point(last.get("round")), "review"):
            self.reconciliation += (
                f"no outward go stop stamp for review round {last.get('round')}; resuming at build",
            )
            passed = False
        if passed:
            self._phase = "outward_go"
        elif self._rounds >= MAX_REVIEW_ROUNDS:
            self._phase = "stopped"
        else:
            self._phase = "build"

    def _closed_problems(
        self,
        card: Mapping[str, Any],
        stored: Mapping[str, Any] | None,
        reviews: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        """What a closed card fails of the checks ``close`` made before confirming."""

        stamps = card.get("stamps") or []
        last = stamps[-1] if stamps else {}
        if not last.get("close"):
            return ["its final stamp is not the confirmed close"]
        point = last.get("point")
        next_skills = list(last.get("next_skills") or ())
        refs = list(last.get("refs") or ())
        problems = _close_stamp_problems(last, point, next_skills, refs)
        try:
            _validate_stamp(point, "close", next_skills, refs)
        except TicketError as exc:
            problems.append(f"the close stamp is malformed ({exc})")
        problems += _close_card_problems(card, self._ticket.key, self._revision, self._rounds)
        final = reviews[-1] if reviews else {}
        rounds = _recorded_rounds(reviews)
        criteria = [
            Criterion(item.get("text"), item.get("passes") is True, item.get("review_evidence"), item.get("review_round"))
            for item in (stored or {}).get("acceptance_criteria") or ()
            if isinstance(item, Mapping)
        ]
        if (
            final.get("verdict") != "pass"
            or not criteria
            or not all(item.review_round == final.get("round") and _verified(item, rounds) for item in criteria)
            or not _has_stamp(card, _outward_go_point(final.get("round")), "review")
        ):
            problems.append("the last review round did not pass every criterion to an outward go stop")
        return problems

    def _snapshot(self, plan: PlanCard, revision: int, plan_pass: str | None = None) -> dict[str, Any]:
        data = plan.as_dict()
        data["revision"] = revision
        data["ticket_status"] = self._ticket.status
        if self._ticket.updated_at:
            data["ticket_updated_at"] = self._ticket.updated_at
        presenter = plan_pass or self._plan_pass
        if presenter:
            data["pass"] = presenter
        return data

    def finish_orient(self) -> None:
        self._require_phase("orient")
        self._phase = "research"

    def finish_research(self) -> None:
        self._require_phase("research")
        self._phase = "plan"

    @_ends_on_failure
    def present_plan(self, plan: PlanCard) -> None:
        self._require_phase("plan")
        if plan.key != self._ticket.key:
            raise TicketError("plan card belongs to another ticket")
        if tuple(item.text for item in plan.acceptance_criteria) != self._ticket.acceptance_criteria:
            raise TicketError("the plan card must carry the ticket's acceptance criteria, in order")
        if any(item.passes for item in plan.acceptance_criteria):
            raise TicketError("a presented plan starts with passes: false on every criterion")
        revision = self._revision + 1
        ref = self.store.record_plan(self._snapshot(plan, revision, self._pass_id))
        self.store.write_stamp(
            _plan_go_point(revision),
            "plan",
            ("ticket",),
            (ref,),
        )
        self._plan, self._revision, self._plan_ref = plan, revision, ref
        self._plan_pass = self._pass_id
        self._phase = "plan_go"

    @_ends_on_failure
    def record_go(self, gate: str, *, evidence: str, verbatim: str) -> None:
        expected = {"plan": "plan_go", "outward": "outward_go"}
        if gate not in expected:
            raise TicketError(f"unknown gate: {gate}")
        self._require_phase(expected[gate])
        evidence = evidence.strip()
        verbatim = verbatim.strip()
        if not evidence or not verbatim:
            raise TicketError("a go record requires evidence and verbatim words")
        pass_id = None
        if gate == "plan":
            # The go is bound to the plan this pass presented: the stored plan
            # must still be that revision, presented by this pass.
            stored = self.store.read_card().get("plan") or {}
            if stored.get("revision") != self._revision or stored.get("pass") != self._pass_id:
                raise StoreUnavailable(
                    f"the stored plan is not plan revision {self._revision} as pass {self._pass_id} "
                    "presented it, so no plan go was recorded",
                    code="plan_changed",
                )
            pass_id = self._pass_id
        self.store.record_decision(
            DecisionRecord(
                who="the person",
                who_kind="person",
                standing="settled",
                source=DecisionSource("ratification", evidence),
                effect=_go_effect(gate, self._revision, pass_id),
                tags=({"type": "ticket", "id": self._ticket.key},),
                verbatim=verbatim,
                phase="plan" if gate == "plan" else "review",
                evidence=evidence,
            )
        )

    def begin_build(self) -> None:
        self._require_phase("plan_go")
        if not self.approved("plan"):
            raise GateRequired("plan go is required before build")
        self._phase = "build"

    @_ends_on_failure
    def record_tried(self, what: str, *, outcome: str | None = None, evidence: str | None = None) -> None:
        self.store.record_tried(TriedRecord(what, self._store_phase(), outcome, evidence))

    @_ends_on_failure
    def record_review(
        self,
        verdict: str,
        *,
        evidence: str,
        findings: Sequence[str] = (),
        reviewer: str = "fresh-context reviewer",
        passed_criteria: Sequence[int] = (),
    ) -> None:
        if self._phase not in ("build", "review"):
            raise TicketError(f"cannot record review during {self._phase}")
        if self._plan is None:
            raise TicketError("review requires a plan card")
        if verdict not in ("pass", "changes"):
            raise TicketError("review verdict must be pass or changes")
        evidence = (evidence or "").strip()
        _ref_target(evidence, "review evidence")
        count = len(self._plan.acceptance_criteria)
        indices: list[int] = []
        for index in passed_criteria:
            if type(index) is not int or not 0 <= index < count:
                raise TicketError(f"criterion index out of range: {index!r}")
            if index in indices:
                raise TicketError(f"criterion index repeated: {index}")
            indices.append(index)
        if self._rounds >= MAX_REVIEW_ROUNDS:
            raise TicketError("review round cap is three")
        if verdict == "pass" and sorted(indices) != list(range(count)):
            raise TicketError(
                "a passing review must give evidence for every acceptance criterion in this round"
            )
        review = ReviewRecord(
            round=self._rounds + 1,
            verdict=verdict,
            findings=tuple(findings),
            reviewer=reviewer,
            evidence=evidence,
        )
        _validate_review(review)
        before = self.store.read_card().get("reviews") or []
        if len(before) != self._rounds:
            raise StoreUnavailable("the store's review rounds changed outside this pass; start a new pass to reload")

        self.store.record_review(review)
        rounds = _recorded_rounds(self.store.read_card().get("reviews") or [])
        if review.round not in rounds or len(rounds) != review.round:
            raise StoreUnavailable(f"review round {review.round} was not recorded by the store")
        self._rounds = review.round
        criteria = list(self._plan.acceptance_criteria)
        for index in indices:
            candidate = Criterion(criteria[index].text, True, evidence, review.round)
            if not _verified(candidate, rounds):
                raise StoreUnavailable(f"review round {review.round} does not carry this evidence")
            criteria[index] = candidate
        self._plan._criteria = tuple(criteria)
        self._plan_ref = self.store.record_plan(self._snapshot(self._plan, self._revision))

        if verdict == "pass":
            self.store.write_stamp(
                _outward_go_point(review.round),
                "review",
                ("ticket",),
                (self._plan_ref, evidence),
            )
            self._phase = "outward_go"
        elif review.round == MAX_REVIEW_ROUNDS:
            self.store.write_stamp(
                "Review did not converge after round three.",
                "review",
                ("ticket", "review"),
                (self._plan_ref, evidence),
            )
            self._phase = "stopped"
            self.end("review did not converge after round three")
        else:
            self._phase = "build"

    def require_outward_go(self) -> None:
        """Call before any outward action; the store must hold the outward go."""

        self._require_phase("outward_go")
        if not self.approved("outward"):
            raise GateRequired("outward go is required before any outward action")

    @_ends_on_failure
    def close(
        self,
        point: str,
        *,
        refs: Sequence[str] = (),
        next_skills: Sequence[str] = (),
    ) -> None:
        self._require_phase("outward_go")
        if not self.approved("outward"):
            raise GateRequired("outward go is required before close")
        # The close stamp is written pending and confirmed only after the card
        # reads back correctly, so a failed close never reads as closed later.
        self.store.write_stamp(point, "close", next_skills, refs)
        card = self.store.read_card()
        stamps = card.get("stamps") or []
        last = stamps[-1] if stamps else {}
        problems = _close_stamp_problems(last, point, next_skills, refs)
        problems += _close_card_problems(card, self._ticket.key, self._revision, self._rounds)
        if problems:
            raise StoreUnavailable("close read-back failed: " + "; ".join(problems))
        # Confirm only the stamp just read back: a stop appended since is refused.
        position = len(stamps) - 1
        self.store.confirm_close(position, point)
        card = self.store.read_card()
        stamps = card.get("stamps") or []
        last = stamps[-1] if stamps else {}
        if card.get("status") != "closed" or len(stamps) - 1 != position or not last.get("close"):
            raise StoreUnavailable("close read-back failed: the card is not marked closed")
        problems = _close_stamp_problems(last, point, next_skills, refs)
        problems += _close_card_problems(card, self._ticket.key, self._revision, self._rounds)
        if problems:
            raise StoreUnavailable("close read-back failed: " + "; ".join(problems))
        self._phase = "closed"
        self.end("closed")

    @_ends_on_failure
    def more_info(self, destination: str, question: str, *, role: str | None = None) -> MoreInfoExit:
        if destination not in INFO_EXITS:
            raise TicketError(f"unknown more-info exit: {destination}")
        question = question.strip()
        if not question:
            raise TicketError("a more-info exit requires an exact question")
        if destination == "someone_else":
            role = (role or "").strip()
            if not role:
                raise TicketError("someone_else requires a role")
            waiting = f"waiting on {role}: {question}"
            drafted = f"Could you help with {self._ticket.key}? {question}"
        else:
            waiting = f"waiting on {destination}: {question}"
            drafted = None
        self.store.write_stamp(
            waiting,
            self._store_phase(),
            ("ticket",),
            (self._plan_ref,) if self._plan_ref else (),
        )
        self._phase = "stopped"
        self.end(waiting)
        return MoreInfoExit(destination, waiting, drafted)

    def _store_phase(self) -> str:
        if self._phase == "plan_go":
            return "plan"
        if self._phase == "research":
            return "orient"
        if self._phase == "outward_go":
            return "review"
        return self._phase if self._phase in PHASES else "close"

    def _require_phase(self, expected: str) -> None:
        if self._phase != expected:
            raise TicketError(f"expected phase {expected}, found {self._phase}")


__all__ = [
    "Criterion",
    "DecisionRecord",
    "DecisionSource",
    "FileTicketSource",
    "GateRequired",
    "LedgerStore",
    "MarkdownStore",
    "MoreInfoExit",
    "PlanCard",
    "ReviewRecord",
    "StoreAdapter",
    "StoreUnavailable",
    "Ticket",
    "TicketError",
    "TicketHeld",
    "TicketPass",
    "TicketSource",
    "TriedRecord",
    "choose_store",
]


def main(argv: Sequence[str] | None = None) -> int:
    """Command line for a person: clear the pass record a crashed pass left behind."""

    parser = argparse.ArgumentParser(prog="ticket.py", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    release = commands.add_parser(
        "release-pass",
        help="release a ticket's live pass record, named by its pass id, after that pass crashed",
    )
    release.add_argument("key", help="the ticket key")
    release.add_argument("pass_id", help="the pass id the refusal named")
    release.add_argument("--repo", default=".", help="the target repository (markdown card only)")
    args = parser.parse_args(argv)
    try:
        store = choose_store(args.repo, args.key, args.key, writer="release-pass")
        store.clear_pass(args.pass_id)
    except (StoreUnavailable, TicketError) as exc:
        print(f"ticket.py: {exc}", file=sys.stderr)
        return 1
    print(f"released pass {args.pass_id} on ticket {args.key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""State and adapter helpers for one attended pass over a ticket."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import functools
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
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


# One live session (a "pass") per ticket. A pass writes a claim record first,
# naming its session id, host and start time, and keeps it, across processes,
# until the ticket is closed or a person releases it. No claim expires by
# elapsed time.


def _host() -> str:
    return "".join(socket.gethostname().split()) or "unknown"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _claim_text(claim: Mapping[str, Any]) -> str:
    return f"session {claim['id']}, host {claim['host']}, started {claim['started_at']}"


def _release_command(key: str, pass_id: str) -> str:
    return f"python3 ticket.py release {key} {pass_id}"


def _held_by_pass(key: str, live: Mapping[str, Any]) -> TicketHeld:
    return TicketHeld(
        key,
        _claim_text(live),
        detail=(
            "That session is live, so this write was refused. It ends when the ticket is closed. "
            "If that session is gone, a person releases it with "
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

    ``passes`` is not settable here. It flips only in ``TicketPass``, from a
    review round the store has recorded: ``record_review`` after storing the
    round, or ``record_outward_stop`` completing a stored passing round of the
    current plan revision.
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
    # A plan go names the acceptance criteria it approved, in order; it
    # approves nothing for any other criteria.
    criteria: tuple[str, ...] | None = None

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["source"] = self.source.as_dict()
        result["tags"] = [dict(tag) for tag in self.tags]
        if self.criteria is not None:
            result["criteria"] = list(self.criteria)
        return {key: value for key, value in result.items() if value is not None}


@dataclass(frozen=True)
class ReviewRecord:
    round: int
    verdict: str
    findings: tuple[str, ...] = ()
    reviewer: str | None = None
    evidence: str | None = None
    # The plan revision this round reviewed; a later plan is not covered by it.
    plan_revision: int | None = None
    # The acceptance criteria this round reviewed, in order; a plan with other
    # criteria is not covered by it.
    criteria: tuple[str, ...] | None = None

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["findings"] = list(self.findings)
        if self.criteria is not None:
            result["criteria"] = list(self.criteria)
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
    if decision.criteria is not None:
        _criteria_texts(decision.criteria, "a decision's criteria")


def _criteria_texts(value: Any, name: str) -> None:
    """Ordered acceptance criterion texts: a non-empty sequence of non-empty strings."""

    if type(value) not in (list, tuple) or not value or not all(_is_text(item) for item in value):
        raise TicketError(f"{name} must be a non-empty list of non-empty text")


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
    if review.plan_revision is not None and (type(review.plan_revision) is not int or review.plan_revision < 1):
        raise TicketError("a review's plan revision must be at least 1")
    if review.criteria is not None:
        _criteria_texts(review.criteria, "a review's criteria")


def _validate_plan(plan: Mapping[str, Any], key: str) -> None:
    if not isinstance(plan, Mapping) or plan.get("schema") != PLAN_SCHEMA:
        raise TicketError(f"a plan snapshot must carry schema {PLAN_SCHEMA}")
    # Every value is checked as stored, never in a coerced form.
    if set(plan) - _PLAN_FIELDS:
        raise TicketError("a plan snapshot has unknown fields")
    # Comparing with a str key or name refuses any other type, without coercion.
    if plan.get("key") != key:
        raise TicketError("plan snapshot belongs to another ticket")
    if not _is_text(plan.get("title")):
        raise TicketError("a plan snapshot requires its title")
    revision = plan.get("revision")
    if type(revision) is not int or revision < 1:
        raise TicketError("a plan snapshot requires a revision of at least 1")
    for name in ("ticket_status", "ticket_updated_at"):
        if name in plan and type(plan[name]) is not str:
            raise TicketError(f"a plan snapshot's {name} must be text")
    if "pass" in plan and not _is_text(plan["pass"]):
        raise TicketError("a plan snapshot's pass must be non-empty text")
    questions = plan.get("open_questions", [])
    if type(questions) is not list or not all(_is_text(question) for question in questions):
        raise TicketError("a plan snapshot's open questions must be a list of non-empty text")
    if "diff_statement" in plan and not _is_text(plan["diff_statement"]):
        raise TicketError("a plan snapshot's diff_statement must be non-empty text")
    criteria = plan.get("acceptance_criteria")
    if type(criteria) is not list or not criteria:
        raise TicketError("a plan snapshot requires its acceptance criteria")
    for item in criteria:
        _validate_criterion(item)
    _validate_steps(plan)


_PLAN_FIELDS = frozenset(
    (
        "schema",
        "key",
        "title",
        "acceptance_criteria",
        "steps",
        "open_questions",
        "diff_statement",
        "revision",
        "ticket_status",
        "ticket_updated_at",
        "pass",
    )
)
_CRITERION_FIELDS = frozenset(("text", "passes", "review_evidence", "review_round"))


def _is_text(value: Any) -> bool:
    return type(value) is str and bool(value.strip())


def _validate_criterion(item: Any) -> None:
    """One acceptance criterion as the plan card schema states it."""

    if not isinstance(item, Mapping) or set(item) - _CRITERION_FIELDS:
        raise TicketError("an acceptance criterion has unknown fields")
    if not _is_text(item.get("text")):
        raise TicketError("an acceptance criterion requires its text")
    if type(item.get("passes")) is not bool:
        raise TicketError("an acceptance criterion's passes must be true or false")
    evidence, review_round = item.get("review_evidence"), item.get("review_round")
    if evidence is not None and type(evidence) is not str:
        raise TicketError("an acceptance criterion's review evidence must be text")
    if review_round is not None and (type(review_round) is not int or review_round < 1):
        raise TicketError("an acceptance criterion's review round must be at least 1")
    if item["passes"] and (not _is_text(evidence) or review_round is None):
        raise TicketError("a passing acceptance criterion cites its review round and evidence")


def _validate_steps(plan: Mapping[str, Any]) -> None:
    """The plan card's step rules: every step once, in order; a skip carries its reason.

    A skipped plan step also needs the one-sentence diff it stands for.
    """

    steps = plan.get("steps")
    if type(steps) is not list or not all(isinstance(step, Mapping) for step in steps):
        raise TicketError("a plan snapshot requires its steps")
    names = [step.get("name") for step in steps]
    if names != list(STEPS):
        raise TicketError(f"a plan snapshot lists the steps {', '.join(STEPS)}, each once and in order")
    for step in steps:
        if set(step) - {"name", "status", "skip"}:
            raise TicketError(f"plan step {step['name']} has unknown fields")
        status = step.get("status")
        if status not in ("pending", "complete", "skipped"):
            raise TicketError(f"plan step {step['name']} has an unknown status")
        skipped = status == "skipped"
        if skipped and not _is_text(step.get("skip")):
            raise TicketError(f"skipped plan step {step['name']} requires skip: <reason>")
        if not skipped and "skip" in step:
            raise TicketError(f"plan step {step['name']} is not skipped, so it carries no skip reason")
        diff = plan.get("diff_statement")
        if skipped and step["name"] == "plan" and not (_is_text(diff) and _one_sentence(diff.strip())):
            raise TicketError("the plan may be skipped only for a one-sentence diff")


def _validate_tried(tried: TriedRecord) -> None:
    _text(tried.what, "what")
    _validate_phase(tried.phase)
    if tried.outcome is not None:
        _text(tried.outcome, "outcome")
    if tried.evidence is not None:
        _ref(tried.evidence, "tried evidence")


def _require_pending_close(stamps: Sequence[Mapping[str, Any]], position: int, point: str) -> None:
    """Refuse unless the stamp at ``position`` is still the last stamp and that close.

    Stamps are only ever appended, so a position names one stamp.
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
    """Ignored ``.ticket/<key>.md`` card with embedded structured data.

    Every write holds an exclusive advisory lock on ``.ticket/<key>.lock``
    across the load, the precondition check and the save. The save writes a
    temporary file beside the card and renames it into place, so a reader
    never sees half a card.

    The card's ``pass`` field is the ticket's live-session record: the
    session id, the host and the start time. It is read and changed under the
    same lock as every other write, and every write checks it first.
    """

    def __init__(self, repo: str | Path, key: str, title: str):
        if not _KEY.fullmatch(key):
            raise TicketError(f"unsafe ticket key: {key!r}")
        self.repo = Path(repo).resolve()
        self.key = key
        self.title = title
        self.relative_path = Path(".ticket") / f"{key}.md"
        self.path = self.repo / self.relative_path
        self.lock_path = self.path.with_name(f"{key}.lock")
        self._depth = 0
        # Threads sharing this handle take turns; the file lock is per process.
        self._threads = threading.RLock()
        # The pass this handle writes for, and the pass it ended, if any.
        self._pass: str | None = None
        self._ended: str | None = None

    @contextlib.contextmanager
    def _writing(self) -> Iterator[None]:
        """Hold this ticket's exclusive lock for one write, then drop it.

        A thread first takes this handle's thread lock, so the reentrancy count
        below is only ever this thread's. The file lock is an advisory
        ``flock`` on ``.ticket/<key>.lock``. A second writer waits for it; the
        operating system drops it if its holder dies, so nothing is left to
        clean up.
        """

        with self._threads, self._file_lock():
            yield

    @contextlib.contextmanager
    def _file_lock(self) -> Iterator[None]:
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
            try:
                fd = os.open(
                    self.lock_path.name,
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
            except OSError as exc:
                raise StoreUnavailable(f"cannot open the write lock for ticket {self.key}: {exc}") from exc
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise StoreUnavailable(
                        f"{self.relative_path.with_suffix('.lock').as_posix()} is not a regular file, so it "
                        "was not used as a lock and nothing was written."
                    )
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._depth = 1
                try:
                    yield
                finally:
                    self._depth = 0
            finally:
                os.close(fd)
        finally:
            os.close(directory)


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
            card["pass"] = {"id": pass_id, "host": _host(), "started_at": _now()}
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
        with self._writing():
            card = self._load()
            live = card.get("pass")
            if live is not None and live.get("id") == pass_id:
                card["pass"] = None
                self._save(card)

    def clear_pass(self, pass_id: str) -> None:
        """A person's recovery: release the named pass, refusing unless it is the live one."""

        self.join_pass(pass_id)
        self.release_pass("cleared by a person")


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
        lines.append(
            f"Live session: {_inline(live['id'])}, host {_inline(live['host'])}, "
            f"started {_inline(live['started_at'])}"
        )
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
    # It leaves `<`, `>` and `&` as they are, so a recorded `-->` would close the
    # comment in rendered Markdown; they appear only inside JSON strings, where
    # their \u escapes read back as the same characters.
    machine = (
        json.dumps(card, sort_keys=True, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    lines.extend(("", _JSON_START.rstrip("\n"), machine, _JSON_END.lstrip("\n"), ""))
    return "\n".join(lines)


# Naming an executable here used to select a ledger-backed store. That store
# is not supported yet, so the setting is refused rather than ignored: a
# person who asked for it is told, and nothing is written.
LEDGER_SELECTOR = "TICKET_LEDGER_CLI"
LEDGER_UNSUPPORTED = "the ledger store is not supported yet; use the markdown store"


class LedgerUnsupported(StoreUnavailable):
    """The ledger store was selected; it is refused until a later release supports it."""

    def __init__(self) -> None:
        super().__init__(LEDGER_UNSUPPORTED, code="ledger_unsupported")


def choose_store(repo: str | Path, key: str, title: str) -> StoreAdapter:
    """Return the markdown store, refusing a ledger selection before anything is written."""

    if os.environ.get(LEDGER_SELECTOR):
        raise LedgerUnsupported()
    return MarkdownStore(repo, key, title)


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


def _go_decisions(card: Mapping[str, Any], gate: str, revision: int, key: str) -> list[Mapping[str, Any]]:
    """The person's settled ratifications of ``gate`` for plan revision ``revision``.

    A plan go names the pass that presented the stored plan.
    """

    if type(revision) is not int or revision < 1:
        return []
    effect = _go_effect(gate, revision, _plan_pass(card) if gate == "plan" else None)
    tag = {"type": "ticket", "id": key}
    found = []
    for decision in card.get("decisions") or ():
        if not isinstance(decision, Mapping):
            continue
        source = decision.get("source") or {}
        if (
            decision.get("effect") == effect
            and decision.get("who_kind") == "person"
            and decision.get("standing") == "settled"
            and isinstance(source, Mapping)
            and source.get("what") == "ratification"
            and tag in (decision.get("tags") or ())
        ):
            found.append(decision)
    return found


def _approved_criteria(decision: Mapping[str, Any], texts: Sequence[str]) -> bool:
    """A plan go approved exactly these criterion texts, in this order."""

    return decision.get("criteria") == list(texts)


def _reviewed_criteria(review: Mapping[str, Any], texts: Sequence[str]) -> bool:
    """A review round reviewed exactly these criterion texts, in this order."""

    return review.get("criteria") == list(texts)


def _has_go(card: Mapping[str, Any], gate: str, revision: int, key: str) -> bool:
    """A gate is open only through a person's settled ratification in the store.

    A plan go counts only for the plan revision the stored plan's own pass
    presented, and only for the acceptance criteria the card's plan holds.
    """

    decisions = _go_decisions(card, gate, revision, key)
    if gate != "plan":
        return bool(decisions)
    texts = _card_texts(card)
    return texts is not None and any(_approved_criteria(decision, texts) for decision in decisions)


def _plan_identity(stored: Any) -> tuple[int, tuple[str, ...]] | None:
    """The stored plan's revision and criterion texts in order, or None when they cannot be read.

    The revision is a whole number of at least 1, read without coercion; the
    criteria are a non-empty list whose every entry carries its text.
    """

    if not isinstance(stored, Mapping):
        return None
    revision = stored.get("revision")
    if type(revision) is not int or revision < 1:
        return None
    criteria = stored.get("acceptance_criteria")
    if type(criteria) is not list or not criteria:
        return None
    texts = []
    for item in criteria:
        if not isinstance(item, Mapping) or not _is_text(item.get("text")):
            return None
        texts.append(item["text"])
    return revision, tuple(texts)


def _card_texts(card: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The card's plan criterion texts in stored order, or None when they cannot be read."""

    plan = card.get("plan")
    criteria = plan.get("acceptance_criteria") if isinstance(plan, Mapping) else None
    if type(criteria) is not list or not criteria:
        return None
    if not all(isinstance(item, Mapping) and _is_text(item.get("text")) for item in criteria):
        return None
    return tuple(item["text"] for item in criteria)


def _source_differences(stored: Sequence[str], source: Sequence[str]) -> list[str]:
    """How the ticket source's criteria differ from the stored ones: positional and exact.

    An added, removed or edited criterion is named; the same texts in another
    order are a reorder. Both are changes.
    """

    if tuple(stored) == tuple(source):
        return []
    added = Counter(source) - Counter(stored)
    removed = Counter(stored) - Counter(source)
    differences = []
    for text in source:
        if added[text] > 0:
            added[text] -= 1
            differences.append(f"acceptance criterion is new or changed on the ticket: {text}")
    for text in stored:
        if removed[text] > 0:
            removed[text] -= 1
            differences.append(f"acceptance criterion was removed from the ticket: {text}")
    if not differences:
        differences.append("the acceptance criteria were reordered on the ticket")
    return differences


def _coverage_problems(card: Mapping[str, Any], revision: int, key: str) -> list[str]:
    """What the card's records of plan revision ``revision`` fail of covering the card's plan.

    Every review round of that revision, and every plan go of it, must name
    exactly the criterion texts the card's plan holds, in order. A mismatch
    means the card's plan was edited after that round or that go.
    """

    texts = _card_texts(card)
    problems = []
    for review in card.get("reviews") or ():
        if (
            isinstance(review, Mapping)
            and type(review.get("plan_revision")) is int
            and review["plan_revision"] == revision
            and (texts is None or not _reviewed_criteria(review, texts))
        ):
            problems.append(
                f"review round {review.get('round')} reviewed other acceptance criteria than plan revision "
                f"{revision} on the card"
            )
    plan_goes = _go_decisions(card, "plan", revision, key)
    if any(texts is None or not _approved_criteria(go, texts) for go in plan_goes):
        problems.append(
            f"the plan go of plan revision {revision} approved other acceptance criteria than the card's plan"
        )
    return problems


def _plan_go_point(revision: int) -> str:
    return f"Waiting at plan go for plan revision {revision}."


def _outward_go_point(review_round: int) -> str:
    return f"Waiting at outward go after review round {review_round}."


# The stop stamp written when the last allowed review round does not pass.
_CAP_POINT = "Review did not converge after round three."
_CAP_NEXT_SKILLS = ("ticket", "review")


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


def _close_card_problems(
    card: Mapping[str, Any], key: str, revision: int, rounds: int, plan_texts: Sequence[str]
) -> list[str]:
    """What the card fails of the checks a close needs, apart from its own stamp.

    ``close`` runs this before it writes the close stamp, after reading that
    stamp back, and after confirming; resume runs it on a closed card.
    """

    problems = [
        f"the {gate} go decision is missing"
        for gate in ("plan", "outward")
        if not _has_go(card, gate, revision, key)
    ]
    if len(card.get("reviews") or ()) != rounds:
        problems.append("the review rounds differ")
    return problems + _review_problems(card, revision, plan_texts, key)


def _passing_final_review(
    card: Mapping[str, Any], revision: int, texts: Sequence[str]
) -> Mapping[str, Any] | None:
    """The card's final review when it is a passing round of plan revision ``revision``
    that reviewed the criterion texts ``texts``, in order.

    A passing round carries its round number and its evidence; a round that
    reviewed another plan revision, or other criteria, covers nothing in this one.
    """

    reviews = card.get("reviews") or ()
    final = reviews[-1] if reviews else None
    if (
        isinstance(final, Mapping)
        and final.get("verdict") == "pass"
        and type(final.get("round")) is int
        and type(final.get("plan_revision")) is int
        and final["plan_revision"] == revision
        and _reviewed_criteria(final, texts)
        and isinstance(final.get("evidence"), str)
        and final["evidence"]
    ):
        return final
    return None


def _card_plan_problems(card: Mapping[str, Any], revision: int, plan_texts: Sequence[str]) -> list[str]:
    """What the card's plan fails of being the plan this pass holds: plan
    revision ``revision`` with the criterion texts ``plan_texts``, same count,
    same order.
    """

    plan = card.get("plan") or {}
    stored = plan.get("acceptance_criteria") or ()
    texts = [item.get("text") if isinstance(item, Mapping) else None for item in stored]
    if (
        type(plan.get("revision")) is not int
        or plan["revision"] != revision
        or len(texts) != len(plan_texts)
        or any(text != expected for text, expected in zip(texts, plan_texts))
    ):
        return [f"the card's plan is not plan revision {revision} with this pass's acceptance criteria in order"]
    return []


def _review_problems(card: Mapping[str, Any], revision: int, plan_texts: Sequence[str], key: str) -> list[str]:
    """What the card fails of reaching outward go: the plan this pass holds
    (plan revision ``revision`` with the criterion texts ``plan_texts``), its
    review rounds and plan go naming the card's criteria, a passing final round
    of that revision, every criterion citing it, and its outward go stop stamp.

    Outward authorization, outward actions, close and resume all read it from
    the card at the moment they rely on it.
    """

    problems = _card_plan_problems(card, revision, plan_texts)
    problems += _coverage_problems(card, revision, key)
    final = _passing_final_review(card, revision, _card_texts(card) or ())
    recorded = _recorded_rounds(card.get("reviews") or ())
    criteria = [
        Criterion(item.get("text"), item.get("passes") is True, item.get("review_evidence"), item.get("review_round"))
        for item in (card.get("plan") or {}).get("acceptance_criteria") or ()
        if isinstance(item, Mapping)
    ]
    if (
        final is None
        or not criteria
        or not all(item.review_round == final["round"] and _verified(item, recorded) for item in criteria)
        or not _has_stamp(card, _outward_go_point(final["round"]), "review")
    ):
        problems.append("the last review round did not pass every criterion to an outward go stop")
    return problems


def _reconcile(
    stored: Mapping[str, Any], texts: Sequence[str], ticket: Ticket, reviews: Sequence[Mapping[str, Any]]
) -> tuple[PlanCard, list[str], list[str]]:
    """Rebuild the plan the card stored, its criteria in stored order, and
    compare those criteria with the fresh ticket's.

    ``texts`` are the stored criterion texts, already read by ``_plan_identity``.
    Returns the plan, a list of flagged differences, and the ticket source's
    differences from the stored criteria (any of which needs a new plan and
    plan go).
    """

    plan = PlanCard(ticket)
    flags: list[str] = []
    rounds = _recorded_rounds(reviews)
    restored: list[Criterion] = []
    for item, text in zip(stored["acceptance_criteria"], texts):
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
    return plan, flags, _source_differences(texts, ticket.acceptance_criteria)


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
        # The last stored review, kept for a stop stamp a resume found missing.
        self._last_review: Mapping[str, Any] | None = None
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
            # Every open reads the card, so an unreadable card fails the open
            # and releases the pass before any step runs.
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
        # clears it with the release command, which fails closed.
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

    def _plan_texts(self) -> tuple[str, ...]:
        """The criterion texts of the plan this pass holds, in order."""

        return tuple(item.text for item in self._plan.acceptance_criteria) if self._plan else ()

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
        changes: list[str] = []
        if stored:
            identity = _plan_identity(stored)
            if identity is None:
                # Nothing can be compared with a plan that cannot be read, and
                # no new revision can follow it, so the pass ends here.
                raise StoreUnavailable(
                    "the card's stored plan cannot be identified: its revision must be a whole number of at "
                    "least 1 and its acceptance criteria a non-empty list of entries with text. The card is "
                    "corrupt, so nothing was written and the pass ends; a person repairs the card.",
                    code="plan_unidentifiable",
                )
            self._revision, texts = identity
            # The pass holds the plan the card stored, in stored order; the
            # ticket source is only compared with it.
            plan, flags, changes = _reconcile(stored, texts, self._ticket, reviews)
            self._plan = plan
            self._plan_ref = card.get("plan_ref")
            self._plan_pass = _plan_pass(card)
            self.reconciliation = tuple(flags)
        stamps = card.get("stamps") or ()
        # A closed status counts only when the final stamp is the confirmed close
        # and the card still validates; otherwise the pass resumes before close.
        if card.get("status") == "closed":
            problems = self._closed_problems(card)
            if not problems:
                # A closed ticket never reopens on a source edit.
                self._phase = "closed"
                if changes:
                    self.reconciliation += (
                        f"the ticket's criteria changed after close ({'; '.join(changes)}); the ticket stays "
                        "closed; follow-up work is a new ticket",
                    )
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
        if _has_stamp(card, _CAP_POINT, "review"):
            if changes:
                self.reconciliation += (
                    f"the acceptance criteria changed since plan revision {self._revision} was stored "
                    f"({'; '.join(changes)}); review stopped at the round cap, so no new plan can be reviewed",
                )
            self._phase = "stopped"
            return
        changes += _coverage_problems(card, self._revision, self._ticket.key)
        if changes:
            self._resume_changed(card, reviews, "; ".join(changes))
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
        self._last_review = last
        # A passing round covers only the plan revision it reviewed.
        final = _passing_final_review(card, self._revision, self._plan_texts())
        passed_this_plan = final is not None
        stamped = passed_this_plan and _has_stamp(card, _outward_go_point(final["round"]), "review")
        if not _review_problems(card, self._revision, self._plan_texts(), self._ticket.key):
            self._phase = "outward_go"
        elif passed_this_plan and not stamped:
            # A passing round reaches outward go only once its stop stamp is
            # stored. A process that died after storing the round, before the
            # plan snapshot or the stamp, leaves the pass able to finish that
            # write, and nothing else.
            self.reconciliation += (
                f"outward stop not recorded: review round {last.get('round')} passed and is stored without "
                "its outward go stop stamp; record_outward_stop() writes it",
            )
            self._phase = "outward_stop"
        elif self._rounds >= MAX_REVIEW_ROUNDS:
            # The cap stops the pass only once its stop stamp is stored (that
            # case returned above). A process that died between storing the
            # last round and writing that stamp leaves the pass able to write
            # it, and nothing else.
            if passed_this_plan:
                # A source change never reaches here, so only an edited card
                # holds a stamped passing round of this plan that the plan
                # card does not carry; it is not a non-converged review.
                self.reconciliation += (
                    f"review round {last.get('round')} passed with its outward go stop stamp, but the plan "
                    "card does not carry that round; a person reconciles the card",
                )
                self._phase = "stopped"
            else:
                self._resume_at_cap_stop(last)
        else:
            self._phase = "build"

    def _resume_at_cap_stop(self, last: Mapping[str, Any]) -> None:
        """Resume where the only step is writing the round-cap stop stamp."""

        # Three stored rounds mean ``last`` is a stored review here.
        if last.get("verdict") == "pass":
            why = (
                f"review round {last.get('round')} passed plan revision {last.get('plan_revision')}, "
                f"not the current plan revision {self._revision}, so the round cap is reached "
                "without a passing round of this plan"
            )
        else:
            why = f"review round {self._rounds} is stored without its stop stamp"
        self.reconciliation += (f"cap stop not recorded: {why}; record_cap_stop() writes it",)
        self._phase = "cap_stop"

    def _resume_changed(self, card: Mapping[str, Any], reviews: Sequence[Mapping[str, Any]], what: str) -> None:
        """Resume after the criteria changed since the card stored its plan.

        No review round or go of the stored plan covers a new plan. Below the
        round cap the pass presents a new plan revision; at the cap it stops.
        """

        revision = self._revision
        last = reviews[-1] if reviews else None
        self._last_review = last
        held = (
            isinstance(last, Mapping)
            and last.get("verdict") == "pass"
            and type(last.get("round")) is int
            and type(last.get("plan_revision")) is int
            and last["plan_revision"] == revision
        )
        if held:
            passed = f"review round {last['round']} passed plan revision {revision}; the criteria changed since ({what})"
            if self._rounds >= MAX_REVIEW_ROUNDS:
                self.reconciliation += (
                    passed + "; no review round is left for a new plan; a person restores the criteria or "
                    "opens a new ticket",
                )
                self._phase = "stopped"
            else:
                self.reconciliation += (passed + "; that round covers nothing in a new plan",)
                self._phase = "plan"
        else:
            self.reconciliation += (
                f"the acceptance criteria changed since plan revision {revision} was stored ({what}); "
                f"plan revision {revision} must be presented again as a new revision for a new plan go",
            )
            if self._rounds >= MAX_REVIEW_ROUNDS:
                self._resume_at_cap_stop(last)
            else:
                self._phase = "plan"
        if _go_decisions(card, "outward", revision, self._ticket.key):
            self.reconciliation += (
                f"the outward go of plan revision {revision} authorizes nothing now; outward actions already "
                "taken are not undone",
            )

    def _closed_problems(self, card: Mapping[str, Any]) -> list[str]:
        """What a closed card fails of the checks ``close`` made before confirming.

        It reads only the card, never the ticket source.
        """

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
        problems += _close_card_problems(card, self._ticket.key, self._revision, self._rounds, _card_texts(card) or ())
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
        criteria = None
        if gate == "plan":
            # The go is bound to the plan this pass presented: the stored plan
            # must still be that revision, presented by this pass. The go names
            # the criteria it approves.
            stored = self.store.read_card().get("plan") or {}
            if (
                type(stored.get("revision")) is not int
                or stored["revision"] != self._revision
                or stored.get("pass") != self._pass_id
            ):
                raise StoreUnavailable(
                    f"the stored plan is not plan revision {self._revision} as pass {self._pass_id} "
                    "presented it, so no plan go was recorded",
                    code="plan_changed",
                )
            pass_id = self._pass_id
            criteria = self._plan_texts()
        else:
            self._require_review_on_card("no outward go was recorded")
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
                criteria=criteria,
            )
        )

    def begin_build(self) -> None:
        self._require_phase("plan_go")
        if not self.approved("plan"):
            raise GateRequired("plan go is required before build")
        self._phase = "build"

    @_ends_on_failure
    def record_tried(self, what: str, *, outcome: str | None = None, evidence: str | None = None) -> None:
        self._refuse_at_pending_stop()
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
            plan_revision=self._revision,
            criteria=self._plan_texts(),
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

        if verdict == "pass" or review.round == MAX_REVIEW_ROUNDS:
            self._write_review_stop(verdict == "pass", review.round, evidence)
        else:
            self._phase = "build"

    @_ends_on_failure
    def record_cap_stop(self) -> None:
        """Write the round-cap stop stamp a resume found missing, then end the pass."""

        self._require_phase("cap_stop")
        last = self._last_review or {}
        self._write_review_stop(False, int(last.get("round") or self._rounds), last.get("evidence"))

    @_ends_on_failure
    def record_outward_stop(self) -> None:
        """Finish a passing review a resume found without its outward go stop stamp.

        The passing round is in the store, and a passing review gives evidence
        for every criterion, so the plan snapshot is brought up to that round
        when it lags, then the stop stamp is written and the pass waits at
        outward go.
        """

        self._require_phase("outward_stop")
        last = self._last_review or {}
        # The card is read again: the round must still be the passing round of
        # this plan revision and these criteria that the resume found, with the
        # same evidence. Otherwise the card changed under the pass, which ends.
        card = self.store.read_card()
        reviews = card.get("reviews") or ()
        stored = reviews[-1] if reviews and isinstance(reviews[-1], Mapping) else {}
        if type(stored.get("round")) is not int:
            raise StoreUnavailable(
                "the stored final review carries no round number, so no outward stop was written",
                code="review_changed",
            )
        final = _passing_final_review(card, self._revision, self._plan_texts())
        if final is None or (final["round"], final["evidence"]) != (last.get("round"), last.get("evidence")):
            raise StoreUnavailable(
                f"the stored final review is no longer the passing round of plan revision {self._revision} "
                "this pass resumed, so no outward stop was written",
                code="review_changed",
            )
        review_round, evidence = final["round"], final["evidence"]
        criteria = tuple(Criterion(item.text, True, evidence, review_round) for item in self._plan.acceptance_criteria)
        if criteria != self._plan.acceptance_criteria:
            self._plan._criteria = criteria
            self._plan_ref = self.store.record_plan(self._snapshot(self._plan, self._revision))
        self._write_review_stop(True, review_round, evidence)

    def _write_review_stop(self, passed: bool, review_round: int, evidence: str | None) -> None:
        """Write the stop stamp that ends a review round, then move to its phase.

        A passing round stops at outward go. A round that did not pass writes a
        stop only as the last allowed round: the round cap, which ends the pass.
        """

        refs = tuple(ref for ref in (self._plan_ref, evidence) if ref)
        if passed:
            self.store.write_stamp(_outward_go_point(review_round), "review", ("ticket",), refs)
            self._phase = "outward_go"
            return
        self.store.write_stamp(_CAP_POINT, "review", _CAP_NEXT_SKILLS, refs)
        self._phase = "stopped"
        self.end("review did not converge after round three")

    @_ends_on_failure
    def require_outward_go(self) -> None:
        """Call before any outward action; the store must hold the outward go.

        The card must also still hold the passing review that reached outward go.
        """

        self._require_phase("outward_go")
        if not self.approved("outward"):
            raise GateRequired("outward go is required before any outward action")
        self._require_review_on_card("no outward action is allowed")

    def _require_review_on_card(self, refused: str) -> None:
        problems = _review_problems(self.store.read_card(), self._revision, self._plan_texts(), self._ticket.key)
        if problems:
            raise StoreUnavailable(f"{refused}: " + "; ".join(problems), code="review_changed")

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
        # The card must already pass every close check, so a close that cannot
        # be confirmed writes no close stamp.
        problems = _close_card_problems(
            self.store.read_card(), self._ticket.key, self._revision, self._rounds, self._plan_texts()
        )
        if problems:
            raise StoreUnavailable("close refused: " + "; ".join(problems))
        # The close stamp is written pending and confirmed only after the card
        # reads back correctly, so a failed close never reads as closed later.
        self.store.write_stamp(point, "close", next_skills, refs)
        card = self.store.read_card()
        stamps = card.get("stamps") or []
        last = stamps[-1] if stamps else {}
        problems = _close_stamp_problems(last, point, next_skills, refs)
        problems += _close_card_problems(card, self._ticket.key, self._revision, self._rounds, self._plan_texts())
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
        problems += _close_card_problems(card, self._ticket.key, self._revision, self._rounds, self._plan_texts())
        if problems:
            raise StoreUnavailable("close read-back failed: " + "; ".join(problems))
        self._phase = "closed"
        self.end("closed")

    @_ends_on_failure
    def more_info(self, destination: str, question: str, *, role: str | None = None) -> MoreInfoExit:
        self._refuse_at_pending_stop()
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

    def _refuse_at_pending_stop(self) -> None:
        if self._phase == "cap_stop":
            raise TicketError("the round-cap stop is not recorded yet; record_cap_stop() is the only step allowed")
        if self._phase == "outward_stop":
            raise TicketError(
                "the outward go stop is not recorded yet; record_outward_stop() is the only step allowed"
            )


__all__ = [
    "Criterion",
    "DecisionRecord",
    "DecisionSource",
    "FileTicketSource",
    "GateRequired",
    "LedgerUnsupported",
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
        "release",
        help="release a ticket's live session claim, named by its session id, after that session is gone",
    )
    release.add_argument("key", help="the ticket key")
    release.add_argument("pass_id", help="the session id the refusal named")
    release.add_argument("--repo", default=".", help="the target repository that holds the card")
    args = parser.parse_args(argv)
    try:
        store = choose_store(args.repo, args.key, args.key)
        store.clear_pass(args.pass_id)
    except LedgerUnsupported as exc:
        print(f"ticket: {exc}", file=sys.stderr)
        return 2
    except (StoreUnavailable, TicketError) as exc:
        print(f"ticket.py: {exc}", file=sys.stderr)
        return 1
    print(f"released session {args.pass_id} on ticket {args.key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""State and adapter helpers for one attended pass over a ticket."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import getpass
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
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

    def __init__(self, key: str, holder: str):
        super().__init__(
            f"ticket {key} already has a live writer: {holder}. This write was refused, "
            "not queued; retry after that writer finishes.",
            code="held",
        )
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
    }


class MarkdownStore:
    """Ignored ``.ticket/<key>.md`` fallback with embedded structured data.

    Every write holds ``.ticket/<key>.md.lock`` for the whole read, change and
    replace. The lock is taken without waiting: when another writer holds it,
    the write is refused with that writer's name. The operating system drops
    the lock if the holding process dies, and every exit path releases it.
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
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise StoreUnavailable(f"cannot open the write lock for ticket {self.key}: {exc}") from exc
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    raise TicketHeld(self.key, _lock_holder(fd)) from None
                raise StoreUnavailable(f"cannot take the write lock for ticket {self.key}: {exc}") from exc
            try:
                os.ftruncate(fd, 0)
                os.pwrite(fd, self.holder.encode("utf-8"), 0)
                self._depth = 1
                yield
            finally:
                self._depth = 0
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, 0)
                fcntl.flock(fd, fcntl.LOCK_UN)
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
        with self._writing():
            card = self._load()
            card["stamps"].append(
                {
                    "point": point,
                    "phase": phase,
                    "next_skills": list(next_skills),
                    "refs": list(refs),
                    "close": False,
                }
            )
            self._save(card)

    def confirm_close(self, position: int, point: str) -> None:
        """Mark that pending close stamp as the close and the card closed.

        The check and the write run under one lock, so no stop can land between them.
        """

        with self._writing():
            card = self._load()
            _require_pending_close(card["stamps"], position, point)
            card["stamps"][position]["close"] = True
            card["status"] = "closed"
            self._save(card)

    def record_decision(self, decision: DecisionRecord) -> None:
        _validate_decision(decision)
        with self._writing():
            card = self._load()
            card["decisions"].append(decision.as_dict())
            self._save(card)

    def record_review(self, review: ReviewRecord) -> None:
        _validate_review(review)
        with self._writing():
            card = self._load()
            card["reviews"].append(review.as_dict())
            self._save(card)

    def record_plan(self, plan: Mapping[str, Any]) -> str:
        _validate_plan(plan, self.key)
        with self._writing():
            card = self._load()
            ref = self.relative_path.as_posix()
            card["plan"] = json.loads(json.dumps(plan))
            card["plan_ref"] = ref
            self._save(card)
        return ref

    def record_tried(self, tried: TriedRecord) -> None:
        _validate_tried(tried)
        with self._writing():
            card = self._load()
            card["tried"].append(tried.as_dict())
            self._save(card)

    def read_card(self) -> dict[str, Any]:
        return self._load()


def _lock_holder(fd: int) -> str:
    """The name the current lock holder wrote into the lock file."""

    try:
        name = os.pread(fd, 4096, 0).decode("utf-8", "replace").strip()
    except OSError:
        name = ""
    return name or "a writer that has not recorded its name yet"


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

    Every write first claims the ticket's status row with the CLI's ``claim``
    verb, which refuses atomically when another owner holds it. A refused
    claim is reported with the holder's name, never retried. The write then
    releases the claim by restoring the status the row had before the claim.
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
        self._depth = 0
        self._release_to: str | None = None

    @contextlib.contextmanager
    def _writing(self) -> Iterator[None]:
        """Hold this ticket's claim for one write; refuse if another owner holds it."""

        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._ensure_item()
        try:
            claimed = self._run("claim", self.key, "--owner", self.holder)
        except StoreUnavailable as exc:
            if exc.code == "claimed":
                raise TicketHeld(self.key, self._claim_holder(str(exc))) from exc
            raise
        self._release_to = None
        try:
            events = self._run("export-item", self.key)["events"]
            self._release_to = _status_before(events, int(claimed["event"]["id"]))
            self._depth = 1
            yield
        finally:
            self._depth = 0
            target, self._release_to = self._release_to, None
            if target is None:
                raise StoreUnavailable(
                    f"ticket {self.key} is still claimed by {self.holder}: its earlier status "
                    "could not be read. Set it back with the ledger CLI's state verb."
                )
            self._run("state", self.key, "--status", target)

    def _claim_holder(self, message: str) -> str:
        """Who holds the claim, from the CLI's refusal or, failing that, the card."""

        _, _, named = message.partition("claimed by ")
        if named.strip():
            return named.strip()
        try:
            card = self._card_envelope() or {}
        except StoreUnavailable:
            card = {}
        return str(card.get("owner") or card.get("claim") or "an owner the ledger did not name")

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

    def _ensure_item(self) -> None:
        if self._ensured:
            return
        if self._card_envelope() is None:
            self._run("new", "--id", self.key, "--type", "ticket", "--title", self.title)
        self._ensured = True

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
        with self._writing():
            self._append("item.stopped", payload)

    def confirm_close(self, position: int, point: str) -> None:
        """Name that pending close's stop event as the close, then mark the item closed.

        The check, the close note and the closed status all happen under one
        claim; releasing that claim is the write that sets the status closed.
        """

        with self._writing():
            events = self._run("export-item", self.key)["events"]
            stops = [event for event in events if event["kind"] == "item.stopped"]
            _require_pending_close([event["payload"] for event in stops], position, point)
            self._append("item.note", {"text": f"{_CLOSE_NOTE}{int(stops[position]['id'])}"})
            self._release_to = "closed"

    def record_decision(self, decision: DecisionRecord) -> None:
        _validate_decision(decision)
        with self._writing():
            source_data = self._append("outside.recorded", decision.source.as_dict())
            source_event_id = int(source_data["event"]["id"])
            payload = decision.as_dict()
            payload.pop("source")
            payload["source"] = decision.source.ref
            payload["source_event_id"] = source_event_id
            self._append("decision.recorded", payload)

    def record_review(self, review: ReviewRecord) -> None:
        _validate_review(review)
        with self._writing():
            self._append("review.recorded", review.as_dict())

    def record_plan(self, plan: Mapping[str, Any]) -> str:
        _validate_plan(plan, self.key)
        text = _PLAN_NOTE + json.dumps(plan, sort_keys=True, separators=(",", ":"))
        with self._writing():
            data = self._append("item.note", {"text": text})
        return f"event:{int(data['event']['id'])}"

    def record_tried(self, tried: TriedRecord) -> None:
        _validate_tried(tried)
        with self._writing():
            self._append("item.tried", tried.as_dict())

    def _append(self, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._run(
            "append",
            "--kind",
            kind,
            "--item",
            self.key,
            "--payload",
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )

    def read_card(self) -> dict[str, Any]:
        card = self._card_envelope()
        if card is None:
            return _blank_card(self.key, self.title)
        events = self._run("export-item", self.key)["events"]
        return _normalize_ledger_card(card, events)


def _status_before(events: Sequence[Mapping[str, Any]], event_id: int | None = None) -> str:
    """The ticket's status just before ``event_id`` (or now), ignoring write claims."""

    status = "open"
    for event in events:
        if event_id is not None and int(event["id"]) >= event_id:
            break
        payload = event.get("payload") or {}
        if event["kind"] == "item.created" and payload.get("status"):
            status = str(payload["status"])
        elif event["kind"] == "item.state" and payload.get("status") not in (None, "claimed"):
            status = str(payload["status"])
    return status


def _normalize_ledger_card(card: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    outside = {
        int(event["id"]): event["payload"]
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
    # A write in progress shows as claimed; the ticket's status is the one under it.
    status = card["status"] if card.get("status") != "claimed" else _status_before(events)
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


def _go_effect(gate: str, revision: int) -> str:
    return f"The {gate} gate is approved for plan revision {revision}."


def _has_go(card: Mapping[str, Any], gate: str, revision: int, key: str) -> bool:
    """A gate is open only through a person's settled ratification in the store."""

    if revision < 1:
        return False
    effect = _go_effect(gate, revision)
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


class TicketPass:
    """Guarded transitions for one pass; outward actions remain caller-owned.

    The phase, the review-round count and the gates are read-only. Gates are
    derived from go decisions the store recorded, never from session state.
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

    @classmethod
    def open(
        cls,
        key: str,
        source: TicketSource,
        store: StoreAdapter,
        *,
        resume: bool = False,
    ) -> "TicketPass":
        ticket = source.pull(key)
        session = cls(ticket, store)
        if resume:
            session._resume(store.read_card())
        return session

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
            if _has_stamp(card, _plan_go_point(self._revision), "plan"):
                self._phase = "plan_go"
            else:
                self.reconciliation += (
                    f"no plan go stop stamp for plan revision {self._revision}; resuming at plan",
                )
                self._phase = "plan"
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

    def _snapshot(self, plan: PlanCard, revision: int) -> dict[str, Any]:
        data = plan.as_dict()
        data["revision"] = revision
        data["ticket_status"] = self._ticket.status
        if self._ticket.updated_at:
            data["ticket_updated_at"] = self._ticket.updated_at
        return data

    def finish_orient(self) -> None:
        self._require_phase("orient")
        self._phase = "research"

    def finish_research(self) -> None:
        self._require_phase("research")
        self._phase = "plan"

    def present_plan(self, plan: PlanCard) -> None:
        self._require_phase("plan")
        if plan.key != self._ticket.key:
            raise TicketError("plan card belongs to another ticket")
        if tuple(item.text for item in plan.acceptance_criteria) != self._ticket.acceptance_criteria:
            raise TicketError("the plan card must carry the ticket's acceptance criteria, in order")
        if any(item.passes for item in plan.acceptance_criteria):
            raise TicketError("a presented plan starts with passes: false on every criterion")
        revision = self._revision + 1
        ref = self.store.record_plan(self._snapshot(plan, revision))
        self.store.write_stamp(
            _plan_go_point(revision),
            "plan",
            ("ticket",),
            (ref,),
        )
        self._plan, self._revision, self._plan_ref = plan, revision, ref
        self._phase = "plan_go"

    def record_go(self, gate: str, *, evidence: str, verbatim: str) -> None:
        expected = {"plan": "plan_go", "outward": "outward_go"}
        if gate not in expected:
            raise TicketError(f"unknown gate: {gate}")
        self._require_phase(expected[gate])
        evidence = evidence.strip()
        verbatim = verbatim.strip()
        if not evidence or not verbatim:
            raise TicketError("a go record requires evidence and verbatim words")
        self.store.record_decision(
            DecisionRecord(
                who="the person",
                who_kind="person",
                standing="settled",
                source=DecisionSource("ratification", evidence),
                effect=_go_effect(gate, self._revision),
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

    def record_tried(self, what: str, *, outcome: str | None = None, evidence: str | None = None) -> None:
        self.store.record_tried(TriedRecord(what, self._store_phase(), outcome, evidence))

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
            raise StoreUnavailable("the store's review rounds changed outside this pass; resume to reload")

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
        else:
            self._phase = "build"

    def require_outward_go(self) -> None:
        """Call before any outward action; the store must hold the outward go."""

        self._require_phase("outward_go")
        if not self.approved("outward"):
            raise GateRequired("outward go is required before any outward action")

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

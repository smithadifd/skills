#!/usr/bin/env python3
"""One attended pass over a ticket: a derived-state machine over a stored card.

The rules are in references/ticket-design.md. In short: a plan's identity is
its revision and its ordered criterion texts, and every record that relies on a
plan stores a full copy of that identity (D1). Every step that writes is one
conditional store write under the token the pass holds (D2). Reviews own the
evidence; the plan snapshot carries no copy of it (D3). A corrupt card is
refused and writes nothing; only an edit of the ticket source, or a record the
card's plan no longer matches, is a change (D4).
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import csv
import dataclasses
import errno
import fcntl
import functools
import hashlib
import importlib.util
import json
import math
import os
import re
import secrets
import shlex
import socket
import stat
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

# The store phases, shared by both stores (design section 2).
PHASES = ("orient", "plan", "build", "review", "close")
STEPS = ("orient", "research", "plan", "build", "review", "outward")
STEP_STATUSES = ("pending", "complete", "skipped")
INFO_EXITS = ("person", "research", "someone_else")
OUTSIDE_EVENTS = ("merge", "reply", "ratification", "review", "release", "other")
TAG_TYPES = ("ticket", "initiative", "team", "project")
GATES = ("plan", "outward")
CARD_SCHEMA = "ticket-card/v2"
PLAN_SCHEMA = "ticket-plan-card/v2"
MAX_REVIEW_ROUNDS = 3
REVIEW_RECORD_TIMEOUT_SECONDS = 120
# The three seams of a pass (TL4 one-item skill design, section 3), each with the name of its default
# implementation: what this skill does when nothing else is selected. Build: dispatch builder-core with the
# approved card. Review: a fresh reviewer, recorded by record_review. Outward: push and open a draft; the merge
# is the person's, or the review-gate merge command. A seam name is not a phase: no card or store phase is
# named from this table, and no store reads it.
SEAMS = MappingProxyType(
    {
        "build": "builder-core",
        "review": "reviewer",
        "outward": "push-and-open-draft",
    }
)
# Provisional default pending owner review; callers may set context_floor_fraction on open.
CONTEXT_FLOOR_FRACTION = 0.8
_COST_PHASES = (*STEPS, "close")


@dataclass(frozen=True)
class ContextReading:
    """Current session usage, supplied by the host; no host-specific meter is read here."""

    used_tokens: int
    window_tokens: int


def load_phase_costs() -> dict[str, int]:
    """Shared seed estimates keyed by phase name, also read by plan-card consumers."""

    with Path(__file__).with_name("phase-costs.csv").open(encoding="utf-8", newline="") as source:
        return {row["phase"]: int(row["estimated_tokens"]) for row in csv.DictReader(source)}


NO_TOKEN = "none"
# The installed script, so a recovery command runs from the target repository.
_SCRIPT = os.path.abspath(__file__)

_SKILL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# A name in a parent's hand-off (design 6.8): its controller, a seam's implementation, or one the opener declares it
# runs. ASCII letters, digits, dot, underscore and hyphen, starting with a letter or digit, at most 64 characters, the
# ledger's own entity token; names are compared by exact equality, so a look-alike letter never makes two equal.
_HANDOFF_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# A pass id is hex (design section 2); no length is fixed.
_PASS_ID = re.compile(r"[0-9a-f]+")
# PR links accept exactly owner/repo#N: ASCII GitHub-style names and a positive decimal number.
_PULL_REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9._-]*#[1-9][0-9]*")
# What a stamp or a review may point at: a path (absolute, or relative with a
# directory or an extension, optionally :line or :start-end), a PR or issue
# (owner/repo#N or #N), a branch (branch:<name>), a commit SHA, or a store
# event (event:<N>).
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

PLAN_GO_POINT = "Waiting at plan go for plan revision {revision}."
OUTWARD_GO_POINT = "Waiting at outward go after review round {round}."
CAP_POINT = "Review did not converge after round three."
# The close point of a pass the person ended at the outward go with a no: the work stays local.
KEPT_LOCAL_POINT = (
    "Closed kept local: the person declined the outward go for plan revision {revision}, "
    "so nothing was pushed or opened."
)


def seam_set(card: Mapping[str, Any], *names: str) -> dict[str, str]:
    """The seam set of a card: each seam name mapped to its implementation name.

    A card carries no seam record (absent means default), so every seam
    resolves to its default in ``SEAMS``, and nothing is written to the card
    or the ledger. The one exception is the ``handoff`` field of a ledger
    item's card: the ledger store derives it from the item's one open
    hand-off on every read (design 6.8), never from a record, and the seams
    are then the hand-off's own names. With no ``names`` the whole set is
    returned; with names, only those. A name that is not a seam is refused,
    naming it: it never resolves to a default.
    """

    for name in names:
        if name not in SEAMS:
            raise TicketError(f"unknown seam: {name!r}; the seams are {', '.join(SEAMS)}")
    handoff = card.get("handoff")
    chosen = handoff["seams"] if isinstance(handoff, Mapping) else SEAMS
    return {name: chosen[name] for name in (names or SEAMS)}


def release_command(key: str, pass_id: str) -> str:
    """The recovery command a person copies: every argv element shell-quoted."""
    return shlex.join(["python3", _SCRIPT, "release", key, pass_id])


class TicketError(ValueError):
    """A wrong step or value. Nothing was written and the pass stays live."""


class GateRequired(TicketError):
    """A person has not explicitly approved the named gate."""


def _role_go_refused(code: str, key: str, why: str) -> GateRequired:
    return GateRequired(f"{code}: ticket {key}: {why}. Nothing was written; the pass stays live.")


class StoreUnavailable(RuntimeError):
    """A refusal: nothing was written and the pass ends. ``code`` names the refusal."""

    # The ledger's own answer code when this refusal turns one of its answers into a stale write (else None).
    ledger_code: str | None = None

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class TicketHeld(StoreUnavailable):
    """Another pass is live on the ticket."""

    def __init__(self, key: str, live: Mapping[str, Any]):
        super().__init__(
            f"ticket {key} has a live pass: id {live['id']}, host {live['host']}, started {live['started_at']}. "
            "Nothing was written. It ends when that pass closes the ticket or ends. If that session is gone, "
            f"a person releases it with `{release_command(key, live['id'])}` "
            "(add `--repo <path>` for a markdown card).",
            code="held",
        )
        self.key = key
        self.holder = dict(live)


# The lease a ticket pass takes on its ledger item (design 6.2): four hours, the ledger's own default claim lease,
# with no heartbeat. A pass idle past it is resumed by its id, and the resume renews it.
LEASE_SECONDS = 4 * 60 * 60
# A lease holder: one name of at most 200 characters with no whitespace or control character.
_HOLDER = re.compile(r"[^\s\x00-\x1f\x7f]{1,200}")


def _check_holder(holder: Any) -> None:
    """Refuse a lease holder that is not a name, as ``holder_invalid``, before anything is read or written."""

    if type(holder) is not str or not _HOLDER.fullmatch(holder):
        raise StoreUnavailable(
            f"the lease holder {holder!r} is not a name: give non-empty text of at most 200 characters with no "
            "whitespace or control character. Nothing was read or written.",
            code="holder_invalid",
        )


def _lease_held(key: str, holder: str) -> StoreUnavailable:
    return StoreUnavailable(
        f"ledger item {key} is leased to {holder}, so this pass does not open on it. Nothing was written and the item "
        "is unchanged. The lease ends when its holder releases it or it expires; only its holder continues under it.",
        code="lease_held",
    )


def _not_claimable(key: str, status: str) -> StoreUnavailable:
    return StoreUnavailable(
        f"ledger item {key} is {status}, so this pass takes no lease on it. Nothing was written and the item is "
        "unchanged; a person resolves its status first.",
        code="item_not_claimable",
    )


def _handoff_not_leased(key: str, holder: str, row: Mapping[str, Any] | None) -> StoreUnavailable:
    row = row or {}
    on_row = row.get("owner") or row.get("claim")
    reads = f"{row.get('status')} (recorded {row.get('recorded_status')}), held by {on_row or 'no holder'}"
    return StoreUnavailable(
        f"ledger item {key} is not leased live to {holder} (it reads {reads}), so a pass opened through its "
        "hand-off would have to take the lease, and it never takes one. Nothing was written and the item is "
        "unchanged; the holder that handed it off takes it again.",
        code="handoff_not_leased",
    )


def _handoff_source_required(key: str, holder: str) -> StoreUnavailable:
    return StoreUnavailable(
        f"ledger item {key} is handed off by a parent (it has an open loop intent of a version other than 1), "
        f"so only the hand-off source opens it, as its lease holder {holder}. Nothing was written and the item is "
        "unchanged.",
        code="handoff_source_required",
    )


def _holder_mismatch(key: str, given: str, holder: str) -> StoreUnavailable:
    return StoreUnavailable(
        f"the pass on ledger item {key} holds its lease as {holder}, not {given}, so it is not resumed under "
        f"{given}. Nothing was written. Resume it with no holder, or with {holder}.",
        code="holder_mismatch",
    )


def _lease_expiry() -> str:
    """The stamp a lease taken now runs to: ``LEASE_SECONDS`` after ``_now()``, in the ledger's UTC millisecond form."""

    start = datetime.strptime(_now(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (start + timedelta(seconds=LEASE_SECONDS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _host() -> str:
    return "".join(socket.gethostname().split()) or "unknown"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _is_text(value: Any) -> bool:
    return type(value) is str and bool(value.strip())


def _items(value: Any) -> list:
    """A record's list field, or no items when a hand edit left something else there."""

    return value if type(value) is list else []


# --- The ticket source -------------------------------------------------------


@dataclass(frozen=True)
class SpecVersion:
    """The spec of a ledger item as one ``spec show`` reported it: its version and content hash (design 6.5)."""

    version: int
    content_hash: str

    def record(self) -> dict[str, Any]:
        return {"version": self.version, "content_hash": self.content_hash}


@dataclass(frozen=True)
class Ticket:
    key: str
    title: str
    status: str
    description: str
    acceptance_criteria: tuple[str, ...]
    links: tuple[dict[str, str], ...] = ()
    updated_at: str | None = None
    # The spec version a ledger-item source read on this pull; None for every other source (design 6.5).
    spec: SpecVersion | None = None
    # The ticket's own address on its tracker, when the source gives one; the recorded spec's source names it (design 6.6).
    url: str | None = None
    # ``ask:<12 hex>`` when the ticket was drafted from an ad-hoc ask, else None: the recorded spec's whole source, and
    # the mark the plan card shows on criteria that bind only through the plan go (one-item skill design, section 4).
    ask_source: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Ticket":
        required = ("key", "title", "status", "description", "acceptance_criteria")
        missing = [name for name in required if name not in value]
        if missing:
            raise TicketError(f"ticket is missing: {', '.join(missing)}")
        if not _is_text(value["title"]):
            raise TicketError("a ticket's title must be non-empty text")
        criteria = value["acceptance_criteria"]
        if type(criteria) is not list:
            raise TicketError("acceptance_criteria must be a list")
        if not criteria:
            raise TicketError("acceptance_criteria must not be empty")
        if not all(type(item) is str for item in criteria):
            raise TicketError("every acceptance criterion must be text")
        if not all(item.strip() for item in criteria):
            raise TicketError("no acceptance criterion may be blank")
        links = value.get("links", [])
        if type(links) is not list:
            raise TicketError("links must be a list")
        normalized: list[dict[str, str]] = []
        for link in links:
            if not isinstance(link, Mapping) or not link.get("kind"):
                raise TicketError("each link needs a kind")
            if not link.get("url"):
                raise TicketError("each link needs a url")
            normalized.append({"kind": str(link["kind"]), "url": str(link["url"])})
        url = value.get("url")
        if url is not None and not (type(url) is str and url and not any(char.isspace() for char in url)):
            raise TicketError("a ticket's url must be non-empty text with no whitespace")
        return cls(
            key=str(value["key"]),
            title=value["title"].strip(),
            status=str(value["status"]),
            description=str(value["description"]),
            acceptance_criteria=tuple(item.strip() for item in criteria),
            links=tuple(normalized),
            updated_at=str(value["updated_at"]) if value.get("updated_at") else None,
            url=url,
        )


class TicketSource(Protocol):
    """A live source returns the current ticket on every pull."""

    def pull(self, key: str) -> Ticket: ...


class FileTicketSource:
    """A ticket held in a local JSON file, for fixtures and dry runs."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def pull(self, key: str) -> Ticket:
        ticket = Ticket.from_mapping(json.loads(self.path.read_text(encoding="utf-8")))
        if ticket.key != key:
            raise TicketError(f"the source returned ticket {ticket.key!r}, expected {key!r}")
        return ticket


# --- The ad-hoc ask source (one-item skill design, section 4) ----------------


def _ask_digest(ask: Any) -> str:
    """The first 12 hex digits of the SHA-256 of the ask's text, exactly as given (no trimming, no case folding).

    An ask that is not text, or is blank, is refused ``ask_empty``; one that cannot be encoded as UTF-8 is refused
    ``ask_not_utf8``. Nothing has been read or written at that point.
    """

    if type(ask) is not str or not ask.strip():
        raise StoreUnavailable(
            "the ad-hoc ask is empty, so there is no ticket to take it for. Nothing was read or written; "
            "give the ask as text.",
            code="ask_empty",
        )
    try:
        return hashlib.sha256(ask.encode("utf-8")).hexdigest()[:12]
    except UnicodeEncodeError as exc:
        raise StoreUnavailable(
            "the ad-hoc ask is not text that encodes as UTF-8, so it has no identity. Nothing was read or written.",
            code="ask_not_utf8",
        ) from exc


def ask_spec_source(ask: str) -> str:
    """The spec source of an ad-hoc ask: ``ask:`` and 12 lowercase hex digits taken from the ask's text alone."""

    return f"ask:{_ask_digest(ask)}"


def ask_key(ask: str) -> str:
    """The id of the item an ad-hoc ask lands on: ``ask-`` and the same 12 hex digits, so a retry finds the same item.

    It comes from the ask's text and from nothing else, never from the environment or from an id the caller names.
    """

    return f"ask-{_ask_digest(ask)}"


class AskTicketSource:
    """A ticket drafted from an ad-hoc ask: the person's own words, with a title, body and acceptance criteria written
    for them. The same ask text always names the same item (``key``) and the same spec source (``ask_source``).

    The criteria are a draft. They reach the plan card marked as drafted from the ask, and bind only through the plan
    go, which is recorded against the plan's criteria as for any ticket. A draft with no usable title, body or
    criterion, or a criterion that runs over more than one line (the acceptance grammar is one ``- `` bullet per
    line), is refused ``ask_draft_invalid`` before anything is read or written. Pulling it by another key is refused,
    so the item id is never the caller's to name.
    """

    def __init__(self, ask: str, *, title: str, description: str, acceptance: Sequence[str]):
        self.ask_source = ask_spec_source(ask)
        self.key = ask_key(ask)
        self._ticket = self._draft(title, description, acceptance)

    def _draft(self, title: Any, description: Any, acceptance: Any) -> Ticket:
        def invalid(why: str) -> StoreUnavailable:
            return StoreUnavailable(
                f"the draft of ad-hoc ask {self.ask_source} cannot be a ticket: {why}. Nothing was read or "
                "written; draft it again.",
                code="ask_draft_invalid",
            )

        if not _is_text(title) or "\n" in title or "\r" in title:
            raise invalid("its title is not one line of text")
        if not _is_text(description):
            raise invalid("its body is not text")
        if type(acceptance) not in (list, tuple) or not acceptance:
            raise invalid("it has no acceptance criteria")
        for index, text in enumerate(acceptance, 1):
            if not _is_text(text):
                raise invalid(f"acceptance criterion {index} is not text")
            if "\n" in text or "\r" in text:
                raise invalid(f"acceptance criterion {index} runs over more than one line")
        drafted = Ticket.from_mapping(
            {
                "key": self.key, "title": title, "status": "open", "description": description,
                "acceptance_criteria": list(acceptance),
            }
        )
        return dataclasses.replace(drafted, ask_source=self.ask_source)

    def pull(self, key: str) -> Ticket:
        if key != self.key:
            raise TicketError(f"the ad-hoc ask {self.ask_source} is item {self.key}, not {key!r}")
        return self._ticket


# --- The plan card -----------------------------------------------------------


@dataclass
class PlanStep:
    name: str
    status: str = "pending"
    skip: str | None = None


def _one_sentence(text: str) -> bool:
    if not text or "\n" in text or "\r" in text:
        return False
    parts = [part for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    return len(parts) == 1


def claim_line(claim: Mapping[str, Any]) -> str:
    """A pass's claim as one line: the id a later session resumes with, the host and the start time ``open`` recorded."""

    return f"pass {claim['id']}, host {claim['host']}, started {claim['started_at']}"


class PlanCard:
    """The plan a pass presents: the ticket's criteria in order, the six steps,
    skip reasons, a one-sentence diff when the plan step is skipped, and open
    questions.

    It carries no ``passes`` field. ``review`` is the passing review round of
    this plan that the pass read from the card, or None; ``render`` derives
    each criterion's ``passes`` from it. In the same way ``reached`` names the
    steps the card's stored events prove were done (``_reached_steps``). It is
    None on a plan not yet stored (built here, never presented), which renders
    the statuses its author set; a stored plan (``from_snapshot``, or one the
    pass presented) holds a set the pass keeps current, and ``render`` shows a
    step of it complete only when the set names it, skipped when the snapshot
    skipped it, and pending otherwise, whatever the snapshot's status says. The
    snapshot itself is never changed.

    ``claim`` is the live claim of the pass that holds the plan (its pass id,
    host and start time), set by the pass while it is live and None on a plan
    not yet stored, on a pass that ended and on a terminal resume. ``render``
    shows it next to the plan.
    """

    def __init__(self, ticket: Ticket, *, items: list[str] | tuple[str, ...] | None = None):
        self.key = ticket.key
        self.title = ticket.title
        self._criteria = tuple(ticket.acceptance_criteria)
        # Explicit planned parts, not acceptance criteria or the six workflow steps.
        items = items if items is not None else (ticket.title,)
        _criteria_texts(items, "a plan card's items")
        self.items = tuple(items)
        self.steps = [PlanStep(name) for name in STEPS]
        self.diff_statement: str | None = None
        self.open_questions: list[str] = []
        self.review: Mapping[str, Any] | None = None
        # None until the plan is stored: an unsynchronized plan renders as its author set it.
        self.reached: frozenset[str] | None = None
        self.claim: Mapping[str, Any] | None = None
        # The ask the criteria were drafted from, when the ticket is an ad-hoc ask: ``render`` marks them as a draft.
        self.ask_source = ticket.ask_source

    @property
    def acceptance_criteria(self) -> tuple[str, ...]:
        return self._criteria

    def _step(self, name: str) -> PlanStep:
        for step in self.steps:
            if step.name == name:
                return step
        raise TicketError(f"unknown plan step: {name}")

    def add_open_question(self, question: str) -> None:
        if not _is_text(question):
            raise TicketError("an open question must be non-empty text")
        self.open_questions.append(question.strip())

    def complete_step(self, name: str) -> None:
        step = self._step(name)
        step.status, step.skip = "complete", None
        if name == "plan":
            self.diff_statement = None

    def skip_step(self, name: str, reason: str | None, *, diff_statement: str | None = None) -> None:
        if not _is_text(reason):
            raise TicketError("a skipped step requires skip: <reason>")
        step = self._step(name)
        if name == "plan":
            statement = (diff_statement or "").strip()
            if not _one_sentence(statement):
                raise TicketError("the plan may be skipped only for a one-sentence diff")
            self.diff_statement = statement
        step.status, step.skip = "skipped", reason.strip()

    def snapshot(self, revision: int, pass_id: str, ticket: Ticket) -> dict[str, Any]:
        """The plan as the card stores it for one revision. It is never changed after."""

        data: dict[str, Any] = {
            "schema": PLAN_SCHEMA,
            "key": self.key,
            "title": self.title,
            "revision": revision,
            "pass": pass_id,
            "criteria": list(self._criteria),
            "steps": [
                {"name": step.name, "status": step.status, **({"skip": step.skip} if step.skip else {})}
                for step in self.steps
            ],
            "open_questions": list(self.open_questions),
            "items": list(self.items),
            "ticket_status": ticket.status,
        }
        if self.diff_statement is not None:
            data["diff_statement"] = self.diff_statement
        if ticket.updated_at:
            data["ticket_updated_at"] = ticket.updated_at
        if ticket.spec is not None:
            data["ticket_spec"] = ticket.spec.record()
        return data

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> "PlanCard":
        """Rebuild the plan the card stored, its criteria in stored order."""

        plan = cls.__new__(cls)
        plan.key = snapshot["key"]
        plan.title = snapshot["title"]
        plan._criteria = tuple(snapshot["criteria"])
        plan.items = tuple(snapshot.get("items", (snapshot["title"],)))
        plan.steps = [PlanStep(step["name"], step["status"], step.get("skip")) for step in snapshot["steps"]]
        plan.diff_statement = snapshot.get("diff_statement")
        plan.open_questions = list(snapshot["open_questions"])
        plan.review = None
        # A stored plan: nothing is proven until the pass sets the steps its card's events reach.
        plan.reached = frozenset()
        plan.claim = None
        plan.ask_source = None
        return plan

    def rendered_status(self, step: PlanStep) -> str:
        """A skipped step stays skipped. On a stored plan a step is complete only when the stored events prove it
        and pending otherwise, whatever the snapshot says; a plan not yet stored shows its author's status (design 3.6)."""

        if step.status == "skipped":
            return "skipped"
        if self.reached is None:
            return step.status
        return "complete" if step.name in self.reached else "pending"

    def render(self) -> str:
        passing = self.review
        lines = [f"# Plan: {self.key}: {self.title}", ""]
        if self.claim is not None:
            lines.extend((f"Claim: {claim_line(self.claim)}", ""))
        lines.append("## Acceptance criteria")
        if self.ask_source is not None:
            lines.append(
                f"Drafted from the ad-hoc ask {self.ask_source}: these criteria are a draft and bind only once the plan go approves them."
            )
        for index, text in enumerate(self._criteria):
            passes = passing is not None and index in passing["passed"]
            line = f"- [{'x' if passes else ' '}] passes: {str(passes).lower()} - {text}"
            if passes:
                line += f" (review round {passing['round']}: {passing['evidence']})"
            lines.append(line)
        lines.extend(("", "## Items"))
        lines.extend(f"- {item}" for item in self.items)
        lines.extend(("", "## Steps"))
        for step in self.steps:
            status = self.rendered_status(step)
            lines.append(f"- {step.name}: skip: {step.skip}" if status == "skipped" else f"- {step.name}: {status}")
        if self.diff_statement:
            lines.extend(("", f"Diff: {self.diff_statement}"))
        if self.open_questions:
            lines.extend(("", "## Open questions"))
            lines.extend(f"- {question}" for question in self.open_questions)
        return "\n".join(lines) + "\n"


# --- Record validators (matrix section I) -----------------------------------


def _text(value: Any, name: str) -> None:
    if not _is_text(value):
        raise TicketError(f"{name} must be non-empty text")


def _ref(value: Any, name: str) -> None:
    _text(value, name)
    if "\n" in value or "\r" in value:
        raise TicketError(f"{name} must be one line")
    if len(value) > 300:
        raise TicketError(f"{name} must be at most 300 characters")


def _ref_target(value: Any, name: str) -> None:
    _ref(value, name)
    if not _REF_TARGET.fullmatch(value):
        raise TicketError(f"{name} must be a path, PR or issue id, branch:<name>, commit SHA, or event:<N>: {value!r}")


def _phase(value: Any) -> None:
    if value not in PHASES:
        raise TicketError(f"unknown phase: {value!r}")


def _whole(value: Any, name: str) -> None:
    if type(value) is not int or value < 1:
        raise TicketError(f"{name} must be a whole number of at least 1")


def _criteria_texts(value: Any, name: str) -> None:
    """Ordered acceptance criterion texts: a non-empty list or tuple of non-empty text."""

    if type(value) not in (list, tuple):
        raise TicketError(f"{name} must be a list")
    if not value:
        raise TicketError(f"{name} must not be empty")
    if not all(_is_text(item) for item in value):
        raise TicketError(f"{name} must hold only non-empty text")


def _validate_claim(claim: Any) -> None:
    if type(claim) is not dict or set(claim) != {"id", "host", "started_at"}:
        raise TicketError("a claim names exactly its id, host and started_at")
    for name in ("id", "host", "started_at"):
        _text(claim[name], f"the claim's {name}")
    if not _PASS_ID.fullmatch(claim["id"]):
        raise TicketError("the claim's id must be lowercase hex")


def _validate_join(join: Any) -> None:
    """A join names exactly the live pass it continues (design section 5): it binds the handle and changes no card."""

    if type(join) is not dict or set(join) != {"id"}:
        raise TicketError("a join names exactly the id of the pass it continues")
    _text(join["id"], "the join's id")
    if not _PASS_ID.fullmatch(join["id"]):
        raise TicketError("the join's id must be lowercase hex")


def _validate_pass_record(record: Any) -> None:
    """A ledger pass record (design 6.2): the claim's id, host and start, the lease holder, and taken or joined."""

    if type(record) is not dict or set(record) != {"id", "host", "started_at", "holder", "lease"}:
        raise TicketError("a pass record names exactly its id, host, started_at, holder and lease")
    _validate_claim({name: record[name] for name in ("id", "host", "started_at")})
    if type(record["holder"]) is not str or not _HOLDER.fullmatch(record["holder"]):
        raise TicketError("a pass record's holder must be a holder name")
    if record["lease"] not in ("taken", "joined"):
        raise TicketError("a pass record's lease is taken or joined")


def _validate_stamp(stamp: Any) -> None:
    if type(stamp) is not dict or set(stamp) != {"point", "phase", "next_skills", "refs", "close"}:
        raise TicketError("a stamp has exactly point, phase, next_skills, refs and close")
    _phase(stamp["phase"])
    point, next_skills, refs = stamp["point"], stamp["next_skills"], stamp["refs"]
    if type(point) is not str:
        raise TicketError("point must be text")
    if not point.strip():
        raise TicketError("point must not be empty")
    if len(point) > 600:
        raise TicketError("point must be at most 600 characters")
    if type(next_skills) is not list or len(next_skills) > 12:
        raise TicketError("next_skills must be a list of at most 12 skills")
    for skill in next_skills:
        if type(skill) is not str:
            raise TicketError("a next skill must be text")
        if len(skill) > 80:
            raise TicketError("a next skill must be at most 80 characters")
        if not _SKILL.fullmatch(skill):
            raise TicketError(f"a next skill must be a skill name: {skill!r}")
    if type(refs) is not list or len(refs) > 20:
        raise TicketError("refs must be a list of at most 20 references")
    for ref in refs:
        _ref_target(ref, "ref")
    if type(stamp["close"]) is not bool:
        raise TicketError("a stamp's close must be true or false")


def _validate_decision(decision: Any) -> None:
    if type(decision) is not dict:
        raise TicketError("a decision must be an object")
    _text(decision.get("who"), "who")
    if decision.get("who_kind") not in ("person", "role"):
        raise TicketError("who_kind must be person or role")
    if decision["who_kind"] == "role" and not _is_text(decision.get("relayed_by")):
        raise TicketError("a role decision requires relayed_by")
    if decision.get("standing") not in ("lean", "settled"):
        raise TicketError("standing must be lean or settled")
    source = decision.get("source")
    if type(source) is not dict or source.get("what") not in OUTSIDE_EVENTS:
        raise TicketError("a decision's source must name a known outside event")
    _ref(source.get("ref"), "the decision source's ref")
    _text(decision.get("effect"), "effect")
    _text(decision.get("verbatim"), "verbatim")
    _phase(decision.get("phase"))
    if "evidence" in decision:
        _ref(decision["evidence"], "the decision's evidence")
    tags = decision.get("tags")
    if type(tags) is not list or not tags:
        raise TicketError("a decision requires at least one tag")
    for tag in tags:
        if type(tag) is not dict or set(tag) != {"type", "id"}:
            raise TicketError("a decision tag has exactly a type and an id")
        if tag["type"] not in TAG_TYPES:
            raise TicketError(f"unknown decision tag type: {tag['type']!r}")
        _ref(tag["id"], "a decision tag's id")
    if "gate" in decision:
        if decision["gate"] not in GATES:
            raise TicketError(f"unknown gate: {decision['gate']!r}")
        _whole(decision.get("plan_revision"), "a go decision's plan_revision")
        _criteria_texts(decision.get("criteria"), "a go decision's criteria")
        if "round" in decision:
            _whole(decision["round"], "an outward go's round")
            if decision["gate"] != "outward":
                raise TicketError("only an outward go names a review round")
    if "declined" in decision:
        # A declined go carries no `gate` key, so no reader, this one or an older one, counts it as a go.
        if decision["declined"] != "outward":
            raise TicketError("a declined decision names only the outward gate")
        if "gate" in decision:
            raise TicketError("a decision is either a go or a declined go, not both")
        _whole(decision.get("plan_revision"), "a declined decision's plan_revision")
        _criteria_texts(decision.get("criteria"), "a declined decision's criteria")
    if "pass" in decision:
        _text(decision["pass"], "a decision's pass")


def _validate_review(review: Any) -> None:
    if type(review) is not dict:
        raise TicketError("a review must be an object")
    _whole(review.get("round"), "a review's round")
    if review.get("verdict") not in ("pass", "changes"):
        raise TicketError("a review's verdict must be pass or changes")
    findings = review.get("findings")
    if type(findings) is not list or len(findings) > 20:
        raise TicketError("a review records a list of at most 20 findings")
    for finding in findings:
        _text(finding, "a review finding")
    _text(review.get("reviewer"), "reviewer")
    _ref_target(review.get("evidence"), "a review's evidence")
    _whole(review.get("plan_revision"), "a review's plan_revision")
    criteria = review.get("criteria")
    _criteria_texts(criteria, "a review's criteria")
    passed = review.get("passed")
    if type(passed) is not list:
        raise TicketError("a review's passed must be a list of criterion indices")
    for index in passed:
        if type(index) is not int or not 0 <= index < len(criteria):
            raise TicketError(f"a review's passed index is out of range: {index!r}")
    if len(set(passed)) != len(passed):
        raise TicketError("a review's passed repeats an index")


_PLAN_FIELDS = frozenset(
    ("schema", "key", "title", "revision", "pass", "criteria", "steps", "diff_statement",
     "open_questions", "items", "ticket_status", "ticket_updated_at", "ticket_spec")
)


def _validate_plan(plan: Any, key: str) -> None:
    if type(plan) is not dict:
        raise TicketError("a plan snapshot must be an object")
    if plan.get("schema") != PLAN_SCHEMA:
        raise TicketError(f"a plan snapshot must carry schema {PLAN_SCHEMA}")
    if set(plan) - _PLAN_FIELDS:
        raise TicketError(f"a plan snapshot has unknown fields: {sorted(set(plan) - _PLAN_FIELDS)}")
    if plan.get("key") != key:
        raise TicketError("the plan snapshot belongs to another ticket")
    _text(plan.get("title"), "a plan snapshot's title")
    _whole(plan.get("revision"), "a plan snapshot's revision")
    _text(plan.get("pass"), "a plan snapshot's pass")
    if "ticket_status" in plan and type(plan["ticket_status"]) is not str:
        raise TicketError("a plan snapshot's ticket_status must be text")
    if "ticket_updated_at" in plan and type(plan["ticket_updated_at"]) is not str:
        raise TicketError("a plan snapshot's ticket_updated_at must be text")
    if "ticket_spec" in plan:
        spec = plan["ticket_spec"]
        if type(spec) is not dict or set(spec) != {"version", "content_hash"}:
            raise TicketError("a plan snapshot's ticket_spec names exactly a version and a content_hash")
        _whole(spec["version"], "a plan snapshot's spec version")
        _text(spec["content_hash"], "a plan snapshot's spec content_hash")
    questions = plan.get("open_questions")
    if type(questions) is not list or not all(_is_text(item) for item in questions):
        raise TicketError("a plan snapshot's open_questions must be a list of non-empty text")
    if "diff_statement" in plan and not _is_text(plan["diff_statement"]):
        raise TicketError("a plan snapshot's diff_statement must be non-empty text")
    _criteria_texts(plan.get("criteria"), "a plan snapshot's criteria")
    if "items" in plan:
        _criteria_texts(plan["items"], "a plan snapshot's items")
    _validate_steps(plan)


def _validate_steps(plan: Mapping[str, Any]) -> None:
    """Every step once, in order; a skip carries its reason; a skipped plan step
    carries the one-sentence diff it stands for."""

    steps = plan.get("steps")
    if type(steps) is not list or not all(type(step) is dict for step in steps):
        raise TicketError("a plan snapshot requires its steps")
    if [step.get("name") for step in steps] != list(STEPS):
        raise TicketError(f"a plan snapshot lists the steps {', '.join(STEPS)}, each once and in order")
    for step in steps:
        if set(step) - {"name", "status", "skip"}:
            raise TicketError(f"plan step {step['name']} has unknown fields")
        if step.get("status") not in STEP_STATUSES:
            raise TicketError(f"plan step {step['name']} has an unknown status")
        skipped = step["status"] == "skipped"
        if skipped and not _is_text(step.get("skip")):
            raise TicketError(f"skipped plan step {step['name']} requires skip: <reason>")
        if not skipped and "skip" in step:
            raise TicketError(f"plan step {step['name']} is not skipped, so it carries no skip reason")
        if skipped and step["name"] == "plan":
            diff = plan.get("diff_statement")
            if type(diff) is not str or not _one_sentence(diff.strip()):
                raise TicketError("the plan may be skipped only for a one-sentence diff")
        if not skipped and step["name"] == "plan" and "diff_statement" in plan:
            raise TicketError("a plan snapshot carries a diff_statement only while the plan step is skipped")


def _validate_tried(tried: Any) -> None:
    if type(tried) is not dict or set(tried) - {"what", "phase", "outcome", "evidence"}:
        raise TicketError("a tried record has what, phase, and optionally outcome and evidence")
    _text(tried.get("what"), "what")
    _phase(tried.get("phase"))
    if "outcome" in tried:
        _text(tried["outcome"], "outcome")
    if "evidence" in tried:
        _ref(tried["evidence"], "the tried record's evidence")


def _validate_builder(fact: Any) -> None:
    """A builder fact as the toolkit's ``row-item.sh facts`` counts it: a round, a served model, the requested model or ``-``, a harness label and a source."""

    if type(fact) is not dict or set(fact) != {"round", "model", "requested", "harness", "source"}:
        raise TicketError("a builder fact has exactly a round, model, requested, harness and source")
    _whole(fact["round"], "a builder fact's round")
    if fact["round"] > _BUILDER_ROUND_MAX:
        raise TicketError("a builder fact's round is above the toolkit's round grammar")
    if type(fact["model"]) is not str or not _BUILDER_MODEL.fullmatch(fact["model"]):
        raise TicketError("a builder fact's model is outside the model grammar")
    requested = fact["requested"]
    if requested != "-" and (type(requested) is not str or not _BUILDER_MODEL.fullmatch(requested)):
        raise TicketError("a builder fact's requested model is outside the model grammar")
    if type(fact["harness"]) is not str or not _BUILDER_LABEL.fullmatch(fact["harness"]):
        raise TicketError("a builder fact's harness is outside the label grammar")
    if fact["source"] not in _BUILDER_SOURCES:
        raise TicketError("a builder fact's source is envelope or seat")


def _validate_event(kind: str, value: Any, key: str) -> None:
    """Validate one event as stored, uncoerced. A refusal is a TicketError."""

    if kind == "claim":
        _validate_claim(value)
    elif kind == "plan":
        _validate_plan(value, key)
    elif kind == "stamp":
        _validate_stamp(value)
    elif kind == "decision":
        _validate_decision(value)
    elif kind == "review":
        _validate_review(value)
    elif kind == "tried":
        _validate_tried(value)
    elif kind == "join":
        _validate_join(value)
    elif kind == "builder":
        _validate_builder(value)
    elif kind == "pr":
        if type(value) is not str or not _PULL_REQUEST.fullmatch(value):
            raise TicketError("pr_invalid: a pull request reference must be owner/repo#N")
    elif kind != "closed":
        raise TicketError(f"unknown event: {kind!r}")


# --- The card ----------------------------------------------------------------


def _blank_card(key: str, title: str) -> dict[str, Any]:
    return {
        "schema": CARD_SCHEMA,
        "key": key,
        "title": title,
        "status": "open",
        "pass": None,
        "plan": None,
        "decisions": [],
        "reviews": [],
        "tried": [],
        "stamps": [],
    }


_CARD_LISTS = ("decisions", "reviews", "tried", "stamps")


def _unreadable(where: str, why: str) -> StoreUnavailable:
    return StoreUnavailable(
        f"cannot read the ticket card {where}: {why}. Nothing was written; a person repairs the card.",
        code="card_unreadable",
    )


def _card_not_regular(where: str) -> StoreUnavailable:
    return StoreUnavailable(
        f"{where} is not a regular file (a link, a directory or another kind of file is at the card's path), "
        "so it was not read and nothing was written; a person removes it.",
        code="card_not_regular",
    )


def _check_card_shape(card: Any, key: str, where: str) -> None:
    """Refuse a card whose top-level fields are not of the section-2 shape."""

    if type(card) is not dict or card.get("schema") != CARD_SCHEMA or card.get("key") != key:
        raise _unreadable(where, f"it is not a {CARD_SCHEMA} card for ticket {key}")
    if "handoff" in card:
        # Only the ledger store derives this field, from an item's open hand-off, after this check (design 6.8).
        raise _unreadable(where, "it stores a handoff field, which only a ledger item's own hand-off event gives")
    if not _is_text(card.get("title")):
        raise _unreadable(where, "it has no title")
    if card.get("status") not in ("open", "closed"):
        raise _unreadable(where, f"its status {card.get('status')!r} is neither open nor closed")
    live = card.get("pass", "absent")
    if live is not None and (type(live) is not dict or not all(_is_text(live.get(n)) for n in ("id", "host", "started_at"))):
        raise _unreadable(where, "its pass is neither null nor a claim")
    if live is not None and not _PASS_ID.fullmatch(live["id"]):
        raise _unreadable(where, "its pass id is not hex")
    if "plan" not in card:
        raise _unreadable(where, "it has no plan field")
    for name in _CARD_LISTS:
        if type(card.get(name)) is not list or not all(type(item) is dict for item in card[name]):
            raise _unreadable(where, f"its {name} is not a list of objects")
    for review in card["reviews"]:
        if type(review.get("round")) is not int:
            raise _unreadable(where, "a stored review has no whole-number round")
    _check_plan_shape(card, key, where)


def _check_plan_shape(card: Mapping[str, Any], key: str, where: str) -> None:
    """Refuse a stored plan snapshot that breaks the section-2 shape, before any gate is derived.

    A plan whose identity cannot be read is left to ``_identity``, which refuses it
    as ``plan_unidentifiable``.
    """

    if card["plan"] is None:
        return
    try:
        _identity(card)
    except StoreUnavailable:
        return
    try:
        _validate_plan(card["plan"], key)
    except TicketError as exc:
        raise _unreadable(where, f"its plan snapshot is not valid ({exc})") from exc


def _fold_event(card: dict[str, Any], kind: str, value: Any) -> None:
    if kind == "claim":
        card["pass"] = dict(value)
    elif kind == "plan":
        card["plan"] = copy.deepcopy(value)
    elif kind == "stamp":
        card["stamps"].append(copy.deepcopy(value))
    elif kind == "decision":
        card["decisions"].append(copy.deepcopy(value))
    elif kind == "review":
        card["reviews"].append(copy.deepcopy(value))
    elif kind == "tried":
        card["tried"].append(copy.deepcopy(value))
    elif kind == "pr":
        card["pr"] = value
    elif kind == "closed":
        card["status"] = "closed"
    elif kind == "join":
        # A join binds the store handle (design section 5); the card is unchanged.
        return
    elif kind == "builder":
        # A builder fact is an item note on the ledger only; the card is unchanged.
        return


def _fold(card: Mapping[str, Any], events: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    """The card with the events applied, in order; the input is not changed."""

    folded = copy.deepcopy(dict(card))
    for kind, value in events:
        _fold_event(folded, kind, value)
    return folded


# --- Plan identity and coverage (design 3.1, D1) ----------------------------


def _unidentifiable(why: str) -> StoreUnavailable:
    return StoreUnavailable(
        f"the card's stored plan cannot be identified: {why}. The card is corrupt, so nothing was written "
        "and the pass ends; a person repairs the card.",
        code="plan_unidentifiable",
    )


def _identity(card: Mapping[str, Any]) -> tuple[int, tuple[str, ...]] | None:
    """The stored plan's ``(revision, criteria)``, None when no plan was presented.

    Only ``None`` means no plan; anything else that cannot be read strictly is
    refused, because a guessed revision could collide with a stored approval.
    """

    plan = card["plan"]
    if plan is None:
        return None
    if not isinstance(plan, Mapping):
        raise _unidentifiable("the plan is not an object")
    revision = plan.get("revision")
    if type(revision) is not int:
        raise _unidentifiable("its revision is not a whole number")
    if revision < 1:
        raise _unidentifiable("its revision is below 1")
    criteria = plan.get("criteria")
    if type(criteria) is not list:
        raise _unidentifiable("its criteria are not a list")
    if not criteria:
        raise _unidentifiable("its criteria are empty")
    if not all(_is_text(item) for item in criteria):
        raise _unidentifiable("a criterion is not non-empty text")
    if not _is_text(plan.get("pass")):
        raise _unidentifiable("it does not name the pass that presented it")
    return revision, tuple(criteria)


def _covers(record: Mapping[str, Any], identity: tuple[int, tuple[str, ...]]) -> bool:
    """A record covers the card's plan when it carries exactly that identity."""

    revision, criteria = identity
    return record.get("plan_revision") == revision and record.get("criteria") == list(criteria)


def _is_go(decision: Mapping[str, Any], gate: str, key: str, card: Mapping[str, Any]) -> bool:
    """A person's go, or the open hand-off controller's go; relaying is attribution only."""

    source = decision.get("source")
    handoff = card.get("handoff")
    controller = handoff.get("controller") if isinstance(handoff, Mapping) else None
    return (
        decision.get("gate") == gate
        and (
            decision.get("who_kind") == "person"
            or (
                decision.get("who_kind") == "role"
                and _is_handoff_name(controller)
                and type(decision.get("who")) is str and decision["who"] == controller
                and _is_text(decision.get("relayed_by"))
            )
        )
        and decision.get("standing") == "settled"
        and type(source) is dict and source.get("what") == "ratification"
        and {"type": "ticket", "id": key} in _items(decision.get("tags"))
    )


def _has_go(card: Mapping[str, Any], gate: str, identity: tuple[int, tuple[str, ...]], key: str) -> bool:
    """A go of ``gate`` covers the card's plan. A plan go counts only when it
    names the pass that presented the stored plan."""

    return any(
        _is_go(decision, gate, key, card)
        and _covers(decision, identity)
        and (gate != "plan" or decision.get("pass") == card["plan"]["pass"])
        for decision in card["decisions"]
    )


def _declines(card: Mapping[str, Any], identity: tuple[int, tuple[str, ...]]) -> list[Mapping[str, Any]]:
    """Every declined outward go that covers the card's plan, whoever recorded it."""

    return [
        decision for decision in card["decisions"]
        if decision.get("declined") == "outward" and _covers(decision, identity)
    ]


def _has_declined(card: Mapping[str, Any], identity: tuple[int, tuple[str, ...]], key: str) -> bool:
    """The person's settled no at the outward go of the card's plan: what a kept-local close stands on."""

    return any(
        decision.get("who_kind") == "person"
        and decision.get("standing") == "settled"
        and type(decision.get("source")) is dict and decision["source"].get("what") == "ratification"
        and {"type": "ticket", "id": key} in _items(decision.get("tags"))
        for decision in _declines(card, identity)
    )


def _outward_open(card: Mapping[str, Any], identity: tuple[int, tuple[str, ...]], key: str) -> bool:
    """A non-declined go covers the plan and final round; legacy roundless goes precede a PR only."""

    final = _passing_final(card, identity)
    return final is not None and not _declines(card, identity) and any(
        _is_go(decision, "outward", key, card)
        and _covers(decision, identity)
        and (
            decision.get("round") == final["round"]
            or ("round" not in decision and not card.get("pr"))
        )
        for decision in card["decisions"]
    )


def _answered(revision: int) -> str:
    return (
        f"the outward gate of plan revision {revision} already holds the person's answer, so nothing more is "
        "recorded: close() after a go, close_local() after a declined go"
    )


def _passing_final(card: Mapping[str, Any], identity: tuple[int, tuple[str, ...]]) -> Mapping[str, Any] | None:
    """The card's last review when it passes, covers the plan, lists every
    criterion and carries its evidence; otherwise None."""

    reviews = card["reviews"]
    if not reviews:
        return None
    final = reviews[-1]
    if (
        final.get("verdict") == "pass"
        and _covers(final, identity)
        and final.get("passed") == list(range(len(identity[1])))
        and _is_text(final.get("evidence"))
    ):
        return final
    return None


def _reached_steps(card: Mapping[str, Any], identity: tuple[int, tuple[str, ...]] | None, key: str) -> frozenset[str]:
    """The plan steps the card's stored events prove were done, from the events alone (design 3.5).

    A pure function of the card, as ``_passing_final`` is for the criteria. A
    step this cannot place is left out, so it renders pending (or skipped,
    when the snapshot skipped it), never complete:

    - ``orient`` and ``research``: a plan was presented, which a pass reaches only through both.
    - ``plan``: a plan go of the pass that presented the plan covers its identity.
    - ``build``: a review round covering the plan identity is stored. ``begin_build`` writes
      nothing, so a round is the one stored proof that a build was begun and handed to review.
    - ``review``: the last round is a passing round of the plan.
    - ``outward``: the card is closed and the outward go covers the plan. The close is the
      atomic write that ends the outward actions the go opened.

    Each step from ``plan`` on is placed only after the one before it, so a
    later event never makes an earlier unplaced step look done.
    """

    if identity is None:
        return frozenset()
    reached = {"orient", "research"}
    if not _has_go(card, "plan", identity, key):
        return frozenset(reached)
    reached.add("plan")
    if not any(_covers(review, identity) for review in card["reviews"]):
        return frozenset(reached)
    reached.add("build")
    if _passing_final(card, identity) is None:
        return frozenset(reached)
    reached.add("review")
    if card["status"] == "closed" and _has_go(card, "outward", identity, key):
        reached.add("outward")
    return frozenset(reached)


# --- Changes (design 4.2, 4.3) and flags (4.6) ------------------------------


def _source_differences(stored: Sequence[str], source: Sequence[str]) -> list[str]:
    """How the source's criteria differ from the stored ones: exact, counting occurrences."""

    if tuple(stored) == tuple(source):
        return []
    extra = Counter(source)
    extra.subtract(stored)
    lines = []
    for text in source:
        if extra[text] > 0:
            lines.append(f"acceptance criterion is new or changed on the ticket: {text}")
            extra[text] -= 1
    for text in stored:
        if extra[text] < 0:
            lines.append(f"acceptance criterion was removed from the ticket: {text}")
            extra[text] += 1
    if not lines:
        lines.append("the acceptance criteria were reordered on the ticket")
    return lines


def _spec_differences(plan: Mapping[str, Any], ticket: Ticket) -> list[str]:
    """How the ticket's current spec differs from the one the stored plan was made against (design 6.5).

    The plan snapshot holds the spec version and content hash the presenting
    pass read; the ticket holds the ones ``spec show`` reported on this open.
    Any difference is a change, handled as a criteria edit is: another version
    (a source-only move included), other content under the same version, a
    spec where the plan had none, or none where it had one. Two sources with
    no spec never differ.
    """

    stored = plan.get("ticket_spec")
    current = None if ticket.spec is None else ticket.spec.record()
    if stored == current:
        return []
    if stored is None:
        return [f"the ticket now has spec v{current['version']} and the plan was stored without one"]
    if current is None:
        return [f"the plan was stored against spec v{stored['version']} and the ticket source has no spec"]
    if stored["version"] != current["version"]:
        return [f"the ticket's spec changed: v{stored['version']} -> v{current['version']}"]
    return [f"the ticket's spec v{current['version']} now holds other content"]


def _card_side_changes(card: Mapping[str, Any], identity: tuple[int, tuple[str, ...]], key: str) -> list[str]:
    """F11: a review or plan go of the plan's revision that no longer covers it."""

    revision = identity[0]
    lines = []
    for review in card["reviews"]:
        if review.get("plan_revision") == revision and not _covers(review, identity):
            lines.append(f"review round {review['round']} reviewed other acceptance criteria than plan revision {revision} on the card")
    for decision in card["decisions"]:
        if _is_go(decision, "plan", key, card) and decision.get("plan_revision") == revision and not _covers(decision, identity):
            lines.append(f"the plan go of plan revision {revision} approved other acceptance criteria than the card's plan")
    return lines


def _f1(m: int, what: str) -> str:
    return (f"the acceptance criteria changed since plan revision {m} was stored ({what}); "
            f"plan revision {m} must be presented again as a new revision for a new plan go")


def _f2(n: int, m: int, what: str) -> str:
    return f"review round {n} passed plan revision {m}; the criteria changed since ({what}); that round covers nothing in a new plan"


def _f3(n: int, m: int, what: str) -> str:
    return (f"review round {n} passed plan revision {m}; the criteria changed since ({what}); no review round is left "
            "for a new plan; a person restores the criteria or opens a new ticket")


def _f4(m: int) -> str:
    return f"the outward go of plan revision {m} authorizes nothing now; outward actions already taken are not undone"


def _f5(m: int) -> str:
    return f"three review rounds are stored and none is a passing round of plan revision {m}; the review cap is reached"


def _f6(m: int, what: str) -> str:
    return (f"the acceptance criteria changed since plan revision {m} was stored ({what}); "
            "review stopped at the round cap, so no new plan can be reviewed")


def _f7(what: str) -> str:
    return f"the ticket's criteria changed after close ({what}); the ticket stays closed; follow-up work is a new ticket"


def _f8(m: int) -> str:
    return f"plan revision {m} was presented by another pass and has no plan go; resuming at plan to present it again"


def _f13() -> str:
    return (
        "no plan is stored for this ticket, so orient and research start again: the card does not record "
        "them (they run in memory and write nothing), so what an earlier pass found is not kept; read the "
        "ticket and the repository again before presenting a plan"
    )


def _source_flags(plan: Mapping[str, Any], ticket: Ticket) -> list[str]:
    """F9, F10 and F12: status, update-time and title edits are flags, never changes."""

    flags = []
    if "ticket_status" in plan and plan["ticket_status"] != ticket.status:
        flags.append(f"ticket status changed: {plan['ticket_status']} -> {ticket.status}")
    if plan.get("ticket_updated_at") != ticket.updated_at:
        flags.append("ticket was updated since the plan card was written")
    if plan["title"] != ticket.title:
        flags.append(f"ticket title changed: {plan['title']} -> {ticket.title}")
    return flags


def _closed_problems(card: Mapping[str, Any], identity: tuple[int, tuple[str, ...]] | None, key: str) -> list[str]:
    """What a closed card fails of the state the atomic close writes."""

    if identity is None:
        return ["the card holds no plan"]
    problems = []
    if not _has_go(card, "plan", identity, key):
        problems.append("no plan go covers the card's plan")
    went_out = _has_go(card, "outward", identity, key)
    kept_local = _has_declined(card, identity, key)
    if went_out and _declines(card, identity):
        problems.append("an outward go and a declined outward go both cover the card's plan")
    elif not went_out and not kept_local:
        problems.append("no outward go covers the card's plan")
    if _passing_final(card, identity) is None:
        problems.append("the last review round is not a passing round of the card's plan")
    if len(card["reviews"]) > MAX_REVIEW_ROUNDS:
        problems.append("more than three review rounds are stored")
    if _card_side_changes(card, identity, key):
        problems.append("a record of the card's plan revision covers other criteria")
    stamps = card["stamps"]
    last = stamps[-1] if stamps else {}
    if last.get("phase") != "close":
        problems.append("the last stamp is not at the close phase")
    if last.get("close") is not True:
        problems.append("no stamp is marked as the close")
    if kept_local and not went_out and last.get("point") != KEPT_LOCAL_POINT.format(revision=identity[0]):
        problems.append("a kept-local close does not carry the kept-local point")
    try:
        _validate_stamp({**last, "phase": "close", "close": True})
    except TicketError as exc:
        problems.append(f"the close stamp is malformed ({exc})")
    return problems


Identity = tuple[int, tuple[str, ...]]


def _derive(card: Mapping[str, Any], ticket: Ticket, pass_id: str | None, key: str) -> tuple[str, list[str], Identity | None]:
    """The resume state, its flags and the plan's identity, from the card and
    the fresh source alone (design 3.2)."""

    identity = _identity(card)
    if card["status"] == "closed":
        problems = _closed_problems(card, identity, key)
        if problems:
            raise StoreUnavailable(
                f"ticket {key} is marked closed but the close does not validate: {'; '.join(problems)}. "
                "Nothing was written; a person repairs the card.",
                code="close_invalid",
            )
        differences = _source_differences(identity[1], ticket.acceptance_criteria) + _spec_differences(card["plan"], ticket)
        flags = [_f7("; ".join(differences))] if differences else []
        return "closed", flags + _source_flags(card["plan"], ticket), identity
    if identity is None:
        # A card an earlier pass already touched (a stop stamp, a tried record) or a join of a live pass is a
        # resume, and orient and research are repeated; a blank card with no pass id is a first open.
        resumed = pass_id is not None or bool(card["stamps"]) or bool(card["tried"])
        return "orient", [_f13()] if resumed else [], None
    revision = identity[0]
    source_changes = _source_differences(identity[1], ticket.acceptance_criteria) + _spec_differences(card["plan"], ticket)
    card_changes = _card_side_changes(card, identity, key)
    rounds = len(card["reviews"])
    final = _passing_final(card, identity)
    extra = _source_flags(card["plan"], ticket)
    if source_changes or card_changes:
        what = "; ".join(source_changes)
        flags = list(card_changes)
        if final is not None:
            state = "plan" if rounds < MAX_REVIEW_ROUNDS else "stopped"
            if source_changes:
                flags.append((_f2 if state == "plan" else _f3)(final["round"], revision, what))
        elif rounds < MAX_REVIEW_ROUNDS:
            state = "plan"
            if source_changes:
                flags.append(_f1(revision, what))
        else:
            state = "stopped"
            flags.append(_f5(revision))
            if source_changes:
                flags.append(_f6(revision, what))
        if _has_go(card, "outward", identity, key):
            flags.append(_f4(revision))
        return state, flags + extra, identity
    if rounds >= MAX_REVIEW_ROUNDS and final is None:
        return "stopped", [_f5(revision)] + extra, identity
    if not _has_go(card, "plan", identity, key):
        if card["plan"]["pass"] == pass_id:
            return "plan_go", extra, identity
        return "plan", [_f8(revision)] + extra, identity
    if final is None:
        return "build", extra, identity
    return "outward_go", extra, identity


# --- The pass claim (design 5) ----------------------------------------------


def _check_pass(key: str, live: Mapping[str, Any] | None, mine: str | None, ended: str | None) -> None:
    """Refuse a write unless this handle may write under the ticket's claim.

    A handle bound to a pass writes only while that pass is the live one; an
    unbound handle only while no pass is live; an ended handle writes nothing.
    """

    if ended is not None:
        raise StoreUnavailable(
            f"pass {ended} on ticket {key} has ended, so nothing was written. Open a new pass to write again.",
            code="pass_ended",
        )
    if mine is None:
        if live is not None:
            raise TicketHeld(key, live)
        return
    if live is not None and live["id"] == mine:
        return
    if live is not None:
        raise TicketHeld(key, live)
    raise StoreUnavailable(
        f"pass {mine} is no longer live on ticket {key}, so nothing was written. Open a new pass to write again.",
        code="pass_ended",
    )


# The two binding events (design section 5): a successful one binds the handle to
# the pass it names, inside the store's locked write, and nowhere else.
_BINDING = ("claim", "join")


def _binding(events: Sequence[tuple[str, Any]]) -> tuple[str, str] | None:
    """The binding event's ``(kind, pass id)`` among ``events``, or None.

    A binding event travels alone: one with company is a ``TicketError`` and
    nothing is written, so a bind is one step and never rides with a record.
    """

    bound = [(kind, value["id"]) for kind, value in events if kind in _BINDING]
    if not bound:
        return None
    if len(events) != 1:
        raise TicketError(f"a {bound[0][0]} travels alone in its apply; nothing was written")
    return bound[0]


def _handle_used(key: str, used: str) -> StoreUnavailable:
    """The refusal of a store handle already used for a pass: one handle serves one pass."""

    return StoreUnavailable(
        f"this store handle was already used for pass {used} on ticket {key}, so nothing was written; "
        "open each pass with a fresh store handle",
        code="handle_used",
    )


def _stale(key: str, detail: str) -> StoreUnavailable:
    return StoreUnavailable(
        f"ticket {key} changed since this pass last read or wrote it: {detail}. Nothing was written and the pass "
        "ends; open it again to see the change.",
        code="stale",
    )


# --- The store interface (design 6) and the markdown store (6.1) -----------


class Store(Protocol):
    """Four calls. A handle carries the pass it writes for in ``pass_id`` and
    binds to it only inside its own ``apply``: a ``claim`` or a ``join`` event,
    alone in its call, binds the handle under the store's serialization, and a
    used handle (bound to a pass, or whose pass has ``ended``) is refused there
    as ``handle_used``. One handle serves one pass (design section 5).
    ``TicketPass`` reads ``pass_id`` and ``ended`` and writes neither: a caller
    binds a handle by sending the event, never by assigning."""

    key: str
    ref: str

    @property
    def pass_id(self) -> str | None:
        """The pass this handle writes for: bound by its own ``apply``, cleared by ``release``."""
        ...

    @property
    def ended(self) -> str | None:
        """The id of the last pass this handle held that has ended, or None."""
        ...

    def read(self) -> tuple[dict[str, Any], str]: ...

    def apply(self, token: str, events: Sequence[tuple[str, Any]]) -> str: ...

    def release(self) -> None: ...

    def clear_pass(self, pass_id: str) -> None: ...


_LOCK_OPEN_ATTEMPTS = 5
_BLOCK_START = "<!-- ticket-card:v2\n"
_BLOCK_END = "\n-->"
# The errors a no-follow open gives for a node at the card's path that is not
# a regular file: a link (ELOOP) or a socket (ENXIO on Linux; EOPNOTSUPP, and
# ENOTSUP where it is a distinct number, on Darwin). An open that fails with
# one of these is refused ``card_not_regular`` from the error alone, so the
# refusal does not depend on the node still being there for a second look.
_NOT_REGULAR_OPEN_ERRNOS = frozenset(
    getattr(errno, name) for name in ("ELOOP", "ENXIO", "EOPNOTSUPP", "ENOTSUP") if hasattr(errno, name)
)


def _token(card: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(card, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class MarkdownStore:
    """The card at ``<repo>/.ticket/<key>.md``: a rendered summary above one
    machine block, next to its lock ``.ticket/<key>.lock``. Both must be
    git-ignored; neither is ever staged or committed.
    """

    # What ``choose_store`` chose and why, printed by ``open``; None on a handle built directly.
    selection: str | None = None

    def __init__(self, repo: str | Path, key: str, title: str):
        if not _KEY.fullmatch(key):
            raise TicketError(f"unsafe ticket key: {key!r}")
        self.repo = Path(repo).resolve()
        self.key = key
        self.title = title
        self.relative_path = Path(".ticket") / f"{key}.md"
        self.path = self.repo / self.relative_path
        self.lock_path = self.path.with_name(f"{key}.lock")
        self.ref = self.relative_path.as_posix()
        self._pass_id: str | None = None
        self._ended: str | None = None
        # Threads sharing this handle take turns before the file lock.
        self._threads = threading.RLock()

    @property
    def pass_id(self) -> str | None:
        """The pass this handle writes for: bound by its own ``apply``, cleared by ``release``.

        Read-only. The field behind it changes in two places only, ``apply``
        on a successful binding event and ``release``, so no caller binds or
        rebinds a handle by assignment.
        """

        return self._pass_id

    @property
    def ended(self) -> str | None:
        """The id of the last pass this handle held that has ended, or None."""

        return self._ended

    # The lock and the files.

    def _inside_repo(self) -> None:
        try:
            self.path.parent.resolve().relative_to(self.repo)
        except ValueError as exc:
            raise StoreUnavailable(
                f".ticket for ticket {self.key} resolves outside the repository, so nothing was written",
                code="outside_repository",
            ) from exc

    def _check_ignored(self) -> None:
        for relative in (self.relative_path, self.relative_path.with_name(self.lock_path.name)):
            result = subprocess.run(
                ["git", "-C", str(self.repo), "check-ignore", "--quiet", "--", relative.as_posix()],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise StoreUnavailable(
                    f"{relative.as_posix()} is not git-ignored; add `.ticket/` to the repository's ignore rules "
                    "and retry. No ignore file was changed and nothing was written.",
                    code="not_ignored",
                )

    def _open_directory(self) -> int:
        """``.ticket/`` itself, opened without following a link; the card and the lock are opened through it."""

        return os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[int]:
        """This handle's thread lock, then an exclusive ``flock`` on the lock file; yields the directory.

        ``.ticket/`` is opened without following a link, and the lock file is
        opened through it, without following a link, and must be a regular
        file. The card is read and written through the same descriptor while
        the lock is held (``_load``, ``_save``): every operation on the card
        goes through it, never through a path, so a ``.ticket`` swapped for a
        link after this open cannot move the card. The operating system drops
        the lock if its holder dies.
        """

        with self._threads:
            self._inside_repo()
            self._check_ignored()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            directory = self._open_directory()
            try:
                fd = self._open_lock(directory)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise StoreUnavailable(
                            f"{self.ref[:-3]}.lock is not a regular file, so it was not used and nothing was written",
                            code="lock_not_regular",
                        )
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    yield directory
                finally:
                    os.close(fd)
            finally:
                os.close(directory)

    def _open_lock(self, directory: int) -> int:
        # Two writers creating the lock at once can see ENOENT from the create
        # through the directory; the file exists by the next attempt.
        attempts = _LOCK_OPEN_ATTEMPTS
        while True:
            try:
                return os.open(self.lock_path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            except OSError as exc:
                attempts -= 1
                if isinstance(exc, FileNotFoundError) and attempts:
                    continue
                raise StoreUnavailable(
                    f"cannot open {self.ref[:-3]}.lock as a regular file ({exc}), so nothing was written",
                    code="lock_unopenable",
                ) from exc

    def _card_mode(self, directory: int) -> int | None:
        """The mode of whatever is at the card's path, read through ``directory`` without following a link; None when nothing is."""

        try:
            return os.stat(self.path.name, dir_fd=directory, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return None

    def _load(self, directory: int) -> tuple[dict[str, Any], str]:
        """The card as read through ``directory``, or the blank card when there is none.

        Every step goes through the verified ``.ticket/`` descriptor, never
        through a path. A node at the card's path that is not a regular file
        (a link, a FIFO, a directory, a socket) is refused ``card_not_regular``
        before it is opened; the open itself follows no link, and what it
        opened is checked once more (``fstat``), the race check for a node
        swapped in between the two. An open that fails with the error such
        a node gives (``_NOT_REGULAR_OPEN_ERRNOS``) is refused the same way
        from the error alone, even if the node is gone again by then; so is
        one whose failure a second no-follow ``stat`` traces to a
        non-regular node still there; any other error (a permission error
        on a regular file) propagates as it is. ``O_NONBLOCK`` keeps a FIFO there
        from holding the open until a writer appears; a regular file reads
        the same with it.
        """

        mode = self._card_mode(directory)
        if mode is None:
            return _blank_card(self.key, self.title), NO_TOKEN
        if not stat.S_ISREG(mode):
            raise _card_not_regular(self.ref)
        try:
            fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return _blank_card(self.key, self.title), NO_TOKEN
        except OSError as exc:
            if exc.errno in _NOT_REGULAR_OPEN_ERRNOS:
                raise _card_not_regular(self.ref) from exc
            mode = self._card_mode(directory)
            if mode is not None and not stat.S_ISREG(mode):
                raise _card_not_regular(self.ref) from exc
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise _card_not_regular(self.ref)
        except BaseException:
            os.close(fd)
            raise
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            body = handle.read()
        start = body.find(_BLOCK_START)
        end = body.find(_BLOCK_END, start + 1)
        if start < 0 or end < 0:
            raise _unreadable(self.ref, "it holds no complete ticket-card block")
        try:
            card = json.loads(body[start + len(_BLOCK_START) : end])
        except json.JSONDecodeError as exc:
            raise _unreadable(self.ref, f"its block is not JSON ({exc.msg})") from exc
        _check_card_shape(card, self.key, self.ref)
        return card, _token(card)

    def _save(self, card: Mapping[str, Any], directory: int) -> None:
        """Write the card through ``directory``, never through a path.

        The temporary file is created relative to the verified descriptor
        (exclusive, mode 0600, no link followed, a random name in the
        ``.<key>.md.<random>.tmp`` shape), written, and renamed over the card
        relative to the same descriptor; on failure it is removed the same
        way. A ``.ticket`` swapped for a link after the directory was opened
        therefore cannot send the card outside the repository.
        """

        temporary = f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(_render_card(card))
            os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=directory)
            raise

    # The four calls.

    def read(self) -> tuple[dict[str, Any], str]:
        """The card and its token, read through ``.ticket/`` without the lock.

        A read takes no lock and runs no ignore check, so it works on a
        repository that has not yet ignored ``.ticket/``; an absent ``.ticket/``
        is the blank card, like an absent card.
        """

        self._inside_repo()
        try:
            directory = self._open_directory()
        except FileNotFoundError:
            return _blank_card(self.key, self.title), NO_TOKEN
        try:
            return self._load(directory)
        finally:
            os.close(directory)

    def apply(self, token: str, events: Sequence[tuple[str, Any]]) -> str:
        """Land every event or none, only while the card is still at ``token``.

        A binding event (a ``claim`` or a ``join``, alone in its call) binds
        this handle to its pass inside the lock, in this order (design section
        5, rule 1): a used handle is refused ``handle_used`` first, before the
        card is read, so that refusal wins over a stale token and over every
        card refusal (two opens on one handle that both read before either
        claimed must see ``handle_used``, never ``held``); the card is loaded
        and a token that differs from the one given is refused ``stale``; the
        claim is checked against the card as loaded (a claim needs no live
        pass, a join needs the pass it names); the events fold; the card is
        saved, except for a join, which changes no byte; only then is the
        handle bound. A save that fails leaves the handle unbound.
        """

        for kind, value in events:
            _validate_event(kind, value, self.key)
        binding = _binding(events)
        joining = binding is not None and binding[0] == "join"
        with self._locked() as directory:
            if binding is not None and (self._pass_id is not None or self._ended is not None):
                raise _handle_used(self.key, self._pass_id or self._ended)
            card, current = self._load(directory)
            if current != token:
                raise _stale(self.key, "the card on disk changed")
            mine = binding[1] if joining else self._pass_id
            _check_pass(self.key, card["pass"], mine, self._ended)
            card = _fold(card, events)
            if not joining:
                self._save(card, directory)
            if binding is not None:
                self._pass_id, self._ended = binding[1], None
        return token if joining else _token(card)

    def release(self) -> None:
        """Clear this handle's own claim, whatever has landed since; no token.

        ``mine`` is the one pass this handle ever held (design section 5, rule
        5): a handle binds once, so this clears no other pass's claim, and a
        handle that was never bound clears nothing.
        """

        mine = self._pass_id
        self._pass_id, self._ended = None, mine
        with self._locked() as directory:
            card, _ = self._load(directory)
            if card["pass"] is not None and card["pass"]["id"] == mine:
                card["pass"] = None
                self._save(card, directory)

    def clear_pass(self, pass_id: str) -> None:
        """A person's release of a crashed pass: refused unless ``pass_id`` is the live claim."""

        with self._locked() as directory:
            card, _ = self._load(directory)
            live = card["pass"]
            if live is None or live["id"] != pass_id:
                raise StoreUnavailable(
                    f"pass {pass_id} is not the live pass on ticket {self.key}"
                    + (f" (the live pass is {live['id']})" if live else " (no pass is live)")
                    + "; nothing was changed",
                    code="held" if live else "pass_ended",
                )
            card["pass"] = None
            self._save(card, directory)


def _inline(value: Any) -> str:
    """Recorded text on one line, escaped for Markdown and HTML comments."""

    text = " / ".join(str(value).splitlines())
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_card(card: Mapping[str, Any]) -> str:
    """The card file: a summary for people above the one machine block."""

    lines = [f"# {_inline(card['key'])}: {_inline(card['title'])}", "", f"Status: {_inline(card['status'])}"]
    live = card.get("pass")
    if live:
        lines.append(f"Live pass: {_inline(live['id'])}, host {_inline(live['host'])}, started {_inline(live['started_at'])}")
    lines.extend(("", "## Plan"))
    plan = card.get("plan")
    if isinstance(plan, Mapping):
        lines.append(f"Revision {_inline(plan.get('revision'))}, presented by pass {_inline(plan.get('pass'))}")
        lines.extend(f"- {_inline(text)}" for text in _items(plan.get("criteria")))
        lines.extend(f"- open question: {_inline(text)}" for text in _items(plan.get("open_questions")))
    else:
        lines.append("(none)")
    lines.extend(("", "## Decisions"))
    for decision in card["decisions"]:
        gate = decision.get("gate")
        if gate:
            label = f"{gate} go, plan revision {_inline(decision.get('plan_revision'))}"
        elif decision.get("declined") == "outward":
            # The ledger elects `no go` over `go` for the same answer; say it here too.
            label = f"declined outward go (no go), plan revision {_inline(decision.get('plan_revision'))}"
        else:
            label = _inline(decision.get("standing"))
        lines.append(f"- {label}, {_inline(decision.get('who'))}: {_inline(decision.get('verbatim'))}")
    lines.extend(("", "## Review rounds"))
    for review in card["reviews"]:
        lines.append(
            f"- round {_inline(review.get('round'))}: {_inline(review.get('verdict'))} on plan revision "
            f"{_inline(review.get('plan_revision'))} ({_inline(review.get('evidence'))})"
        )
        lines.extend(f"  finding: {_inline(text)}" for text in _items(review.get("findings")))
    lines.extend(("", "## Tried"))
    for tried in card["tried"]:
        line = f"- [{_inline(tried.get('phase'))}] {_inline(tried.get('what'))}"
        if tried.get("outcome"):
            line += f": {_inline(tried['outcome'])}"
        lines.append(line)
    lines.extend(("", "## Stamps"))
    for stamp in card["stamps"]:
        lines.append(f"- {'close' if stamp.get('close') else 'stop'} [{_inline(stamp.get('phase'))}]: {_inline(stamp.get('point'))}")
        if stamp.get("next_skills"):
            lines.append(f"  next skills: {_inline(', '.join(map(str, _items(stamp['next_skills']))))}")
        if stamp.get("refs"):
            lines.append(f"  refs: {_inline(', '.join(map(str, _items(stamp['refs']))))}")
    # JSON keeps the block on one line; escaping <, > and & keeps a recorded
    # `-->` or `<!--` from ending or opening an HTML comment.
    machine = (
        json.dumps(card, sort_keys=True, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    lines.extend(("", _BLOCK_START + machine + _BLOCK_END, ""))
    return "\n".join(lines)


# --- The ledger store (design 6.2) ------------------------------------------

# The writer name every ledger payload carries in its ``source``.
_LEDGER_WRITER = "ticket"
# A ticket record in an ``item.note``: the frame names the record's kind and
# its format version, and the body is the record's canonical JSON (design 6.2.3).
_RECORD_FRAME = re.compile(r"ticket-([a-z]+):v([1-9][0-9]*) (.+)", re.DOTALL)
_RECORD_KINDS = ("plan", "decision", "review")
# The pass record a ledger pass writes with its claim (design 6.2): a framed note, never a card event.
_PASS_RECORD = "pass"
_RECORD_VERSION = 2
# Item facts the pass writes beside its records (TL4 one-item skill design 3, CP1 3.5): a recorded decision also
# becomes one ``decided:`` note, and a build result that names a served model becomes one builder note. Neither is
# a ticket record, so no reader of the card sees them; the toolkit's ``row-item.sh facts`` counts them. The
# grammar below is that command's: its model and label patterns, its ``built-by:`` text, its builder key and its
# rule for encoding a key component.
_DECIDED_PREFIX = "decided: "
_BUILDER_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,199}")
_BUILDER_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}")
_BUILDER_SOURCES = ("envelope", "seat")
_BUILDER_ROUND_MAX = 999_999_999
_KEY_SAFE = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@+-")
_CLAIM_PURPOSE = re.compile(r"ticket pass [0-9a-f]+ on (.+) at (\S+)")
# How an ``ok: false`` answer is read (design 6.2.4). Only the two lost races
# are ``stale``. A permanent refusal was rolled back or never started, so its
# batch is never looked up. Any other code is ambiguous.
_LEDGER_RACES = frozenset(("stale_revision", "conflict"))
_LEDGER_PERMANENT = frozenset(
    ("validation", "missing_harness", "shared_database", "synced_path", "not_found", "missing_db", "integrity")
)
# What a query for an item that does not exist answers: the design's
# ``not_found``, and the ``empty`` the CLI's query verb gives a filter that
# matches no row.
_LEDGER_NO_ITEM = frozenset(("not_found", "empty"))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _enc(component: str) -> str:
    """Every byte of a key component outside letters, digits and ``._@+-`` becomes ``%XX``."""

    return "".join(chr(byte) if byte in _KEY_SAFE else f"%{byte:02X}" for byte in component.encode("utf-8"))


def _builder_key(item: str, fact: Mapping[str, Any]) -> str:
    return f"row-item:builder:{_enc(item)}:r{fact['round']}:{_enc(fact['model'])}"


def _builder_text(fact: Mapping[str, Any]) -> str:
    return (
        f"built-by: harness={fact['harness']} model={fact['model']} requested={fact['requested']} "
        f"source={fact['source']} round={fact['round']}"
    )


def _decided_text(decision: Any) -> str | None:
    """The one-line ``decided:`` note for a decision record, or None when the record lacks what the note states.

    The note says what was decided (the record's ``effect``) and by whom (its ``who``, and ``relayed_by`` for a
    role's decision), each folded to one line. A record without text for either gets no note.
    """

    if type(decision) is not dict or not _is_text(decision.get("who")) or not _is_text(decision.get("effect")):
        return None
    by = _one_line(decision["who"])
    if _is_text(decision.get("relayed_by")):
        by += f", relayed by {_one_line(decision['relayed_by'])}"
    return f"{_DECIDED_PREFIX}{_one_line(decision['effect'])} (by {by})"


def _one_line(text: Any) -> str:
    return " ".join(str(text).split())


@dataclass(frozen=True)
class _Answer:
    """One answer of the ledger CLI: its envelope's ``ok``, ``data`` and ``error``.

    ``ok`` is None when there is no envelope (garbage, a crash), and ``lost``
    is true when nothing came back at all. ``status`` is the exit status, kept
    for the message only: the store decides on the envelope. ``stderr`` is the
    CLI's stderr on one line, for a message that names it.
    """

    status: int
    output: str
    ok: bool | None
    data: Any = None
    code: str | None = None
    message: str = ""
    lost: bool = False
    stderr: str = ""


def _ledger_answer(status: int, stdout: str, stderr: str) -> _Answer:
    return dataclasses.replace(_envelope_answer(status, stdout, _one_line(f"{stdout} {stderr}")), stderr=_one_line(stderr))


def _envelope_answer(status: int, stdout: str, output: str) -> _Answer:
    if not stdout.strip():
        return _Answer(status, output, None, lost=True)
    try:
        envelope = json.loads(stdout)
    except ValueError:
        return _Answer(status, output, None)
    if type(envelope) is not dict or type(envelope.get("ok")) is not bool:
        return _Answer(status, output, None)
    if envelope["ok"]:
        return _Answer(status, output, True, data=envelope.get("data"))
    error = envelope.get("error")
    if type(error) is not dict or not _is_text(error.get("code")):
        return _Answer(status, output, None)
    return _Answer(status, output, False, code=error["code"], message=_one_line(error.get("message", "")))


def _ledger_incompatible(cli: str, detail: str) -> StoreUnavailable:
    return StoreUnavailable(
        f"the ledger CLI {cli} lacks append-many or --expect-revision, or does not accept the command the store "
        f"sent ({_one_line(detail)}); select a ledger CLI that has both. Nothing was written.",
        code="ledger_incompatible",
    )


def _fold_record(card: dict[str, Any], text: str, key: str, where: str) -> dict[str, Any] | None:
    """Fold one ``item.note`` into the card when it is a ticket record (design 6.2.3); return it when it is a pass record.

    A text that is not framed is an ordinary note. A framed record of a kind or
    version this skill does not know, a body that is not a JSON object, or a
    body its section-I validator refuses is ``card_unreadable``: a record is
    never skipped. A plan's identity is read first, as on the markdown card. A
    ``ticket-pass:v2`` record is returned for the caller to fold; nothing else
    is.
    """

    frame = _RECORD_FRAME.fullmatch(text)
    if frame is None:
        return None
    kind, version, body = frame[1], int(frame[2]), frame[3]
    name = f"ticket-{kind}:v{version}"
    if kind not in (*_RECORD_KINDS, _PASS_RECORD) or version != _RECORD_VERSION:
        raise _unreadable(where, f"it holds a ticket record this skill does not know ({name})")
    try:
        record = json.loads(body)
    except ValueError as exc:
        raise _unreadable(where, f"a {name} record is not JSON") from exc
    if type(record) is not dict:
        raise _unreadable(where, f"a {name} record is not an object")
    try:
        if kind == "plan":
            _identity({"plan": record})
            _validate_plan(record, key)
        elif kind == "decision":
            _validate_decision(record)
        elif kind == _PASS_RECORD:
            _validate_pass_record(record)
        else:
            _validate_review(record)
    except TicketError as exc:
        raise _unreadable(where, f"a {name} record is not valid ({exc})") from exc
    if kind == _PASS_RECORD:
        return record
    if kind == "plan":
        card["plan"] = record
    else:
        card[f"{kind}s"].append(record)
    return None


def _trusted_close(events: Sequence[Mapping[str, Any]], states: list[int], stops: list[int], key: str) -> bool:
    """Whether the item's closed status is this store's own atomic close (design 6.2).

    It is when the latest ``item.state`` C has a close key of this ticket, the
    event just before C is the close-phase ``item.stopped`` S with the same
    pass and close token in its key and the same ``source``, and S is the
    latest stop. A later note, link or session record leaves it trusted; a
    later stop or state does not.
    """

    state_key = re.compile(rf"ticket-close/{re.escape(key)}/([0-9a-f]+)/([0-9a-f]+)/state")
    closing = [index for index in states if state_key.fullmatch(events[index].get("idempotency_key") or "")]
    if not closing:
        return False
    last = closing[-1]
    state, stop = events[last], events[last - 1]
    mark = state_key.fullmatch(state["idempotency_key"])
    return (
        last == states[-1]
        and stops[-1:] == [last - 1]
        and stop["kind"] == "item.stopped"
        and stop["payload"].get("phase") == "close"
        and stop.get("idempotency_key") == f"ticket-close/{key}/{mark[1]}/{mark[2]}/stop"
        and stop["payload"].get("source") == state["payload"].get("source")
    )


# --- A parent's hand-off on a ledger item (design 6.8) ----------------------

_LOOP_KINDS = ("loop.managed", "loop.intent", "loop.done")
# A hand-off's payload: exactly these keys, plus any of the optional ones (the ledger's ``loop.intent`` version 2).
_HANDOFF_REQUIRED = ("operation", "operation_id", "controller", "seams")
_HANDOFF_OPTIONAL = ("target", "head", "asked_event_id", "source")


def _handoff_refused(code: str, key: str, why: str) -> StoreUnavailable:
    return StoreUnavailable(
        f"ledger item {key} is not opened through a parent's hand-off: {why}. Nothing was written and the item is "
        "unchanged.",
        code=code,
    )


def _is_handoff_name(value: Any) -> bool:
    return type(value) is str and _HANDOFF_NAME.fullmatch(value) is not None


def _read_handoff(events: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    """The item's one open hand-off, read from its exported events, as the card's ``handoff`` field (design 6.8).

    The reader rule is the ledger's own: an intent is open unless a ``loop.done`` in the same events names the same
    ``operation`` and ``operation_id``, and the hand-off is the one open ``loop.intent`` of payload version 2. The
    field is the hand-off's controller, its three seams, its operation id and its event id. Any doubt is a refusal by
    name, never a guess: no ``loop.managed`` event is ``handoff_not_managed``; a loop event with no whole-number id
    and payload version, an intent or done that names no operation and operation id, an open intent of a version
    other than 1 or 2, or a hand-off whose payload breaks the grammar is ``handoff_unreadable``; two open hand-offs
    are ``handoff_ambiguous``, never the newer; no hand-off with an open version 1 build intent is
    ``handoff_intent_v1``, and with none ``handoff_no_intent``. Nothing else is read: not a note, a stamp, the pass
    record, the intent's ``target``, a link or the environment.
    """

    loop = [event for event in events if event.get("kind") in _LOOP_KINDS]
    if not any(event["kind"] == "loop.managed" for event in loop):
        raise _handoff_refused("handoff_not_managed", key, "it is not loop-managed (it has no loop.managed event)")
    for event in loop:
        what = f"its {event['kind']} event {event.get('id')!r}"
        if type(event.get("id")) is not int or type(event.get("payload_version")) is not int:
            raise _handoff_refused("handoff_unreadable", key, f"{what} has no whole-number id and payload version")
        if event["kind"] == "loop.managed":
            continue
        payload = event.get("payload")
        if type(payload) is not dict or not _is_text(payload.get("operation")) or not _is_text(payload.get("operation_id")):
            raise _handoff_refused("handoff_unreadable", key, f"{what} does not name its operation and operation id")
    closed = {
        (event["payload"]["operation"], event["payload"]["operation_id"]) for event in loop if event["kind"] == "loop.done"
    }
    open_intents = [
        event for event in loop
        if event["kind"] == "loop.intent"
        and (event["payload"]["operation"], event["payload"]["operation_id"]) not in closed
    ]
    for event in open_intents:
        if event["payload_version"] not in (1, 2):
            why = f"its open loop.intent event {event['id']} has payload version {event['payload_version']}, neither 1 nor 2"
            raise _handoff_refused("handoff_unreadable", key, why)
    handoffs = [event for event in open_intents if event["payload_version"] == 2]
    if len(handoffs) > 1:
        named = ", ".join(event["payload"]["operation_id"] for event in handoffs)
        raise _handoff_refused("handoff_ambiguous", key, f"it has {len(handoffs)} open hand-offs ({named}), and none is picked")
    if not handoffs:
        builds = [event["payload"]["operation_id"] for event in open_intents if event["payload"]["operation"] == "build"]
        if builds:
            why = f"its open build intent ({', '.join(builds)}) is version 1, which names no controller and no seams"
            raise _handoff_refused("handoff_intent_v1", key, why)
        raise _handoff_refused("handoff_no_intent", key, "it has no open build hand-off")
    event = handoffs[0]
    payload = event["payload"]

    def unreadable(why: str) -> StoreUnavailable:
        return _handoff_refused("handoff_unreadable", key, f"its hand-off (event {event['id']}) {why}")

    missing = [name for name in _HANDOFF_REQUIRED if name not in payload]
    if missing:
        raise unreadable(f"has no {', '.join(missing)}")
    extra = sorted(set(payload) - set(_HANDOFF_REQUIRED) - set(_HANDOFF_OPTIONAL))
    if extra:
        raise unreadable(f"has keys a hand-off does not have: {', '.join(extra)}")
    if payload["operation"] != "build":
        raise unreadable(f"is for the operation {payload['operation']!r}, not build")
    if not _is_handoff_name(payload["controller"]):
        raise unreadable(f"names the controller {payload['controller']!r}, which is not a name token")
    seams = payload["seams"]
    if type(seams) is not dict or set(seams) != set(SEAMS):
        raise unreadable("does not name exactly the seams build, review and outward")
    for seam in SEAMS:
        if not _is_handoff_name(seams[seam]):
            raise unreadable(f"names the {seam} implementation {seams[seam]!r}, which is not a name token")
    return {
        "controller": payload["controller"],
        "seams": {seam: seams[seam] for seam in SEAMS},
        "operation_id": payload["operation_id"],
        "event_id": event["id"],
    }


def _handoff_outcome(events: Sequence[Mapping[str, Any]], key: str) -> tuple[dict[str, Any] | None, StoreUnavailable | None]:
    """The item's hand-off and None, or None and the refusal ``_read_handoff`` gives; never raises."""

    try:
        return _read_handoff(events, key), None
    except StoreUnavailable as exc:
        return None, exc


def _hand_offs(events: Sequence[Mapping[str, Any]], since: int | None = None) -> list[int]:
    """The index of every ``loop.intent`` whose payload version is not 1 that counts as open (design 6.8); with
    ``since``, of every one open at the event at index ``since`` or written after it.

    An intent whose operation and operation id cannot be read, or whose version cannot be, counts as open: a done
    cannot be matched to it. With ``since`` only a done before that event closes an intent, so an intent open then,
    or written after it, counts even when its done came later: these are the hand-offs that overlapped the life of
    the pass whose record is that event, after the handback too.
    """

    closed = set()
    for event in events[:since]:
        payload = event.get("payload")
        if event.get("kind") == "loop.done" and type(payload) is dict:
            closed.add((payload.get("operation"), payload.get("operation_id")))
    found = []
    for index, event in enumerate(events):
        if event.get("kind") != "loop.intent":
            continue
        version = event.get("payload_version")
        if type(version) is int and version == 1:
            continue
        payload = event.get("payload")
        ident = (payload.get("operation"), payload.get("operation_id")) if type(payload) is dict else (None, None)
        if not (_is_text(ident[0]) and _is_text(ident[1])) or ident not in closed:
            found.append(index)
    return found


def _handed_off(events: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the item has an open ``loop.intent`` whose payload version is not 1 (design 6.8): such an item opens
    only through the hand-off source."""

    return bool(_hand_offs(events))


def _historical_holder(events: Sequence[Mapping[str, Any]], index: int) -> str | None:
    """The holder the hand-off at ``events[index]`` was handed under (design 6.8), or None when it cannot be
    established.

    It is the holder (the ``owner``, else the ``claim``, each when present and not empty) of the latest
    claim-bearing ``item.state`` strictly before the hand-off: the claim the ledger's loop writer checks before it
    writes one. A state that names nobody is passed over. No such state, or a holder that is not a string, is
    None. It is never read from today's row or from a later stamp.
    """

    for event in reversed(events[:index]):
        if event.get("kind") != "item.state":
            continue
        for name in ("owner", "claim"):
            value = event["payload"].get(name)
            if value is None or value == "":
                continue
            return value if type(value) is str else None
    return None


def _protects(events: Sequence[Mapping[str, Any]], since: int, holder: str) -> bool:
    """Whether a hand-off that overlapped the life of the pass whose record is ``events[since]`` protects that
    pass's holder ``holder`` (design 6.8).

    Each such hand-off (``_hand_offs`` with ``since``) protects the holder it was handed under
    (``_historical_holder``). When that is ``holder``, by exact equality whatever the name (a default
    ``ticket-pass/<id>`` included), or cannot be established, the pass never releases a lease under its holder. A
    hand-off handed under another holder protects nothing of this pass.
    """

    for index in _hand_offs(events, since):
        historical = _historical_holder(events, index)
        if historical is None or historical == holder:
            return True
    return False


def _ledger_card(
    events: Sequence[Mapping[str, Any]], key: str, title: str,
) -> tuple[dict[str, Any], Any, dict[str, Any] | None]:
    """The section-2 card folded from one item's exported events, in id order, the latest state's blocked-by, and
    how the live pass claimed the item.

    The pass is the latest claim of this ticket not yet released: a
    ``ticket-pass:v2`` pass record, or a claim of the old shape, the
    ``session.opened`` keyed ``ticket-pass/K/<pass>/claimed``. The
    ``session.closed`` keyed ``ticket-pass/K/<pass>/released`` releases
    either, and a closed status releases any pass: the export shows session
    records only while the item is open, but a pass record stays in it after
    the close. The third value is ``{"shape": "lease", "holder": <name>,
    "lease": "taken" | "joined"}`` for a pass record, ``{"shape": "claim"}``
    for the old shape, and None with no live pass. A lease of the record's
    holder that the live pass wrote after its record (the ``item.state``
    keyed ``ticket-lease/K/<pass>/<revision>``, as a resume writes when it
    takes the lease again) makes it ``taken``, so the pass releases it at its
    end. A pass whose holder a hand-off that overlapped its life was handed
    under (``_protects``: open when the pass began or written since, its done
    no matter) reads ``joined`` whatever its record says or it took since: that
    holder name is the parent's for the rest of the pass's life, after the
    handback too, and no end path of the pass releases a lease under it (design
    6.8). A hand-off under another holder changes nothing. An item that was closed
    once reads ``closed``; its close stamp is marked ``close`` only when the
    close is trusted, so an outside status write is refused ``close_invalid``
    and never resumed.
    """

    where = f"ledger item {key}"
    card = _blank_card(key, title)
    live: dict[str, Any] | None = None
    start = 0  # the index of the live pass's record: where the pass that holds a lease began
    pass_key = re.compile(rf"ticket-pass/{re.escape(key)}/([0-9a-f]+)/(claimed|released)")
    lease_key = re.compile(rf"ticket-lease/{re.escape(key)}/([0-9a-f]+)/[^/]+")
    states: list[int] = []
    stops: list[int] = []
    for index, event in enumerate(events):
        kind, payload = event["kind"], event["payload"]
        mark = pass_key.fullmatch(event.get("idempotency_key") or "")
        if kind == "session.opened" and mark and mark[2] == "claimed":
            purpose = _CLAIM_PURPOSE.fullmatch(payload.get("purpose", ""))
            card["pass"] = {"id": mark[1], "host": purpose and purpose[1], "started_at": purpose and purpose[2]}
            live = {"shape": "claim"}
        elif kind == "session.closed" and mark and mark[2] == "released":
            if card["pass"] is not None and card["pass"]["id"] == mark[1]:
                card["pass"], live = None, None
        elif kind == "item.note":
            record = _fold_record(card, payload["text"], key, where)
            if record is not None:
                card["pass"] = {name: record[name] for name in ("id", "host", "started_at")}
                live, start = {"shape": "lease", "holder": record["holder"], "lease": record["lease"]}, index
        elif kind == "item.linked" and payload.get("rel") == "pr":
            # The first recorded PR stays the pass's reference; later links never rewrite closed history.
            if card.get("pr") is None:
                card["pr"] = payload.get("target")
        elif kind == "item.stopped":
            stops.append(index)
            card["stamps"].append(
                {"point": payload["point"], "phase": payload["phase"], "next_skills": payload.get("skills", []),
                 "refs": payload.get("refs", []), "close": False}
            )
        elif kind == "item.tried":
            card["tried"].append({name: payload[name] for name in ("what", "phase", "outcome", "evidence") if name in payload})
        elif kind == "item.state":
            states.append(index)
            if payload["status"] == "closed":
                card["status"] = "closed"
                card["pass"], live = None, None
            elif payload["status"] == "claimed" and live is not None and live["shape"] == "lease":
                wrote = lease_key.fullmatch(event.get("idempotency_key") or "")
                if wrote and wrote[1] == card["pass"]["id"] and payload.get("owner") == live["holder"]:
                    live = {**live, "lease": "taken"}
    if live is not None and live["shape"] == "lease" and _protects(events, start, live["holder"]):
        # The one rule every end path reads (design 6.8): a hand-off that overlapped the pass's life under the pass's
        # own holder name keeps that name the parent's for the rest of the pass's life, so no end path releases it.
        live = {**live, "lease": "joined"}
    if card["status"] == "closed" and _trusted_close(events, states, stops, key):
        card["stamps"][-1]["close"] = True
    _check_card_shape(card, key, where)
    # A parent's hand-off, derived from the item's loop events on every read and never from a record (design 6.8).
    # A problem hand-off gives no field and no refusal here: only an open through the hand-off source refuses it.
    handoff, _ = _handoff_outcome(events, key)
    if handoff is not None:
        card["handoff"] = handoff
    if any(event["kind"] == "loop.managed" for event in events):
        card["loop_managed"] = True
    return card, events[states[-1]]["payload"].get("blocked_by") if states else None, live


# --- Recording a pulled ticket as the item's spec (design 6.6) -------------


def intake_source(ticket: Ticket) -> str:
    """The ``--source`` an open records with a pulled ticket's spec: ``ticket:<key>``, then a space and the url when the ticket has one.

    It is built from the key and the url alone, which the source refuses with
    whitespace in them, so the same ticket gives the same bytes on every open
    and an unchanged ticket never writes a version for its source. A ticket
    drafted from an ad-hoc ask has the ask's source alone, ``ask:<12 hex>``.
    """

    if ticket.ask_source is not None:
        return ticket.ask_source
    return f"ticket:{ticket.key}" + (f" {ticket.url}" if ticket.url is not None else "")


def _acceptance_text(criteria: Sequence[str]) -> str:
    """The criteria as a spec's acceptance text: one ``- `` bullet per criterion, in order (the grammar of 6.5)."""

    return "\n".join(f"- {text}" for text in criteria)


def _said(answer: _Answer) -> str:
    """What a CLI answer that is not ``ok`` said: its code and message, or its exit status and output; then its stderr."""

    if answer.code:
        said = f"answered {answer.code}: {answer.message}"
        return said + (f" (stderr: {answer.stderr})" if answer.stderr else "")
    return f"gave no envelope, exit status {answer.status}: {answer.output}"


def _spec_set_failed(key: str, command: str, why: str) -> StoreUnavailable:
    return StoreUnavailable(
        f"the ledger did not record ticket {key} as the spec of its item: `{command}` {why}. The open stops before "
        "the card is read, so the pass does not plan from a ticket the ledger did not record, and nothing else was "
        "written. A person reads the ledger's reason, then opens the ticket again.",
        code="spec_set_failed",
    )


def _spec_diff_failed(key: str, version: int, command: str, why: str) -> StoreUnavailable:
    return StoreUnavailable(
        f"spec v{version} of ledger item {key} records the ticket as pulled now, but `{command}` {why}. The open "
        "stops so the change is not missed, before the card is read. Run that command to read the change, then open "
        "the ticket again: the next open of the same ticket records nothing and prints no diff.",
        code="spec_diff_failed",
    )


class LedgerStore:
    """The card as the events of one item in a ledger, read and written through the ledger's CLI.

    The token is the item's ``revision`` from ``query``: the id of its newest
    event, ``none`` before the first. Every write is one ``append-many`` under
    ``--expect-revision`` of the held token, so it lands only on the card the
    pass last read or wrote, all of its events or none (design 6.2). The
    ticket's own records travel as framed notes; the ledger's kinds carry the
    lease, the pass's end, stamps, tried records and the closed status.

    A new pass takes or joins the item's lease in its one claim batch: the
    lease is an ``item.state`` (status ``claimed``, ``owner`` and ``claim``
    both the holder, ``expires_at``, and ``prior_holder`` when it replaces an
    expired lease), decided on the item's ``query`` row read at the revision
    the batch is sent under, and the pass record (``ticket-pass:v2``) says
    which holder and whether the lease was taken or joined. A claim of the old
    shape (``session.opened``) still holds the item against every new pass, and
    another holder's lease blocks its resume.
    """

    # What ``choose_store`` chose and why, printed by ``open``; None on a handle built directly.
    selection: str | None = None

    def __init__(self, cli: str | Path, key: str, title: str, repo: str | Path | None = None):
        if not _KEY.fullmatch(key):
            raise TicketError(f"unsafe ticket key: {key!r}")
        self.cli = str(cli)
        self.key = key
        self.title = title
        # The repository the pass works in, when known: only the leftover-card notice reads it.
        self.repo = None if repo is None else Path(repo).resolve()
        # A stamp's reference to the card: the ledger item itself.
        self.ref = f"ledger/{key}"
        self._pass_id: str | None = None
        self._ended: str | None = None
        # The card of this handle's last read or landed write; the claim check runs on it.
        self._card = _blank_card(key, title)
        # The item's owner, claim and blocked-by as last read, carried into the closed status.
        self._item: dict[str, Any] = {}
        # The item's query row, how its live pass claimed it (``_ledger_card``) and the revision, as last read.
        self._row: Mapping[str, Any] | None = None
        self._live: dict[str, Any] | None = None
        self._read_token: str | None = None
        # No holder and no lease is kept here. The holder an open names travels with that open's claim or join
        # (``apply``), and the end reads the pass's holder and lease from a fresh read (``_release_lease``).
        # The item's hand-off as last read (design 6.8): the open hand-off, or the refusal an open through the
        # hand-off source gives; and whether the item has an open loop intent of a version other than 1.
        self._handoff: dict[str, Any] | None = None
        self._handoff_problem: StoreUnavailable | None = None
        self._handed_off = False
        # Refusal diagnostics only, never a controller or authority for a go.
        self._role_go_problem: GateRequired | None = None
        # Threads sharing this handle take turns; a binding event binds under it.
        self._threads = threading.RLock()

    @property
    def pass_id(self) -> str | None:
        """The pass this handle writes for: bound by its own ``apply``, cleared by ``release``."""

        return self._pass_id

    @property
    def ended(self) -> str | None:
        """The id of the last pass this handle held that has ended, or None."""

        return self._ended

    def leftover_notice(self) -> str | None:
        """The one line an open prints when the repository still holds unimported markdown cards (design 6.4), else None."""

        if self.repo is None or not _leftover_cards(self.repo):
            return None
        return (
            f"markdown ticket cards remain in {self.repo}/.ticket and are not in the ledger; "
            f"import them with `{import_command(self.repo)}`"
        )

    def _run(self, *args: str, stdin: str = "") -> _Answer:
        _require_runnable(self.cli)
        result = subprocess.run([self.cli, "--json", *args], input=stdin, capture_output=True, encoding="utf-8", check=False)
        return _ledger_answer(result.returncode, result.stdout, result.stderr)

    def find_utterance(self, session_id: str, verbatim: str, since: str, *, after: int) -> Mapping[str, Any] | None:
        """Newest person prompt in ``session_id`` written after event ``after`` that contains ``verbatim``.

        Reads ``ledger events --kind utterance.captured --since --limit 200``.
        ``since`` is a coarse prefilter only: it is floored to whole seconds,
        so a prompt captured just before the pass opened can share its second.
        Event order decides: a match has an integer event id greater than
        ``after`` (the pass's claim event), payload.session_id equal to
        ``session_id``, origin "person", no omitted_bytes, and payload.text
        containing ``verbatim`` as a byte-exact substring. Newest first; the
        first match wins. Any other answer (empty, usage, unreadable) is no
        match.
        """

        answer = self._run(
            "events", "--kind", "utterance.captured", "--since", since, "--limit", "200",
        )
        if not answer.ok or type(answer.data) is not dict or type(answer.data.get("events")) is not list:
            return None
        needle = verbatim.encode("utf-8")
        for event in answer.data["events"]:
            if type(event) is not dict:
                continue
            event_id = event.get("id")
            if type(event_id) is not int or event_id <= after:
                continue
            payload = event.get("payload")
            if type(payload) is not dict:
                continue
            if payload.get("session_id") != session_id:
                continue
            if payload.get("origin") != "person":
                continue
            if "omitted_bytes" in payload:
                continue
            text = payload.get("text")
            if type(text) is not str:
                continue
            try:
                haystack = text.encode("utf-8")
            except UnicodeEncodeError:
                continue
            if needle not in haystack:
                continue
            return event
        return None

    def _go_election(self, gate: str, revision: int, declined: bool = False) -> tuple[str, str, str, str]:
        """Question, chosen description, rejected label and scope for a bound go, or for a declined outward go."""

        if declined:
            return (
                f"Approve outward actions for {self.key} (revision {revision})?",
                "Keep the work local: nothing is pushed or opened.",
                "go",
                f"the outward gate of {self.key} revision {revision}",
            )
        if gate == "plan":
            return (
                f"Approve the plan for {self.key} (revision {revision})?",
                "Approve the plan and begin the build.",
                "hold",
                f"the plan gate of {self.key} revision {revision}",
            )
        return (
            f"Approve outward actions for {self.key} (revision {revision})?",
            "Approve the outward actions of this pass.",
            "no go",
            f"the outward gate of {self.key} revision {revision}",
        )

    def bind_go(
        self,
        *,
        gate: str,
        revision: int,
        verbatim: str,
        evidence: str,
        phase: str,
        effect: str,
        started_at: str,
        token: str,
        declined: bool = False,
    ) -> dict[str, Any]:
        """Elect the go against this session's captured prompt, then record it.

        With ``declined`` the election chooses ``no go`` over ``go`` and its
        keys name ``<gate>-declined``; everything else is the same.

        Runs after the ticket-decision note has landed, outside apply's batch;
        ``token`` is the pass's token after that note (the pre-bind token).
        Fail closed: any miss or refusal writes nothing more and returns
        ``bound=False`` with a reason. Never raises; never claims a ledger gate
        opened. No fallback when LEDGER_SESSION is unset. The utterance must be
        newer than this pass's claim event, read from the ledger; an unreadable
        claim is ``bound=False``. ``bound=True`` only when both the election id
        and the decision id are integers read from the CLI's success payloads.
        An elect that did not answer with an event id is looked up by its
        idempotency key, and a landed election counts as this call's own event
        (the call then stops before ``append``). ``pass_token`` is returned
        only when the item's events after ``token`` are exactly the events this
        call wrote, in order; any other event in that range, or any doubt in
        reading it, omits it, so the next step reads ``stale``.
        """

        session_id = os.environ.get("LEDGER_SESSION")
        if not session_id:
            return {"bound": False, "reason": "LEDGER_SESSION unset"}
        claim_id = self._claim_event_id()
        if claim_id is None:
            return {"bound": False, "reason": "pass claim not readable"}
        utterance = self.find_utterance(session_id, verbatim, started_at, after=claim_id)
        if utterance is None:
            return {"bound": False, "reason": "no matching utterance"}
        utterance_id = utterance.get("id")
        if type(utterance_id) is not int:
            return {"bound": False, "reason": "no matching utterance"}
        question, chosen_description, rejected, scope = self._go_election(gate, revision, declined)
        answer = f"{gate}-declined" if declined else gate
        elect_key = f"ticket-bind/{self.key}/{answer}/{revision}/{utterance_id}/elect"
        decision_key = f"ticket-bind/{self.key}/{answer}/{revision}/{utterance_id}/decision"
        elect = self._run(
            "elect", self.key,
            "--question", question,
            "--chosen", "no go" if declined else "go",
            "--chosen-description", chosen_description,
            "--rejected", rejected,
            "--scope", scope,
            "--quote-ref", str(utterance_id),
            "--quote", verbatim,
            "--idempotency-key", elect_key,
        )
        election_id = self._cli_event_id(elect) if elect.ok else None
        if election_id is None:
            if elect.ok:
                why = "no event id"
            else:
                why = elect.code or elect.output or "no envelope"
                if elect.message:
                    why = f"{why}: {elect.message}" if elect.code else elect.message
            # The answer named no event: the election may still have landed
            # (a lost or unreadable response). Look it up by its key.
            landed = self._landed_election(token, elect_key)
            result: dict[str, Any] = {"bound": False, "reason": f"elect refused: {why}"}
            if landed is not None:
                result["election_id"] = landed
            return self._binder_result(result, token, () if landed is None else (landed,))
        payload = {
            "who": "the person",
            "who_kind": "person",
            "standing": "settled",
            "effect": effect,
            "tags": [{"type": "ticket", "id": self.key}],
            "verbatim": verbatim,
            "phase": phase,
            "evidence": evidence,
            "source_event_id": election_id,
            "source": _LEDGER_WRITER,
        }
        appended = self._run(
            "append",
            "--kind", "decision.recorded",
            "--item", self.key,
            "--payload", json.dumps(payload),
            "--idempotency-key", decision_key,
        )
        if not appended.ok:
            why = appended.code or appended.output or "no envelope"
            if appended.message:
                why = f"{why}: {appended.message}" if appended.code else appended.message
            return self._binder_result(
                {"bound": False, "reason": f"append refused: {why}", "election_id": election_id},
                token, (election_id,),
            )
        decision_id = self._cli_event_id(appended)
        if decision_id is None:
            return self._binder_result(
                {"bound": False, "reason": "append refused: no event id", "election_id": election_id},
                token, (election_id,),
            )
        return self._binder_result(
            {
                "bound": True,
                "utterance_id": utterance_id,
                "election_id": election_id,
                "decision_id": decision_id,
            },
            token,
            (election_id, decision_id),
        )

    def _cli_event_id(self, answer: _Answer) -> int | None:
        """The event id a successful CLI payload named, or None when it did not."""

        event = answer.data.get("event") if type(answer.data) is dict else None
        eid = event.get("id") if type(event) is dict else None
        return eid if type(eid) is int else None

    def _claim_event_id(self) -> int | None:
        """The event id of this handle's pass claim, read from the item's export, or None.

        The claim is the one pass record, the ``item.note`` keyed
        ``ticket-pass/K/<pass>/record``, or for a pass of the old shape the one
        ``session.opened`` keyed ``ticket-pass/K/<pass>/claimed``. No bound
        pass, an unreadable export, no such event, more than one, or an id that
        is not an integer is None. Never raises.
        """

        pass_id = self._pass_id
        if not _is_text(pass_id):
            return None
        try:
            export = self._run("export-item", self.key)
        except Exception:
            return None
        if not export.ok or type(export.data) is not dict or type(export.data.get("events")) is not list:
            return None
        wanted = {
            ("item.note", f"ticket-pass/{self.key}/{pass_id}/record"),
            ("session.opened", f"ticket-pass/{self.key}/{pass_id}/claimed"),
        }
        found = [
            event.get("id") for event in export.data["events"]
            if type(event) is dict and (event.get("kind"), event.get("idempotency_key")) in wanted
        ]
        if len(found) != 1 or type(found[0]) is not int:
            return None
        return found[0]

    def _events_after(self, token: str) -> list[Mapping[str, Any]] | None:
        """The item's events after revision ``token``, oldest first, or None on any doubt.

        ``query`` gives the revision and ``export-item`` the events; the
        export's newest event must be that revision, so a write that lands
        between the two reads is doubt. A closed item is doubt, because its
        export hides session records. Never raises.
        """

        if type(token) is not str or not token.isdigit():
            return None
        try:
            row = self._ticket_row()
            if row is None or row.get("status") == "closed":
                return None
            export = self._run("export-item", self.key)
        except Exception:
            return None
        if not export.ok or type(export.data) is not dict:
            return None
        events = export.data.get("events")
        if type(events) is not list or not events or any(type(event) is not dict for event in events):
            return None
        ids = [event.get("id") for event in events]
        if any(type(eid) is not int for eid in ids) or ids != sorted(set(ids)):
            return None
        floor = int(token)
        if str(row.get("revision")) != str(ids[-1]) or floor > ids[-1]:
            return None
        return [event for event in events if event["id"] > floor]

    def _landed_election(self, token: str, key: str) -> int | None:
        """The id of the one ``decision.elected`` keyed ``key`` written after ``token``, or None."""

        after = self._events_after(token)
        if after is None:
            return None
        found = [
            event["id"] for event in after
            if event.get("kind") == "decision.elected" and event.get("idempotency_key") == key
        ]
        return found[0] if len(found) == 1 else None

    def _binder_result(self, result: dict[str, Any], token: str, ids: tuple[int, ...]) -> dict[str, Any]:
        """``result`` with ``pass_token`` when the item's events after ``token`` are exactly ``ids``, in order.

        Those are the events this binder call wrote: the election, then the
        decision when it landed. Any other event in that range (an outsider's
        note, a release of this pass) or any doubt in reading the range leaves
        the token where it was, so the pass's next step reads ``stale``.
        """

        if not ids:
            return result
        after = self._events_after(token)
        if after is not None and [event["id"] for event in after] == list(ids):
            result = {**result, "pass_token": str(ids[-1])}
        return result

    def _refused(self, what: str, answer: _Answer) -> StoreUnavailable:
        said = f"{answer.code}: {answer.message}" if answer.code else f"no envelope, exit status {answer.status}: {answer.output}"
        return StoreUnavailable(
            f"the ledger refused to {what} ticket {self.key} ({said}). Nothing more was written and the pass ends; "
            "a person reads the ledger's reason.",
            code="ledger_refused",
        )

    def _data(self, answer: _Answer, what: str) -> Any:
        if answer.ok:
            return answer.data
        if answer.code == "usage":
            raise _ledger_incompatible(self.cli, f"{answer.code}: {answer.message}")
        raise self._refused(what, answer)

    # The four calls.

    def read(self) -> tuple[dict[str, Any], str]:
        """The card and its token: ``query`` gives the token, ``export-item`` the events.

        The token is never taken from the export, which hides the session
        records of a closed item and would leave a held revision stale.
        """

        with self._threads:
            events = []
            row = self._ticket_row()
            if row is None:
                card, token, item, live = _blank_card(self.key, self.title), NO_TOKEN, {}, None
                handoff, problem = _handoff_outcome([], self.key)
                handed_off = False
            else:
                events = self._data(self._run("export-item", self.key), "read")["events"]
                card, blocked, live = _ledger_card(events, self.key, self.title)
                token = str(row["revision"])
                item = {"owner": row.get("owner"), "claim": row.get("claim"), "blocked_by": blocked}
                handoff, problem = _handoff_outcome(events, self.key)
                handed_off = _handed_off(events)
            self._card, self._item, self._row, self._live, self._read_token = card, item, row, live, token
            self._handoff, self._handoff_problem, self._handed_off = handoff, problem, handed_off
            self._role_go_problem = None
            if handoff is None:
                if problem is not None and problem.code in ("handoff_unreadable", "handoff_ambiguous"):
                    self._role_go_problem = _role_go_refused(
                        "role_go_controller_unreadable", self.key, f"{problem.code}: {problem}",
                    )
                elif problem is not None and problem.code in ("handoff_no_intent", "handoff_intent_v1"):
                    # The reader proved there is no open v2 intent, so these ids are closed.
                    closed = [event["payload"]["operation_id"] for event in events
                              if event.get("kind") == "loop.intent" and event.get("payload_version") == 2]
                    if closed:
                        self._role_go_problem = _role_go_refused(
                            "role_go_handoff_closed", self.key, f"the hand-off is closed ({', '.join(closed)})",
                        )
            return copy.deepcopy(card), token

    def note_text(self, idempotency_key: str) -> str | None:
        """The text of the item note stored under ``idempotency_key`` on a fresh export, or None when there is none.

        A look only: it keeps no card, token or row on this handle, so it moves the handle past nothing. An item the
        ledger does not hold has no note. Any other failure to read is refused as ``read`` refuses it.
        """

        with self._threads:
            answer = self._run("export-item", self.key)
            if answer.ok is False and answer.code in _LEDGER_NO_ITEM:
                return None
            for event in self._data(answer, "read")["events"]:
                if event.get("idempotency_key") == idempotency_key and event.get("kind") == "item.note":
                    payload = event.get("payload")
                    text = payload.get("text") if type(payload) is dict else None
                    return text if type(text) is str else ""
        return None

    def directly_follows(self, held: str, token: str) -> bool:
        """Whether, on a fresh export, the item's event ``token`` comes straight after its event ``held``.

        The ledger's answer to ``append-many`` does not say whether an event is new or a stored one returned for
        its key, and a stored one skips the revision check. A write made under ``--expect-revision`` ``held`` that
        really appended is the next event after ``held``; one that is not was a replay of an event that came later,
        with events between that the handle has not read. ``held`` of ``none`` means ``token`` is the item's first.
        Like ``note_text`` it keeps nothing on this handle.
        """

        with self._threads:
            answer = self._run("export-item", self.key)
            if answer.ok is False and answer.code in _LEDGER_NO_ITEM:
                return False
            ids = [str(event.get("id")) for event in self._data(answer, "read")["events"]]
        if token not in ids:
            return False
        index = ids.index(token)
        return held == NO_TOKEN if index == 0 else ids[index - 1] == held

    def hand_off(self) -> dict[str, Any]:
        """The item's open hand-off as this handle's last read found it; else the refusal it gives (design 6.8).

        Only an open through the hand-off source calls it. ``read`` itself never refuses over a hand-off.
        """

        if self._handoff is None:
            raise self._handoff_problem or _handoff_refused("handoff_not_managed", self.key, "the item was not read")
        return copy.deepcopy(self._handoff)

    @property
    def handed_off(self) -> str | None:
        """When the last read found an open loop intent of a version other than 1, the item's lease holder as read
        (``no holder`` when the row names none); else None. Every source but the hand-off source is refused there."""

        if not self._handed_off:
            return None
        row = self._row or {}
        holder = row.get("owner") or row.get("claim")
        return holder if _is_text(holder) else "no holder"

    def _leased_live(self, holder: str) -> bool:
        """Whether the item's row, as last read, is leased live to ``holder``: recorded and read ``claimed``, by it."""

        row = self._row or {}
        on_row = row.get("owner") or row.get("claim")
        return row.get("recorded_status") == "claimed" and row.get("status") == "claimed" and on_row == holder

    def _ticket_row(self, ask: str | None = None) -> Mapping[str, Any] | None:
        """The item's ``query --item`` row, or None when the ledger holds no such item.

        An item of another type is refused ``ledger_refused`` before anything
        is written to it; for the item of an ad-hoc ask (``ask`` is its
        source), ``ask_item_not_ticket``.
        """

        answer = self._run("query", "--item", self.key)
        if answer.ok is False and answer.code in _LEDGER_NO_ITEM:
            return None
        row = next(row for row in self._data(answer, "read")["items"] if row["item_id"] == self.key)
        if row.get("type") != "ticket":
            found = repr(row["type"]) if "type" in row else "none"
            if ask is not None:
                raise StoreUnavailable(
                    f"ledger item {self.key} is where ad-hoc ask {ask} lands, but it is not a ticket (its type is "
                    f"{found}), so the pass will not claim or write to it. Nothing was written; a person resolves "
                    "the key.",
                    code="ask_item_not_ticket",
                )
            raise StoreUnavailable(
                f"ledger item {self.key} is not a ticket (its type is {found}), so the pass will not claim or "
                "write to it. Nothing was written; a person resolves the key.",
                code="ledger_refused",
            )
        return row

    def _url_link_targets(self) -> list[str]:
        """The targets of the item's outgoing ``url`` links, read now (``links``); none when no link names the item."""

        answer = self._run("links", self.key)
        if answer.ok is False and answer.code in _LEDGER_NO_ITEM:
            return []
        data = self._data(answer, "read the links of")
        outgoing = data.get("outgoing") if type(data) is dict else None
        if type(outgoing) is not list:
            raise _item_unreadable(self.key, "links", "it does not list outgoing links")
        targets = []
        for row in outgoing:
            if type(row) is not dict or not _is_text(row.get("rel")) or not _is_text(row.get("target")):
                raise _item_unreadable(self.key, "links", "an outgoing link does not name its relation and target")
            if row["rel"] == "url":
                targets.append(row["target"])
        return targets

    def _url_link_missing(self, url: str) -> bool:
        """Whether the item still needs its ``url`` link to ``url``: True when it has no ``url`` link, False when it has one to ``url``.

        A ``url`` link to another address, and none to ``url``, is ``url_link_conflict`` and writes nothing: the
        tracker's address is never added beside another, and never replaces one.
        """

        targets = self._url_link_targets()
        if url in targets:
            return False
        if targets:
            raise StoreUnavailable(
                f"ledger item {self.key} already has a url link to {targets[0]}, not to the tracker address {url}, "
                "so the pass does not add a second one. Nothing was written. A person decides which address the "
                "item keeps, then opens the ticket again.",
                code="url_link_conflict",
            )
        return True

    def _url_link(self, url: str) -> dict[str, Any]:
        """The ``url`` link of the item to ``url``, keyed by the address so a rerun replays it and writes nothing."""

        digest = hashlib.sha256(url.encode("utf-8", "surrogatepass")).hexdigest()[:16]
        return self._element("item.linked", {"rel": "url", "target": url}, f"ticket-link/{self.key}/url/{digest}")

    def record_spec(self, ticket: Ticket) -> tuple[SpecVersion, tuple[str, ...]]:
        """Record the pulled ``ticket`` as the item's spec; return the spec it left current and the lines the open prints (design 6.6).

        One ``spec set`` sends the ticket's title, description and criteria
        with ``intake_source(ticket)``; the ledger appends the next version
        only when that content or the source changed, and answers
        ``unchanged`` otherwise. An unchanged ticket and a first version print
        nothing; a later version N prints ``spec diff <item> N-1 N``. The
        ticket's status, update time and links are not part of the spec, and
        nothing here writes a status or a claim. An item that does not exist
        yet is created first with the claim's own ``item.created`` event and
        key, under ``--expect-revision none``; an item of another type is
        refused before anything is written. A ticket with a ``url`` also gets
        one ``url`` link to it on the item, in that same write (under the
        revision of the row read, so a writer racing it is ``stale``), unless
        the item already links that address; a ``url`` link to another address
        is ``url_link_conflict``, refused before anything is written. A
        ticket drafted from an ad-hoc ask records the ask's source
        (``intake_source``) and refuses an item that is not a ticket as
        ``ask_item_not_ticket``. A ``spec set`` that fails or
        answers what cannot be read is ``spec_set_failed``, a ``spec diff``
        that does is ``spec_diff_failed``; neither is retried. The spec it
        returns is the version and content hash ``spec set`` reported, a new
        version or the one it found unchanged; ``open`` confirms after the card
        read that it is still the item's current spec (``confirm_spec``).
        """

        with self._threads:
            row = self._ticket_row(ticket.ask_source)
            batch = [self._created()] if row is None else []
            if ticket.url is not None and (row is None or self._url_link_missing(ticket.url)):
                batch.append(self._url_link(ticket.url))
            if batch:
                self._send(batch, NO_TOKEN if row is None else str(row["revision"]))
            recorded, written = self._spec_set(ticket)
            if not written or recorded.version == 1:
                return recorded, ()
            return recorded, (self._spec_diff(recorded.version),)

    def confirm_spec(self, recorded: SpecVersion) -> None:
        """Refuse ``stale`` unless the item's current spec is still ``recorded``, the one ``record_spec`` left current.

        ``open`` calls it after the card read, as it calls
        ``LedgerItemSource.confirm`` for a ledger item (design 6.6): a spec
        another open recorded after this record and before the read is seen
        here, and one recorded after the read moves the item's revision past
        the held token, so the claim and every later write are refused
        ``stale``. A ``spec show`` that fails or cannot be read is refused by
        name; the recorded spec never stands in for it.
        """

        with self._threads:
            current = _spec_identity(self.key, self._data(self._run("spec", "show", self.key), "read the spec of"))
        _confirm_spec(self.key, recorded, current)

    def _spec_set(self, ticket: Ticket) -> tuple[SpecVersion, bool]:
        """One ``spec set`` of the ticket: the spec it left current, and whether it wrote it (False when ``unchanged``)."""

        source = intake_source(ticket)
        args = (
            "spec", "set", self.key, f"--title={ticket.title}", f"--body={ticket.description}",
            f"--acceptance={_acceptance_text(ticket.acceptance_criteria)}", f"--source={source}",
        )
        command = (
            shlex.join([self.cli, "spec", "set", self.key, f"--source={source}"])
            + " --title=<title> --body=<description> --acceptance=<criteria>"
        )
        try:
            answer = self._run(*args)
        except (OSError, ValueError) as exc:
            raise _spec_set_failed(self.key, command, f"could not run ({_one_line(exc)})") from exc
        if not answer.ok:
            raise _spec_set_failed(self.key, command, _said(answer))
        data = answer.data
        if type(data) is not dict or data.get("item_id") != self.key or type(data.get("unchanged")) is not bool:
            raise _spec_set_failed(self.key, command, "answered what cannot be read: it does not give this item's id and an unchanged flag")
        version = data.get("version")
        if type(version) is not int or version < 1:
            why = f"answered what cannot be read: version {version!r} is not a whole number of at least 1"
            raise _spec_set_failed(self.key, command, why)
        event = data.get("event")
        payload = event.get("payload") if type(event) is dict else None
        content_hash = payload.get("content_hash") if type(payload) is dict else None
        if type(payload) is not dict or type(payload.get("version")) is not int or payload["version"] != version:
            why = f"answered what cannot be read: its event is not spec v{version}"
            raise _spec_set_failed(self.key, command, why)
        if type(content_hash) is not str or not _SPEC_HASH.fullmatch(content_hash):
            why = f"answered what cannot be read: its event gives no SHA-256 content hash for spec v{version}"
            raise _spec_set_failed(self.key, command, why)
        return SpecVersion(version, content_hash), not data["unchanged"]

    def _spec_diff(self, version: int) -> str:
        """The ledger's ``spec diff`` from version ``version - 1`` to ``version``, as the lines the open prints."""

        older = version - 1
        args = ("spec", "diff", self.key, str(older), str(version))
        command = shlex.join([self.cli, *args])
        answer = self._run(*args)
        if not answer.ok:
            raise _spec_diff_failed(self.key, version, command, _said(answer))
        data = answer.data
        fields = data.get("fields") if type(data) is dict else None
        readable = (
            type(fields) is list
            and data.get("item_id") == self.key
            and type(data.get("from")) is int and data["from"] == older
            and type(data.get("to")) is int and data["to"] == version
            and all(
                type(entry) is dict and _is_text(entry.get("field")) and type(entry.get("diff")) is list
                and all(type(line) is str for line in entry["diff"])
                for entry in fields
            )
        )
        if not readable:
            raise _spec_diff_failed(self.key, version, command, "answered what cannot be read as the diff of those two versions")
        changed = ", ".join(entry["field"] for entry in fields) or "no content field; the source moved"
        lines = [
            f"ledger spec diff {self.key} {older} {version}: the ticket changed since spec v{older}, "
            f"and spec v{version} records it (changed: {changed})"
        ]
        for entry in fields:
            lines.extend([f"## {entry['field']}", *entry["diff"]])
        return "\n".join(lines)

    def apply(
        self, token: str, events: Sequence[tuple[str, Any]], *, holder: str | None = None, handoff: bool = False,
    ) -> str:
        """Land every event or none, only while the item's revision is still ``token``.

        A used handle is refused ``handle_used`` for a binding event before any
        CLI call; the claim is checked on the card this handle last read or
        wrote, and ``--expect-revision`` makes that check hold at write time. A
        claim or a join decides the lease on the row this handle read at
        ``token`` (``_claim``, ``_join``) and binds the handle only after any
        batch it sends landed; a join that keeps a live lease of its own
        holder, or continues a pass of the old shape, sends nothing and returns
        ``token``.

        ``holder`` is the lease holder the open named for its claim or join
        (None for the default, ``ticket-pass/<pass id>``). It is this call's
        own argument, never state kept on the handle, so two opens racing on
        one handle each claim as the holder they named. A holder that is not a
        name is refused ``holder_invalid`` before any CLI call.

        ``handoff`` is true for the claim or join of an open through the
        hand-off source (design 6.8), and travels the same way. Such a claim or
        join never writes a lease: where the item is not leased live to the
        holder, so the pass would have to take the lease, it is refused
        ``handoff_not_leased`` and nothing is written.
        """

        if holder is not None:
            _check_holder(holder)
        for kind, value in events:
            _validate_event(kind, value, self.key)
        binding = _binding(events)
        with self._threads:
            if binding is not None and (self._pass_id is not None or self._ended is not None):
                raise _handle_used(self.key, self._pass_id or self._ended)
            joining = binding is not None and binding[0] == "join"
            _check_pass(self.key, self._card["pass"], binding[1] if joining else self._pass_id, self._ended)
            if binding is None:
                token = self._send(self._batch(token, events), token)
                self._card = _fold(self._card, events)
                return token
            if token != (self._read_token or NO_TOKEN):
                raise _stale(self.key, f"the lease is decided on the item as read at revision {self._read_token or NO_TOKEN}, not {token}")
            if joining:
                token = self._join(token, binding[1], holder, handoff)
            else:
                token = self._claim(token, events[0][1], holder, handoff)
            self._pass_id, self._ended = binding[1], None
            return token

    def _lease_decision(self, holder: str) -> tuple[bool, str | None]:
        """Whether the pass takes the lease as ``holder`` (else it joins it), and the expired holder it replaces.

        Read from the item's row as last read: no item, or an open one, is
        taken; a live lease of ``holder`` is joined; an expired lease is taken
        over, naming its holder as the prior holder. Another holder's live
        lease, a claim naming nobody and a lease whose stamp the ledger cannot
        read (it reads as live) are ``lease_held``; a blocked or closed item is
        ``item_not_claimable``.
        """

        row = self._row
        if row is None:
            return True, None
        recorded = row.get("recorded_status", row.get("status"))
        if recorded in ("blocked", "closed"):
            raise _not_claimable(self.key, recorded)
        if recorded != "claimed":
            return True, None
        on_row = row.get("owner") or row.get("claim")
        expired = row.get("status") == "open" and row.get("recorded_status") == "claimed"
        if expired and _is_text(row.get("expired_holder")):
            prior = row["expired_holder"]
            return True, (None if prior == holder else prior)
        if _is_text(on_row) and on_row == holder:
            return False, None
        raise _lease_held(self.key, on_row if _is_text(on_row) else "a claim that names no holder")

    def _lease_event(self, holder: str, prior: str | None, pass_id: str, token: str) -> dict[str, Any]:
        payload = {"status": "claimed", "owner": holder, "claim": holder, "expires_at": _lease_expiry()}
        if prior is not None:
            payload["prior_holder"] = prior
        return self._element("item.state", payload, f"ticket-lease/{self.key}/{pass_id}/{token}")

    def _claim(self, token: str, claim: Mapping[str, Any], given: str | None, handoff: bool = False) -> str:
        """A new pass's claim: one batch under ``token`` of the creation (on ``none``), the lease when taken, and the pass record.

        Through the hand-off source the pass only joins: an item not leased live to the holder is ``handoff_not_leased``.
        """

        holder = given or f"ticket-pass/{claim['id']}"
        take, prior = self._lease_decision(holder)
        if handoff and not self._leased_live(holder):
            raise _handoff_not_leased(self.key, holder, self._row)
        record = {**claim, "holder": holder, "lease": "taken" if take else "joined"}
        batch = [self._created()] if token == NO_TOKEN else []
        if take:
            batch.append(self._lease_event(holder, prior, claim["id"], token))
        text = f"ticket-{_PASS_RECORD}:v{_RECORD_VERSION} {_canonical(record)}"
        batch.append(self._element("item.note", {"text": text}, f"ticket-pass/{self.key}/{claim['id']}/record"))
        token = self._send(batch, token)
        self._card = _fold(self._card, [("claim", claim)])
        self._live = {"shape": "lease", "holder": holder, "lease": record["lease"]}
        if take:
            self._item = {"owner": holder, "claim": holder, "blocked_by": None}
        return token

    def _join(self, token: str, pass_id: str, given: str | None, handoff: bool = False) -> str:
        """Continue the live pass ``pass_id``: its holder keeps, renews or takes back the lease; the old shape takes none.

        A pass record names the holder. Its live lease is kept with no write;
        an expired or released one is taken again under ``token``; another
        holder's live lease is ``lease_held``, and a holder the open named that
        is not the record's is ``holder_mismatch``. A resume that writes the
        lease has taken it, though its record says joined, so its end releases
        it, unless a hand-off that overlapped the pass's life was handed under
        its holder (the fold, design 6.8). A pass of the old shape
        takes no lease and is ``lease_held`` while the item is claimed by
        anyone but the holder the open named (the default holder,
        ``ticket-pass/<pass id>``, when it named none); its claim stays.
        Through the hand-off source a resume of either shape takes no lease:
        an item not leased live to the holder is ``handoff_not_leased``. What
        the resume took is never kept on the handle: its end decides from a
        fresh read, whichever source resumed it.
        """

        live = self._live or {}
        if live.get("shape") == "lease":
            holder = live["holder"]
            if given is not None and given != holder:
                raise _holder_mismatch(self.key, given, holder)
            take, prior = self._lease_decision(holder)
            if handoff and not self._leased_live(holder):
                raise _handoff_not_leased(self.key, holder, self._row)
            if take:
                token = self._send([self._lease_event(holder, prior, pass_id, token)], token)
                self._item = {"owner": holder, "claim": holder, "blocked_by": None}
                self._live = {**live, "lease": "taken"}
            return token
        holder = given or f"ticket-pass/{pass_id}"
        row = self._row or {}
        on_row = row.get("owner") or row.get("claim")
        if row.get("recorded_status", row.get("status")) == "claimed" and on_row != holder:
            raise _lease_held(self.key, on_row if _is_text(on_row) else "a claim that names no holder")
        if handoff and not self._leased_live(holder):
            raise _handoff_not_leased(self.key, holder, self._row)
        return token

    def release(self) -> None:
        """End this handle's own pass; its pass-end lands, and a lease the pass took is released while it still holds it.

        A bound handle always asks a fresh read (``_release_lease``), whatever
        it took, joined or read before: when that read says the pass may
        release, the pass-end and the lease's release travel in one batch under
        that read's revision. Otherwise, or when that batch does not land, the
        pass-end is sent alone with no ``--expect-revision`` and any lease is
        left to its expiry. A failure is reported and ignored. Releasing again
        sends the pass-end's key alone, so the ledger replays the stored one
        and writes nothing.
        """

        with self._threads:
            mine = self._pass_id or self._ended
            bound = self._pass_id is not None
            self._pass_id, self._ended = None, mine
            if mine is None:
                return
            if bound and self._release_lease(mine):
                return
            try:
                self._send([self._released(mine)], None)
            except StoreUnavailable as exc:
                print(f"ticket.py: the release of pass {mine} did not land: {exc}", file=sys.stderr)

    def _release_lease(self, pass_id: str) -> bool:
        """Send the pass-end and the lease's release under a fresh read's revision; True when that batch landed.

        The one release decision of every end path (``release`` and
        ``clear_pass``), read now from ``export-item`` and ``query --item``,
        never from this handle. It is sent only when that read shows
        ``pass_id`` still the live pass with a lease record naming its holder
        H, the pass having taken the lease as H (its record says ``taken``,
        or it wrote the lease since) with no hand-off that overlapped its life
        handed under H (the fold reads such a pass's lease as joined, design
        6.8), and the item's row still recorded claimed by H. Never raises.
        """

        try:
            card, token = self.read()
            live, row = self._live or {}, self._row or {}
            holder = live.get("holder")
            holds = (
                card["pass"] is not None and card["pass"]["id"] == pass_id
                and live.get("shape") == "lease" and live.get("lease") == "taken"
                and row.get("recorded_status", row.get("status")) == "claimed"
                and (row.get("owner") or row.get("claim")) == holder
            )
            if not holds:
                return False
            released = self._element("item.state", {"status": "open"}, f"ticket-lease/{self.key}/{pass_id}/{token}/released")
            self._send([self._released(pass_id), released], token)
            return True
        except Exception:
            return False

    def clear_pass(self, pass_id: str) -> None:
        """A person's release of a crashed pass: refused unless ``pass_id`` is the live claim.

        The pass-end lands as ``release`` sends it, and the lease the pass took
        is released with it only when ``release``'s own fresh decision
        (``_release_lease``) says so: this checks the live pass id and adds no
        lease check of its own (design 6.8).
        """

        card, _ = self.read()
        live = card["pass"]
        if live is None or live["id"] != pass_id:
            raise StoreUnavailable(
                f"pass {pass_id} is not the live pass on ticket {self.key}"
                + (f" (the live pass is {live['id']})" if live else " (no pass is live)")
                + "; nothing was changed",
                code="held" if live else "pass_ended",
            )
        if self._release_lease(pass_id):
            return
        self._send([self._released(pass_id)], None)

    # The batch and the answer.

    def _element(self, kind: str, payload: Mapping[str, Any], key: str) -> dict[str, Any]:
        return {"kind": kind, "payload": {**payload, "source": _LEDGER_WRITER}, "item_id": self.key, "idempotency_key": key}

    def _created(self) -> dict[str, Any]:
        """The item's creation event: the same event and key whether a claim on ``none`` or the spec intake (6.6) sends it."""

        return self._element("item.created", {"type": "ticket", "title": self.title}, f"ticket-item/{self.key}/created")

    def _released(self, pass_id: str) -> dict[str, Any]:
        return self._element("session.closed", {"outcome": f"ticket pass {pass_id} released"}, f"ticket-pass/{self.key}/{pass_id}/released")

    def _batch(self, token: str, events: Sequence[tuple[str, Any]]) -> list[dict[str, Any]]:
        """The events as ledger batch elements, keyed by the mapping of design 6.2.

        Ordinary keys carry the held token, one random request token per
        ``apply`` and the event's index; the close's two keys share one random
        close token and name the pass. A claim is never here: ``_claim`` builds
        its batch. A decision also gets a ``decided:`` item note, the next
        element, keyed by its ordinary key and ``/decided`` (CP1 3.5), so a retry
        of the batch writes it once; a builder fact is one note under its own
        key, which the toolkit's ``row-item.sh facts`` reads.
        """

        key = self.key
        request, close = secrets.token_hex(8), secrets.token_hex(8)
        batch = []
        for index, (kind, value) in enumerate(events):
            ordinary = f"ticket-write/{key}/{token}/{request}/{index}"
            if kind in _RECORD_KINDS:
                batch.append(self._element("item.note", {"text": f"ticket-{kind}:v{_RECORD_VERSION} {_canonical(value)}"}, ordinary))
                decided = _decided_text(value) if kind == "decision" else None
                if decided is not None:
                    batch.append(self._element("item.note", {"text": decided}, f"{ordinary}/decided"))
            elif kind == "pr":
                batch.append(self._element("item.linked", {"rel": "pr", "target": value}, ordinary))
            elif kind == "builder":
                batch.append(self._element("item.note", {"text": _builder_text(value)}, _builder_key(key, value)))
            elif kind == "stamp":
                payload = {"point": value["point"], "phase": value["phase"], "skills": value["next_skills"], "refs": value["refs"]}
                stop = f"ticket-close/{key}/{self._pass_id}/{close}/stop" if value["close"] else ordinary
                batch.append(self._element("item.stopped", payload, stop))
            elif kind == "tried":
                batch.append(self._element("item.tried", value, ordinary))
            elif kind == "closed":
                payload = {"status": "closed"}
                payload.update({name: self._item[name] for name in ("owner", "claim") if self._item.get(name)})
                if self._item.get("blocked_by") is not None:
                    payload["blocked_by"] = self._item["blocked_by"]
                batch.append(self._element("item.state", payload, f"ticket-close/{key}/{self._pass_id}/{close}/state"))
        return batch

    def _send(self, batch: list[dict[str, Any]], expect: str | None) -> str:
        """One ``append-many`` of ``batch``, read by design 6.2.4; returns the new token.

        ``expect`` is the held token for ``--expect-revision``, or None for a
        release. The two lost races are ``stale``; ``usage`` is
        ``ledger_incompatible``; a permanent refusal is ``ledger_refused``
        with no lookup; any other answer is resolved by the batch's last key.
        """

        args = ["append-many", self.key] + ([] if expect is None else ["--expect-revision", expect])
        stdin = json.dumps(batch)
        answer = self._run(*args, stdin=stdin)
        if answer.lost:
            # Nothing came back. The same batch again, under the same keys: a
            # batch that landed replays with no write and no revision check.
            answer = self._run(*args, stdin=stdin)
        if answer.ok:
            return str(answer.data["events"][-1]["id"])
        if answer.code in _LEDGER_RACES:
            race = _stale(self.key, f"the ledger answered {answer.code}: {answer.message}")
            race.ledger_code = answer.code
            raise race
        if answer.code == "usage":
            raise _ledger_incompatible(self.cli, f"{answer.code}: {answer.message}")
        if answer.code in _LEDGER_PERMANENT:
            raise self._refused("write", answer)
        return self._look_up(batch, answer)

    def _look_up(self, batch: list[dict[str, Any]], answer: _Answer) -> str:
        """An ambiguous answer: the batch landed exactly when its last key is stored with the same kind and payload."""

        last = batch[-1]
        wanted = (last["idempotency_key"], last["kind"], _canonical(last["payload"]))
        export = self._run("export-item", self.key)
        if export.ok:
            for event in export.data["events"]:
                if (event.get("idempotency_key"), event.get("kind"), _canonical(event.get("payload"))) == wanted:
                    return str(event["id"])
        raise self._refused("write", answer)


# --- Store selection (design 6.3) -------------------------------------------

# The ticket skill's own explicit setting; it comes before the topic config's ``ledger_cli`` key.
LEDGER_SELECTOR = "TICKET_LEDGER_CLI"
# Seconds the capability probe (``append-many --help``) may run before the CLI counts as not running.
LEDGER_PROBE_TIMEOUT = 10.0
# The shared rule: ``skills/_lib/ledger_cli.py``, beside this skill's directory.
LEDGER_RULE_PATH = Path(__file__).resolve().parent.parent / "_lib" / "ledger_cli.py"


def _load_ledger_rule():
    """The shared rule every skill uses to decide whether the ledger is on."""

    spec = importlib.util.spec_from_file_location("ticket_ledger_cli", LEDGER_RULE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the shared ledger rule {LEDGER_RULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Loading the shared helper must not leave bytecode beside it.
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except OSError as exc:
        raise ImportError(
            f"the shared ledger rule {LEDGER_RULE_PATH} is missing ({exc}); reinstall the skills "
            "(`install.sh` installs `_lib` beside them)"
        ) from exc
    finally:
        sys.dont_write_bytecode = saved
    return module


_LEDGER_RULE = _load_ledger_rule()


@dataclass(frozen=True)
class Selection:
    """The store the shared rule gives: the ledger CLI to use (None for markdown) and why, starting with the rule."""

    cli: str | None
    reason: str

    @property
    def line(self) -> str:
        return f"{'ledger' if self.cli else 'markdown'} ({self.reason})"


def select_store() -> Selection:
    """Pick the store by the shared rule, ``decide`` in ``skills/_lib/ledger_cli.py`` (design 6.3).

    ``TICKET_LEDGER_CLI`` is an explicit setting ahead of the topic config's
    ``ledger_cli`` key. Either one set to ``none`` (any case) is the markdown
    store. Set to anything else, it names the ledger CLI: one that fails the
    capability probe is refused ``ledger_incompatible`` rather than falling
    back. With neither set, a ``ledger`` on PATH is used when ``LEDGER_DB``
    names an existing database and the CLI passes the same probe; every other
    case is the markdown store with the reason, never a refusal. The reason
    starts with the rule that chose.
    """

    decision = _LEDGER_RULE.decide(selector_env=LEDGER_SELECTOR, who="ticket", quiet=True, probe_timeout=LEDGER_PROBE_TIMEOUT)
    if decision.rule == _LEDGER_RULE.RULE_EXPLICIT:
        _probe_ledger(decision.cli)
    return Selection(decision.cli, decision.reason)


def choose_store(repo: str | Path, key: str, title: str) -> Store:
    """The ledger store or the markdown store, as ``select_store`` decides; the handle carries the chosen store and why.

    An explicitly named CLI that fails the capability probe (its
    ``append-many --help`` does not exit 0 naming ``--expect-revision``) is
    refused ``ledger_incompatible`` before anything is read or written. A CLI
    found on PATH that fails it is not a refusal: the markdown store is chosen.
    """

    chosen = select_store()
    store: Store = LedgerStore(chosen.cli, key, title, repo) if chosen.cli else MarkdownStore(repo, key, title)
    store.selection = chosen.line
    return store


def _loop_adapter_present() -> bool | None:
    """Optional add-on discovery, like review-gate: env, topic override, shipped topic config.

    Presence is an absolute executable path, not permission to run it. Unreadable
    configuration is unknown (None), not evidence that the loop is absent.
    """

    try:
        adapter = os.environ.get("TICKET_LOOP_ADAPTER", "").strip()
        if not adapter:
            shipped, problem = _LEDGER_RULE._read_object(_LEDGER_RULE.CONFIG_PATH, False, "ticket", True)
            override, override_problem = _LEDGER_RULE._read_object(
                Path(_LEDGER_RULE.OVERRIDE_PATH).expanduser(), False, "ticket", True,
            )
            if problem or override_problem:
                return None
            adapter = override.get("loop_adapter", shipped.get("loop_adapter"))
        if adapter is None or adapter == "" or (isinstance(adapter, str) and adapter.lower() == "none"):
            return False
        if isinstance(adapter, str) and os.path.isabs(adapter) and os.path.isfile(adapter) and os.access(adapter, os.X_OK):
            return True
    except (OSError, ValueError, TypeError):
        pass
    return None


def _require_runnable(cli: str) -> None:
    """Refuse ``ledger_cli_moved`` when ``cli`` is an absolute path that is no longer an executable file.

    The check is the shared rule's, the one its ``call`` makes: this skill runs the CLI itself, so it asks first.
    Nothing is looked up on PATH, and the refusal line starts with the fixed token ``ledger_cli_moved:``.
    """

    try:
        _LEDGER_RULE.require_runnable(cli)
    except _LEDGER_RULE.LedgerCallError as exc:
        raise StoreUnavailable(str(exc), code="ledger_cli_moved") from exc


def _probe_ledger(cli: str) -> None:
    """Refuse ``ledger_cli_moved`` for a pinned CLI that is gone, else ``ledger_incompatible`` unless ``cli`` passes the shared capability probe."""

    _require_runnable(cli)
    failure = _LEDGER_RULE.probe(cli, LEDGER_PROBE_TIMEOUT)
    if failure is not None:
        raise _ledger_incompatible(cli, failure)


# --- The ledger-item source (design 6.5) ------------------------------------

# An acceptance line that starts like a list item but is not a ``- `` bullet at
# the very start of the line: another marker, or a marker after indentation.
_NOT_A_BULLET = re.compile(r"\s*(?:[-*+]|[0-9]+[.)])(?:\s|$)")
# A spec's content hash as the ledger writes it: a SHA-256 hex digest.
_SPEC_HASH = re.compile(r"[0-9a-f]{64}")


def _ledger_read(cli: str, *args: str) -> _Answer:
    """One read call of the ledger CLI, ``<cli> --json <args>``, as the ledger store makes its calls."""

    _require_runnable(cli)
    result = subprocess.run([cli, "--json", *args], input="", capture_output=True, encoding="utf-8", check=False)
    return _ledger_answer(result.returncode, result.stdout, result.stderr)


def spec_set_command(cli: str, key: str, title: str) -> str:
    """The command that gives ledger item ``key`` a spec, shell-quoted; the person fills in the two placeholders."""

    return shlex.join([cli, "spec", "set", key, "--title", title]) + " --body-file <body.md> --acceptance <criteria>"


def _parse_acceptance(text: str) -> list[str]:
    """A spec's acceptance text as ordered criteria, by the grammar of design 6.5.

    Two forms are accepted. Every non-blank line is a bullet (``- `` at the
    very start of the line), and each bullet's text is one criterion. Or no
    line starts like a list item, and the whole text is one criterion. Blank
    lines are ignored. Anything else raises ``ValueError`` naming the first
    line that breaks the grammar; nothing is guessed.
    """

    lines = [(number, line) for number, line in enumerate(text.splitlines(), 1) if line.strip()]
    if any(line.startswith("- ") for _, line in lines):
        criteria = []
        for number, line in lines:
            if not line.startswith("- "):
                raise ValueError(f"line {number} is not a `- ` bullet at the start of the line, but other lines are bullets")
            if not line[2:].strip():
                raise ValueError(f"line {number} is a bullet with no text")
            criteria.append(line[2:].strip())
        return criteria
    for number, line in lines:
        if _NOT_A_BULLET.match(line):
            raise ValueError(f"line {number} starts like a list item, but not with `- ` at the start of the line")
    return [text.strip()]


def _item_unreadable(key: str, verb: str, why: str) -> StoreUnavailable:
    return StoreUnavailable(
        f"the ledger's `{verb}` answer for item {key} cannot be read: {why}. Nothing was written, and no earlier "
        "read stands in for it; a person checks the ledger.",
        code="item_unreadable",
    )


def _spec_identity(key: str, data: Any) -> SpecVersion:
    """The version and content hash of a ``spec show`` answer for item ``key``; any other shape is ``item_unreadable``."""

    if type(data) is not dict or data.get("item_id") != key:
        raise _item_unreadable(key, "spec show", f"it is not the spec of item {key}")
    version = data.get("version")
    if type(version) is not int or version < 1:
        raise _item_unreadable(key, "spec show", f"its version {version!r} is not a whole number of at least 1")
    content_hash = data.get("content_hash")
    if type(content_hash) is not str or not _SPEC_HASH.fullmatch(content_hash):
        raise _item_unreadable(key, "spec show", f"its content_hash {content_hash!r} is not a SHA-256 hex digest")
    return SpecVersion(version, content_hash)


def _confirm_spec(key: str, held: SpecVersion | None, current: SpecVersion) -> None:
    """Refuse ``stale`` unless ``current``, the item's spec read after the card, is still ``held``, the one the open holds."""

    if current != held:
        shown = "none" if held is None else f"v{held.version}"
        raise _stale(key, f"its spec moved from {shown} to v{current.version} while the pass opened")


class LedgerItemSource:
    """A ticket that is a ledger item (design 6.5): its title and status (``query --item``), its current
    spec (``spec show``) and its links (``links``), read through the ledger CLI.

    Every ``pull`` reads all three again and keeps nothing between pulls, so an
    open plans or resumes only from the spec version it read itself. The spec's
    body is the description, its acceptance text gives the criteria, and its
    version and content hash travel on the ticket as ``spec``. An item with no
    spec, or a spec with no acceptance criteria, is refused by name with the
    ``spec set`` command that fixes it; a read that fails or cannot be parsed
    is refused, never answered from an earlier read. The pass keeps its card on
    the same item, through the ledger store of the same CLI.
    """

    def __init__(self, cli: str | Path):
        self.cli = str(cli)

    def _data(self, key: str, verb: str, answer: _Answer) -> Any:
        if answer.ok:
            return answer.data
        if answer.code == "usage":
            raise _ledger_incompatible(self.cli, f"{verb}: {answer.code}: {answer.message}")
        said = f"{answer.code}: {answer.message}" if answer.code else f"no envelope, exit status {answer.status}: {answer.output}"
        raise StoreUnavailable(
            f"the ledger refused `{verb}` for ticket {key} ({said}). Nothing was written, and no earlier read stands "
            "in for it; a person reads the ledger's reason.",
            code="ledger_refused",
        )

    def _row(self, key: str, answer: _Answer) -> Mapping[str, Any]:
        """The item's row from a ``query --item`` answer."""

        data = self._data(key, "query", answer)
        rows = data.get("items") if type(data) is dict else None
        if type(rows) is not list:
            raise _item_unreadable(key, "query", "it lists no items")
        row = next((row for row in rows if type(row) is dict and row.get("item_id") == key), None)
        if row is None:
            raise _item_unreadable(key, "query", f"it does not list item {key}")
        if not _is_text(row.get("title")) or not _is_text(row.get("status")):
            raise _item_unreadable(key, "query", "the item's title or status is not text")
        return row

    def read_spec(self, key: str, title: str) -> tuple[SpecVersion, str, list[str], str | None]:
        """The item's current spec: its version, body, criteria and recorded time, read now."""

        answer = _ledger_read(self.cli, "spec", "show", key)
        if answer.ok is False and answer.code == "no_spec":
            raise StoreUnavailable(
                f"ledger item {key} has no spec, so there is nothing to plan against. Nothing was written. Give it "
                f"one with `{spec_set_command(self.cli, key, title)}`, then open the ticket again.",
                code="no_spec",
            )
        data = self._data(key, "spec show", answer)
        spec = _spec_identity(key, data)
        version = spec.version
        body = data.get("body_md")
        if not _is_text(body):
            raise _item_unreadable(key, "spec show", "its body is not text")
        recorded = data.get("recorded_at")
        if recorded is not None and not _is_text(recorded):
            raise _item_unreadable(key, "spec show", "its recorded_at is not text")
        acceptance = data.get("acceptance")
        if acceptance is not None and type(acceptance) is not str:
            raise _item_unreadable(key, "spec show", "its acceptance is not text")
        if acceptance is None or not acceptance.strip():
            raise StoreUnavailable(
                f"spec v{version} of ledger item {key} has no acceptance criteria, so a review has nothing to check. "
                f"Nothing was written. Set them with `{spec_set_command(self.cli, key, title)}`, then open the "
                "ticket again.",
                code="no_acceptance",
            )
        try:
            criteria = _parse_acceptance(acceptance)
        except ValueError as exc:
            raise StoreUnavailable(
                f"the acceptance criteria of spec v{version} of ledger item {key} cannot be read: {exc}. Nothing was "
                "written. Write them as `- ` bullets, one per line, or as one text with no list markers, with "
                f"`{spec_set_command(self.cli, key, title)}`.",
                code="acceptance_unparseable",
            ) from exc
        return spec, body, criteria, recorded

    def _links(self, key: str) -> list[dict[str, str]]:
        """The item's links: each outgoing link as its relation, each incoming one as ``<relation> (incoming)``."""

        answer = _ledger_read(self.cli, "links", key)
        if answer.ok is False and answer.code == "empty":
            return []
        data = self._data(key, "links", answer)
        if type(data) is not dict or type(data.get("outgoing")) is not list or type(data.get("incoming")) is not list:
            raise _item_unreadable(key, "links", "it does not list outgoing and incoming links")
        links = []
        for direction, end, suffix in (("outgoing", "target", ""), ("incoming", "source", " (incoming)")):
            for row in data[direction]:
                if type(row) is not dict or not _is_text(row.get("rel")) or not _is_text(row.get(end)):
                    raise _item_unreadable(key, "links", f"an {direction} link does not name its relation and {end}")
                links.append({"kind": row["rel"] + suffix, "url": row[end]})
        return links

    def pull(self, key: str) -> Ticket:
        if not _KEY.fullmatch(key):
            raise TicketError(f"unsafe ticket key: {key!r}")
        return self._ticket(key, _ledger_read(self.cli, "query", "--item", key))

    def _ticket(self, key: str, answer: _Answer) -> Ticket:
        """The ticket from the item's ``query`` answer, its spec read now and its links."""

        row = self._row(key, answer)
        spec, body, criteria, recorded = self.read_spec(key, row["title"])
        ticket = Ticket.from_mapping(
            {
                "key": key,
                "title": row["title"],
                # A claim is a pass's lease on the item, not the ticket's status: the item reads open while it is held.
                "status": "open" if row["status"] == "claimed" else row["status"],
                "description": body,
                "acceptance_criteria": criteria,
                "updated_at": recorded,
                "links": self._links(key),
            }
        )
        return dataclasses.replace(ticket, spec=spec)

    def confirm(self, ticket: Ticket) -> None:
        """Refuse ``stale`` unless the item's spec is still the one ``ticket`` was pulled at.

        ``open`` calls it after the card read: a spec set before the read is
        seen here, and one after it moves the item's revision past the held
        token, so the claim and every later write are refused ``stale``.
        """

        _confirm_spec(ticket.key, ticket.spec, self.read_spec(ticket.key, ticket.title)[0])


def ledger_item_source(key: str) -> LedgerItemSource | None:
    """The source for ``/ticket <key>`` when no tracker key and no JSON file matched it (design 6.5).

    With the ledger off by the shared rule, or no item ``key`` in it, this
    returns None: the key names no ticket, as before. An item that exists gives
    its source; the open then refuses it ``no_spec`` when it has no spec. A
    named CLI that fails the capability check is refused as ``choose_store``
    refuses it, and any other ledger refusal is ``ledger_refused``.
    """

    if not _KEY.fullmatch(key):
        raise TicketError(f"unsafe ticket key: {key!r}")
    chosen = select_store()
    if chosen.cli is None:
        return None
    source = LedgerItemSource(chosen.cli)
    answer = _ledger_read(source.cli, "query", "--item", key)
    if answer.ok is False and answer.code in _LEDGER_NO_ITEM:
        return None
    source._row(key, answer)
    return source


# --- A parent's hand-off as the ticket source (design 6.8) -----------------

# The ledger release whose ``export-item`` carries each loop event's payload version and whose loop writer records a
# hand-off. The hand-off source refuses an older CLI, or one whose ``--version`` line cannot be read.
HANDOFF_LEDGER_VERSION = (0, 7, 0)
_VERSION_LINE = re.compile(r"ledger ([0-9]+)\.([0-9]+)\.([0-9]+)\n?")


def _check_handoff_ledger(cli: str) -> None:
    """Refuse ``handoff_ledger_too_old`` unless ``<cli> --version`` exits 0 printing ``ledger X.Y.Z`` at 0.7.0 or later.

    The three numbers are compared as integers. Another shape, a suffix, a non-zero exit, output that is not text
    and no answer within ``LEDGER_PROBE_TIMEOUT`` are the same refusal: a version line that cannot be read is too old.
    """

    _require_runnable(cli)
    line = None
    try:
        result = subprocess.run(
            [cli, "--version"], input="", capture_output=True, encoding="utf-8", timeout=LEDGER_PROBE_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        printed = f"nothing within {LEDGER_PROBE_TIMEOUT:g} seconds"
    except (OSError, ValueError) as exc:
        printed = f"nothing readable ({_one_line(exc)})"
    else:
        printed = f"{result.stdout!r} with exit status {result.returncode}"
        line = _VERSION_LINE.fullmatch(result.stdout) if result.returncode == 0 else None
    if line is None or tuple(int(part) for part in line.groups()) < HANDOFF_LEDGER_VERSION:
        least = ".".join(str(part) for part in HANDOFF_LEDGER_VERSION)
        raise StoreUnavailable(
            f"the ledger CLI {cli} answered `--version` with {printed}, which is not `ledger X.Y.Z` at {least} or later, "
            "so it may not read a parent's hand-off as the loop wrote it. Nothing was read or written; select a ledger "
            f"CLI of release {least} or later.",
            code="handoff_ledger_too_old",
        )


def _check_implementations(value: Any) -> Mapping[str, tuple[str, ...]]:
    """The implementations an opener declares it runs, per seam; a ``TicketError`` before anything is read otherwise.

    A mapping whose keys are among ``build``, ``review`` and ``outward``, each value one or more name tokens.
    """

    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise TicketError("the implementations an opener declares are a mapping of seam names to lists of names")
    declared: dict[str, tuple[str, ...]] = {}
    for seam, names in value.items():
        if seam not in SEAMS:
            raise TicketError(f"unknown seam: {seam!r}; the seams are {', '.join(SEAMS)}")
        if type(names) not in (list, tuple) or not names:
            raise TicketError(f"the implementations declared for the {seam} seam must be a non-empty list of names")
        for name in names:
            if not _is_handoff_name(name):
                raise TicketError(
                    f"the implementation {name!r} declared for the {seam} seam is not a name token (ASCII letters, "
                    "digits, dot, underscore and hyphen, starting with a letter or digit, at most 64 characters)"
                )
        declared[seam] = tuple(names)
    return MappingProxyType(declared)


class HandoffSource(LedgerItemSource):
    """A parent's hand-off as the ticket (design 6.8): the ledger-item source plus the hand-off's conditions.

    Only a parent's adapter uses it, never ``/ticket <KEY>``. The ticket is the item's, read as the ledger-item source
    reads it, after one ``<cli> --version`` call (``handoff_ledger_too_old`` below 0.7.0); an item the ledger does not
    hold is ``handoff_not_managed``. ``implementations`` is what the opener runs for each seam beyond its default; the
    open refuses a hand-off that names any other (``handoff_seam_unknown``). The controller and the seams are never
    the opener's to give: they are the item's open hand-off event's own, read again at every read.
    """

    def __init__(self, cli: str | Path, implementations: Mapping[str, Sequence[str]] | None = None):
        self.implementations = _check_implementations(implementations)
        super().__init__(cli)

    def pull(self, key: str) -> Ticket:
        if not _KEY.fullmatch(key):
            raise TicketError(f"unsafe ticket key: {key!r}")
        _check_handoff_ledger(self.cli)
        answer = _ledger_read(self.cli, "query", "--item", key)
        if answer.ok is False and answer.code in _LEDGER_NO_ITEM:
            raise _handoff_refused("handoff_not_managed", key, "the ledger holds no such item")
        return self._ticket(key, answer)

    def check(self, key: str, handoff: Mapping[str, Any]) -> None:
        """Refuse ``handoff_seam_unknown`` unless every seam of ``handoff`` names its default or a declared name of that seam."""

        for seam in SEAMS:
            name = handoff["seams"][seam]
            if name != SEAMS[seam] and name not in self.implementations.get(seam, ()):
                raise _handoff_refused(
                    "handoff_seam_unknown", key,
                    f"its {seam} seam names the implementation {name}, which is neither the default ({SEAMS[seam]}) nor "
                    f"one this opener declared it runs for {seam}; it is not run as the default either",
                )


def handoff_source(key: str, implementations: Mapping[str, Sequence[str]] | None = None) -> HandoffSource:
    """The hand-off source for ledger item ``key`` through the CLI the store rule picks (design 6.8).

    The declared implementations are checked first (``TicketError``). With the ledger off by the rule this is
    ``handoff_no_ledger`` and no ledger call is made; a named CLI that fails the capability check is refused as
    ``choose_store`` refuses it. Open it with ``choose_store`` for the same key and ``holder=`` the lease holder the
    parent took the item as.
    """

    declared = _check_implementations(implementations)
    if not _KEY.fullmatch(key):
        raise TicketError(f"unsafe ticket key: {key!r}")
    chosen = select_store()
    if chosen.cli is None:
        raise _handoff_refused("handoff_no_ledger", key, f"the ledger is off by the store rule ({chosen.reason})")
    return HandoffSource(chosen.cli, declared)


def _check_item_store(key: str, source: Any, store: Any) -> None:
    """A ledger item's card lives on that same item: refuse any other store before anything is read (design 6.5).

    An ad-hoc ask has one item, the one its text names, so the store must be that item's too.
    """

    if isinstance(source, AskTicketSource):
        if store.key != key:
            raise StoreUnavailable(
                f"ad-hoc ask {source.ask_source} is item {source.key}, but this open was given the store of item "
                f"{store.key}. Nothing was read or written; open it with the store choose_store gives for {source.key}.",
                code="source_store_mismatch",
            )
        return
    if not isinstance(source, LedgerItemSource):
        return
    if isinstance(store, LedgerStore) and store.key == key and store.cli == source.cli:
        return
    given = (
        f"the ledger store of item {store.key} through {store.cli}" if isinstance(store, LedgerStore)
        else f"a {type(store).__name__}"
    )
    raise StoreUnavailable(
        f"ticket {key} is a ledger item read through {source.cli}, so its card lives on that item, but this open "
        f"was given {given}. Nothing was read or written; open it with the store choose_store gives for {key}.",
        code="source_store_mismatch",
    )


# --- The pass ----------------------------------------------------------------


@dataclass(frozen=True)
class MoreInfoExit:
    destination: str
    waiting: str
    drafted_ask: str | None = None


_STORE_PHASE = {
    "orient": "orient",
    "research": "orient",
    "plan": "plan",
    "plan_go": "plan",
    "build": "build",
    "outward_go": "review",
}


def _handoff_line(handoff: Mapping[str, Any]) -> str:
    """The reconciliation line of an open through the hand-off source: the controller, the seams, the operation and the event."""

    seams = ", ".join(f"{seam} {handoff['seams'][seam]}" for seam in SEAMS)
    return (
        f"hand-off: controller {handoff['controller']}; seams {seams}; "
        f"operation {handoff['operation_id']}, event {handoff['event_id']}"
    )


def _ends_on_failure(method):
    """Any failure past validation ends the pass and releases its claim, then raises.

    A ``TicketError`` (a wrong phase, an invalid value, a gate not yet
    approved) wrote nothing, so the pass stays live.
    """

    @functools.wraps(method)
    def guarded(self: "TicketPass", *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except TicketError:
            raise
        except BaseException:
            self._end_quietly()
            raise

    return guarded


class TicketPass:
    """One pass over a ticket. Outward actions stay with the caller.

    The pass holds the card and token of its last read or last successful
    write, and its live phase. Every write is one ``apply`` under that token;
    the pair advances only after a write lands.
    """

    def __init__(self, ticket: Ticket, store: Store, card: dict[str, Any], token: str, identity: Identity | None):
        self._ticket = ticket
        self.store = store
        self._card = card
        self._token = token
        self._phase = "orient"
        self._identity = identity
        self._plan: PlanCard | None = None
        self._rounds = len(card["reviews"])
        self._pass_id: str | None = None
        self._live = False
        # The lease holder this open named (None for the default); only the ledger store takes it.
        self._lease_holder: str | None = None
        # True for an open through the hand-off source: its claim or join never takes a lease (design 6.8).
        self._handoff = False
        self._context_reading: Callable[[], ContextReading | None] | None = None
        self._context_floor_fraction = CONTEXT_FLOOR_FRACTION
        self.reconciliation: tuple[str, ...] = ()
        self.resume_card: dict[str, Any] = card

    @classmethod
    def open(
        cls, key: str, source: TicketSource, store: Store, *, pass_id: str | None = None, holder: str | None = None,
        context_reading: Callable[[], ContextReading | None] | None = None,
        context_floor_fraction: float = CONTEXT_FLOOR_FRACTION,
    ) -> "TicketPass":
        """Open a pass: a new one claims the ticket; ``pass_id`` continues that live pass.

        On the ledger store a new pass takes the item's lease as ``holder``, or
        joins it when the item is already leased to that holder; with no holder
        it is ``ticket-pass/<pass id>``, so two opens that name none are two
        holders. Another holder's live lease is ``lease_held`` and nothing is
        written. A resume keeps the holder its pass record names. A holder that
        is not a name is refused ``holder_invalid`` before anything is pulled,
        read or written. The markdown store has no lease and ignores it.

        One store handle serves one pass. A handle bound to a pass, or whose
        pass has ended, is reported ``handle_used`` here, before anything is
        pulled or read; the guard is the store's own check, under its lock,
        when the claim or join reaches it, so a handle that becomes used during
        the open is refused there. Then the source is pulled, the card is read
        and its state derived. A ledger-item source is checked against the
        store first (``source_store_mismatch``) and its spec is read again
        after the card (``stale`` when it moved). On the ledger store, any
        other source's ticket is recorded as the item's spec between the pull
        and the card read (``LedgerStore.record_spec``, design 6.6), and a
        failure there stops the open before the card is read; after the card
        read that spec is confirmed still current (``stale`` when another open
        recorded a newer one). A refused card
        writes nothing more; a stopped or closed ticket is reported and takes
        no claim. Beyond that spec record the open writes nothing into a store:
        a new pass sends its claim, a join sends a ``join`` event.

        Through the hand-off source (design 6.8) the holder is required. After
        the card read the item's one open hand-off is checked
        (``LedgerStore.hand_off``) and each of its seams must name the default
        or an implementation the source declares (``handoff_seam_unknown``);
        the claim or join never takes a lease (``handoff_not_leased``). Every
        other source is refused ``handoff_source_required`` on an item with an
        open loop intent of a version other than 1.
        """

        handing_off = isinstance(source, HandoffSource)
        if holder is not None or handing_off:
            _check_holder(holder)
        if store.pass_id is not None or store.ended is not None:
            raise _handle_used(key, store.pass_id or store.ended)
        _check_item_store(key, source, store)
        ticket = source.pull(key)
        if ticket.key != key:
            raise TicketError(f"the source returned ticket {ticket.key!r}, expected {key!r}")
        # Before the card read, so the token it reads already holds any spec version this wrote.
        intake: tuple[str, ...] = ()
        recorded: SpecVersion | None = None
        if isinstance(store, LedgerStore) and not isinstance(source, LedgerItemSource):
            recorded, intake = store.record_spec(ticket)
        card, token = store.read()
        handoff: dict[str, Any] | None = None
        if handing_off:
            handoff = store.hand_off()
            source.check(key, handoff)
        elif isinstance(store, LedgerStore) and store.handed_off is not None:
            raise _handoff_source_required(key, store.handed_off)
        if isinstance(source, LedgerItemSource):
            source.confirm(ticket)
        elif recorded is not None:
            # Another open may have recorded a newer spec between this record and the card read.
            store.confirm_spec(recorded)
        state, flags, identity = _derive(card, ticket, pass_id, key)
        session = cls(ticket, store, card, token, identity)
        session._phase = state
        session._lease_holder = holder
        session._handoff = handing_off
        session._context_reading = context_reading
        session._context_floor_fraction = context_floor_fraction
        notice = getattr(store, "leftover_notice", None)
        leftover = notice() if callable(notice) else None
        stored = getattr(store, "selection", None)
        session.reconciliation = (
            *((f"store: {stored}",) if stored else ()),
            *((_handoff_line(handoff),) if handoff is not None else ()),
            *intake,
            *flags,
            *((leftover,) if leftover else ()),
        )
        if session._identity is not None:
            session._plan = PlanCard.from_snapshot(card["plan"])
            session._plan.ask_source = ticket.ask_source
            session._plan.review = _passing_final(card, session._identity) if state in ("outward_go", "closed") else None
            session._sync_steps()
        if state in ("stopped", "closed"):
            return session
        if pass_id is None:
            session._claim()
        else:
            # The join binds the handle inside the store, under its lock; it changes no card byte.
            session._token = session._bind(token, [("join", {"id": pass_id})])
            session._pass_id = pass_id
        session._live = True
        session._sync_steps()
        return session

    def _claim(self) -> None:
        claim = {"id": secrets.token_hex(8), "host": _host(), "started_at": _now()}
        try:
            token = self._bind(self._token, [("claim", claim)])
        except StoreUnavailable as exc:
            if exc.code in ("handle_used", "lease_held", "item_not_claimable", "handoff_not_leased"):
                # Not a lost race: the handle was bound meanwhile, or the item's lease or status refuses the claim.
                raise
            # A lost race: read once more and name the pass that won.
            card, _ = self.store.read()
            if card["pass"] is not None:
                raise TicketHeld(self.key, card["pass"]) from exc
            raise
        self._advance(token, [("claim", claim)])
        self._pass_id = claim["id"]

    def _bind(self, token: str, events: Sequence[tuple[str, Any]]) -> str:
        """Send this open's claim or join; on the ledger store the holder it named travels with it, under its lock."""

        if isinstance(self.store, LedgerStore):
            return self.store.apply(token, events, holder=self._lease_holder, handoff=self._handoff)
        return self.store.apply(token, events)

    @property
    def key(self) -> str:
        return self._ticket.key

    @property
    def ticket(self) -> Ticket:
        return self._ticket

    @property
    def pass_id(self) -> str | None:
        """The id a later process presents to continue this pass."""

        return self._pass_id

    @property
    def live(self) -> bool:
        return self._live

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def plan(self) -> PlanCard | None:
        return self._plan

    @property
    def plan_revision(self) -> int:
        return self._identity[0] if self._identity else 0

    @property
    def review_round(self) -> int:
        return self._rounds

    # Lifecycle.

    def end(self) -> None:
        """End the pass and release its claim. Calling it again does nothing."""

        if not self._live:
            return
        self._live = False
        if self._plan is not None:
            self._plan.claim = None
        self.store.release()

    def _end_quietly(self) -> None:
        # The original error wins. A release that fails too leaves the claim,
        # and the ticket stays refused until a person runs `release`.
        with contextlib.suppress(Exception):
            self.end()

    def __enter__(self) -> "TicketPass":
        return self

    def __exit__(self, kind: Any, value: Any, traceback: Any) -> bool:
        if kind is not None:
            self._end_quietly()
        return False

    # What every step does first.

    def _require(self, *phases: str) -> None:
        """This pass's own claim on the held card, then the live phase."""

        _check_pass(self.key, self._card["pass"], self._pass_id, None if self._live else (self._pass_id or "none"))
        if self._phase not in phases:
            raise TicketError(f"expected phase {' or '.join(phases)}, found {self._phase}")

    def _fresh(self) -> dict[str, Any]:
        """A fresh read, proven equal to the held card by its token."""

        card, token = self.store.read()
        if token != self._token:
            raise _stale(self.key, "a fresh read differs from the card this pass holds")
        return card

    def _write(self, events: list[tuple[str, Any]]) -> None:
        token = self.store.apply(self._token, events)
        self._advance(token, events)

    def _advance(self, token: str, events: Sequence[tuple[str, Any]]) -> None:
        self._card = _fold(self._card, events)
        self._token = token
        self._sync_steps()

    def _sync_steps(self) -> None:
        """Set the held plan's derived steps from the held card."""

        if self._plan is not None:
            self._plan.reached = _reached_steps(self._card, self._identity, self.key)
            # The claim is shown only while this pass is live: an ended pass or a terminal resume holds none.
            self._plan.claim = self._card["pass"] if self._live else None

    def _stamp(self, point: str, next_skills: Sequence[str], refs: Sequence[str], *, phase: str | None = None, close: bool = False) -> tuple[str, Any]:
        return (
            "stamp",
            {"point": point, "phase": phase or _STORE_PHASE[self._phase], "next_skills": list(next_skills), "refs": list(refs), "close": close},
        )

    def _read_context(self) -> ContextReading | None:
        """One shared reading guard for phase and plan-card advice; failures never block."""

        try:
            reading = self._context_reading() if self._context_reading is not None else None
            if (
                isinstance(reading, ContextReading)
                and type(reading.used_tokens) is int and reading.used_tokens >= 0
                and type(reading.window_tokens) is int and reading.window_tokens > 0
            ):
                return reading
        except Exception:
            pass
        return None

    def _delegation_line(
        self, plan: PlanCard, reading: ContextReading, costs: Mapping[str, int], floor: float,
    ) -> str | None:
        prefix = f"Delegation at plan card (pass {self._pass_id}): "
        if len(plan.items) <= 1 or any(stamp["point"].startswith(prefix) for stamp in self._card["stamps"]):
            return None
        estimate = len(plan.items) * sum(costs[phase] for phase in ("build", "review"))
        if reading.used_tokens + estimate > floor:
            installed = _loop_adapter_present()
            if installed is None:
                return None
            recommendation = (
                "recommend a plan to file the parts as child-of items and hand them downstream to the loop, only after the plan go."
                if installed else
                "the loop is the add-on that would take these parts; any filing or hand-off needs the plan go."
            )
            return (
                f"{prefix}{len(plan.items)} items implied; used {reading.used_tokens} + estimate {estimate} "
                f"= {reading.used_tokens + estimate} tokens > bound {floor:.0f} "
                f"({self._context_floor_fraction:g} of {reading.window_tokens}); {recommendation}"
            )
        return None

    def _write_boundary(
        self, events: list[tuple[str, Any]], boundary: str, *, remaining_from: str | None = None,
        plan: PlanCard | None = None,
    ) -> None:
        """Land an advisory stamp with this transition, then print it; never switch sessions."""

        prefix = f"Context bound after {boundary} (pass {self._pass_id}): "
        lines: list[tuple[str, tuple[str, ...]]] = []
        context_pending = not any(stamp["point"].startswith(prefix) for stamp in self._card["stamps"])
        reading = self._read_context() if context_pending or plan is not None else None
        if reading is not None:
            try:
                start = _COST_PHASES.index(remaining_from) if remaining_from else _COST_PHASES.index(boundary) + 1
                costs = load_phase_costs()
                floor = reading.window_tokens * self._context_floor_fraction
                if not math.isfinite(floor) or floor <= 0:
                    raise ValueError
                if any(type(costs[phase]) is not int or costs[phase] < 0 for phase in _COST_PHASES):
                    raise ValueError
                remaining = sum(costs[phase] for phase in _COST_PHASES[start:])
                projected = reading.used_tokens + remaining
                if context_pending and projected > floor:
                    line = (
                        f"{prefix}used {reading.used_tokens} + remaining {remaining} = {projected} tokens "
                        f"> floor {floor:.0f} ({self._context_floor_fraction:g} of {reading.window_tokens}); "
                        f"recommend handoff to a fresh session on {self.key} from its resume card {self.store.ref}."
                    )
                    lines.append((line, ("handoff", "ticket")))
                if plan is not None:
                    delegation = self._delegation_line(plan, reading, costs, floor)
                    if delegation is not None:
                        lines.append((delegation, ("ticket",)))
            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                lines = []
        if lines:
            # Advice precedes every stop; a boundary without a new stop preserves the last one.
            if self._card["stamps"] and not any(kind == "stamp" for kind, _ in events):
                events.append(("stamp", self._card["stamps"][-1]))
            events[:0] = [self._stamp(line, skills, (self.store.ref,)) for line, skills in lines]
        if events:
            self._write(events)
        for line, _ in lines:
            print(line)

    # The steps (design 3.3).

    @_ends_on_failure
    def finish_orient(self) -> None:
        self._require("orient")
        self._write_boundary([], "orient")
        self._phase = "research"

    @_ends_on_failure
    def finish_research(self) -> None:
        self._require("research")
        self._write_boundary([], "research")
        self._phase = "plan"

    @_ends_on_failure
    def present_plan(self, plan: PlanCard) -> None:
        self._require("plan")
        if tuple(plan.acceptance_criteria) != self._ticket.acceptance_criteria:
            raise TicketError("the plan card must carry the ticket's acceptance criteria, in order")
        revision = self._identity[0] + 1 if self._identity else 1
        snapshot = plan.snapshot(revision, self._pass_id, self._ticket)
        point = PLAN_GO_POINT.format(revision=revision)
        self._write_boundary([("plan", snapshot), self._stamp(point, ("ticket",), (self.store.ref,))], "plan", plan=plan)
        self._identity = (revision, tuple(plan.acceptance_criteria))
        plan.review = None
        self._plan = plan
        self._sync_steps()
        self._phase = "plan_go"

    def _bind_recorded_go(
        self, gate: str, revision: int, decision: Mapping[str, Any], *, declined: bool = False,
    ) -> dict[str, Any]:
        """Bind the landed go to a captured utterance, or fail closed. Never raises.

        The binder gets the token held after the note landed. The pass token
        advances only when the item's events after it are exactly the events
        the binder wrote, so the binder never leaves the pass unable to take
        its next step because of its own writes, and any other write in the
        bind window still reads as stale.
        """

        bind = getattr(self.store, "bind_go", None)
        if not callable(bind):
            return {"bound": False, "reason": "no ledger"}
        started = (self._card.get("pass") or {}).get("started_at")
        if not _is_text(started):
            return {"bound": False, "reason": "no matching utterance"}
        try:
            result = bind(
                gate=gate,
                revision=revision,
                verbatim=decision["verbatim"],
                evidence=decision["evidence"],
                phase=decision["phase"],
                effect=decision["effect"],
                started_at=started,
                token=self._token,
                **({"declined": True} if declined else {}),
            )
        except Exception:
            return {"bound": False, "reason": "elect refused"}
        if type(result) is not dict:
            return {"bound": False, "reason": "not bound"}
        token = result.pop("pass_token", None)
        if type(token) is str and token.isdigit():
            self._token = token
        if result.get("bound") is True:
            return result
        reason = result.get("reason")
        return {"bound": False, "reason": reason if _is_text(reason) else "not bound"}

    @_ends_on_failure
    def record_go(self, gate: str, *, evidence: str, verbatim: str) -> dict[str, Any]:
        if gate not in GATES:
            raise TicketError(f"unknown gate: {gate}")
        self._require("plan_go" if gate == "plan" else "outward_go")
        if not _is_text(evidence) or not _is_text(verbatim):
            raise TicketError("a go record requires evidence and the person's verbatim words")
        revision, criteria = self._identity
        if gate == "outward" and _declines(self._card, self._identity):
            raise TicketError(_answered(revision))
        decision = {
            "who": "the person",
            "who_kind": "person",
            "standing": "settled",
            "source": {"what": "ratification", "ref": evidence.strip()},
            "effect": f"The {gate} gate is approved for plan revision {revision}.",
            "tags": [{"type": "ticket", "id": self.key}],
            "verbatim": verbatim.strip(),
            "phase": _STORE_PHASE[self._phase],
            "evidence": evidence.strip(),
            "gate": gate,
            "plan_revision": revision,
            "criteria": list(criteria),
        }
        if gate == "plan":
            decision["pass"] = self._pass_id
        else:
            decision["round"] = self._card["reviews"][-1]["round"]
        self._write([("decision", decision)])
        return self._bind_recorded_go(gate, revision, decision)

    @_ends_on_failure
    def record_role_go(
        self, gate: str, *, who: str, relayed_by: str, plan_revision: int, evidence: str, verbatim: str,
    ) -> dict[str, Any]:
        """Record the current controller's answer as one unbound decision, never an election."""

        if gate not in GATES:
            raise TicketError(f"unknown gate: {gate}")
        self._require("plan_go" if gate == "plan" else "outward_go")
        if not _is_text(evidence) or not _is_text(verbatim) or not _is_text(relayed_by):
            raise TicketError("a role go record requires evidence, verbatim words and relayed_by")
        if gate == "outward" and _declines(self._card, self._identity):
            raise TicketError(_answered(self._identity[0]))
        card = self._fresh()
        handoff = card.get("handoff")
        controller = handoff.get("controller") if isinstance(handoff, Mapping) else None
        if not _is_handoff_name(controller):
            if isinstance(self.store, LedgerStore) and self.store._role_go_problem is not None:
                raise self.store._role_go_problem
            raise _role_go_refused("role_go_no_handoff", self.key, "there is no open hand-off naming a controller")
        if type(who) is not str or who != controller:
            raise _role_go_refused("role_go_not_controller", self.key, f"role {who!r} is not controller {controller!r}")
        revision, criteria = _identity(card)
        if type(plan_revision) is not int or plan_revision != revision:
            raise _role_go_refused(
                "role_go_other_revision", self.key, f"answer names revision {plan_revision!r}, card has revision {revision}",
            )
        decision = {
            "who": who,
            "who_kind": "role",
            "relayed_by": relayed_by.strip(),
            "standing": "settled",
            "source": {"what": "ratification", "ref": evidence.strip()},
            "effect": f"The {gate} gate is approved for plan revision {revision}.",
            "tags": [{"type": "ticket", "id": self.key}],
            "verbatim": verbatim.strip(),
            "phase": _STORE_PHASE[self._phase],
            "evidence": evidence.strip(),
            "gate": gate,
            "plan_revision": revision,
            "criteria": list(criteria),
        }
        if gate == "plan":
            decision["pass"] = self._pass_id
        else:
            decision["round"] = card["reviews"][-1]["round"]
        self._write([("decision", decision)])
        return {"bound": False, "reason": "role go"}

    @_ends_on_failure
    def approved(self, gate: str) -> bool:
        if gate not in GATES:
            raise TicketError(f"unknown gate: {gate}")
        self._require(*_STORE_PHASE)
        card = self._fresh()
        if gate == "outward" and self._phase != "outward_go":
            # F4: once a change sends the pass back to plan, the outward go of
            # the stored plan authorizes nothing.
            return False
        if self._identity is None:
            return False
        if gate == "outward":
            return _outward_open(card, self._identity, self.key)
        return _has_go(card, gate, self._identity, self.key)

    @_ends_on_failure
    def begin_build(self) -> None:
        self._require("plan_go")
        card = self._fresh()
        if not _has_go(card, "plan", self._identity, self.key):
            raise GateRequired("a plan go is required before build")
        self._phase = "build"

    @_ends_on_failure
    def record_build(
        self, model: Any, *, harness: Any, requested: Any = None, source: Any = "seat",
    ) -> dict[str, Any]:
        """Write who built this round as an item fact, from the build result: ``model`` is the model it names as served.

        ``requested`` is the model asked for (None when the result names none, written as ``-``), ``harness`` a
        label for the harness that ran the build, and ``source`` is ``envelope`` when the identity came from a
        lane-result envelope, else ``seat``. The round is the review round this build goes into. Only the ledger
        store writes it: one ``item.note`` under ``row-item:builder:<item>:r<round>:<model>`` in the text
        ``row-item.sh facts`` counts. Fail closed: a result with no served model, a value outside the toolkit's
        grammar, a store with no ledger, or a second fact for the same round and model with other text, whether
        this handle wrote the first or an earlier handle did (the ledger answering a key conflict), writes
        nothing and returns ``written: False`` with the reason; it never raises for them and the pass stays
        live. A model is never invented. The same fact again writes nothing more. The item is read fresh first,
        and a note already under the key is never appended again, so a replay never moves this handle's revision;
        a write that does go out is the ordinary one, under the held token, and a stale handle is refused as for
        any other write.
        """

        self._require("build")
        self._write_boundary([], "build")
        if not _is_text(model):
            return {"written": False, "reason": "the build result names no served model"}
        fact = {
            "round": self._rounds + 1, "model": model, "requested": "-" if requested is None else requested,
            "harness": harness, "source": source,
        }
        try:
            _validate_builder(fact)
        except TicketError as exc:
            return {"written": False, "reason": str(exc)}
        if not isinstance(self.store, LedgerStore):
            return {"written": False, "reason": "no ledger"}
        key, text = _builder_key(self.key, fact), _builder_text(fact)
        other = f"a builder fact for round {fact['round']} and model {model} is already recorded with other text"
        stored = self.store.note_text(key)
        if stored is not None:
            # An earlier handle wrote it. Appending would replay it, and a replay answers an event this handle has not validated.
            if stored == text:
                return {"written": True, "key": key}
            return {"written": False, "reason": other}
        held, events = self._token, [("builder", fact)]
        try:
            token = self.store.apply(held, events)
        except StoreUnavailable as exc:
            if exc.ledger_code == "conflict":
                # A handle wrote the key with other text after the read above; the batch holds only this note.
                return {"written": False, "reason": other}
            raise
        if not self.store.directly_follows(held, token):
            # A handle wrote the same fact after the read above, so the ledger replayed it without a revision check. Its id
            # is not this pass's revision: events this pass has not read lie between, and the held token stays, so the
            # next write is refused as stale.
            raise _stale(self.key, "a builder fact written since the last read was replayed past events this pass has not read")
        self._advance(token, events)
        return {"written": True, "key": key}

    @_ends_on_failure
    def record_review(
        self,
        verdict: str,
        *,
        evidence: str,
        passed: Sequence[int] = (),
        findings: Sequence[str] = (),
        reviewer: str = "fresh-context reviewer",
    ) -> None:
        self._require("build", "outward_go")
        if self._phase == "outward_go":
            card = self._fresh()
            if not card.get("pr") or verdict != "changes" or self._rounds >= MAX_REVIEW_ROUNDS:
                raise TicketError("late_review_refused: only changes on a recorded pull request may start a fix round below the cap")
        revision, criteria = self._identity
        number = self._rounds + 1
        review = {
            "round": number,
            "verdict": verdict,
            "plan_revision": revision,
            "criteria": list(criteria),
            "passed": list(passed),
            "findings": list(findings),
            "reviewer": reviewer,
            "evidence": evidence,
        }
        _validate_review(review)
        review["passed"].sort()
        if verdict == "pass" and review["passed"] != list(range(len(criteria))):
            raise TicketError("a passing review must list every acceptance criterion")
        events: list[tuple[str, Any]] = [("review", review)]
        refs = (self.store.ref, review["evidence"])
        if verdict == "pass":
            events.append(self._stamp(OUTWARD_GO_POINT.format(round=number), ("ticket",), refs, phase="review"))
        elif number == MAX_REVIEW_ROUNDS:
            events.append(self._stamp(CAP_POINT, ("ticket", "review"), refs, phase="review"))
        if verdict == "pass" or number < MAX_REVIEW_ROUNDS:
            self._write_boundary(events, "review", remaining_from="outward" if verdict == "pass" else "build")
        else:
            self._write(events)
        self._rounds = number
        self._plan.review = review if verdict == "pass" else None
        if verdict == "pass":
            self._phase = "outward_go"
        elif number == MAX_REVIEW_ROUNDS:
            self._phase = "stopped"
            self.end()
        else:
            self._phase = "build"

    @_ends_on_failure
    def require_outward_go(self) -> None:
        """Call before each outward action; the card must hold the outward go."""

        self._require("outward_go")
        card = self._fresh()
        if not _outward_open(card, self._identity, self.key):
            raise GateRequired("an outward go is required before any outward action")

    def record_lane_receipts(
        self, documents: Sequence[str | Path], *, published_pr: str | None = None, say=print,
    ) -> None:
        """Optional section-6 hand-off; the caller supplies the publisher's returned PR URL.

        Documents belong to the lane runner, not this pass. No card event,
        retry, approval or receipt interpretation happens here.
        """
        if isinstance(self.store, MarkdownStore):
            say("review receipts not recorded: markdown store")
            return
        if not documents:
            say("review receipts not recorded: no documents handed")
            return
        if not published_pr:
            say("review receipts not recorded: pull request not published")
            return
        try:
            # A receipt failure must not invoke the ordinary pass-ending wrapper.
            TicketPass.require_outward_go.__wrapped__(self)
        except (TicketError, StoreUnavailable) as exc:
            say(f"review receipts not recorded: {_one_line(exc)}")
            return
        cli = os.environ.get("TICKET_REVIEW_GATE", "").strip()
        if not cli:
            # Same topic sources and precedence as the ledger CLI, but no PATH discovery.
            shipped, problem = _LEDGER_RULE._read_object(
                _LEDGER_RULE.CONFIG_PATH, False, "ticket", True,
            )
            override, override_problem = _LEDGER_RULE._read_object(
                Path(_LEDGER_RULE.OVERRIDE_PATH).expanduser(), False, "ticket", True,
            )
            if problem or override_problem:
                say("review receipts not recorded: topic config unreadable")
                return
            cli = override.get("review_gate", shipped.get("review_gate"))
        if cli is None or cli == "" or (isinstance(cli, str) and cli.lower() == "none"):
            say("review receipts not recorded: not configured")
            return
        if not isinstance(cli, str) or not os.path.isabs(cli) or not os.path.isfile(cli) or not os.access(cli, os.X_OK):
            say("review receipts not recorded: review-gate must be an absolute executable path")
            return
        for document in documents:
            try:
                payload = Path(document).read_bytes()
            except OSError as exc:
                say(f"review receipts not recorded: {document}: {_one_line(exc)}")
                continue
            try:
                result = subprocess.run(
                    [cli, "record"], input=payload, capture_output=True, check=False, timeout=REVIEW_RECORD_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                say(f"review receipt not recorded: {document}: timed out after {REVIEW_RECORD_TIMEOUT_SECONDS}s")
                continue
            except OSError as exc:
                say(f"review receipt not recorded: {document}: {_one_line(exc)}")
                continue
            if result.returncode:
                lines = result.stderr.decode("utf-8", errors="replace").splitlines()
                reason = lines[0] if lines else f"exit {result.returncode}"
                say(f"review receipt not recorded: {document}: {reason}")
                continue
            say(result.stdout.decode("utf-8", errors="replace"), end="")

    def _require_unmanaged(self, card: Mapping[str, Any]) -> None:
        if card.get("loop_managed"):
            raise GateRequired(f"ticket {self.key}: this step belongs to the loop; its adapter links the pull request and closes the item at merge.")

    @_ends_on_failure
    def record_pull_request(self, ref: str) -> None:
        """Record the published PR and review stop atomically; replaying that reference writes nothing."""

        self._require("outward_go")
        card = self._fresh()
        if not _outward_open(card, self._identity, self.key):
            raise GateRequired("an outward go is required before recording a pull request")
        self._require_unmanaged(card)
        _validate_event("pr", ref, self.key)
        if card.get("pr") == ref:
            return
        if card.get("pr") is not None:
            raise TicketError(f"pr_conflict: ticket {self.key} already records {card['pr']}")
        self._write([("pr", ref), self._stamp(
            f"Pull request {ref} recorded; waiting for merge or close.", ("ticket",), (ref,), phase="review",
        )])

    @_ends_on_failure
    def close(
        self, point: str, *, refs: Sequence[str] = (), next_skills: Sequence[str] = (),
        worktree: str | Path | None = None,
    ) -> str | None:
        """Close only after the recorded pull request reads merged or closed live.

        Then the builder's ``worktree``, when given, is named and kept
        (``tidy_worktree``); the line it returns is for the close output.
        """

        self._require("outward_go")
        card = self._fresh()
        if not _outward_open(card, self._identity, self.key):
            raise GateRequired("an outward go is required before close")
        ref = card.get("pr")
        if ref is None:
            raise TicketError(f"pr_required: ticket {self.key} has no recorded pull request")
        self._require_unmanaged(card)
        # Validate caller input before adding the mandatory close evidence.
        _validate_stamp(self._stamp(point, next_skills, refs, phase="close", close=True)[1])
        state = pull_request_state(ref)
        if state not in ("MERGED", "CLOSED"):
            raise TicketError(f"pr_open: ticket {self.key}: pull request {ref} is open or unreadable")
        outcome = "merged" if state == "MERGED" else "closed unmerged"
        close_point = f"{point} Pull request {ref}: {outcome}."
        close_refs = tuple(refs) if ref in refs else (*refs, ref)
        self._write([self._stamp(close_point, next_skills, close_refs, phase="close", close=True), ("closed", None)])
        self._phase = "closed"
        self.end()
        return tidy_worktree(worktree)

    @_ends_on_failure
    def record_outward_declined(self, *, evidence: str, verbatim: str) -> dict[str, Any]:
        """Record the person's no at the outward go, in their own words, then bind it as a go is bound.

        The decline is a ticket decision of its own (``declined: outward``,
        never a ``gate``), so no reader counts it as a go, and once it covers
        the plan no outward action is allowed. Binding never gates: the result
        says whether it bound, as ``record_go``'s does.
        """

        self._require("outward_go")
        if not _is_text(evidence) or not _is_text(verbatim):
            raise TicketError("a declined go record requires evidence and the person's verbatim words")
        revision, criteria = self._identity
        if _has_go(self._card, "outward", self._identity, self.key) or _declines(self._card, self._identity):
            raise TicketError(_answered(revision))
        decision = {
            "who": "the person",
            "who_kind": "person",
            "standing": "settled",
            "source": {"what": "ratification", "ref": evidence.strip()},
            "effect": f"The outward gate is declined for plan revision {revision}; the work stays local.",
            "tags": [{"type": "ticket", "id": self.key}],
            "verbatim": verbatim.strip(),
            "phase": _STORE_PHASE[self._phase],
            "evidence": evidence.strip(),
            "declined": "outward",
            "plan_revision": revision,
            "criteria": list(criteria),
        }
        self._write([("decision", decision)])
        return self._bind_recorded_go("outward", revision, decision, declined=True)

    @_ends_on_failure
    def close_local(
        self, *, refs: Sequence[str] = (), next_skills: Sequence[str] = (), worktree: str | Path | None = None,
    ) -> str | None:
        """Close kept local after the person declined the outward go: nothing was pushed or opened.

        One write holds the kept-local close stamp (``KEPT_LOCAL_POINT``) and
        the closed status; then the pass ends and the builder's ``worktree``
        is named and kept, as ``close`` does.
        """

        self._require("outward_go")
        card = self._fresh()
        self._require_unmanaged(card)
        if not _has_declined(self._card, self._identity, self.key) or _has_go(self._card, "outward", self._identity, self.key):
            raise GateRequired("a kept-local close requires the person's declined outward go and no outward go")
        point = KEPT_LOCAL_POINT.format(revision=self._identity[0])
        self._write([self._stamp(point, next_skills, refs, phase="close", close=True), ("closed", None)])
        self._phase = "closed"
        self.end()
        return tidy_worktree(worktree)

    @_ends_on_failure
    def record_tried(self, what: str, *, outcome: str | None = None, evidence: str | None = None) -> None:
        self._require(*_STORE_PHASE)
        tried = {"what": what, "phase": _STORE_PHASE[self._phase]}
        if outcome is not None:
            tried["outcome"] = outcome
        if evidence is not None:
            tried["evidence"] = evidence
        self._write([("tried", tried)])

    @_ends_on_failure
    def more_info(self, destination: str, question: str, *, role: str | None = None) -> MoreInfoExit:
        self._require(*_STORE_PHASE)
        if destination not in INFO_EXITS:
            raise TicketError(f"unknown more-information exit: {destination}")
        if not _is_text(question):
            raise TicketError("a more-information exit requires the exact question")
        question = question.strip()
        drafted = None
        if destination == "someone_else":
            if not _is_text(role):
                raise TicketError("the someone_else exit requires a role")
            waiting = f"waiting on {role.strip()}: {question}"
            drafted = f"Could you help with {self.key}? {question}"
        else:
            waiting = f"waiting on {destination}: {question}"
        refs = (self.store.ref,) if self._identity else ()
        self._write([self._stamp(waiting, ("ticket",), refs)])
        self.end()
        return MoreInfoExit(destination, waiting, drafted)


# --- What the outward go may propose, and the builder's worktree at close ---

NO_REMOTE_LINE = (
    "the repository has no remote, so nothing can be pushed and no pull request opened; "
    "offer to close the pass kept local, which needs the person's own words"
)


@dataclass(frozen=True)
class OutwardRoute:
    """The repository's remotes as ``git remote`` listed them at the outward go.

    ``remotes`` is the names, or None when the list could not be read. Only a
    list read as empty is no remote: an unreadable list counts as remotes
    present, so the outward go stops as usual and nothing closes kept local
    on its own.
    """

    remotes: tuple[str, ...] | None
    detail: str = ""

    @property
    def propose_push(self) -> bool:
        return self.remotes != ()

    @property
    def line(self) -> str:
        if self.remotes == ():
            return NO_REMOTE_LINE
        if self.remotes is None:
            return (
                f"the repository's remotes could not be read ({self.detail}), so it counts as having one "
                "and the outward go stops as usual"
            )
        return f"remotes: {', '.join(self.remotes)}"


def outward_route(repo: str | Path) -> OutwardRoute:
    """Read the repository's remotes live, before the outward go is presented."""

    try:
        result = subprocess.run(["git", "-C", str(repo), "remote"], capture_output=True, text=True, check=False)
    except (OSError, ValueError) as exc:
        return OutwardRoute(None, _one_line(exc))
    if result.returncode != 0:
        return OutwardRoute(None, _one_line(result.stderr) or f"git remote exited {result.returncode}")
    return OutwardRoute(tuple(line.strip() for line in result.stdout.splitlines() if line.strip()))


def pull_request_state(ref: str) -> str:
    """Read gh's live JSON state; a missing reader, failed command or unreadable answer is OPEN."""

    if type(ref) is not str or not _PULL_REQUEST.fullmatch(ref):
        return "OPEN"
    repository, number = ref.split("#")
    try:
        result = subprocess.run(
            ["gh", "pr", "view", number, "--repo", repository, "--json", "state"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            return "OPEN"
        answer = json.loads(result.stdout)
        state = answer.get("state") if type(answer) is dict else None
        return state if state in ("MERGED", "CLOSED") else "OPEN"
    except (OSError, ValueError):
        return "OPEN"


def _git_line(path: str, *args: str) -> tuple[int, str, str]:
    result = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, _one_line(result.stderr)


def _symlink_in(path: str) -> str | None:
    """The first component of ``path`` that is a symbolic link, or None when none is.

    Read component by component on the path as given, made absolute with its
    ``..`` kept, so no lexical collapse skips a link.
    """

    parts = Path(os.path.join(os.getcwd(), path)).parts
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        if os.path.islink(current):
            return str(current)
    return None


def worktree_remove_command(path: str | Path) -> str:
    """The command the person may run to remove a worktree: every argv element shell-quoted, never forced."""
    return shlex.join(["git", "worktree", "remove", str(path)])


def _status_listing(changes: str) -> str:
    """What ``git status --porcelain --ignored --untracked-files=all`` listed, in one short clause."""

    entries = [entry for entry in changes.splitlines() if entry.strip()]
    if not entries:
        return "git status listed nothing"
    untracked = sum(entry.startswith("?? ") for entry in entries)
    ignored = sum(entry.startswith("!! ") for entry in entries)
    changed = len(entries) - untracked - ignored
    shown = "; ".join(entries[:3]) + ("; ..." if len(entries) > 3 else "")
    noun = "entry" if len(entries) == 1 else "entries"
    return (
        f"git status listed {len(entries)} {noun} ({changed} changed, {untracked} untracked, "
        f"{ignored} ignored): {shown}"
    )


def tidy_worktree(path: str | Path | None) -> str | None:
    """Name the builder's worktree for the close output; never remove, move or write anything.

    Never raises. The line names the path as given, whether it sits under the
    repository's ``.claude/worktrees``, its branch (left in place, not
    deleted), and what ``git status --porcelain --ignored
    --untracked-files=all`` listed there, read with ``--no-optional-locks`` so
    git does not refresh the worktree's index. It ends with the command the
    person may run, ``git worktree remove <path>`` with the given path
    shell-quoted and never ``--force``; whether to run it is the person's
    call. A path that goes through a symbolic link is named with the link and
    no command, since the link could name a different worktree; so is a path
    whose links could not be checked. A missing path is named as not found.
    None when no worktree was given.
    """

    if path is None:
        return None
    shown = str(path)
    command = f"to remove it, run `{worktree_remove_command(shown)}`"
    place = ""
    try:
        where = Path(os.path.abspath(shown))
        if where.parent.name == "worktrees" and where.parent.parent.name == ".claude":
            place = " (under the repository's .claude/worktrees)"
        link = _symlink_in(shown)
    except (OSError, ValueError) as exc:
        return (
            f"builder worktree {shown} is kept: its path could not be checked ({_one_line(exc)}), "
            "so no removal command is given; nothing was removed"
        )
    head = f"builder worktree {shown}{place}"
    if link is not None:
        return (
            f"{head} is kept: its path goes through a symbolic link ({link}), which could name a different "
            "worktree, so no removal command is given; nothing was removed"
        )
    try:
        if not where.is_dir():
            return f"{head} was not found; nothing was removed"
        status, branch, _ = _git_line(shown, "--no-optional-locks", "symbolic-ref", "-q", "--short", "HEAD")
        branch = branch.strip()
        if status == 0 and branch:
            branch_part = f"its branch {branch} is left in place, not deleted"
        elif status == 1:
            branch_part = "it is not on a branch"
        else:
            branch_part = "its branch could not be read"
        # Every untracked and every ignored file shows, whatever the repository's status settings.
        status, changes, said = _git_line(
            shown, "--no-optional-locks", "status", "--porcelain", "--ignored", "--untracked-files=all",
        )
        listed = _status_listing(changes) if status == 0 else f"git status could not be read ({said or 'git status failed'})"
    except (OSError, ValueError) as exc:
        branch_part, listed = "its branch could not be read", f"git status could not be read ({_one_line(exc)})"
    return f"{head} is kept; {branch_part}; {listed}; nothing was removed; {command}"


# --- Importing markdown cards into the ledger (design 6.4) ------------------

IMPORTED_DIRECTORY = "imported"
_WALK_SKIPPED = frozenset((".git", "node_modules", ".ticket"))


def import_command(repo: str | Path) -> str:
    """The command that imports one repository's markdown cards: every argv element shell-quoted."""
    return shlex.join(["python3", _SCRIPT, "import-cards", str(repo)])


def _open_ticket_directory(repo: Path) -> int | None:
    """``<repo>/.ticket`` opened without following a link, or None when there is none."""

    try:
        return os.open(repo / ".ticket", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None


def _card_names(directory: int, *, regular_only: bool) -> list[str]:
    """The ``*.md`` entries directly in ``.ticket/`` (read through ``directory``), sorted; never ``imported/``."""

    names = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".md"):
            continue
        if regular_only:
            try:
                if not stat.S_ISREG(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
                    continue
            except OSError:
                continue
        names.append(name)
    return names


def _leftover_cards(repo: Path) -> list[str]:
    """The names of the markdown cards a repository holds that are not imported; empty when it cannot be read."""

    try:
        directory = _open_ticket_directory(repo)
        if directory is None:
            return []
        try:
            return _card_names(directory, regular_only=True)
        finally:
            os.close(directory)
    except OSError:
        return []


def _root_spelling(root: str | Path) -> str:
    """``root`` without trailing separators or trailing ``.`` segments, and nothing else changed.

    ``..`` is never collapsed: the OS resolves ``dir/link/..`` through the link, so a lexical collapse
    would name a different directory.
    """

    text = os.fspath(root)
    while True:
        trimmed = text.rstrip(os.sep)
        if not trimmed:
            return os.sep if text else text
        if trimmed == ".":
            return trimmed
        if not trimmed.endswith(os.sep + "."):
            return trimmed
        text = trimmed[:-2] or os.sep


def find_cards(roots: Sequence[str | Path]) -> list[str]:
    """Every git repository under ``roots`` whose ``.ticket/`` holds an unimported card, as absolute paths.

    Links are not followed. ``.git``, ``node_modules`` and ``.ticket`` (so
    ``.ticket/imported``) are not entered. Each path names the directory the OS resolves for the root.
    """

    found: set[str] = set()
    for root in roots:
        root = _root_spelling(root)  # one spelling for the check and the walk
        if os.path.islink(root):
            continue  # a root that is itself a link is not followed either
        for current, names, files in os.walk(root, followlinks=False):
            if ".git" in names or ".git" in files:
                if ".ticket" in names and _leftover_cards(Path(current)):
                    found.add(os.path.realpath(current))  # nothing under the root is a link; no lexical `..` collapse
            names[:] = [name for name in names if name not in _WALK_SKIPPED]
    return sorted(found)


class _Skip(Exception):
    """A card that was left in place; the message is the reason printed after its key."""


def _import_ignored(store: MarkdownStore) -> None:
    """The card, its lock and ``.ticket/imported/<key>.md`` must all be git-ignored; no ignore file is edited."""

    try:
        store._check_ignored()
    except StoreUnavailable as exc:
        raise _Skip(_one_line(exc)) from exc
    moved = Path(".ticket") / IMPORTED_DIRECTORY / store.path.name
    result = subprocess.run(
        ["git", "-C", str(store.repo), "check-ignore", "--quiet", "--", moved.as_posix()],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise _Skip(
            f"{moved.parent.as_posix()}/ is not git-ignored; add `.ticket/` to the repository's ignore rules and "
            "retry. No ignore file was changed and nothing was written"
        )


def _import_lock(store: MarkdownStore, directory: int, dry_run: bool) -> int | None:
    """The card's lock, taken without waiting; a lock another process holds skips the card.

    A dry run opens an existing lock for reading and never creates one.
    """

    if dry_run:
        try:
            fd = os.open(store.lock_path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _Skip(f"the card's lock cannot be opened ({exc})") from exc
    else:
        try:
            fd = store._open_lock(directory)
        except StoreUnavailable as exc:
            raise _Skip(_one_line(exc)) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Skip("the card's lock is not a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException as exc:
        os.close(fd)
        if isinstance(exc, BlockingIOError):
            raise _Skip("the card's lock is held by another process (a live pass may be writing it)") from exc
        raise
    return fd


def _frame_version(store: MarkdownStore, directory: int) -> str | None:
    """The version of a ``ticket-card:v<N>`` frame in the card when N is not 2, read without following a link."""

    try:
        fd = os.open(store.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as handle:
            found = re.search(rb"<!-- ticket-card:v([0-9]+)", handle.read())
    except OSError:
        return None
    return found[1].decode() if found and found[1] != b"2" else None


def _import_read(store: MarkdownStore, directory: int) -> dict[str, Any]:
    """The card through the markdown store's own reader and the validators a write runs, or the reason it is skipped."""

    if store._card_mode(directory) is None:
        raise _Skip("the card is gone")
    try:
        card, _ = store._load(directory)
        if card["plan"] is not None:
            _validate_event("plan", card["plan"], store.key)
        for kind, name in (("decision", "decisions"), ("review", "reviews"), ("tried", "tried"), ("stamp", "stamps")):
            for record in card[name]:
                _validate_event(kind, record, store.key)
    except Exception as exc:  # fail closed: whatever the reader or a validator cannot take is left in place
        version = _frame_version(store, directory)
        if version is not None:
            raise _Skip(f"unknown card frame version v{version}; only ticket-card:v2 cards import") from exc
        raise _Skip(f"malformed: {_one_line(exc)}") from exc
    return card


def _import_batch(card: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The events of one ledger item that replay as ``card`` (design 6.4), keyed by the card's content.

    Every key is derived from the SHA-256 of the card's canonical JSON, so the
    same card is always the same batch and a rerun replays it.
    """

    key, closed, stamps = card["key"], card["status"] == "closed", card["stamps"]
    digest = hashlib.sha256(_canonical(card).encode("utf-8")).hexdigest()
    if closed and card["pass"] is not None:
        raise _Skip("a closed card holds a live pass, which the ledger cannot replay")
    for index, stamp in enumerate(stamps):
        if stamp["close"] and not (closed and index == len(stamps) - 1 and stamp["phase"] == "close"):
            raise _Skip("a close stamp is not the last stamp of a closed card, which the ledger cannot replay")
    trusted = bool(stamps) and closed and stamps[-1]["close"]
    element = LedgerStore("-", key, card["title"])._element
    sequence = iter(range(len(stamps) + len(card["decisions"]) + len(card["reviews"]) + len(card["tried"]) + 8))

    def ordinary() -> str:
        return f"ticket-import/{key}/{digest[:32]}/{next(sequence)}"

    batch = [element("item.created", {"type": "ticket", "title": card["title"]}, f"ticket-item/{key}/created")]
    if card["pass"] is not None:
        live = card["pass"]
        purpose = f"ticket pass {live['id']} on {live['host']} at {live['started_at']}"
        batch.append(element("session.opened", {"purpose": purpose}, f"ticket-pass/{key}/{live['id']}/claimed"))
    if card["plan"] is not None:
        batch.append(element("item.note", {"text": f"ticket-plan:v{_RECORD_VERSION} {_canonical(card['plan'])}"}, ordinary()))
    for kind, name in (("decision", "decisions"), ("review", "reviews")):
        for record in card[name]:
            batch.append(element("item.note", {"text": f"ticket-{kind}:v{_RECORD_VERSION} {_canonical(record)}"}, ordinary()))
    for record in card["tried"]:
        batch.append(element("item.tried", dict(record), ordinary()))
    close = f"ticket-close/{key}/{digest[:16]}/{digest[16:32]}"
    for stamp in stamps:
        payload = {"point": stamp["point"], "phase": stamp["phase"], "skills": stamp["next_skills"], "refs": stamp["refs"]}
        batch.append(element("item.stopped", payload, f"{close}/stop" if stamp["close"] else ordinary()))
    if closed:
        batch.append(element("item.state", {"status": "closed"}, f"{close}/state" if trusted else ordinary()))
    replayed = [{"id": number, **event} for number, event in enumerate(batch, 1)]
    try:
        card_back, _, _ = _ledger_card(replayed, key, card["title"])
    except StoreUnavailable as exc:
        raise _Skip(f"the card cannot be replayed through the ledger mapping: {_one_line(exc)}") from exc
    if card_back != card:
        differing = ", ".join(name for name in card if card_back.get(name) != card[name])
        raise _Skip(f"the card cannot be replayed through the ledger mapping (differs in {differing})")
    return batch


def _import_existing(ledger: LedgerStore, key: str) -> list[dict[str, Any]] | None:
    """The events of the ledger item ``key``, or None when there is no such item; anything unclear skips the card."""

    answer = ledger._run("query", "--item", key)
    if answer.ok is False and answer.code in _LEDGER_NO_ITEM:
        return None
    try:
        if not answer.ok or not any(row["item_id"] == key for row in answer.data["items"]):
            raise _Skip(f"the ledger's answer about the item is unclear ({answer.code or answer.output})")
        export = ledger._run("export-item", key)
        if not export.ok:
            raise _Skip(f"the ledger's export of the item is unclear ({export.code or export.output})")
        return list(export.data["events"])
    except (KeyError, TypeError) as exc:
        raise _Skip("the ledger's answer about the item is unclear") from exc


def _same_history(events: Sequence[Mapping[str, Any]], batch: Sequence[Mapping[str, Any]]) -> bool:
    def shape(items: Sequence[Mapping[str, Any]]) -> list[tuple[Any, Any, str]]:
        return [(item.get("idempotency_key"), item.get("kind"), _canonical(item.get("payload"))) for item in items]

    return shape(events) == shape(batch)


def _same_file(directory: int, moved: int, name: str) -> bool:
    """Whether ``.ticket/imported/<name>`` is the card itself (same st_dev and st_ino, no link followed).

    That is a move an earlier run linked and stopped before the unlink; a stat error is a skip that leaves both names.
    """

    try:
        card = os.stat(name, dir_fd=directory, follow_symlinks=False)
        copy = os.stat(name, dir_fd=moved, follow_symlinks=False)
    except OSError as exc:
        raise _Skip(f".ticket/{IMPORTED_DIRECTORY}/{name} cannot be compared with the card ({exc}), so both were left as they are") from exc
    return (card.st_dev, card.st_ino) == (copy.st_dev, copy.st_ino)


def _imported_directory(directory: int, name: str, *, create: bool = True) -> int | None:
    """``.ticket/imported/`` created if need be and opened through ``directory`` without following a link; the card's new name must be free.

    The name may also already be the card itself (a move stopped between the link and the unlink); the caller finishes it.
    With ``create`` false nothing is created: None comes back when the directory does not exist yet.
    """

    if create:
        try:
            os.mkdir(IMPORTED_DIRECTORY, 0o700, dir_fd=directory)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _Skip(f".ticket/{IMPORTED_DIRECTORY}/ cannot be created ({exc})") from exc
    try:
        moved = os.open(IMPORTED_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    except FileNotFoundError:
        if not create:
            return None
        raise _Skip(f".ticket/{IMPORTED_DIRECTORY}/ is not a plain directory (it vanished)") from None
    except OSError as exc:
        raise _Skip(f".ticket/{IMPORTED_DIRECTORY}/ is not a plain directory ({exc})") from exc
    try:
        os.stat(name, dir_fd=moved, follow_symlinks=False)
    except FileNotFoundError:
        return moved
    except OSError as exc:
        os.close(moved)
        raise _Skip(f".ticket/{IMPORTED_DIRECTORY}/{name} cannot be checked ({exc})") from exc
    try:
        if _same_file(directory, moved, name):
            return moved
    except _Skip:
        os.close(moved)
        raise
    os.close(moved)
    raise _Skip(f".ticket/{IMPORTED_DIRECTORY}/{name} already exists, so the card was not moved and nothing was written")


def _import_card(repo: Path, name: str, cli: str, dry_run: bool) -> str:
    """Import one card; returns its output line, or raises ``_Skip`` with the reason it was left in place."""

    key = name[:-3]
    if not _KEY.fullmatch(key):
        raise _Skip("the file name is not a safe ticket key")
    store = MarkdownStore(repo, key, key)
    try:
        store._inside_repo()
        directory = store._open_directory()
    except StoreUnavailable as exc:
        raise _Skip(_one_line(exc)) from exc
    except OSError as exc:
        raise _Skip(f".ticket cannot be opened without following a link ({exc})") from exc
    try:
        # Before the lock: the lock file is not created in a repository that is refused.
        _import_ignored(store)
        lock = _import_lock(store, directory, dry_run)
        try:
            return _import_locked(store, directory, name, cli, dry_run)
        finally:
            if lock is not None:
                os.close(lock)
    finally:
        os.close(directory)


def _import_locked(store: MarkdownStore, directory: int, name: str, cli: str, dry_run: bool) -> str:
    key = store.key
    card = _import_read(store, directory)
    batch = _import_batch(card)
    ledger = LedgerStore(cli, key, card["title"])
    events = _import_existing(ledger, key)
    if events is not None and not _same_history(events, batch):
        raise _Skip("ledger item exists with different history")
    if dry_run:
        # The real run's destination check, without creating the directory.
        free = _imported_directory(directory, name, create=False)
        if free is not None:
            os.close(free)
        return f"already imported {key}" if events is not None else f"would import {key}"
    moved = _imported_directory(directory, name)
    assert moved is not None
    try:
        if events is None:
            try:
                ledger._send(batch, NO_TOKEN)
            except StoreUnavailable as exc:
                raise _Skip(f"the ledger did not take the card ({exc.code}): {_one_line(exc)}") from exc
        try:
            replayed, _ = LedgerStore(cli, key, card["title"]).read()
        except (StoreUnavailable, StopIteration, KeyError, TypeError) as exc:
            raise _Skip(f"the ledger item could not be read back, so the card was left in place: {_one_line(exc)}") from exc
        if replayed != card:
            raise _Skip("the ledger item reads differently from the card, so the card was left in place")
        try:
            # Published without replacement: link fails if the name exists; only the card itself there finishes the move.
            os.link(name, name, src_dir_fd=directory, dst_dir_fd=moved, follow_symlinks=False)
        except FileExistsError as exc:
            if not _same_file(directory, moved, name):
                raise _Skip("imported copy already exists") from exc
        except NotImplementedError as exc:
            raise _Skip("this platform cannot link between directory descriptors, so the card was left in place") from exc
        except OSError as exc:
            raise _Skip(
                f"the card is in the ledger but could not be moved aside ({exc}); run the import again to retry the move"
            ) from exc
        try:
            os.unlink(name, dir_fd=directory)
        except OSError as exc:
            raise _Skip(
                f"the card is in the ledger and linked as .ticket/{IMPORTED_DIRECTORY}/{name}, but .ticket/{name} could not be "
                f"removed ({exc}); run the import again to finish the move"
            ) from exc
    finally:
        os.close(moved)
    return f"already imported {key}" if events is not None else f"imported {key}"


def import_repo(repo: str | Path, cli: str, *, dry_run: bool = False, say=print) -> int:
    """Import every markdown card of one repository, one output line each; returns how many were skipped."""

    repo = Path(repo).resolve()
    skipped = 0
    try:
        directory = _open_ticket_directory(repo)
    except OSError as exc:
        say(f"skipped .ticket: it cannot be opened without following a link ({exc})")
        return 1
    if directory is None:
        return 0
    try:
        names = _card_names(directory, regular_only=False)
    finally:
        os.close(directory)
    for name in names:
        try:
            say(_import_card(repo, name, cli, dry_run))
        except _Skip as exc:
            skipped += 1
            say(f"skipped {name[:-3]}: {_one_line(exc)}")
        except Exception as exc:  # fail closed: the card stays and the other cards still import
            skipped += 1
            say(f"skipped {name[:-3]}: unexpected {type(exc).__name__}: {_one_line(exc)}")
    return skipped


__all__ = [
    "SEAMS",
    "CONTEXT_FLOOR_FRACTION",
    "ContextReading",
    "load_phase_costs",
    "FileTicketSource",
    "GateRequired",
    "LedgerStore",
    "MarkdownStore",
    "MoreInfoExit",
    "PlanCard",
    "Store",
    "StoreUnavailable",
    "Ticket",
    "TicketError",
    "TicketHeld",
    "TicketPass",
    "TicketSource",
    "choose_store",
    "seam_set",
    "select_store",
    "find_cards",
    "import_command",
    "import_repo",
]


def _import_cards_command(repos: Sequence[str], dry_run: bool) -> int:
    """Exit 0 when every card imported or was already imported, 1 when any was skipped, 2 on usage or a store refusal."""

    try:
        chosen = select_store()
    except StoreUnavailable as exc:
        print(f"ticket.py: {exc}", file=sys.stderr)
        return 2
    if chosen.cli is None:
        print(f"ticket.py: import-cards needs the ledger store, and {chosen.line} was chosen. No card was read.", file=sys.stderr)
        return 2
    cli = chosen.cli
    missing = [repo for repo in repos if not os.path.isdir(repo)]
    if missing:
        print(f"ticket.py: not a directory: {', '.join(missing)}. No card was read.", file=sys.stderr)
        return 2
    skipped = sum(import_repo(repo, cli, dry_run=dry_run) for repo in repos)
    return 1 if skipped else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Command line for a person: release a crashed pass's claim, import markdown cards, find them."""

    parser = argparse.ArgumentParser(prog="ticket.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    release = commands.add_parser("release", help="release a ticket's live pass, named by its id, after that session is gone")
    release.add_argument("key", help="the ticket key")
    release.add_argument("pass_id", help="the pass id the refusal named")
    release.add_argument("--repo", default=".", help="the repository that holds the markdown card")
    importing = commands.add_parser(
        "import-cards", help="import the markdown cards of each repository into the ledger, then move them aside"
    )
    importing.add_argument("repos", nargs="+", help="a repository that holds .ticket/<key>.md cards")
    importing.add_argument("--dry-run", action="store_true", help="read and validate; write nothing, move nothing")
    finding = commands.add_parser("find-cards", help="list the git repositories under the roots that hold unimported cards")
    finding.add_argument("roots", nargs="*", help="directories to search (default: the current directory)")
    args = parser.parse_args(argv)
    if args.command == "find-cards":
        roots = args.roots or ["."]
        missing = [root for root in roots if not os.path.isdir(_root_spelling(root))]
        if missing:
            print(f"ticket.py: not a directory: {', '.join(missing)}", file=sys.stderr)
            return 2
        for repo in find_cards(roots):
            print(repo)
        return 0
    if args.command == "import-cards":
        return _import_cards_command(args.repos, args.dry_run)
    try:
        store = choose_store(args.repo, args.key, args.key)
    except StoreUnavailable as exc:
        print(f"ticket.py: {exc}", file=sys.stderr)
        return 2
    try:
        store.clear_pass(args.pass_id)
    except (StoreUnavailable, TicketError) as exc:
        print(f"ticket.py: {exc}", file=sys.stderr)
        return 1
    print(f"released pass {args.pass_id} on ticket {args.key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

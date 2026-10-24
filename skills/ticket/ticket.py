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
import fcntl
import functools
import hashlib
import json
import os
import re
import secrets
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, Sequence

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
NO_TOKEN = "none"
# The installed script, so a recovery command runs from the target repository.
_SCRIPT = os.path.abspath(__file__)

_SKILL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# A pass id is hex (design section 2); no length is fixed.
_PASS_ID = re.compile(r"[0-9a-f]+")
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


def release_command(key: str, pass_id: str) -> str:
    """The recovery command a person copies: every argv element shell-quoted."""
    return shlex.join(["python3", _SCRIPT, "release", key, pass_id])


class TicketError(ValueError):
    """A wrong step or value. Nothing was written and the pass stays live."""


class GateRequired(TicketError):
    """A person has not explicitly approved the named gate."""


class StoreUnavailable(RuntimeError):
    """A refusal: nothing was written and the pass ends. ``code`` names the refusal."""

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
        return cls(
            key=str(value["key"]),
            title=value["title"].strip(),
            status=str(value["status"]),
            description=str(value["description"]),
            acceptance_criteria=tuple(item.strip() for item in criteria),
            links=tuple(normalized),
            updated_at=str(value["updated_at"]) if value.get("updated_at") else None,
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


class PlanCard:
    """The plan a pass presents: the ticket's criteria in order, the six steps,
    skip reasons, a one-sentence diff when the plan step is skipped, and open
    questions.

    It carries no ``passes`` field. ``review`` is the passing review round of
    this plan that the pass read from the card, or None; ``render`` derives
    each criterion's ``passes`` from it.
    """

    def __init__(self, ticket: Ticket):
        self.key = ticket.key
        self.title = ticket.title
        self._criteria = tuple(ticket.acceptance_criteria)
        self.steps = [PlanStep(name) for name in STEPS]
        self.diff_statement: str | None = None
        self.open_questions: list[str] = []
        self.review: Mapping[str, Any] | None = None

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
            "ticket_status": ticket.status,
        }
        if self.diff_statement is not None:
            data["diff_statement"] = self.diff_statement
        if ticket.updated_at:
            data["ticket_updated_at"] = ticket.updated_at
        return data

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> "PlanCard":
        """Rebuild the plan the card stored, its criteria in stored order."""

        plan = cls.__new__(cls)
        plan.key = snapshot["key"]
        plan.title = snapshot["title"]
        plan._criteria = tuple(snapshot["criteria"])
        plan.steps = [PlanStep(step["name"], step["status"], step.get("skip")) for step in snapshot["steps"]]
        plan.diff_statement = snapshot.get("diff_statement")
        plan.open_questions = list(snapshot["open_questions"])
        plan.review = None
        return plan

    def render(self) -> str:
        passing = self.review
        lines = [f"# Plan: {self.key}: {self.title}", "", "## Acceptance criteria"]
        for index, text in enumerate(self._criteria):
            passes = passing is not None and index in passing["passed"]
            line = f"- [{'x' if passes else ' '}] passes: {str(passes).lower()} - {text}"
            if passes:
                line += f" (review round {passing['round']}: {passing['evidence']})"
            lines.append(line)
        lines.extend(("", "## Steps"))
        for step in self.steps:
            lines.append(f"- {step.name}: skip: {step.skip}" if step.status == "skipped" else f"- {step.name}: {step.status}")
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
     "open_questions", "ticket_status", "ticket_updated_at")
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
    questions = plan.get("open_questions")
    if type(questions) is not list or not all(_is_text(item) for item in questions):
        raise TicketError("a plan snapshot's open_questions must be a list of non-empty text")
    if "diff_statement" in plan and not _is_text(plan["diff_statement"]):
        raise TicketError("a plan snapshot's diff_statement must be non-empty text")
    _criteria_texts(plan.get("criteria"), "a plan snapshot's criteria")
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


def _check_card_shape(card: Any, key: str, where: str) -> None:
    """Refuse a card whose top-level fields are not of the section-2 shape."""

    if type(card) is not dict or card.get("schema") != CARD_SCHEMA or card.get("key") != key:
        raise _unreadable(where, f"it is not a {CARD_SCHEMA} card for ticket {key}")
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
    elif kind == "closed":
        card["status"] = "closed"


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


def _is_go(decision: Mapping[str, Any], gate: str, key: str) -> bool:
    """A person's settled ratification of ``gate`` on this ticket."""

    source = decision.get("source")
    return (
        decision.get("gate") == gate
        and decision.get("who_kind") == "person"
        and decision.get("standing") == "settled"
        and type(source) is dict and source.get("what") == "ratification"
        and {"type": "ticket", "id": key} in _items(decision.get("tags"))
    )


def _has_go(card: Mapping[str, Any], gate: str, identity: tuple[int, tuple[str, ...]], key: str) -> bool:
    """A go of ``gate`` covers the card's plan. A plan go counts only when it
    names the pass that presented the stored plan."""

    return any(
        _is_go(decision, gate, key)
        and _covers(decision, identity)
        and (gate != "plan" or decision.get("pass") == card["plan"]["pass"])
        for decision in card["decisions"]
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


def _card_side_changes(card: Mapping[str, Any], identity: tuple[int, tuple[str, ...]], key: str) -> list[str]:
    """F11: a review or plan go of the plan's revision that no longer covers it."""

    revision = identity[0]
    lines = []
    for review in card["reviews"]:
        if review.get("plan_revision") == revision and not _covers(review, identity):
            lines.append(f"review round {review['round']} reviewed other acceptance criteria than plan revision {revision} on the card")
    for decision in card["decisions"]:
        if _is_go(decision, "plan", key) and decision.get("plan_revision") == revision and not _covers(decision, identity):
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
    if not _has_go(card, "outward", identity, key):
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
        differences = _source_differences(identity[1], ticket.acceptance_criteria)
        flags = [_f7("; ".join(differences))] if differences else []
        return "closed", flags + _source_flags(card["plan"], ticket), identity
    if identity is None:
        return "orient", [], None
    revision = identity[0]
    source_changes = _source_differences(identity[1], ticket.acceptance_criteria)
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


def _stale(key: str, detail: str) -> StoreUnavailable:
    return StoreUnavailable(
        f"ticket {key} changed since this pass last read or wrote it: {detail}. Nothing was written and the pass "
        "ends; open it again to see the change.",
        code="stale",
    )


# --- The store interface (design 6) and the markdown store (6.1) -----------


class Store(Protocol):
    """Four calls. A handle carries the pass it writes for in ``pass_id``: a
    successful ``claim`` binds it, and a pass that joins sets it. One handle
    serves one pass: ``TicketPass.open`` refuses a handle that is bound, or
    whose pass has ``ended``, and reads nothing else of a store."""

    key: str
    pass_id: str | None
    ref: str

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


def _token(card: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(card, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class MarkdownStore:
    """The card at ``<repo>/.ticket/<key>.md``: a rendered summary above one
    machine block, next to its lock ``.ticket/<key>.lock``. Both must be
    git-ignored; neither is ever staged or committed.
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
        self.ref = self.relative_path.as_posix()
        self.pass_id: str | None = None
        self._ended: str | None = None
        # Threads sharing this handle take turns before the file lock.
        self._threads = threading.RLock()

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
                f".ticket for ticket {self.key} resolves outside the repository, so nothing was written"
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
                    "and retry. No ignore file was changed and nothing was written."
                )

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """This handle's thread lock, then an exclusive ``flock`` on the lock file.

        The lock file is opened without following a link, through the
        directory, and must be a regular file. The operating system drops the
        lock if its holder dies.
        """

        with self._threads:
            self._inside_repo()
            self._check_ignored()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fd = self._open_lock(directory)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise StoreUnavailable(
                            f"{self.ref[:-3]}.lock is not a regular file, so it was not used and nothing was written"
                        )
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    yield
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
                    f"cannot open {self.ref[:-3]}.lock as a regular file ({exc}), so nothing was written"
                ) from exc

    def _load(self) -> tuple[dict[str, Any], str]:
        if not self.path.exists():
            return _blank_card(self.key, self.title), NO_TOKEN
        body = self.path.read_text(encoding="utf-8")
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

    def _save(self, card: Mapping[str, Any]) -> None:
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(_render_card(card))
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    # The four calls.

    def read(self) -> tuple[dict[str, Any], str]:
        self._inside_repo()
        return self._load()

    def apply(self, token: str, events: Sequence[tuple[str, Any]]) -> str:
        """Land every event or none, only while the card is still at ``token``."""

        for kind, value in events:
            _validate_event(kind, value, self.key)
        with self._locked():
            card, current = self._load()
            if current != token:
                raise _stale(self.key, "the card on disk changed")
            _check_pass(self.key, card["pass"], self.pass_id, self._ended)
            card = _fold(card, events)
            self._save(card)
        for kind, value in events:
            if kind == "claim":
                self.pass_id, self._ended = value["id"], None
        return _token(card)

    def release(self) -> None:
        """Clear this handle's own claim, whatever has landed since; no token."""

        mine = self.pass_id
        self.pass_id, self._ended = None, mine
        with self._locked():
            card, _ = self._load()
            if card["pass"] is not None and card["pass"]["id"] == mine:
                card["pass"] = None
                self._save(card)

    def clear_pass(self, pass_id: str) -> None:
        """A person's release of a crashed pass: refused unless ``pass_id`` is the live claim."""

        with self._locked():
            card, _ = self._load()
            live = card["pass"]
            if live is None or live["id"] != pass_id:
                raise StoreUnavailable(
                    f"pass {pass_id} is not the live pass on ticket {self.key}"
                    + (f" (the live pass is {live['id']})" if live else " (no pass is live)")
                    + "; nothing was changed",
                    code="held" if live else "pass_ended",
                )
            card["pass"] = None
            self._save(card)


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
        label = f"{gate} go, plan revision {_inline(decision.get('plan_revision'))}" if gate else _inline(decision.get("standing"))
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


# --- Store selection (design 6.3) -------------------------------------------

LEDGER_SELECTOR = "TICKET_LEDGER_CLI"


def choose_store(repo: str | Path, key: str, title: str) -> Store:
    """The markdown store unless ``TICKET_LEDGER_CLI`` is set; an empty value counts as unset.

    The ledger store is not part of this release, so selecting it is refused
    before anything is read or written rather than falling back.
    """

    if os.environ.get(LEDGER_SELECTOR, ""):
        raise StoreUnavailable(
            f"{LEDGER_SELECTOR} selects the ledger store, which this release does not include; unset it to use "
            "the markdown store. Nothing was read or written.",
            code="ledger_store_unavailable",
        )
    return MarkdownStore(repo, key, title)


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
        self.reconciliation: tuple[str, ...] = ()
        self.resume_card: dict[str, Any] = card

    @classmethod
    def open(cls, key: str, source: TicketSource, store: Store, *, pass_id: str | None = None) -> "TicketPass":
        """Open a pass: a new one claims the ticket; ``pass_id`` continues that live pass.

        One store handle serves one pass: a handle bound to a pass, or whose
        pass has ended, is refused (``handle_used``) before anything is
        pulled, read or written. Then the source is pulled, the card is read
        and its state derived. A refused card writes nothing; a stopped or
        closed ticket is reported and takes no claim.
        """

        if store.pass_id is not None or store.ended is not None:
            raise StoreUnavailable(
                f"this store handle was already used for pass {store.pass_id or store.ended} on ticket {key}, "
                "so nothing was read or written; open each pass with a fresh store handle",
                code="handle_used",
            )
        ticket = source.pull(key)
        card, token = store.read()
        state, flags, identity = _derive(card, ticket, pass_id, key)
        session = cls(ticket, store, card, token, identity)
        session._phase = state
        session.reconciliation = tuple(flags)
        if session._identity is not None:
            session._plan = PlanCard.from_snapshot(card["plan"])
            session._plan.review = _passing_final(card, session._identity) if state == "outward_go" else None
        if state in ("stopped", "closed"):
            return session
        if pass_id is None:
            session._claim()
        else:
            _check_pass(key, card["pass"], pass_id, None)
            store.pass_id = pass_id
            session._pass_id = pass_id
        session._live = True
        return session

    def _claim(self) -> None:
        claim = {"id": secrets.token_hex(8), "host": _host(), "started_at": _now()}
        try:
            token = self.store.apply(self._token, [("claim", claim)])
        except StoreUnavailable as exc:
            # A lost race: read once more and name the pass that won.
            card, _ = self.store.read()
            if card["pass"] is not None:
                raise TicketHeld(self.key, card["pass"]) from exc
            raise
        self._advance(token, [("claim", claim)])
        self._pass_id = claim["id"]

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

    def _stamp(self, point: str, next_skills: Sequence[str], refs: Sequence[str], *, phase: str | None = None, close: bool = False) -> tuple[str, Any]:
        return (
            "stamp",
            {"point": point, "phase": phase or _STORE_PHASE[self._phase], "next_skills": list(next_skills), "refs": list(refs), "close": close},
        )

    # The steps (design 3.3).

    def finish_orient(self) -> None:
        self._require("orient")
        self._phase = "research"

    def finish_research(self) -> None:
        self._require("research")
        self._phase = "plan"

    @_ends_on_failure
    def present_plan(self, plan: PlanCard) -> None:
        self._require("plan")
        if tuple(plan.acceptance_criteria) != self._ticket.acceptance_criteria:
            raise TicketError("the plan card must carry the ticket's acceptance criteria, in order")
        revision = self._identity[0] + 1 if self._identity else 1
        snapshot = plan.snapshot(revision, self._pass_id, self._ticket)
        point = PLAN_GO_POINT.format(revision=revision)
        self._write([("plan", snapshot), self._stamp(point, ("ticket",), (self.store.ref,))])
        self._identity = (revision, tuple(plan.acceptance_criteria))
        plan.review = None
        self._plan = plan
        self._phase = "plan_go"

    @_ends_on_failure
    def record_go(self, gate: str, *, evidence: str, verbatim: str) -> None:
        if gate not in GATES:
            raise TicketError(f"unknown gate: {gate}")
        self._require("plan_go" if gate == "plan" else "outward_go")
        if not _is_text(evidence) or not _is_text(verbatim):
            raise TicketError("a go record requires evidence and the person's verbatim words")
        revision, criteria = self._identity
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
        self._write([("decision", decision)])

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
        return self._identity is not None and _has_go(card, gate, self._identity, self.key)

    @_ends_on_failure
    def begin_build(self) -> None:
        self._require("plan_go")
        card = self._fresh()
        if not _has_go(card, "plan", self._identity, self.key):
            raise GateRequired("a plan go is required before build")
        self._phase = "build"

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
        self._require("build")
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
        self._write(events)
        self._rounds = number
        if verdict == "pass":
            self._plan.review = review
            self._phase = "outward_go"
        elif number == MAX_REVIEW_ROUNDS:
            self._phase = "stopped"
            self.end()

    @_ends_on_failure
    def require_outward_go(self) -> None:
        """Call before each outward action; the card must hold the outward go."""

        self._require("outward_go")
        card = self._fresh()
        if not _has_go(card, "outward", self._identity, self.key):
            raise GateRequired("an outward go is required before any outward action")

    @_ends_on_failure
    def close(self, point: str, *, refs: Sequence[str] = (), next_skills: Sequence[str] = ()) -> None:
        self._require("outward_go")
        if not _has_go(self._card, "outward", self._identity, self.key):
            raise GateRequired("an outward go is required before close")
        self._write([self._stamp(point, next_skills, refs, phase="close", close=True), ("closed", None)])
        self._phase = "closed"
        self.end()

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


__all__ = [
    "FileTicketSource",
    "GateRequired",
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
]


def main(argv: Sequence[str] | None = None) -> int:
    """Command line for a person: release the claim a crashed pass left behind."""

    parser = argparse.ArgumentParser(prog="ticket.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    release = commands.add_parser("release", help="release a ticket's live pass, named by its id, after that session is gone")
    release.add_argument("key", help="the ticket key")
    release.add_argument("pass_id", help="the pass id the refusal named")
    release.add_argument("--repo", default=".", help="the repository that holds the markdown card")
    args = parser.parse_args(argv)
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

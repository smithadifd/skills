#!/usr/bin/env python3
"""State and adapter helpers for one attended pass over a ticket."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

PHASES = ("orient", "plan", "build", "review", "close")
STEPS = ("orient", "research", "plan", "build", "review", "outward")
INFO_EXITS = ("person", "research", "someone_else")
OUTSIDE_EVENTS = ("merge", "reply", "ratification", "review", "release", "other")
TAG_TYPES = ("ticket", "initiative", "team", "project")
_SKILL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_JSON_START = "<!-- ticket-card:v1\n"
_JSON_END = "\n-->"


class TicketError(ValueError):
    """The ticket pass cannot accept the requested transition or value."""


class GateRequired(TicketError):
    """A person has not explicitly approved the named gate."""


class StoreUnavailable(RuntimeError):
    """The configured store cannot be used safely."""


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


@dataclass
class PlanStep:
    name: str
    status: str = "pending"
    skip: str | None = None


class PlanCard:
    """Acceptance criteria and explicit skip records for one ticket."""

    def __init__(self, ticket: Ticket):
        self.key = ticket.key
        self.title = ticket.title
        self.acceptance_criteria = [Criterion(text) for text in ticket.acceptance_criteria]
        self.steps = [PlanStep(name) for name in STEPS]
        self.diff_statement: str | None = None

    def mark_passes(self, index: int, review_evidence: str | None) -> None:
        evidence = (review_evidence or "").strip()
        if not evidence:
            raise TicketError("passes=true requires a review-evidence reference")
        criterion = self.acceptance_criteria[index]
        self.acceptance_criteria[index] = Criterion(criterion.text, True, evidence)

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
            "schema": "ticket-plan-card/v1",
            "key": self.key,
            "title": self.title,
            "acceptance_criteria": [asdict(item) for item in self.acceptance_criteria],
            "steps": [
                {key: value for key, value in asdict(item).items() if value is not None}
                for item in self.steps
            ],
        }
        if self.diff_statement is not None:
            result["diff_statement"] = self.diff_statement
        return result

    def render(self) -> str:
        lines = [f"# Plan — {self.key}: {self.title}", "", "## Acceptance criteria"]
        for item in self.acceptance_criteria:
            line = f"- [ ] passes: {str(item.passes).lower()} — {item.text}"
            if item.review_evidence:
                line += f" (review evidence: {item.review_evidence})"
            lines.append(line)
        lines.extend(("", "## Steps"))
        for step in self.steps:
            if step.status == "skipped":
                lines.append(f"- {step.name}: skip: {step.skip}")
            else:
                lines.append(f"- {step.name}: {step.status}")
        if self.diff_statement:
            lines.extend(("", f"Diff: {self.diff_statement}"))
        return "\n".join(lines) + "\n"


def _one_sentence(text: str) -> bool:
    if not text or "\n" in text or "\r" in text:
        return False
    parts = [part for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    return len(parts) == 1


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


class StoreAdapter(Protocol):
    """The complete persistence boundary used by a ticket pass."""

    def write_stamp(
        self,
        point: str,
        phase: str,
        next_skills: Sequence[str] = (),
        refs: Sequence[str] = (),
        *,
        close: bool = False,
    ) -> None: ...

    def record_decision(self, decision: DecisionRecord) -> None: ...

    def record_review(self, review: ReviewRecord) -> None: ...

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
        _ref(ref, "ref")


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
    if review.round < 1 or review.verdict not in ("pass", "changes"):
        raise TicketError("invalid review record")
    if len(review.findings) > 20:
        raise TicketError("a review may record at most 20 findings")
    for finding in review.findings:
        _text(finding, "review finding")
    if review.reviewer is not None:
        _text(review.reviewer, "reviewer")
    if review.evidence is not None:
        _ref(review.evidence, "review evidence")


def _blank_card(key: str, title: str) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "status": "open",
        "stamps": [],
        "decisions": [],
        "reviews": [],
    }


class MarkdownStore:
    """Ignored ``.ticket/<key>.md`` fallback with embedded structured data."""

    def __init__(self, repo: str | Path, key: str, title: str):
        if not _KEY.fullmatch(key):
            raise TicketError(f"unsafe ticket key: {key!r}")
        self.repo = Path(repo).resolve()
        self.key = key
        self.title = title
        self.relative_path = Path(".ticket") / f"{key}.md"
        self.path = self.repo / self.relative_path

    def _check_ignored(self) -> None:
        result = subprocess.run(
            ["git", "-C", str(self.repo), "check-ignore", "--quiet", "--", str(self.relative_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise StoreUnavailable(
                f"{self.relative_path.as_posix()} is not git-ignored; add `.ticket/` to "
                "the target repository's ignore rules and retry. No ignore file was changed."
            )

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _blank_card(self.key, self.title)
        body = self.path.read_text(encoding="utf-8")
        start = body.find(_JSON_START)
        end = body.find(_JSON_END, start + len(_JSON_START))
        if start < 0 or end < 0:
            raise StoreUnavailable(f"cannot read structured card data from {self.relative_path}")
        return json.loads(body[start + len(_JSON_START) : end])

    def _save(self, card: Mapping[str, Any]) -> None:
        self._check_ignored()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = self.path.parent.resolve()
        try:
            resolved_parent.relative_to(self.repo)
        except ValueError as exc:
            raise StoreUnavailable(".ticket resolves outside the target repository") from exc
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
        *,
        close: bool = False,
    ) -> None:
        _validate_stamp(point, phase, next_skills, refs)
        card = self._load()
        card["stamps"].append(
            {
                "point": point,
                "phase": phase,
                "next_skills": list(next_skills),
                "refs": list(refs),
                "close": close,
            }
        )
        if close:
            card["status"] = "closed"
        self._save(card)

    def record_decision(self, decision: DecisionRecord) -> None:
        _validate_decision(decision)
        card = self._load()
        card["decisions"].append(decision.as_dict())
        self._save(card)

    def record_review(self, review: ReviewRecord) -> None:
        _validate_review(review)
        card = self._load()
        card["reviews"].append(review.as_dict())
        self._save(card)

    def read_card(self) -> dict[str, Any]:
        return self._load()


def _render_markdown_card(card: Mapping[str, Any]) -> str:
    lines = [f"# {card['key']} — {card['title']}", "", f"Status: {card['status']}"]
    lines.extend(("", "## Stop and close stamps"))
    if card["stamps"]:
        for stamp in card["stamps"]:
            label = "close" if stamp["close"] else "stop"
            lines.append(f"- {label} [{stamp['phase']}]: {stamp['point']}")
            if stamp["next_skills"]:
                lines.append(f"  next skills: {', '.join(stamp['next_skills'])}")
            if stamp["refs"]:
                lines.append(f"  refs: {', '.join(stamp['refs'])}")
    else:
        lines.append("(none)")
    lines.extend(("", "## Decision records"))
    if card["decisions"]:
        for decision in card["decisions"]:
            lines.append(
                f"- {decision['standing']} [{decision['phase']}], {decision['who']}: "
                f"{decision['verbatim']}"
            )
            lines.append(f"  effect: {decision['effect']}")
    else:
        lines.append("(none)")
    lines.extend(("", "## Review rounds"))
    if card["reviews"]:
        for review in card["reviews"]:
            lines.append(f"- round {review['round']}: {review['verdict']}")
            lines.extend(f"  finding: {finding}" for finding in review["findings"])
    else:
        lines.append("(none)")
    machine = json.dumps(card, sort_keys=True, separators=(",", ":"))
    lines.extend(("", _JSON_START.rstrip("\n"), machine, _JSON_END.lstrip("\n"), ""))
    return "\n".join(lines)


class LedgerStore:
    """CLI-only adapter for an installed event ledger."""

    def __init__(
        self,
        key: str,
        title: str,
        *,
        cli: str | Path | None = None,
        db: str | Path | None = None,
    ):
        configured = str(cli) if cli is not None else os.environ.get("TICKET_LEDGER_CLI")
        self.cli = configured or shutil.which("ledger") or ""
        self.db = str(db) if db is not None else os.environ.get("LEDGER_DB")
        self.key = key
        self.title = title
        self._ensured = False

    def _invoke(self, *args: str) -> subprocess.CompletedProcess[str]:
        if not self.cli:
            raise StoreUnavailable("no ledger CLI is configured or available on PATH")
        command = [self.cli, "--json"]
        if self.db:
            command.extend(("--db", self.db))
        command.extend(args)
        env = os.environ.copy()
        env.setdefault("LEDGER_" + "HAR" + "NESS", "ticket")
        try:
            return subprocess.run(command, capture_output=True, text=True, env=env, check=False)
        except OSError as exc:
            raise StoreUnavailable(f"cannot execute ledger CLI: {exc}") from exc

    def _run(self, *args: str) -> dict[str, Any]:
        result = self._invoke(*args)
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            message = result.stderr.strip() or result.stdout.strip() or "invalid JSON response"
            raise StoreUnavailable(f"ledger CLI failed: {message}") from exc
        if result.returncode != 0 or not envelope.get("ok"):
            error = envelope.get("error") or {}
            raise StoreUnavailable(str(error.get("message") or result.stderr.strip() or "ledger error"))
        return envelope["data"]

    def doctor(self) -> dict[str, Any]:
        return self._run("doctor")

    def is_available(self) -> bool:
        try:
            self.doctor()
        except StoreUnavailable:
            return False
        return True

    def _ensure_item(self) -> None:
        if self._ensured:
            return
        result = self._invoke("card", self.key)
        if result.returncode == 0:
            self._ensured = True
            return
        if result.returncode != 1:
            message = result.stderr.strip() or result.stdout.strip() or "ledger card failed"
            raise StoreUnavailable(message)
        self._run("new", "--id", self.key, "--type", "ticket", "--title", self.title)
        self._ensured = True

    def write_stamp(
        self,
        point: str,
        phase: str,
        next_skills: Sequence[str] = (),
        refs: Sequence[str] = (),
        *,
        close: bool = False,
    ) -> None:
        _validate_stamp(point, phase, next_skills, refs)
        self._ensure_item()
        payload: dict[str, Any] = {"point": point, "phase": phase}
        if next_skills:
            payload["skills"] = list(next_skills)
        if refs:
            payload["refs"] = list(refs)
        self._append("item.stopped", payload)
        if close:
            self._run("state", self.key, "--status", "closed")

    def record_decision(self, decision: DecisionRecord) -> None:
        _validate_decision(decision)
        self._ensure_item()
        source_data = self._append("outside.recorded", decision.source.as_dict())
        source_event_id = int(source_data["event"]["id"])
        payload = decision.as_dict()
        payload.pop("source")
        payload["source"] = decision.source.ref
        payload["source_event_id"] = source_event_id
        self._append("decision.recorded", payload)

    def record_review(self, review: ReviewRecord) -> None:
        _validate_review(review)
        self._ensure_item()
        self._append("review.recorded", review.as_dict())

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
        card_result = self._invoke("card", self.key)
        if card_result.returncode == 1:
            return _blank_card(self.key, self.title)
        if card_result.returncode != 0:
            raise StoreUnavailable(card_result.stderr.strip() or "ledger card failed")
        try:
            envelope = json.loads(card_result.stdout)
        except json.JSONDecodeError as exc:
            raise StoreUnavailable("ledger card returned invalid JSON") from exc
        if not envelope.get("ok"):
            raise StoreUnavailable(str((envelope.get("error") or {}).get("message") or "ledger error"))
        card = envelope["data"]
        events = self._run("export-item", self.key)["events"]
        return _normalize_ledger_card(card, events)


def _normalize_ledger_card(card: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    outside = {
        int(event["id"]): event["payload"]
        for event in events
        if event["kind"] == "outside.recorded"
    }
    decisions: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    stamps: list[dict[str, Any]] = []
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
    if stamps and card.get("status") == "closed":
        stamps[-1]["close"] = True
    return {
        "key": card["item_id"],
        "title": card["title"],
        "status": card["status"],
        "stamps": stamps,
        "decisions": decisions,
        "reviews": reviews,
    }


def choose_store(
    repo: str | Path,
    key: str,
    title: str,
    *,
    cli: str | Path | None = None,
    db: str | Path | None = None,
) -> StoreAdapter:
    """Use the CLI adapter only when its doctor check succeeds."""

    ledger = LedgerStore(key, title, cli=cli, db=db)
    if ledger.is_available():
        return ledger
    return MarkdownStore(repo, key, title)


@dataclass(frozen=True)
class MoreInfoExit:
    destination: str
    waiting: str
    drafted_ask: str | None = None


@dataclass
class TicketPass:
    """Guarded transitions for one pass; outward actions remain caller-owned."""

    ticket: Ticket
    store: StoreAdapter
    resume_card: dict[str, Any] | None = None
    phase: str = "orient"
    plan: PlanCard | None = None
    go_records: dict[str, str] = field(default_factory=dict)
    review_round: int = 0

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
        prior = store.read_card() if resume else None
        return cls(ticket=ticket, store=store, resume_card=prior)

    def finish_orient(self) -> None:
        self._require_phase("orient")
        self.phase = "research"

    def finish_research(self) -> None:
        self._require_phase("research")
        self.phase = "plan"

    def present_plan(self, plan: PlanCard) -> None:
        self._require_phase("plan")
        if plan.key != self.ticket.key:
            raise TicketError("plan card belongs to another ticket")
        self.plan = plan
        self.phase = "plan_go"

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
                effect=f"The {gate} gate is approved for this pass.",
                tags=({"type": "ticket", "id": self.ticket.key},),
                verbatim=verbatim,
                phase="plan" if gate == "plan" else "review",
                evidence=evidence,
            )
        )
        self.go_records[gate] = evidence

    def begin_build(self) -> None:
        self._require_phase("plan_go")
        if "plan" not in self.go_records:
            raise GateRequired("plan go is required before build")
        self.phase = "build"

    def record_review(
        self,
        verdict: str,
        *,
        evidence: str,
        findings: Sequence[str] = (),
        reviewer: str = "fresh-context reviewer",
        passed_criteria: Sequence[int] = (),
    ) -> None:
        if self.phase not in ("build", "review"):
            raise TicketError(f"cannot record review during {self.phase}")
        if self.plan is None:
            raise TicketError("review requires a plan card")
        if verdict not in ("pass", "changes"):
            raise TicketError("review verdict must be pass or changes")
        evidence = evidence.strip()
        if not evidence:
            raise TicketError("review requires an evidence reference")
        self.review_round += 1
        if self.review_round > 3:
            raise TicketError("review round cap is three")
        for index in passed_criteria:
            self.plan.mark_passes(index, evidence)
        review = ReviewRecord(
            round=self.review_round,
            verdict=verdict,
            findings=tuple(findings),
            reviewer=reviewer,
            evidence=evidence,
        )
        self.store.record_review(review)
        if verdict == "pass":
            if not all(item.passes for item in self.plan.acceptance_criteria):
                raise TicketError("review cannot pass while acceptance criteria remain false")
            self.phase = "outward_go"
        elif self.review_round == 3:
            self.store.write_stamp(
                "Review did not converge after round three.",
                "review",
                ("ticket", "review"),
                (evidence,),
            )
            self.phase = "stopped"
        else:
            self.phase = "build"

    def close(self, point: str, *, refs: Sequence[str] = ()) -> None:
        self._require_phase("outward_go")
        if "outward" not in self.go_records:
            raise GateRequired("outward go is required before close")
        self.store.write_stamp(point, "close", (), refs, close=True)
        self.phase = "closed"

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
            drafted = f"Could you help with {self.ticket.key}? {question}"
        else:
            waiting = f"waiting on {destination}: {question}"
            drafted = None
        self.store.write_stamp(
            waiting,
            self._store_phase(),
            ("ticket",),
            (),
        )
        self.phase = "stopped"
        return MoreInfoExit(destination, waiting, drafted)

    def _store_phase(self) -> str:
        if self.phase == "plan_go":
            return "plan"
        if self.phase == "research":
            return "orient"
        if self.phase == "outward_go":
            return "review"
        return self.phase if self.phase in PHASES else "close"

    def _require_phase(self, expected: str) -> None:
        if self.phase != expected:
            raise TicketError(f"expected phase {expected}, found {self.phase}")


__all__ = [
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
    "TicketPass",
    "TicketSource",
    "choose_store",
]

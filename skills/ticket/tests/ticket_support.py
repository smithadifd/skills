"""Shared builders for the ticket tests: a scriptable source, the card fixtures S0-S6, and card edits.

The fixture names follow the test matrix: S1 a presented plan with no go;
S2[r] the plan go and r `changes` rounds; S3[r] round r passed; S4[r] S3 plus
the outward go; S5 three `changes` rounds; S6 closed from S4.
"""

from __future__ import annotations

from pathlib import Path

import ticket

KEY = "EK-14"
TITLE = "Add guarded ticket pass"
A = "A plan card keeps review-gated pass evidence."
B = "Plan and outward work require explicit approval."
B_EDITED = "Plan and outward work require explicit written approval."
C = "A resumed pass reports every flag it raised."
STATUS = "In Progress"
UPDATED = "2026-10-17T14:30:00Z"

REORDERED = "the acceptance criteria were reordered on the ticket"


def new_text(text):
    return f"acceptance criterion is new or changed on the ticket: {text}"


def removed_text(text):
    return f"acceptance criterion was removed from the ticket: {text}"


EDIT_WHAT = {
    "reorder": REORDERED,
    "text": f"{new_text(B_EDITED)}; {removed_text(B)}",
    "added": new_text(C),
    "removed": removed_text(B),
}


# The flag texts of design 4.6, spelled out here rather than imported.


def F1(m, what):
    return (f"the acceptance criteria changed since plan revision {m} was stored ({what}); "
            f"plan revision {m} must be presented again as a new revision for a new plan go")


def F2(n, m, what):
    return f"review round {n} passed plan revision {m}; the criteria changed since ({what}); that round covers nothing in a new plan"


def F3(n, m, what):
    return (f"review round {n} passed plan revision {m}; the criteria changed since ({what}); no review round is "
            "left for a new plan; a person restores the criteria or opens a new ticket")


def F4(m):
    return f"the outward go of plan revision {m} authorizes nothing now; outward actions already taken are not undone"


def F5(m):
    return f"three review rounds are stored and none is a passing round of plan revision {m}; the review cap is reached"


def F6(m, what):
    return (f"the acceptance criteria changed since plan revision {m} was stored ({what}); review stopped at the "
            "round cap, so no new plan can be reviewed")


def F7(what):
    return f"the ticket's criteria changed after close ({what}); the ticket stays closed; follow-up work is a new ticket"


def F8(m):
    return f"plan revision {m} was presented by another pass and has no plan go; resuming at plan to present it again"


def F9(old, new):
    return f"ticket status changed: {old} -> {new}"


F10 = "ticket was updated since the plan card was written"


def F12(old, new):
    return f"ticket title changed: {old} -> {new}"


def F11_review(n, m):
    return f"review round {n} reviewed other acceptance criteria than plan revision {m} on the card"


def F11_go(m):
    return f"the plan go of plan revision {m} approved other acceptance criteria than the card's plan"


class Source:
    """A live source the test edits between pulls; it records each pull in ``trace``."""

    def __init__(self, criteria=(A, B), trace=None):
        self.criteria = list(criteria)
        self.status = STATUS
        self.title = TITLE
        self.updated_at = UPDATED
        self.trace = trace if trace is not None else []

    def pull(self, key):
        self.trace.append("pull")
        return ticket.Ticket.from_mapping(
            {
                "key": key,
                "title": self.title,
                "status": self.status,
                "description": "Run one ticket from live orientation through approved outward work.",
                "acceptance_criteria": list(self.criteria),
                "updated_at": self.updated_at,
                "links": [],
            }
        )

    def edit(self, kind):
        """The matrix's edit(kind) on a two-criterion source."""

        if kind == "reorder":
            self.criteria = [self.criteria[1], self.criteria[0]] + self.criteria[2:]
        elif kind == "text":
            self.criteria = [self.criteria[0], B_EDITED] + self.criteria[2:]
        elif kind == "added":
            self.criteria = self.criteria + [C]
        elif kind == "removed":
            self.criteria = [self.criteria[0]] + self.criteria[2:]
        else:
            raise ValueError(kind)


def store(repo):
    return ticket.MarkdownStore(repo, KEY, TITLE)


CLEARED_PASS = "0000000000000000"


def bound_to_a_cleared_pass(repo):
    """A handle bound to a pass of its own that a person has since cleared: it holds an id that is not live.

    The handle binds the only way a handle can, through its own ``apply``: a
    ``claim`` on the blank card. ``clear_pass`` then clears that claim as a
    person would, so the card is blank again apart from having been written.
    """

    handle = store(repo)
    handle.apply(ticket.NO_TOKEN, [("claim", {"id": CLEARED_PASS, "host": "elsewhere", "started_at": UPDATED})])
    handle.clear_pass(CLEARED_PASS)
    return handle


def card_path(repo):
    return Path(repo) / ".ticket" / f"{KEY}.md"


def card_bytes(repo):
    path = card_path(repo)
    return path.read_bytes() if path.exists() else None


def read_card(repo):
    return store(repo).read()[0]


def write_card(repo, card):
    """Write a card as the store would, bypassing every check: a hand edit."""

    card_path(repo).parent.mkdir(parents=True, exist_ok=True)
    card_path(repo).write_text(ticket._render_card(card), encoding="utf-8")


def edit_card(repo, change):
    card = read_card(repo)
    change(card)
    write_card(repo, card)
    return card


def open_pass(repo, src, pass_id=None, handle=None):
    return ticket.TicketPass.open(KEY, src, handle or store(repo), pass_id=pass_id)


def present(session):
    if session.phase == "orient":
        session.finish_orient()
    if session.phase == "research":
        session.finish_research()
    session.present_plan(ticket.PlanCard(session.ticket))


def go_plan(session):
    session.record_go("plan", evidence="conversation:plan-go", verbatim="Go with this plan.")


def go_outward(session):
    session.record_go("outward", evidence="conversation:outward-go", verbatim="Open the pull request.")


def changes(session):
    session.record_review("changes", evidence=f"reviews/round-{session.review_round + 1}.md", findings=["Fix the guard."])


def passing(session):
    count = len(session.plan.acceptance_criteria)
    session.record_review("pass", evidence=f"reviews/round-{session.review_round + 1}.md", passed=tuple(range(count)))


def build(repo, src, stage, rounds=0):
    """Drive a fresh pass to a matrix stage and return it (live at S1-S4).

    ``stage`` is one of S0, S1, S2, S3, S4, S5, S6; ``rounds`` is r.
    """

    session = open_pass(repo, src)
    if stage == "S0":
        return session
    present(session)
    if stage == "S1":
        return session
    go_plan(session)
    session.begin_build()
    if stage == "S2":
        for _ in range(rounds):
            changes(session)
        return session
    if stage == "S5":
        for _ in range(3):
            changes(session)
        return session
    for _ in range(max(rounds, 1) - 1):
        changes(session)
    passing(session)
    if stage == "S3":
        return session
    go_outward(session)
    if stage == "S4":
        return session
    session.close("Ticket pass complete.", refs=("example/repo#14",), next_skills=("ticket",))
    return session


def build_released(repo, src, stage, rounds=0):
    """A stage whose pass has ended, so a new pass can resume the card."""

    session = build(repo, src, stage, rounds)
    session.end()
    return session


def stage_of(name):
    """Split a matrix stage name such as ``S2[1]`` into ``("S2", 1)``."""

    if "[" in name:
        base, rest = name.split("[")
        return base, int(rest.rstrip("]"))
    return name, 0


def without_pass(card):
    return {name: value for name, value in card.items() if name != "pass"}


def assert_only_a_claim(repo, before):
    """The card equals ``before`` but for a new live claim: a resume wrote its claim and nothing else."""

    after = read_card(repo)
    assert without_pass(after) == without_pass(before)
    assert after["pass"] is not None and after["pass"] != before.get("pass")
    return after


def refusal(excinfo):
    return getattr(excinfo.value, "code", None)

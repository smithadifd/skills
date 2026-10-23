---
name: ticket
kind: portable
version: 2.0.0
description: >
  Run one attended pass over a ticket: pull the live ticket, research, present
  a plan card for a plan go, build and review for up to three rounds, stop for
  an outward go before anything leaves the machine, then close, with a resume
  card that a later session continues from. Use when the person says "work
  this ticket", "take <key> through to a pull request", or "resume the
  ticket". NOT for a change that has no ticket (use /tdd or /debug), and NOT
  for a session resume note (use /handoff).
---

# Ticket

Run one ticket in this order:

`orient -> research -> plan card -> plan go -> build <-> review -> outward go -> close`

The **plan go** and the **outward go** are the only approval stops. Never infer
approval from silence or from an earlier approval. Record the person's exact
words and an evidence reference before continuing. A need for information
takes one of the more-information exits below; it is never approval.

`ticket.py` holds the whole state machine and the store. The rules it follows,
and why, are in [references/ticket-design.md](references/ticket-design.md); the
tests it is checked against are in
[references/test-matrix.md](references/test-matrix.md). Drive it from Python:

```python
import sys; sys.path.insert(0, "${CLAUDE_SKILL_DIR}")
import ticket
store = ticket.choose_store("<target-repo>", "<KEY>", "<title>")
session = ticket.TicketPass.open("<KEY>", source, store)              # a new pass
session = ticket.TicketPass.open("<KEY>", source, store, pass_id=P)   # the same pass, later
```

## The ticket source

`source.pull(key)` returns the current ticket: key, title, status,
description, acceptance criteria, update time and typed links. `open` pulls it
first, before it reads the card, so every change on the ticket is visible
before any stored plan is trusted. A live pass never pulls again; the next
open sees an edit.

For a ticket in an issue tracker, read it through the tracker's connector
with the exact key: status, description, acceptance criteria, updated time,
and links. Open each linked design or document page and read the parts that
constrain the ticket. Record a link you cannot open as an open question; never
guess its contents. Do not mirror the tracker's status into the card. For a
local JSON ticket, `ticket.FileTicketSource(path)` implements the same call.

## The card and the store

The card is the ticket's durable record: its claim, the plan snapshot, go
decisions, review rounds, what was tried, and stamps. The markdown store keeps
it at `<target-repo>/.ticket/<key>.md`, next to its lock
`.ticket/<key>.lock`. Both must be git-ignored: before every write the store
runs `git check-ignore`, and if either is not ignored it refuses and asks you
to add `.ticket/` to the repository's ignore rules. It never edits an ignore
file itself. Never stage or commit `.ticket/`.

Every step that writes is one atomic write under the token the pass holds. The
token is a digest of the card as the pass last read or wrote it. If anything
changed the card since then, another session or a hand edit, the write is
refused as `stale` and the pass ends. Open it again to see the change.

`choose_store` returns the markdown store. Setting `TICKET_LEDGER_CLI` to a
non-empty value selects a ledger store, which this release does not include,
so it is refused (`ledger_store_unavailable`) before anything is read or
written.

## One live pass

A ticket has at most one live pass. `open` claims the ticket with the pass's
id, host and start time. Read `session.pass_id`, keep it in the conversation,
and report it at every stop: each later step, in a new process, continues the
same pass with `open(..., pass_id=<id>)`. Never take a pass id from the card or
from a refusal to continue a pass you did not start.

Another open while a pass is live is refused as `held`, naming the live pass
and the command that releases it. The pass ends, and releases its claim, at
close, at a more-information exit, at the review cap, on `session.end()`,
when an exception leaves a `with TicketPass.open(...) as session:` block, or
on any refusal. A `TicketError` (a wrong step, an invalid value, or
`GateRequired`) writes nothing and leaves the pass live. After a pass ends,
every step refuses with `pass_ended`.

A session that crashed leaves its claim, and the ticket stays refused. When
the person confirms that session is gone, release it with
`python3 ${CLAUDE_SKILL_DIR}/ticket.py release <key> <pass-id> --repo <target-repo>`.
It refuses unless that id is the live claim. It exits 0 when it released the
claim, 1 when it refused, and 2 when the ledger store was selected.

## 1. Orient

1. `TicketPass.open` pulls the ticket, reads the card and reports where the
   pass resumes (`session.phase`) and why (`session.reconciliation`). Read
   every reconciliation line to the person before reusing anything stored.
2. Read the target repository's `AGENTS.md`, `CLAUDE.md`, contribution guide,
   pull-request template, the code involved and its verification commands.
   The repository's conventions win over defaults here.
3. Identify assumptions and focused research questions, then
   `session.finish_orient()`. Do not start building.

Where a resume lands:

| Card | `phase` |
|---|---|
| no plan yet | `orient` |
| plan presented, no plan go | `plan_go` for the pass that presented it; `plan` for another pass, which presents it again |
| plan go recorded | `build` (do not call `begin_build` again) |
| a passing final review of the plan | `outward_go` |
| outward go recorded | `outward_go`, and `approved("outward")` is true |
| three review rounds and no passing one | `stopped` |
| closed | `closed` |

A change on the ticket's acceptance criteria (added, removed, edited,
duplicated or reordered) sends the pass back to `plan`: the stored plan must be
presented again as a new revision, with its own plan go and review rounds.
Rounds already stored still count toward the cap, so at the cap a change
leaves the ticket `stopped`. A change after the outward go spends that go;
outward actions already taken are not undone. A closed ticket stays closed
and the change is reported; follow-up work is a new ticket. A changed status,
title or update time is reported but is not a change.

`stopped` and `closed` are terminal: the open takes no claim, and a person
decides what happens next (restore the criteria, or open a new ticket). A card
that cannot be read (`card_unreadable`), a stored plan that cannot be
identified (`plan_unidentifiable`), or a closed status the card does not
support (`close_invalid`) is refused and nothing is written. A person repairs
the card; never edit it to get past a refusal.

## 2. Research

Give each independent unknown to the `researcher` agent with one focused
question and the exact repository or source to inspect. Bring back only
findings with file-line or URL evidence, and reconcile conflicting ones. Then
`session.finish_research()`.

## 3. Plan card

Create `ticket.PlanCard(session.ticket)`. It carries every acceptance
criterion of the ticket, in the ticket's order, and the six steps orient,
research, plan, build, review and outward:

- `plan.skip_step(name, reason)` records `skip: <reason>`; a skip without a
  reason is refused.
- The plan step may be skipped only when the whole diff fits in one sentence:
  `plan.skip_step("plan", reason, diff_statement="<one sentence>")`. Skipping
  the plan step never skips the plan go.
- `plan.complete_step(name)` and `plan.add_open_question(text)` record the
  rest.

Present assumptions, intended changes and the test plan in prose around the
card.

## 4. Plan go: a hard stop

`session.present_plan(plan)` stores the plan as the next revision, with the
pass that presented it, and stamps the stop in the same write. Present the
card and stop. Do not edit product files or dispatch `builder-core` until the
person approves, then record it from the same pass with
`session.record_go("plan", evidence=<ref>, verbatim=<their words>)`. A plan go
records the plan revision, its criteria and the pass; it opens nothing for
other criteria or another revision. `session.begin_build()` raises
`GateRequired` without it.

## 5. Build and review

1. Give `builder-core` the ticket key, the approved card, the acceptance
   criteria, the repository's conventions, the branch boundary, and the exact
   verification commands. Outward actions stay forbidden.
2. Give a fresh-context `reviewer` the ticket, the approved card, the diff and
   prior findings. It checks the criteria first and the repository's
   standards second. When the target repository has `scripts/verify.sh`, the
   reviewer runs it as well as the required checks.
3. Record each round with `session.record_review(verdict, evidence=<ref>,
   passed=<criterion indices>, findings=[...])`. A `pass` verdict must list
   every criterion. A round records the plan revision and criteria it
   reviewed, and the rendered card (`session.plan.render()`) derives each
   criterion's `passes` from the stored rounds.
4. Send findings back to `builder-core`, then review the delta with a fresh
   reviewer. Continue without asking the person.
5. A passing round stops at the outward go. Three rounds are the cap, counted
   across passes and plan revisions: a third round that does not pass stamps
   the unresolved findings and ends the pass at `stopped`. There is no fourth
   round.

## 6. Outward go: a hard stop

Show the final diff summary, verification evidence, the evidence for each
criterion, the decisions, and every proposed outward action. Stop. Nothing
leaves the machine before the person approves and you record it with
`session.record_go("outward", evidence=..., verbatim=...)`: no push, pull
request, tracker comment, status change, message, merge, release or deploy.
Call `session.require_outward_go()` before each outward action; it reads the
card fresh and refuses (`GateRequired`, or `stale` if the card changed).

After the listed outward actions, call
`session.close(point, refs=..., next_skills=...)`. It writes the close stamp
and the closed status in one write, then ends the pass.

## More-information exits

Use exactly one when the pass cannot proceed:

- **person**: `session.more_info("person", "<exact question>")`; ask directly.
- **research**: `session.more_info("research", "<exact question>")`; give that
  question to `researcher`, then resume with a fresh open.
- **someone else**: `session.more_info("someone_else", "<exact question>", role="<role>")`
  stamps `waiting on <role>: <question>` and returns a drafted ask for the
  person to send. Do not contact the role or imply the ask was sent.

Each exit stamps the stop and ends the pass.

## Records

Record what was tried with `session.record_tried(what, outcome=..., evidence=...)`.
References are paths, PR or issue ids (`owner/repo#N` or `#N`),
`branch:<name>`, commit SHAs or `event:<N>`; never paste content into a
reference. Decision words are verbatim.

After close, offer `/reflect` only when the pass exposed a reusable defect in a
skill or rule. Propose at most one edit, show the reason and the exact target,
and wait for the person's approval. Do not apply it as part of the pass.

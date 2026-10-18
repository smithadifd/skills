---
name: ticket
kind: portable
description: >
  Run one attended pass over a ticket: pull live context, research, present a
  plan card for approval, build and review for up to three rounds, stop for
  approval before outward actions, and leave a durable resume card.
---

# Ticket

Run one ticket in this order:

`orient → research → plan card → plan go → build ↔ review → outward go → close`

The **plan go** and **outward go** are the only approval stops. Do not infer
approval from silence or earlier approval. Record the person's explicit words
and their evidence reference before continuing. A need for information takes
one of the exits below; it is not approval.

## Adapters

Keep ticket intake and persistence behind these two boundaries. `ticket.py`
contains the executable contracts and the file fixtures used by the tests.

### Ticket source

`pull(key) -> Ticket` returns the current key, title, status, description,
acceptance criteria, update marker, and typed links. Pull on every start. On a
resume, pull **before** reading the resume card so changed status and content
are visible before any prior plan is trusted.

The first live source is Jira through the Atlassian Rovo connector. In the
current conversation:

1. Use the connector's issue lookup with the exact Jira key. Request status,
   description, acceptance criteria, updated time, comments needed by the
   ticket, and links. Do not mirror Jira status into the store.
2. Open every Confluence link with the Atlassian connector and read the linked
   page sections that constrain the ticket.
3. Open every Figma link with the Figma connector and read the linked frame's
   design context, annotations, and component references.
4. Record unavailable or permission-denied links as open questions. Never
   guess their contents.

`FileTicketSource` implements the same call for a JSON fixture or an explicitly
provided local ticket file.

### Store

Both stores implement exactly seven calls:

- `write_stamp(point, phase, next_skills, refs)` — a stop, or a pending close.
  Each ref is a path, a PR or issue id (`owner/repo#N` or `#N`),
  `branch:<name>`, a commit SHA, or a store event (`event:<N>`); content is
  never copied into a ref;
- `confirm_close(position, point)` — marks the pending close stamp at that
  position as the close and the ticket closed, and refuses, changing nothing,
  when that stamp is no longer the last one;
- `record_decision(decision)` — `who`, `who_kind`, optional `relayed_by`,
  `standing`, outside `source`, `effect`, tags, verbatim words, phase, and
  evidence reference;
- `record_review(review)` — round, verdict, findings, reviewer, and evidence;
- `record_plan(plan)` — the latest plan-card snapshot (criteria with `passes`
  and review evidence, skips, diff statement, open questions, revision);
  returns the ref the store keeps it under, which the stop stamps cite;
- `record_tried(tried)` — what was tried, its outcome, phase, and evidence;
- `read_card()` — the ticket's resume card: stamps, decisions, reviews, the
  plan snapshot and its ref, and what was tried.

The plan card is a dedicated store record rather than a file a stamp points
at, so the store alone decides what a resume trusts: a file outside the store
could change or vanish without the store knowing.

Use the event-ledger adapter when `ledger` is on `PATH`, or `TICKET_LEDGER_CLI`
names the configured executable. Invoke only that CLI. Never import its Python
package or read its database directly. `ledger doctor` must succeed for the
configured database. When it fails, report the CLI's message and stop; do not
fall back to the markdown card, which would split the ticket across two stores.
A database that does not exist yet (`missing_db`) is initialised by creating
this ticket's item once with the CLI's own `new` verb (`ledger --db <path> new
--id <key> --type ticket --title <title>`), then retrying.

The ledger CLI may require a writer identity in its environment before it
accepts writes. This skill never sets one. When a write is refused, the adapter
raises `StoreUnavailable` with the CLI's message: tell the person to export the
writer-identity variable that message names (in their shell or the wrapper that
starts the session), then retry.

With no ledger CLI installed, the card lives at `<target-repo>/.ticket/<key>.md`,
in the repository's ignored scratch, next to its lock file
`.ticket/<key>.md.lock`. Before every write, the markdown adapter runs
`git check-ignore` for both files. If either is not ignored, stop and say: add
`.ticket/` to the target repository's ignore rules, then retry. Do not edit an
ignore file silently. Never stage or commit `.ticket/`.

### One writer per ticket

A ticket has at most one live writer at a time, whichever store holds it. A
second writer is refused with `TicketHeld`, which names the ticket and the
writer that holds it or wrote first (`writer` label, process id, and handle),
and nothing of the refused write is stored. Nothing waits for a lock. Pass
`writer=` to the store or to `choose_store` to label this session; the default
is `user@host`.

- **markdown** — every write holds `.ticket/<key>.md.lock` across the whole
  read, change and replace of the card, and releases it on every exit path,
  including an exception. The writer prepares a new file that already carries
  its name and its exclusive lock, then links it into place, so the lock file
  names its holder from the moment it exists until the holder removes it.
  When a writer is refused, retry after the named writer finishes. If the
  holding process dies, the next writer replaces the lock file it left. A lock
  path that is a symbolic link, is not a regular file, or has more than one
  link is refused untouched: check what it points at, remove it, and retry.
- **ledger** — the precondition is the ticket's revision, the newest ledger
  event of the ticket a store handle has accounted for. `TicketPass.open`
  reads the card, which pins the revision for the pass; the pass's own writes
  move it, later reads do not. A write is refused when the ticket holds a
  newer event, naming whoever wrote it, and each append carries an idempotency
  key built from the revision, so of two writers that read the same revision
  only the first append lands. A refused pass has nothing to clean up: open
  a new store handle and resume, which shows the other writer's changes, and
  decide with the person whether to continue. The adapter claims nothing. It
  never changes the ticket's status, owner, claim or blocked-by fields, except
  that a confirmed close sets the status to `closed` and keeps the other three.
  A ticket someone has claimed for work stays claimed and can still be written.

## 1. Orient

1. Pull the ticket from its source. On resume, do this before `read_card()`.
2. Read the target repository's `AGENTS.md`, `CLAUDE.md`, contribution guide,
   pull-request template, relevant code, and verification commands when they
   exist. Team and repository conventions win over defaults here.
3. Follow the ticket's Figma and Confluence links as described above.
4. On a resume, `TicketPass.open(..., resume=True)` pulls the ticket, then reads
   the card and rebuilds the pass from it: the plan snapshot, the review-round
   count, the recorded go decisions, and the phase. It reconciles the plan
   against the fresh ticket and lists every difference in `reconciliation`. A
   new, changed, or removed acceptance criterion sends the pass back to the plan
   step for a new plan card and a new plan go; a criterion whose `passes` is not
   backed by a recorded review round is reset to false. A resume re-enters plan
   go or outward go only when the store holds that stop's stamp for the plan
   revision or review round; otherwise it resumes at plan or build and flags
   why. An unconfirmed close stamp is flagged and the ticket stays open. State
   every flagged difference before reusing the plan, and read the card's
   referenced paths or ids.
5. Identify assumptions and focused research questions. Do not start building.

## 2. Research

Give each independent unknown to the `researcher` agent with one focused
question and the exact repository or external source to inspect. Bring back
only findings with file-line or URL evidence. Reconcile conflicting findings.
Use the research exit when the question can be answered without the person or
another role; return to the plan after the answer arrives.

## 3. Plan card

Create `PlanCard(ticket)` from `ticket.py`. It must contain:

- every acceptance criterion as its own item;
- `passes: false` initially on every item;
- `review_evidence` whenever, and only when, review evidence flips `passes` to
  true;
- the orient, research, plan, build, review, and outward steps;
- literal `skip: <reason>` on every skipped step;
- assumptions, open questions, intended changes, and the test plan in the
  presented prose around the card.

A skipped step without a reason is invalid. The plan step may be skipped only
when the entire proposed diff fits in one sentence; put that sentence in
`diff_statement` and still present the card. Skipping the plan step never skips
the plan go.

## 4. Plan go — hard stop

`present_plan(plan)` records the plan snapshot and writes a stop stamp at plan
go before the pass reports that phase, so ending the session here leaves a
resume card at this point. Present the plan card and ask the person to
approve, change, or take a more-information exit. Stop. Do not edit product
files or dispatch `builder-core` until explicit approval is recorded with
`record_go("plan", …)`. Gates are read from the person's go decisions in the
store, never from session state.

## 5. Build and review

After plan go:

1. Give `builder-core` the ticket key, approved card, acceptance criteria,
   repository conventions, branch/worktree boundary, and exact verification
   commands. Outward actions remain forbidden.
2. Give a fresh-context `reviewer` the ticket, approved card, diff, and prior
   findings. It reviews specification compliance first and repository
   standards second.
3. When the target repository has `scripts/verify.sh`, the reviewer runs it in
   addition to the repository's required checks.
4. Record each round with `record_review(verdict, evidence=…,
   passed_criteria=…)`. It is the only way `passes` flips: the store records
   the round first, and each listed criterion then cites that recorded round
   and its evidence path or id. A passing verdict must list every criterion;
   an invalid one is refused before anything is written and spends no round.
5. Send findings back to `builder-core`, then use a fresh-context review of
   the delta. Continue without asking the person.
6. Stop after a passing review or after round three. The cap counts every
   round the store holds for the ticket, across resumes. At the cap, stamp the
   unresolved findings and exact next evidence needed; do not start round four.

## 6. Outward go — hard stop

A passing review records the plan snapshot and writes a stop stamp at outward
go before the pass reports that phase. Show the final diff summary,
verification evidence, acceptance-criterion evidence, decision records, and
every proposed outward action. Stop. Nothing may leave the machine before
explicit approval is recorded with `record_go("outward", …)`: no push, pull
request, Jira comment, status change, message, merge, release, or deploy. Call
`require_outward_go()` before each outward action; it checks the store.

After approval, perform only the listed outward actions under the target
repository's rules. Then call `close(point, refs=…, next_skills=…)`: it writes
a pending close stamp and reads the card back to confirm the stop point, both
go decisions, the review rounds, next skills, and references; only then does
it confirm the close. A mismatch raises and leaves the ticket open, in the
store and on resume; repair the card before reporting the ticket closed.

## More-information exits

Use exactly one when the pass cannot proceed:

- **person** — `waiting on person: <exact question>`; ask directly;
- **research** — `waiting on research: <exact question>`; give that question to
  `researcher`, then resume from the fresh ticket pull;
- **someone else** — `waiting on <role>: <exact question>`; stamp that exact
  text and draft a concise ask for the person to send. Do not contact the role
  or imply that the ask was sent.

Every exit writes a stop stamp with the current phase, next skills, and
references. Every later resume starts with the live ticket pull.

## Close and optional reflection

At any stop or close, persist what was tried (`record_tried`), decisions and
their effects, open questions (on the plan card), branch or PR ids, review
rounds and findings, current point, next skills, and references. Decision words are verbatim: a role's position uses
`who_kind: role` plus `relayed_by`; never attribute that position to the relay.
A lean becomes settled only through a cited outside event.

After close, offer `/reflect` only when the pass exposed a reusable defect in a
skill or rule. Propose at most one edit, show the reason and exact target, and
wait for the person's approval. Do not apply it as part of the ticket pass.

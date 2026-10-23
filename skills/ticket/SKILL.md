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

Both stores implement exactly eleven calls, seven for the card and four for
the session claim (see "One concurrency rule per store"):

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
  plan snapshot and its ref, what was tried, and the live session claim (`pass`: its
  session id, host and started-at, or none);
- `take_pass()` — starts a pass: writes the claim and returns its id,
  or refuses with `TicketHeld` while another pass is live;
- `join_pass(pass_id)` — continues that live pass from a new handle, and
  refuses unless it is the live pass;
- `release_pass(reason)` — ends this handle's pass and releases its claim;
- `clear_pass(pass_id)` — a person's `release` for a crashed session (below).

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
`.ticket/<key>.lock`. Before every write, the markdown adapter runs
`git check-ignore` for both files. If either is not ignored, stop and say: add
`.ticket/` to the target repository's ignore rules, then retry. Do not edit an
ignore file silently. Never stage or commit `.ticket/`.

### One concurrency rule per store

**Markdown.** Every write to the card takes an exclusive advisory lock on
`.ticket/<key>.lock` and holds it across the load, the precondition check and
the save. A second writer waits its turn. The save writes a temporary file
beside the card and renames it into place, and `confirm_close(position,
point)` re-checks inside the lock that the stamp at `position` is still the
pending close and still the last stamp. The operating system drops the lock
if its holder dies, so a crashed writer leaves nothing to clean up. A lock
path that is a symbolic link is refused untouched.

**Ledger.** A ticket has one live session. The first thing a session writes
is a claim record holding its session id, host and started-at time, and the
store releases it when the close is confirmed or when a person runs
`release`. A second session that finds a live claim is refused with
`TicketHeld`, which prints the claim's session id, host and started-at time,
and nothing of the refused write is stored. No claim expires by elapsed time.

The markdown card keeps the same claim in its `pass` field (session id, host,
started-at), read and changed under the same lock, so both stores refuse a
second session the same way.

**One live pass.** A pass outlives any one process: the plan go and outward
go stops end the turn, and every later step runs in a new process with a new
store handle. So the ticket's claim, not a process, is what keeps a second
session out.

- `TicketPass.open(key, source, store)` starts a pass and writes the claim.
  While another pass is live it is refused with that claim's session id, host
  and started-at time, along with the command that releases it. Read
  `session.pass_id` and keep it in the conversation; report it at every stop.
- Every later step of the same pass, in a new process, calls
  `TicketPass.open(key, source, store, resume=True, pass_id=<that id>)`. It is
  refused, naming the live claim, unless that pass is still the live one.
  Never take a pass id from the card or a refusal to continue a pass you did
  not start; the id is a handle for the pass, not a password.
- Every write checks the claim first. A store handle outside any pass writes
  only while no pass is live.
- The pass ends, and releases its claim, when `close` confirms the close, at
  a more-information exit, at the review-round cap, when any store call fails
  or a write is refused, when an exception leaves a `with` block over the pass
  (`with TicketPass.open(...) as session:`), or on `session.end()`. A
  validation error (`TicketError`, including a gate not yet approved) changes
  nothing and leaves the pass live. After it ends, the pass writes nothing; a
  new pass starts with `TicketPass.open(..., resume=True)` and rebuilds from
  the card.
- A session whose process crashed leaves its claim, and the ticket stays
  refused: that is deliberate. A person who has confirmed the session is gone
  releases it with `python3 ticket.py release <key> <session-id>` (add
  `--repo <target-repo>` for a markdown card). The command refuses unless that
  session id is the live one.

On the ledger the claim is a `session.opened` event naming the session, host
and start time, and a `session.closed` event naming it when the pass ends.
Within one process the precondition is also the ticket's revision, the newest
ledger event of the ticket a store handle has accounted for: opening or
continuing a pass pins it, and the handle's own writes move it. A write is
refused when the ticket holds a newer event, naming whoever wrote it, and each
append carries an idempotency key built from the revision, so of two skill
writers that read the same revision only the first append lands. The ledger
CLI cannot make the check and the append one step: it has no
expected-revision option. A write through another verb (for example `ledger
state`) that lands between them is accepted by the CLI, and the skill's event
lands after it. The adapter therefore reads the events again after every
append and, when anything landed in between, refuses by that writer's name and
says its own event landed after it and was not undone; check both with the
person. Two of those interleavings are handled before
the refusal. A claim that landed is released with a matching `session.closed`.
A close whose status write landed after an outside event is invalidated by a
note: it is detected and never silently trusted, and resume reports the close
as invalidated and not closed, naming the outside event, its writer and the
fields it set. The ledger CLI has no compare-and-set, so the skill never
writes ticket state as a repair and does not undo the outside write's
overwrite; the person reconciles status, owner, claim and blocked-by from the
printed line. The adapter never changes the ticket's status, owner, claim or
blocked-by fields, except that a confirmed close sets the status to `closed`
and keeps the other three. A ticket someone has claimed for work stays claimed
and can still be written.

## 1. Orient

1. Pull the ticket from its source. On resume, do this before `read_card()`.
2. Read the target repository's `AGENTS.md`, `CLAUDE.md`, contribution guide,
   pull-request template, relevant code, and verification commands when they
   exist. Team and repository conventions win over defaults here.
3. Follow the ticket's Figma and Confluence links as described above.
4. On a resume, `TicketPass.open(..., resume=True)` (with `pass_id=` when it
   continues the same pass) pulls the ticket, then reads the card and rebuilds
   the pass from it: the plan snapshot, the review-round
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

The pass stays live at this stop. Record the go from the same pass, continued
with its `pass_id`. The plan snapshot names the pass that presented it, and a
plan go names that pass too: `record_go("plan", …)` refuses unless the stored
plan is the revision this pass presented, and a go naming another pass opens
nothing. A different pass that resumes at an unapproved plan goes back to the
plan step and presents it again.

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

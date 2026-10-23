---
name: ticket
kind: portable
version: 1.0.0
description: >
  Run one attended pass over a ticket: pull live context, research, present a
  plan card for approval, build and review for up to three rounds, stop for
  approval before outward actions, and leave a durable resume card. Use when
  the person says "work this ticket", "take <key> through to a pull request",
  or "resume the ticket". NOT for a change that has no ticket (use /tdd or
  /debug), and NOT for a session resume note (use /handoff).
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

The store implements exactly eleven calls, seven for the card and four for
the session claim (see "Concurrency"):

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

The store is a markdown card. `choose_store(repo, key, title)` returns it. The
card lives at `<target-repo>/.ticket/<key>.md`, in the repository's ignored
scratch, next to its lock file `.ticket/<key>.lock`. Before every write, the
store runs `git check-ignore` for both files. If either is not ignored, stop
and say: add `.ticket/` to the target repository's ignore rules, then retry.
Do not edit an ignore file silently. Never stage or commit `.ticket/`.

A ledger-backed store is planned; today, setting `TICKET_LEDGER_CLI` selects
it and is refused (`ticket: the ledger store is not supported yet; use the
markdown store`, exit code 2) before anything is written.

### Concurrency

Every write to the card takes an exclusive advisory lock on
`.ticket/<key>.lock` and holds it across the load, the precondition check and
the save. A second writer waits its turn. The save writes a temporary file
beside the card and renames it into place, and `confirm_close(position,
point)` re-checks inside the lock that the stamp at `position` is still the
pending close and still the last stamp. The operating system drops the lock
if its holder dies, so a crashed writer leaves nothing to clean up. A lock
path that is a symbolic link is refused untouched.

A ticket has one live session. The first thing a session writes is a claim
in the card's `pass` field holding its session id, host and started-at time,
read and changed under the same lock, and the store releases it when the
close is confirmed or when a person runs `release`. A second session that
finds a live claim is refused with `TicketHeld`, which prints the claim's
session id, host and started-at time, and nothing of the refused write is
stored. No claim expires by elapsed time.

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
  `--repo <target-repo>` when you run it from outside that repository). The
  command refuses unless that session id is the live one.

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
   revision or review round. Without the plan go stamp it resumes at plan;
   without the outward go or round-cap stamp it resumes in a state whose only
   step writes that stamp (see Build and review). It flags why. An
   unconfirmed close stamp is flagged and the ticket stays open. State every
   flagged difference before reusing the plan, and read the card's
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
the plan go. The store refuses a plan snapshot that breaks these rules, or
whose steps are not the six above, once each and in order.

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
   passed_criteria=…)`. Each round records the plan revision it reviewed.
   `passes` flips only through a recorded round: `record_review` stores the
   round first, and each listed criterion then cites that round and its
   evidence path or id. The one other path is `record_outward_stop()` below,
   which completes a stored passing round of the current plan revision. A passing verdict must list every criterion;
   an invalid one is refused before anything is written and spends no round.
5. Send findings back to `builder-core`, then use a fresh-context review of
   the delta. Continue without asking the person.
6. Stop after a passing review or after round three. The cap counts every
   round the store holds for the ticket, across resumes. At the cap, stamp the
   unresolved findings and exact next evidence needed; do not start round four.
   The cap stops the pass only once its stop stamp is stored. If a session
   ended after round three was stored but before that stamp, a resume reports
   `cap stop not recorded` and the pass stays live with one allowed step:
   `record_cap_stop()`, which writes the stamp and ends the pass. A passing
   round works the same way: it reaches outward go only once its outward go
   stop stamp is stored. If a session ended after the passing round was
   stored but before that stamp (or before the plan snapshot that marks the
   round), a resume reports `outward stop not recorded`, and the one allowed
   step is `record_outward_stop()`. It brings the plan snapshot up to that
   round, writes the stamp, and waits at outward go. This applies only when
   that round reviewed the current plan revision; a passing round of an
   earlier plan covers nothing in a new one, so the pass resumes at build.

## 6. Outward go — hard stop

A passing review records the plan snapshot and writes a stop stamp at outward
go before the pass reports that phase. Show the final diff summary,
verification evidence, acceptance-criterion evidence, decision records, and
every proposed outward action. Stop. Nothing may leave the machine before
explicit approval is recorded with `record_go("outward", …)`: no push, pull
request, Jira comment, status change, message, merge, release, or deploy. Call
`require_outward_go()` before each outward action; it checks the store.

After approval, perform only the listed outward actions under the target
repository's rules. Then call `close(point, refs=…, next_skills=…)`. It first
checks the card: both go decisions, the review rounds, and a final review that
passed every criterion, with its evidence, to an outward go stop. It refuses,
writing no close stamp, when any of these fails. It then writes a pending
close stamp and reads the card back to confirm the stop point, next skills,
references and the same checks; only then does it confirm the close, and it
runs them once more on the confirmed card. A resume applies the same checks to
a closed card. A mismatch raises and leaves the ticket open, in the
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

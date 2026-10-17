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

Both stores implement exactly four calls:

- `write_stamp(point, phase, next_skills, refs, close=False)` — stop or close,
  with file and PR references kept as paths or ids rather than copied content;
- `record_decision(decision)` — `who`, `who_kind`, optional `relayed_by`,
  `standing`, outside `source`, `effect`, tags, verbatim words, phase, and
  evidence reference;
- `record_review(review)` — round, verdict, findings, reviewer, and evidence;
- `read_card()` — the ticket's resume card.

Use the event-ledger adapter only when `ledger` is on `PATH`, or
`TICKET_LEDGER_CLI` names the configured executable, and `ledger doctor`
succeeds for the configured database. Invoke only that CLI. Never import its
Python package or read its database directly.

Otherwise use `<target-repo>/.ticket/<key>.md`. Before every write, the markdown
adapter runs `git check-ignore` for that card. If it is not ignored, stop and
say: add `.ticket/` to the target repository's ignore rules, then retry. Do not
edit an ignore file silently. Never stage or commit `.ticket/`.

## 1. Orient

1. Pull the ticket from its source. On resume, do this before `read_card()`.
2. Read the target repository's `AGENTS.md`, `CLAUDE.md`, contribution guide,
   pull-request template, relevant code, and verification commands when they
   exist. Team and repository conventions win over defaults here.
3. Follow the ticket's Figma and Confluence links as described above.
4. Read the resume card and its referenced paths or ids on a resume. Reconcile
   it against the fresh ticket; state every changed fact before reusing a plan.
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

Present the plan card and ask the person to approve, change, or take a
more-information exit. Stop. Do not edit product files or dispatch
`builder-core` until explicit approval is recorded with `record_go("plan", …)`.

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
4. Only a review-evidence path or id may flip an acceptance criterion's
   `passes` field. A passing verdict is invalid while any criterion remains
   false.
5. Record each review round. Send findings back to `builder-core`, then use a
   fresh-context review of the delta. Continue without asking the person.
6. Stop after a passing review or after round three. At the cap, stamp the
   unresolved findings and exact next evidence needed; do not start round four.

## 6. Outward go — hard stop

When review passes, show the final diff summary, verification evidence,
acceptance-criterion evidence, decision records, and every proposed outward
action. Stop. Nothing may leave the machine before explicit approval is
recorded with `record_go("outward", …)`: no push, pull request, Jira comment,
status change, message, merge, release, or deploy.

After approval, perform only the listed outward actions under the target
repository's rules. Then write the close stamp and read the card back once to
confirm its stop point, decisions, review rounds, next skills, and references.

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

At any stop or close, persist what was tried, decisions and their effects, open
questions, branch or PR ids, review rounds and findings, current point, next
skills, and references. Decision words are verbatim: a role's position uses
`who_kind: role` plus `relayed_by`; never attribute that position to the relay.
A lean becomes settled only through a cited outside event.

After close, offer `/reflect` only when the pass exposed a reusable defect in a
skill or rule. Propose at most one edit, show the reason and exact target, and
wait for the person's approval. Do not apply it as part of the ticket pass.

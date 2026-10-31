# `ticket`: the rule note

The code is checked against `test-matrix.md`: every behavior here maps to a named test.
Scope: `skills/ticket/` (the pass, the plan identity, resume, both stores). The ledger CLI it relies on
is an external CLI that implements the contract in `SKILL.md`, at the version that has `append --expect-revision` and `append-many`.
Both stores are built: the markdown store (section 6.1) and the ledger store (section 6.2, matrix
section H). A configured `TICKET_LEDGER_CLI` selects the ledger store (section 6.3).

## 0. The four decisions behind the resume and plan-identity rules

Resume and plan identity go wrong in four ways, and each of the decisions below removes one of them
rather than guarding it:

1. **Several copies of "which plan is this".** The plan revision lived in the session, on the card, on
   each review, on each go decision, and each check compared a different pair of them (the session's
   cached revision, the card's revision, a subset of criteria, the source's criteria). Each added check
   binds one more pair.
2. **Half-written states.** A step was two or three store writes (review, then plan snapshot, then stop
   stamp; pending close stamp, then read-back, then confirm). A crash between writes needed its own
   resume state (`outward_stop`, `cap_stop`, "unconfirmed close") and each of those needed its own
   rules and messages.
3. **Trust read from mutable copies.** `passes` on the plan snapshot was a copy of what a review said,
   so it had to be re-verified against the review at every point of reliance, and the check could be
   skipped at one of them.
4. **Fail-open on a malformed card.** A stored plan that could not be read became a "change" or a
   revision 0.

The decisions:

- **D1. One plan identity, compared by equality.** The identity of a plan is `(revision, criteria)`:
  a whole number and the ordered criterion texts. Every record that relies on a plan (a review, a plan
  go, an outward go, the close) stores a full copy of the identity it was made for, and *covers* the
  card's plan exactly when its copy equals the card's identity. There is one function that reads the
  card's identity and one predicate that tests coverage. Nothing compares a revision alone, and no
  record is compared with the ticket source.
- **D2. Every transition that writes is one atomic, conditional store write, and the resume state is
  derived from the card.** A step decides on the card and token it *holds* from its last read or last
  successful write, and writes all of its records in one `apply` that lands only if that held token is
  still current. There is no half-written state, so `outward_stop`, `cap_stop`, the pending-close
  stamp and the close read-backs do not exist. The state an open lands at (the *resume state*) is a
  pure function of the card, the fresh source and the pass id; a live pass then holds its *live phase*
  in memory (section 3.2b) and never re-derives it from the card between writes. The card is trusted
  across a write only through the held token (section 3.5).
- **D3. Reviews own the evidence; the plan snapshot is immutable per revision.** A review records the
  criteria it passed. The plan snapshot carries no `passes`, `review_evidence` or `review_round`; the
  rendered card derives them from the stored rounds. Nothing on the card is a copy that could disagree
  with its source record.
- **D4. Fail closed on a corrupt card, flag only a source edit.** An unreadable card, an unidentifiable
  stored plan, or a closed status that does not validate is a refusal that writes nothing. Only an edit
  of the ticket source (and a card edit that a record no longer covers) is a *change*, which is a flag
  and a re-plan.

Both stores sit behind one four-call interface (section 6), so the pass has one code path. The
markdown store makes `apply` atomic with its lock and a content token; the ledger store makes it
atomic with `append-many --expect-revision`.

## 1. Vocabulary

- **Card**: the ticket's durable record, one per ticket key. Both stores produce the same card shape
  on read (section 2).
- **Token**: the store's opaque version of the card as read. Markdown: a digest of the card's canonical
  JSON. Ledger: the item's `revision` from `query` (the id of its newest event), or `none`.
- **Held token**, **held card**: the token and card from the pass's last read or last successful write
  (section 3.5). Every write carries the held token; every fresh read is compared with it.
- **Pass**: one live session over a ticket, identified by a random id, recorded on the card as its
  claim (`pass`: id, host, started-at). A pass outlives processes; a later process continues it with
  the same id. At most one pass is live per ticket.
- **Resume state**: what `_derive` computes from the card, the fresh source and the pass id (section
  3.2); where an open lands. **Live phase**: the phase a live pass holds in memory and moves with each
  step (section 3.2b). A step requires the live phase.
- **Identity** `I`: `(revision, criteria)` of the card's stored plan, read strictly (section 3.1).
- **Covers**: a record `r` covers `I` when `r.plan_revision == I.revision` and
  `tuple(r.criteria) == I.criteria`. Reviews, plan goes and outward goes carry both fields.
- **Round**: one stored review. The cap is three rounds stored on the card, counted across revisions
  and across passes.
- **Passing final review**: the card's last review, when its verdict is `pass`, it covers `I`, its
  `passed` lists every criterion index of `I`, and its evidence is a non-empty reference.
- **Change**: the ticket source's criteria differ from `I.criteria` (section 4.2), or a record of
  `I.revision` on the card does not cover `I` (section 4.3).
- **Flag**: a line in `reconciliation`. The open succeeds. **Refusal**: a `StoreUnavailable` with a
  code (section 4.5); the pass ends, nothing is written. **`TicketError`**: a wrong step or value;
  nothing is written and the pass stays live.

## 2. The card

```
{
  "schema": "ticket-card/v2",
  "key": "<ticket key>", "title": "<text>",
  "status": "open" | "closed",
  "pass": null | {"id": "<hex>", "host": "<text>", "started_at": "<utc>"},
  "plan": null | {
      "schema": "ticket-plan-card/v2", "key", "title",
      "revision": <int >= 1>, "pass": "<id of the pass that presented it>",
      "criteria": ["<text>", ...],                      # ordered, non-empty, each non-empty
      "steps": [{"name", "status", "skip"?} x 6],       # orient research plan build review outward
      "diff_statement"?: "<one sentence>",   # only while the plan step is skipped
      "open_questions": ["<text>", ...],
      "ticket_status"?: "<text>", "ticket_updated_at"?: "<text>"
  },
  "decisions": [{"who", "who_kind", "relayed_by"?, "standing", "source": {"what", "ref", "who"?, "text"?},
                 "effect", "tags": [{"type", "id"}], "verbatim", "phase", "evidence"?,
                 "gate"?: "plan" | "outward", "plan_revision"?: <int>, "criteria"?: [...], "pass"?: "<id>"}],
  "reviews":   [{"round": <int>, "verdict": "pass" | "changes", "plan_revision": <int>, "criteria": [...],
                 "passed": [<index>, ...], "findings": [...], "reviewer": "<text>", "evidence": "<ref>"}],
  "tried":     [{"what", "phase", "outcome"?, "evidence"?}],
  "stamps":    [{"point", "phase", "next_skills": [...], "refs": [...], "close": <bool>}]
}
```

- A **go decision** is a decision with `gate`, `plan_revision`, `criteria`, `who_kind: person`,
  `standing: settled`, `source.what: ratification`, and a `ticket` tag naming the key. A plan go also
  carries `pass`, the id of the pass that presented that plan (section 3.4). A go is never
  recognized by matching the `effect` sentence; `effect` is prose for people.
- **Stamps** are the human trail and the resume card: the point text, next skills and references at
  each stop. The derived state never depends on a stop stamp (D2 writes each stamp in the same `apply`
  as the record it describes, so a stamp is never missing). The one stamp the state reads is the close
  stamp: the last stamp of a closed card has `phase: close` and `close: true`.
- The `phase` of a stamp or a tried record is the *store phase*: one of `orient`, `plan`, `build`,
  `review`, `close`, mapped from the live phase as `orient` and `research` → `orient`; `plan` and
  `plan_go` → `plan`; `build` → `build`; `outward_go` → `review`; the close → `close`. Both stores hold
  this one vocabulary (it is the ledger's `PHASES` enum, section 6.2.1).
- The store validates every record on write with the validators the matrix pins (section I of the
  matrix), as stored and uncoerced.
- A card whose top-level fields are not of the shape above (a `reviews` that is not a list, a
  `stamps` entry that is not an object, a `status` outside the two values) is refused on read with
  `card_unreadable`, the same code as a markdown block that does not parse. Nothing in the pass
  defends against a non-list with `or ()`: the reader does it once.
- A stored plan snapshot is validated on read with the same rules as on write (the I5 and I7 rules),
  before any gate is derived: an unknown `schema`, a missing `key`, `title`, `steps` or
  `open_questions`, a key of another ticket, a malformed field, or a `diff_statement` while the plan
  step is not skipped is refused with `card_unreadable`. A snapshot whose identity cannot be read
  (section 3.1) keeps `plan_unidentifiable`: the identity check runs first, so a card that fails
  both is refused as unidentifiable. A skipped plan step carries the one-sentence `diff_statement`;
  completing that step clears it, so a snapshot never says the plan step is complete beside the
  diff that stood in for it.

## 3. The state machine

### 3.1 Reading the identity

`_identity(card) -> (revision, criteria) | None | refusal`:

- `plan` is `None`: no plan was presented; return `None`.
- Otherwise the plan must be an object; `revision` must satisfy `type(revision) is int and revision >= 1`
  (`True` and `1.0` are not whole numbers); `criteria` must be a non-empty list whose every entry is
  non-empty text; `pass` must be non-empty text. Anything else, `{}`, `[]` and `""` included, is a
  refusal `plan_unidentifiable`. There is no fallback revision: a change needs revision N+1 of a plan
  whose N can be read, and a guessed N could collide with a stored approval keyed by it.

### 3.2 Derived states

`_derive(card, source, pass_id) -> (state, flags)` reads only the card and the fresh ticket source. In
this order, the first match wins:

| # | Condition on the card | State | Notes |
|---|---|---|---|
| 0 | the card does not parse or has the wrong shape, or its plan snapshot breaks the snapshot rules (section 2) while its identity is readable | refusal `card_unreadable` | before anything else |
| 1 | `_identity` refuses | refusal `plan_unidentifiable` | in any state, closed included |
| 2 | `status == "closed"` and `_closed_problems(card)` is empty | `closed` | terminal; a source change is flag F7; a metadata change is F9, F10 or F12 (section 4.2) |
| 3 | `status == "closed"` and problems remain | refusal `close_invalid` | the card was edited or an outside writer set the status (ledger); a person repairs it |
| 4 | no plan (`_identity` is `None`) | `orient` | S0 |
| 5 | a change is present (section 4.2 or 4.3) | section 4.4 decides: `plan` or `stopped` | the change rows |
| 6 | three rounds stored and no passing final review covers `I` | `stopped` | S5, flag F5 |
| 7 | no plan go covers `I` | `plan_go` when `plan.pass == pass_id`, else `plan` with flag F8 | S1 |
| 8 | a plan go covers `I`, no passing final review covers `I` | `build` | S2 (rounds 0–2); the go is on the card, so a resume lands past `begin_build` |
| 9 | a passing final review covers `I`, no outward go covers `I` | `outward_go` | S3 |
| 10 | an outward go covers `I` | `outward_go`, `approved("outward")` true | S4 |

The seven card states the matrix uses: **S0 fresh**, **S1 presented**, **S2 building**, **S3 passed**,
**S4 approved**, **S5 capped**, **S6 closed**. The state above is the *resume state*; the phases a live
pass reports are the live phases of section 3.2b, which add `research`.

`stopped` and `closed` are terminal: an open that derives either reports it, takes no claim, and
returns a pass that is not live. A stopped ticket is left for a person (restore the criteria, or open
a new ticket).

### 3.2b Live phase and resume state

`_derive` runs once per open and gives the resume state. A live pass then holds its phase in memory
and moves it with the steps of section 3.3; between writes the phase is never re-derived from the card,
and the card is trusted only through the held token (section 3.5). Three transitions are in-memory
only and write nothing: `finish_orient` (`orient` → `research`), `finish_research` (`research` →
`plan`) and `begin_build` (`plan_go` → `build`). What a resume derives after each live phase:

| Live phase, reached by | Card | A resume derives |
|---|---|---|
| `orient`; `research` (`finish_orient`); `plan` (`finish_research`) | S0 | `orient`: the card does not record the three in-memory steps; the pass repeats them |
| `plan_go` before its go (`present_plan`) | S1 | `plan_go` for the same pass (join); `plan` with F8 for a new pass |
| `plan_go` approved (`record_go("plan")`, before `begin_build`) | S2[0] | `build`: the go is on the card and nothing is left to wait for; `begin_build` is not called again |
| `build` (`begin_build`; `record_review("changes")`, rounds 1–2) | S2[r] | `build` |
| `outward_go` before its go (`record_review("pass")`) | S3[r] | `outward_go` |
| `outward_go` approved (`record_go("outward")`) | S4[r] | `outward_go`, `approved("outward")` true |
| `plan` after a changed resume (section 4.4) | S1–S4 with the change | `plan` again, with the same flags, until `present_plan` lands |
| `stopped`; `closed` | S5; S6 | the same; not live |

A step requires the live phase, never the resume state: `present_plan` requires `plan`, which a fresh
pass reaches through the two in-memory steps and a resumed pass reaches only through a change (4.4)
or F8; `begin_build` requires `plan_go` with its go, and is a `TicketError` (wrong phase) at `build`,
so a resumed pass at S2 never calls it. The matrix pins the split in E17, E21 and E22.

### 3.3 Transitions

Every step: require the live phase and the pass's own claim on the held card, validate the values (a
`TicketError` writes nothing), then one `apply(held token, events)`; a refused `apply` is refusal
`stale` and the pass ends. A step that writes nothing but relies on the card (`begin_build`,
`require_outward_go`, `approved`) reads fresh and compares the token with the held one before anything
else: a different token is refusal `stale`, raised before the gate is checked, and the pass ends; an
equal token proves the fresh card is the held card, and the gate is checked on it. No step adopts a
fresh read's token. The held pair advances only after a successful `apply` (section 3.5). The events
are store-neutral (section 6).

| Step | Requires | Writes, in one `apply` | Then (live phase) | A resume now derives |
|---|---|---|---|---|
| `open(key, source, store)` | source pulled first; the card derives to a non-terminal state; no live pass | `claim {id, host, started_at}` | the resume state; the pass holds the token the claim returned | the same |
| `open(..., pass_id=P)` | `card.pass.id == P` | nothing (a join writes nothing) | the resume state; the pass holds the read | the same |
| `finish_orient`, `finish_research` | live `orient` → `research` → `plan` | nothing | in-memory | `orient` (S0) |
| `present_plan(plan)` | live `plan`; `plan.criteria == source.criteria` in order | `plan {revision: I.revision + 1 (or 1), pass: P, ...}`, `stamp {point: "Waiting at plan go for plan revision N.", phase: plan, next_skills: [ticket], refs: [plan ref]}` | `plan_go` | `plan_go` (join); `plan` with F8 (new pass) |
| `record_go("plan", evidence, verbatim)` | live `plan_go`; `card.plan.pass == P` | `decision {gate: plan, plan_revision, criteria, pass: P, ...}` | `plan_go` (approved) | `build` |
| `begin_build()` | live `plan_go`; a fresh read equal to the held token; `_has_go(card, "plan", I)` | nothing | `build`; `GateRequired` without the go | `build` |
| `record_review("changes", round < 3)` | live `build` | `review {round, verdict, plan_revision, criteria, passed, ...}` | `build` | `build` |
| `record_review("changes", round == 3)` | live `build` | `review`, `stamp {point: "Review did not converge after round three.", phase: review, next_skills: [ticket, review], refs}` | `stopped`; the pass ends | `stopped` |
| `record_review("pass")` | live `build`; `passed` names every criterion | `review`, `stamp {point: "Waiting at outward go after review round N.", phase: review, next_skills: [ticket], refs}` | `outward_go` | `outward_go` |
| `record_go("outward", ...)` | live `outward_go` | `decision {gate: outward, plan_revision, criteria, ...}` | `outward_go` (approved) | `outward_go`, approved |
| `require_outward_go()` | live `outward_go`; a fresh read equal to the held token; `_has_go(card, "outward", I)` | nothing | caller performs one outward action | unchanged |
| `close(point, refs, next_skills)` | live `outward_go`; an outward go covers `I` on the held card | `stamp {point, phase: close, close: true, ...}`, `closed` | `closed`; then `release` | `closed` |
| `record_tried(...)` | any live phase | `tried` | unchanged | unchanged |
| `more_info(destination, question)` | any live phase | `stamp {point: "waiting on <x>: <question>", phase: <store phase>}` | the pass ends | unchanged (the stamp is the human trail) |
| `end()` | — | `release` (unconditional) | not live | unchanged |

Gates: the **plan go** opens `begin_build`; the **outward go** opens `require_outward_go` and `close`.
Both are read from the card at the moment of reliance through `_has_go(card, gate, I)`, never from
session state; the card at that moment is the held card, or a fresh read proven equal to it by its
token. The **review cap** is three stored rounds. The **close** is the single atomic write above; there
is no pending stamp, no confirm and no read-back. It needs no validation of its own beyond the phase,
the go and the held token: a card at `outward_go` with its go satisfies `_closed_problems` by
construction, and the token guarantees that the card the close writes under is the one that derived to
that state. `_closed_problems` guards every later read (3.2 rows 2–3). No guard exists that no matrix
row can reach: a guard with no row is removed, not kept as defense.

### 3.4 Which pass a plan go belongs to

The plan snapshot names the pass that presented it (`plan.pass`). A plan go is recorded only by that
pass (`record_go("plan")` requires `card.plan.pass == P`) and carries `pass: P`. `_has_go(card, "plan",
I)` counts a plan go only when it covers `I` and its `pass` equals `card.plan.pass`. A new pass that
finds a presented plan without its go presents it again (flag F8): the person approves what the pass
in front of them showed.

### 3.5 What the pass holds in memory

The pass id, the held pair `(card, token)` from its last read or last successful write, the live
phase, the identity `I`, the rounds count, and the `PlanCard` rebuilt from the stored plan. The held
token is the only thing that binds the pass to the store:

- Every write is `apply(held token, events)`. The store compares the held token with the card as it
  is at write time (markdown: under the lock; ledger: `--expect-revision`), so an outside write that
  landed after the pass's last read refuses the step as `stale`, whatever a fresh read would show.
- Every fresh read by a live pass (`begin_build`, `require_outward_go`, `approved`) is compared with
  the held token before any validation or gate check; a different token is refusal `stale`.
- The pair advances in exactly one place: after a successful `apply`, the held token becomes the one
  `apply` returned and the held card becomes the held card with the events folded in. A `TicketError`,
  a refusal and a fresh read never move it.

A live pass never pulls the ticket source again; the next open sees a source edit.

## 4. The resume rules

### 4.1 Order

An open, with or without a pass id, runs: pull the source; read the card; `_derive`; then, for a new
pass, claim. The checks inside `_derive` run in the order of section 3.2. In particular a refusal
(unreadable card, unidentifiable plan, invalid close) wins over everything; a valid close wins over a
change; a change wins over the ladder.

### 4.2 Source edits

`_source_differences(I.criteria, source.criteria)` is exact (tuple equality of the stripped texts)
and counts occurrences. When the tuples differ it takes the multiset difference of the two lists and
names, in this order: each occurrence on the source beyond the stored count of that text, walking the
source in order (`acceptance criterion is new or changed on the ticket: <text>`, one line per extra
occurrence); each occurrence on the card beyond the source count, walking the card in order
(`acceptance criterion was removed from the ticket: <text>`, one line per missing occurrence); and,
when neither walk names anything (the same multiset in another order), `the acceptance criteria were
reordered on the ticket`. In code: `extra = Counter(source); extra.subtract(stored)`; walk the source,
naming a text while `extra[text] > 0` and decrementing; walk the stored list, naming a text while
`extra[text] < 0` and incrementing; fall through to reordered. The kinds the matrix uses:

| Kind | Source versus stored | Named as |
|---|---|---|
| reorder | same texts, another order | reordered |
| text edit | one text replaced | new or changed + removed |
| added | one more text | new or changed |
| removed | one fewer text | removed |
| duplicate added | `["A"]` → `["A", "A"]` | new or changed: A |
| duplicate removed | `["A", "A"]` → `["A"]` | removed: A |
| duplicate moved | `["A", "A", "B"]` → `["A", "B", "A"]` | reordered |

Why exact: the review vouched for exact texts; a normalizing rule needs two correct implementations.
Leading and trailing whitespace is stripped by `Ticket.from_mapping` on both sides already; interior
whitespace, case and punctuation count. Why occurrences: a membership diff sees `["A", "A"]` and
`["A"]` as the same set and falls through to "reordered", which names nothing a person can act on; the
multiset diff names the extra or missing occurrence. A change to status, title, description, links or
`updated_at` is never a change. The three the plan snapshot stores are compared explicitly against
the fresh source and are flags, in this order: status (F9), update time (F10), title (F12). The update
time is nullable: a time that appears where the snapshot stored none, one that disappears, and one
that differs are each F10. Description and links are not stored, so they are not compared. The flags
are read in every state, a closed card included, where they follow F7.

### 4.3 Card-side changes

A review or plan go whose `plan_revision == I.revision` but which does not cover `I` means the card's
plan was edited after that record. It is a change with flag F11 (one line per record), handled as a
source change would be. Its F11 lines stand alone: F1, F2, F3 and F6 are not added for it. Records of other revisions are history and cover nothing; they are not flags.

### 4.4 What a change does, by state

| State when the change is seen | Resulting state | Written | Flags |
|---|---|---|---|
| S1 presented (no go) | `plan` | nothing | F1 |
| S2 building, rounds 0–2 | `plan`; the stored rounds count toward the cap | nothing | F1 |
| S2 building, rounds 3 | `stopped`; the pass is not live | nothing | F5, F6 |
| S3 passed, rounds 1–2 | `plan`; the passing round stays as history | nothing | F2 |
| S3 passed, rounds 3 | `stopped` | nothing | F3 |
| S4 approved (outward go recorded) | as S3 by rounds | nothing | F2 or F3, plus F4 |
| S5 capped | `stopped` | nothing | F5, plus F6 for a source change |
| S6 closed, close validates | `closed` | nothing | F7 (source change); F9, F10, F12 (metadata change) |

"Written: nothing" means nothing beyond the new pass's own claim; a join writes nothing, and a
`stopped` or `closed` open takes no claim. F1, F2, F3 and F6 are produced only for a source change
(section 4.6); a card-side change alone yields its F11 lines (section 4.3). S2 at three rounds has no
passing final review, so it is S5: its flags are F5 and F6, not F1.

A change at S1–S4 below the cap sends the pass to `plan`: it presents revision N+1 with the source's
criteria, and that revision needs its own plan go and its own review rounds. Rounds already stored
count toward the cap. The pass holds the *stored* plan after a changed resume (`session.plan` returns
revision N's card with its stored texts); `present_plan` is where the new texts enter.

### 4.5 Refusal codes

| Code | Meaning | Notes |
|---|---|---|
| `held` (`TicketHeld`) | another pass is live; the message names its id, host, started-at and the release command | the command names the installed script by its absolute path (the directory of `ticket.py` itself, as `SKILL.md` writes `${CLAUDE_SKILL_DIR}/ticket.py`), so it runs from the target repository. The command is rendered with `shlex.join` over its whole argv (interpreter, script path, subcommand, key, pass id), so no stored value can add a shell word. A pass id is lowercase hex; a card whose live pass id is anything else is `card_unreadable` |
| `pass_ended` | this handle's pass has ended, or is no longer the live pass; nothing was written | a refusal (`StoreUnavailable`), never a `TicketError`: every step taken on a pass that is not live raises it, including any step on a pass that stopped at the cap |
| `handle_used` | the store handle is bound to a pass, or a pass it held has ended, and a binding event (`claim` or `join`) or an `open` reached it; nothing was written | a refusal (`StoreUnavailable`) raised by `apply` under the store's lock on a `claim` or `join` (the guard), ahead of the card read and the token check, so it wins over `stale` and over every card refusal, and by `open` before `source.pull` (the early report), for a new pass and for a join alike; the message names the used pass and the fix (open each pass with a fresh store handle); the lost-claim handler re-raises it unchanged. The rule it enforces is section 5's one handle per pass |
| `plan_unidentifiable` | the stored plan cannot be read (section 3.1) | also raised when the plan is `{}`, `[]` or `""` (only `None` is "no plan") |
| `card_unreadable` | the card does not parse or has the wrong shape, including a plan snapshot that breaks the snapshot rules while its identity is readable (section 2); on the ledger, also a ticket record note whose frame, version or body cannot be read (section 6.2.3) | |
| `close_invalid` | `status` is closed but the close does not validate | a valid card can only reach `closed` through the atomic close (D2), so an invalid close is an edit or an outside status write, and D4 says a corrupt card is refused, not resumed into a writable pass |
| `stale` | the card changed since this pass last read or wrote it: a write refused under the held token, a fresh read whose token differs from the held one, or on the ledger a batch key already taken (`conflict`); the message names the newest writer when the store knows it (ledger) or says the card on disk changed (markdown) | the held token detects every change under a live pass at every write and every fresh read, so there is one code and one place that raises it |
| `ledger_refused` | the ledger CLI refused a read or write for a reason that is not a race (section 6.2.4: validation, environment, a missing item or database, integrity, or an unresolved ambiguous answer); the message carries the CLI's `error.code` and message | a permanent refusal is never reported as the retryable `stale` |
| `ledger_incompatible` | the selected ledger CLI lacks `append-many` or `--expect-revision`, or answers `usage` to a command the store sends | |
| `outside_repository` | `.ticket/` resolves outside the repository; nothing was written | markdown store, on every read and write |
| `not_ignored` | the card or its lock is not git-ignored; no ignore file was changed and nothing was written | markdown store, before every write |
| `lock_not_regular` | `.ticket/<key>.lock` exists but is not a regular file; it was not used and nothing was written | markdown store; the lock is opened without following a link, so a link at the lock path is `lock_unopenable` instead |
| `lock_unopenable` | the lock file could not be opened as a regular file through the directory; the message carries the operating system's error | markdown store; a link or a directory at the lock path lands here |
| `card_not_regular` | `.ticket/<key>.md` exists but is not a regular file (a link, a FIFO, a directory, a socket); it was not read and nothing was written | markdown store, on every read and write; an existing node at the card's path that is not a regular file is refused before it is opened (a no-follow `stat` through the verified `.ticket/` directory descriptor), the open itself follows no link, what it opened is checked once more (`fstat`) as the race check, and an open that fails with the error such a node gives (`ELOOP` for a link; `ENXIO`, `EOPNOTSUPP` or `ENOTSUP` for a socket) lands here from that error alone, with no second look at the path, as does one whose failure a second no-follow `stat` traces to a non-regular node still there; so a link, a FIFO, a directory or a socket at the card path is refused by whichever of the three sees it; any other error of the open (a permission error on a regular file) propagates |

`GateRequired` (a `TicketError`) stays for a step taken before its go.

### 4.6 Flag texts (verbatim)

- F1 `the acceptance criteria changed since plan revision {M} was stored ({what}); plan revision {M} must be presented again as a new revision for a new plan go`
- F2 `review round {N} passed plan revision {M}; the criteria changed since ({what}); that round covers nothing in a new plan`
- F3 `review round {N} passed plan revision {M}; the criteria changed since ({what}); no review round is left for a new plan; a person restores the criteria or opens a new ticket`
- F4 `the outward go of plan revision {M} authorizes nothing now; outward actions already taken are not undone`
- F5 `three review rounds are stored and none is a passing round of plan revision {M}; the review cap is reached`
- F6 `the acceptance criteria changed since plan revision {M} was stored ({what}); review stopped at the round cap, so no new plan can be reviewed`
- F7 `the ticket's criteria changed after close ({what}); the ticket stays closed; follow-up work is a new ticket`
- F8 `plan revision {M} was presented by another pass and has no plan go; resuming at plan to present it again`
- F9 `ticket status changed: {old} -> {new}`
- F10 `ticket was updated since the plan card was written`
- F11 `review round {N} reviewed other acceptance criteria than plan revision {M} on the card` / `the plan go of plan revision {M} approved other acceptance criteria than the card's plan`
- F12 `ticket title changed: {old} -> {new}`

`{what}` is the `; `-joined list from 4.2 alone: F11 lines (4.3) stand alone and are never part of
`{what}`. F1, F2, F3 and F6 appear only when the ticket source differs from the stored criteria; a
card-side change with an unchanged source produces its F11 lines (and F5 at the cap, F4 after an
outward go) and nothing else. F2 and F3 are produced only when the last review
passes and covers `I`; with an earlier revision's passing round as the last review, F1 is produced
(the second-edit case). No flag ever says the card lacks a round it holds.

### 4.7 Live-pass rules

- Every write is `apply(held token, events)`. A stale token is refusal `stale`; the pass ends. The
  held token advances only when the `apply` succeeds (section 3.5).
- `require_outward_go()` reads fresh, requires the token equal to the held one (else refusal `stale`,
  before anything else) and `_has_go(card, "outward", I)`; it is called before each outward action.
- `approved(gate)` reads fresh, requires the token equal to the held one (else refusal `stale`) and
  returns `_has_go`. The outward gate answers true only while the live phase is `outward_go`: once a
  change has sent the pass back to `plan`, the stored outward go reads false (F4), even though the
  card still holds it. The plan gate returns `_has_go` as written.
- A `TicketError` (wrong phase, invalid value, `GateRequired`) writes nothing and leaves the pass
  live. Any other exception past validation (a store read or write failure, `held`, `stale`,
  `ledger_refused`) ends the pass and releases its claim, then re-raises. A failed release leaves the
  claim: the ticket stays refused until a person runs `release`, which fails closed.
- The pass ends on `close`, `more_info`, the cap, `end()`, an exception leaving a `with` block, or a
  refusal. After it ends every write refuses `pass_ended`.

## 5. The pass claim

One live pass per ticket. `open` reads first and claims only when the derived state is not terminal
and no pass is live. The claim is a conditional write at the token read, so two sessions racing from
the same card cannot both claim: the loser's write is stale, it re-reads once, and reports `held`
naming the winner. A person's `release <key> <pass-id>` clears the live claim only when the id names
it. Release is the one unconditional write: it clears this handle's own claim whatever has landed
since, so a pass that gave up never stays held. No claim expires by time.

One live pass per ticket, and **one store handle per pass**, enforced at the one moment it can be:
inside the store, under its own lock, as part of the write that binds the handle. A handle carries the
pass it writes for (`pass_id`), and `release` clears whatever pass the handle holds; so a handle that
came to hold a pass other than the one its caller thinks it holds would let `release` clear that other
pass's claim. The rules below make that state unreachable. Each has matrix rows.

1. **A handle binds once, under the lock, or not at all.** A *binding event* is `claim` or `join`.
   `apply` runs, inside the lock, in this order: (a) if the events carry a binding event and the
   handle is used (bound to a pass, or a pass it held has ended), refuse `handle_used` naming the
   used pass, before the card is read and before the token check, so this refusal wins over a stale
   token and over every card refusal; then load the card and refuse `stale` if its token differs
   from the held one; (b) check the claim against the card as loaded, with the pass
   the join names for a join (the live pass must be that id) and no pass for a claim (none may be
   live, else `held`); (c) fold the events; (d) save, except for a join, which changes no card byte;
   (e) bind the handle to the pass. All five happen under one hold of the lock, and nothing binds a
   handle anywhere else. Two opens that share one fresh handle therefore never both claim: the second
   one's claim meets a bound handle under the lock and is refused `handle_used`, whatever the card
   showed before and whether or not its token is still current.
2. **A binding event travels alone.** An `apply` whose events carry a `claim` or `join` beside any
   other event is a `TicketError` and writes nothing: a bind is one step and never rides with a record.
3. **`open` writes nothing into a store.** It reads `pass_id` and `ended` for an early `handle_used`
   report before `source.pull` (a courtesy to the caller; the lock-time check is the guard) and then
   drives the store only through `read` and `apply`: a new pass sends `[claim]`, a join sends
   `[join {id: P}]` under the token it just read. `pass_id` and `ended` are read-only on the interface
   (section 6); a store sets them only in its own `apply` and `release`, so a second store conforms
   from the interface alone.
4. **A `join` is an event with no card effect.** It names exactly the id of the live pass it continues
   (lowercase hex, like a claim id); folding it changes nothing; the markdown store saves nothing for
   it, so the card's bytes and modification time are unchanged and the token returned is the one
   passed. The ledger store appends nothing for it and binds the handle under its own serialization
   (section 6.2).
5. **`release()` clears only its own claim.** It reads the handle's `pass_id`, marks the handle ended,
   and under the lock clears the card's `pass` only when its id equals that one. By rule 1 a handle
   never held another id, so no pass object can release a pass it did not open, and a handle that was
   never bound clears nothing. This is what makes the unconditional release safe.
6. **The lost-claim handler passes `handle_used` through.** A refused claim is re-read once and
   reported `held` naming the winner, except a `handle_used`, which is re-raised unchanged: `held`
   would send the caller to the release command for a pass that is not stuck, while the fix is a fresh
   handle. Because the used-handle check runs ahead of the token check (rule 1), the second of two
   opens on one handle whose reads both predate the first's claim is refused `handle_used`, not
   `stale`, so the handler never names the pass that same handle now holds.
7. **Sequential reuse is refused before the pull.** A bound or ended handle given to `open`, for a new
   pass or a join, is refused `handle_used` before `source.pull`, with the card untouched. A fresh
   handle after a pass ends opens and joins as before.
8. **A failed binding write leaves the handle unbound.** If the save raises during a claim, step (e)
   never runs: the handle stays fresh and a retry on it claims.
9. **Every markdown-store refusal carries a code** (section 4.5).

A handle's states: *fresh* (no pass, nothing ended) becomes *bound to P* only when `apply([claim])` or
`apply([join])` succeeds under the lock, and a failed save leaves it fresh; `release()` takes it to
*ended, held P*; neither later state returns to fresh, and a binding event on either is `handle_used`
(any other event on an ended handle is `pass_ended`). A handle shared by threads for the *same* pass
stays supported: they take turns on its thread lock.

## 6. Both stores behind one interface

```
read()                      -> (card, token)
apply(token, events)        -> token      # all events land, or none; refuses `stale`
release()                   -> None       # unconditional; clears this handle's claim
clear_pass(pass_id)         -> None       # a person's release: refuse unless pass_id is the live claim
pass_id                     -> str | None # read-only: the pass this handle writes for; its own apply binds it
ended                       -> str | None # read-only: the last pass this handle held that has ended, or None
```

Events: `claim`, `join`, `release`, `plan`, `stamp`, `decision`, `review`, `tried`, `closed`. The pass
folds them into the card shape of section 2 in memory for validation; the store validates each event
on write with the same validators (section I of the matrix) and refuses the whole `apply` on any
invalid value (`TicketError`, nothing written). The `token` passed to `apply` is always the pass's held
token. `claim` and `join` are the *binding events* (section 5): each travels alone in its `apply`, binds
the handle to its pass under the store's serialization, and is refused `handle_used` on a handle that is
bound or has held an ended pass. A `join` names the live pass it continues and changes no card; a caller
binds a handle only by sending one of the two, never by assigning `pass_id`.

### 6.1 Markdown store (first)

- The card is `<repo>/.ticket/<key>.md`: a rendered summary above one `<!-- ticket-card:v2 ... -->`
  block holding the JSON on one line with `<`, `>` and `&` escaped. Both the card and its lock
  `.ticket/<key>.lock` must be git-ignored; the store runs `git check-ignore` before every write and
  refuses without editing any ignore file. Never staged, never committed.
- **Token**: SHA-256 of the canonical JSON (`sort_keys`, compact separators) of the card as loaded;
  `none` when the file does not exist.
- **Through the directory**: every filesystem operation the store makes on a card, whether read,
  temporary-file create, write, replace or cleanup, goes through the verified `.ticket/` directory
  descriptor (opened without following a link) and never through a path, so a `.ticket` swapped for
  a link after that open cannot send the card outside the repository; an existing card-path node that
  is not a regular file is refused `card_not_regular` before it is opened, with the post-open `fstat`
  kept as the race check, and an open that fails with a link's or a socket's error is refused from
  that error alone.
- **`apply`**: take the handle's thread lock, then an exclusive `flock` on the lock file (opened with
  `O_NOFOLLOW` through a directory fd; a lock path that is not a regular file is refused untouched;
  creating the lock file through the directory retries up to five attempts on `ENOENT`, because two
  first writers creating it at the same moment can see that error);
  for a binding event (a `claim` or `join`, alone in the call), refuse `handle_used` if this handle
  is bound or has held an ended pass, before the card is touched;
  check and open the card through the same directory descriptor (a no-follow `stat` refuses
  `card_not_regular` before the open unless the node is a regular file, the open follows no link,
  and an `fstat` of what opened is the race check), before a byte is read; load; compute the token;
  refuse `stale` if it differs from the expected (held) one;
  check the claim (a bound handle writes only while its pass is live, an unbound
  handle only while none is, a join only while the pass it names is live); fold the events; create a
  temporary file beside the card (exclusive, no link followed, a random name), write it and rename it
  into place, every step relative to the same directory descriptor, except for a join, which saves
  nothing;
  bind the handle to the claim's or join's pass, still under the lock; return the new token (a join
  returns the one passed). A save that fails leaves the handle unbound. The operating system drops
  the lock if the holder dies. A hand edit of the card between two steps changes the digest, so the
  next write refuses `stale` and the pass ends.
- **`release`**: under the lock, load, clear `pass` only if its id is this handle's, save. No token.
- **`read`**: no lock and no ignore check; `.ticket/` and the card are opened the same way, without
  following a link, and a card that is not a regular file is refused `card_not_regular` before it is
  opened; an absent directory or card is the blank card.
- `closed` sets `status: "closed"`; the close stamp in the same `apply` has `close: true`.

### 6.2 Ledger store (second)

The ledger's event schemas are closed: a kind takes exactly the keys its schema lists and refuses any
other. `decision.recorded` and `review.recorded` carry neither a plan identity nor a gate, and a
decision cannot cite an event that lands in the same batch (its `source_event_id` must already
exist). The store therefore writes the ticket's own records (plan, decision, review) as `item.note`
events whose `text` is a self-describing *ticket record* (6.2.3), and uses the ledger's own kinds only
where the schema holds every field unchanged (`item.created`, `session.*`, `item.stopped`,
`item.tried`, `item.state`). No event cites another event; every record is complete on its own, so a
batch resolves nothing and the ledger needs no new schema.

Mapping of events to ledger kinds, the writer name `W` in each payload's `source`, ticket key `K`,
pass `P`, held token `R`, close token `T`:

| Event | Ledger kind, payload (every key named; no other key is sent) | Idempotency key |
|---|---|---|
| `claim` on token `none` | `item.created {type: "ticket", title, source: W}`, then the `claim` row below, in one batch | `ticket-item/K/created` |
| `claim` | `session.opened {purpose: "ticket pass <id> on <host> at <started_at>", source: W}` | `ticket-pass/K/P/claimed` |
| `join` | no event: a join appends nothing; the handle binds to `P` under the store's own serialization, and a used handle is refused `handle_used` before any CLI call | none |
| `release` | `session.closed {outcome, source: W}`, no `--expect-revision` | `ticket-pass/K/P/released` |
| `plan` | `item.note {text: "ticket-plan:v2 " + canonical JSON of the plan snapshot, source: W}` | ordinary |
| `decision` | `item.note {text: "ticket-decision:v2 " + canonical JSON of the decision record, source: W}` | ordinary |
| `review` | `item.note {text: "ticket-review:v2 " + canonical JSON of the review record, source: W}` | ordinary |
| `stamp` | `item.stopped {point, phase: <store phase>, skills: next_skills, refs, source: W}`; `close` is not a payload key: the normalizer derives it from the key on read | ordinary; the close stamp: `ticket-close/K/P/T/stop` |
| `tried` | `item.tried {what, phase: <store phase>, outcome?, evidence?, source: W}`; an absent optional field is omitted, never sent as null | ordinary |
| `closed` | `item.state {status: "closed", owner?, claim?, blocked_by?, source: W}`; `owner`, `claim` and `blocked_by` carried from the read when present | `ticket-close/K/P/T/state` |

Ordinary keys are `ticket-write/K/R/<request token>/<n>`: one random request token per `apply`, `n`
the event's index in the batch. A retry of the same `apply` reuses the request token. Every batch
element is sent with `kind`, `payload`, `item_id: K` and `idempotency_key`, and nothing else: payload
version 1 is the only version the schema module defines at this head, so a ticket record's own
version lives inside the note text, where the skill, not the ledger, reads it.

#### 6.2.1 Every written field, against the schema

| Kind, field | Schema rule at this head | What the store sends | Pinned by |
|---|---|---|---|
| `item.created.type` | enum of item types | `"ticket"` | H23 |
| `item.created.title` | text, length ≥ 1 | the ticket title (the source refuses an empty title) | I1, H23 |
| `session.opened.purpose`, `session.closed.outcome` | optional text ≥ 1 | the purpose or outcome sentence | H1, H13 |
| `item.note.text` | text ≥ 1, no maximum | frame, one space, canonical JSON (6.2.3) | H18, H24 |
| `item.stopped.point` | text, 1–600 | the point; the skill refuses an empty or over-long point first | I2 |
| `item.stopped.phase` | enum `orient plan build review close` | the store phase (section 2) | I2, H8 |
| `item.stopped.skills` | ≤ 12 entries, each 1–80 matching `[A-Za-z0-9][A-Za-z0-9._:/-]*` | `next_skills` | I2 |
| `item.stopped.refs` | ≤ 20 entries, each ≤ 300, one line, a ref target | `refs` | I2 |
| `item.tried.what` | text ≥ 1 | `what` | I8 |
| `item.tried.phase` | enum, as above | the store phase | I8 |
| `item.tried.outcome` | optional text ≥ 1 | `outcome`, when present | I8 |
| `item.tried.evidence` | optional, ≤ 300, one line | `evidence`, when present | I8 |
| `item.state.status` | enum `open claimed blocked closed` | `"closed"` | H8 |
| `item.state.owner`, `item.state.claim` | optional text ≥ 1 | the values `query` reported, when non-empty | H8 |
| `item.state.blocked_by` | optional list of text ≥ 1 | the latest `item.state`'s list, when present | H8, H10 |
| every `source` | optional text ≥ 1 | `W`, never empty | H24 |

The skill's validators (section I of the matrix) are at least as strict as each rule, so a payload the
skill accepts is one the schema accepts; H24 pins this by running the ledger's own `validate_payload`
over every batch the store builds. The kinds the store no longer writes are `decision.recorded`,
`review.recorded` and `outside.recorded`; no payload carries `source_event_id`.

#### 6.2.2 What each transition appends

One `append-many K --expect-revision R` per step, the batch in this order, with the keys of the
mapping table:

| Step | Batch | `--expect-revision` |
|---|---|---|
| `open` (new pass), token `none` | `item.created`, `session.opened` | `none` |
| `open` (new pass), token `R` | `session.opened` | `R` |
| `open(pass_id=P)`; `finish_orient`; `finish_research` | nothing | — |
| `begin_build`; `require_outward_go`; `approved` | nothing (a `query` for the token comparison, then `export-item` only when equal) | — |
| `present_plan` | `item.note` (plan), `item.stopped` (phase `plan`) | `R` |
| `record_go("plan")` | `item.note` (decision) | `R` |
| `record_review("changes")`, rounds 1–2 | `item.note` (review) | `R` |
| `record_review("changes")`, round 3 | `item.note` (review), `item.stopped` (phase `review`) | `R` |
| `record_review("pass")` | `item.note` (review), `item.stopped` (phase `review`) | `R` |
| `record_go("outward")` | `item.note` (decision) | `R` |
| `record_tried` | `item.tried` | `R` |
| `more_info` | `item.stopped` (the store phase of the live phase) | `R` |
| `close` | `item.stopped` (phase `close`, key `ticket-close/K/P/T/stop`), `item.state` (key `ticket-close/K/P/T/state`) | `R` |
| `end()`, release | `session.closed` | none |

#### 6.2.3 Ticket records in notes

A ticket record is an `item.note` whose `text` matches `^ticket-([a-z]+):v([1-9][0-9]*) (.+)$` with
`.` matching newlines: the *frame* names the record kind and its format version, and the body is the
canonical JSON (`sort_keys`, compact separators) of the record exactly as the markdown card stores it
(section 2). `ticket-plan:v2` carries the plan snapshot; `ticket-decision:v2` the decision record,
with its `source` object and its `gate`, `plan_revision`, `criteria` and `pass` inside the JSON;
`ticket-review:v2` the review record, with `plan_revision`, `criteria` and `passed` inside. On read
the normalizer walks every `item.note` of the export in id order:

- a text that does not match the frame is an ordinary note and folds into nothing;
- a frame whose kind is not one of the three, or whose version is not 2, is refusal `card_unreadable`
  naming the frame (a record this skill does not know is never skipped);
- a body that is not a JSON object, or that the record's section-I validator (I3, I4, I5) refuses, is
  refusal `card_unreadable`;
- the latest plan note is `card.plan` (earlier plan notes are the history of earlier revisions);
  every decision note is one entry of `decisions` and every review note one entry of `reviews`, in id
  order.

The ledger's own `decision.recorded`, `review.recorded` and `outside.recorded` events on the item are
another tool's and fold into nothing (H12).

#### 6.2.4 Reading the CLI's answer

The store runs every command with `--json` and decides on the envelope's `ok` and `error.code`, never
on the exit status alone (the status is kept for the message). The codes the CLI's error mapping
produces at this head, and what each becomes:

| `error.code` (exit) | The CLI raises it when | Class | Becomes |
|---|---|---|---|
| `ok: true` (0) | the batch landed, or a stored batch replayed | landed | the token becomes `data.events[-1].id` |
| `stale_revision` (5) | `--expect-revision` no longer matches the item's newest event | lost race (the revision condition) | refusal `stale`; the message is the CLI's, naming the newest event's actor and session |
| `conflict` (3) | a batch key is already stored under a different event | lost race (the write-once key condition: two sessions creating the item under `ticket-item/K/created`, or a key another writer took) | refusal `stale`; the message is the CLI's, naming the key |
| `validation` (3) | a payload fails its schema (rolled back, nothing written) | permanent: a skill/ledger mismatch the section-I validators should have caught | `StoreUnavailable` `ledger_refused` |
| `missing_harness`, `shared_database`, `synced_path` (3) | the environment refuses writes | permanent | `ledger_refused` |
| `not_found` (1) | the item does not exist | permanent on a write; on `read` it means no item: the blank card with token `none` | `ledger_refused` / the blank card |
| `missing_db` (1) | the database file does not exist | permanent | `ledger_refused` |
| `integrity` (4) | the hash chain is broken | permanent | `ledger_refused` |
| `usage` (2) | the CLI does not accept the command shape | permanent: another CLI version | `ledger_incompatible` |
| `error`, `unusable` (3), any code not listed, no envelope, a crash, a lost response | may be raised after the transaction committed (for example while closing the database) | ambiguous | the batch-key lookup below; landed → the token advances; absent → `StoreUnavailable` `ledger_refused` with the CLI's output |

Only `stale_revision` and `conflict` are races, and `stale` is raised for nothing else. A permanent
refusal ends the pass with no retry and no batch-key lookup (the batch was rolled back or never
started); its message carries the CLI's code and message so a person can read what the ledger
refused. At open, a refused claim (`stale_revision` or `conflict`) re-reads once and reports `held`
(section 5).

- **`read`**: `ledger --json query --item K` gives the token (`revision`, the id of the newest event
  of any kind, `none` for no events) and the status, owner and claim; `not_found` means no item and
  reads as the blank card with token `none`; a row whose `type` is not exactly `ticket` is refused
  `ledger_refused` before any fold, claim or append; `export-item K` gives the events to fold into the card
  (6.2.3 for the notes; `session.*` are shown only while the item is open, so `pass` is `None` on a
  closed item; the latest `item.state`'s `blocked_by` is carried into the close). The token is never
  the largest id in `export-item`, which hides ephemeral events and would make a held revision
  permanently stale on a closed item.
- **`apply`**: `ledger --json append-many K --expect-revision R` with the batch on stdin, `R` the
  held token. The CLI checks the revision once, inside the transaction, before the first event not
  already stored, and writes all or none. The answer is read by 6.2.4. The claim check runs on the
  held card; the revision check makes it hold at write time, so the ledger store needs no separate
  ownership read before each write. The release is exempt from both (after the closed state lands the
  ledger hides the pass, and giving up a ticket is safe whatever landed).
- **Ambiguous outcomes** (6.2.4's last row): the store exports the item and looks for the batch's
  last key with the same kind and payload; found means the batch landed (the token becomes that id);
  absent means it did not, and the write raises `StoreUnavailable` (`ledger_refused`) with the CLI's
  output. A retry reuses the keys: a stored batch replays with no write and no revision check. Never a
  retry under new keys. The release's failures are reported and ignored.
- **The close** is one `append-many` of `[stop S, state C]` under `--expect-revision R`, with a random
  close token `T` shared by their keys. There is no orphan stamp: S and C land together or not at all.
- **Trusting a closed status on read**: the normalizer marks the close stamp `close: true`, and so
  lets `_closed_problems` pass, exactly when: the status is `closed`; the latest `item.state` C has
  key `ticket-close/K/<pass>/<token>/state`; the event before C in the export is an `item.stopped` S
  with phase `close`, key `ticket-close/K/<same pass>/<same token>/stop` and `source` equal to C's; and
  S is the item's latest `item.stopped`. Otherwise the card reads as `status: closed` with no confirmed
  close stamp, which `_derive` refuses as `close_invalid` and never claims. A later `item.note`,
  decision, review or link does not disturb a trusted close; a later `item.stopped` or `item.state`
  does, because each is caught by "latest stop" or "latest state". An outside `state` write has a
  random key and cannot match. The card-level checks of `_closed_problems` then run as for markdown.
- **Capability**: at store selection the store runs the CLI's `append-many --help` and refuses
  `ledger_incompatible`, before any write, unless it exits 0 and names `--expect-revision`.
- **The close is one conditional batch append.** The claim batch, the review batch and the close batch
  all use `append-many`: the close lands the stamp and the state together under `--expect-revision`,
  so no stamp is ever left without its state, and that is what makes D2 hold on the ledger. The ledger
  store requires a ledger CLI that has `append-many` and `--expect-revision`; the capability probe
  above refuses `ledger_incompatible` otherwise. The markdown store does not depend on it.

### 6.3 Store selection

`choose_store(repo, key, title)`: the ledger store when `TICKET_LEDGER_CLI` names an executable (after
the capability probe), else the markdown store. An empty value counts as unset. The `release` command
selects the same way. A selected CLI that fails the capability probe is refused `ledger_incompatible`
before anything is read or written, and the `release` command exits 2.

## 7. Non-goals

- Pulling the source during a live pass, or before each outward action.
- Normalizing criterion text beyond the strip the source applies.
- Reopening a closed ticket, or undoing outward actions, on a change.
- Repairing a corrupt card (an unidentifiable plan, an invalid close, an unreadable block). A person
  does it.
- Migrating cards of an earlier schema: none exists.
- A fourth review round.
- Defending the card against a writer that forges the store's own record shapes (a go decision, a
  close key, a framed ticket note). The store is what the skill trusts.

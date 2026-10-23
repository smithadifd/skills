# Design note: which plan a resumed pass holds

Status: amended after design review, and implemented in `ticket.py`.
Scope: `skills/ticket/ticket.py` plan storage, resume, plan go, review validity, outward go and
close.

## 1. The rule

> A resumed pass compares against the criterion texts and order the card stored. Any edit of
> the ticket source after the card stored them, including a reorder, is a change.

**What "stored" means.** Card fields, captured by the pass and never taken from the source:

- `plan.revision` (a whole number, `type(...) is int`, at least 1) and
  `plan.acceptance_criteria[i].text`, in list order. `present_plan` writes them, from the
  presented plan, which must equal the source's criteria in order. `record_review` and
  `record_outward_stop` rewrite the snapshot from the plan the pass holds. Only `passes`,
  `review_evidence` and `review_round` may change; the texts and their order stay the same.
- Each review record carries `criteria`, the ordered texts that round reviewed, copied from the
  plan the pass holds when `record_review` stores the round. This field lets the card be checked
  without the source. A review whose `criteria` differ from the card's plan for the same
  revision means the card's plan was edited after that round.
- The plan go decision record carries `criteria` too: the ordered texts it approved, copied
  from the plan the pass holds when `record_go("plan", …)` stores the go. A plan go approves
  nothing for other criteria. Without this, a plan go would be checked against nothing until
  the first review of its revision exists: before that, review coverage holds vacuously, so
  editing the card's criteria and the ticket source alike after the go would reuse the go for
  criteria the person never approved.

**A stored plan that cannot be identified is a corrupt card.** That covers a plan that is not
an object, a missing or non-integer revision (`True` and `1.0` included), a revision below 1, a
criteria field that is not a list, an empty criteria list, and an entry with no non-empty text
string. Resume refuses it with `StoreUnavailable(code="plan_unidentifiable")`: the pass ends,
releasing its record, and nothing else is written. It is not a flagged change, because a change
needs revision N+1 of a plan whose N cannot be read, and guessing a fallback revision could
collide with an existing approval keyed by revision. A person repairs the card.

**What counts as a change.** The comparison is positional and exact: tuple equality of texts. It
catches an added criterion, a removed criterion, a text edit (including case and punctuation),
and a reorder (the same texts in another order). Duplicate texts need no special handling, since
the comparison is positional and not keyed by text.

- Whitespace: `Ticket.from_mapping` already strips each criterion, and the card stores the
  stripped texts, so a leading or trailing whitespace edit is invisible on both sides. Interior
  whitespace edits count as changes.
- Why exact: the review vouched for exact texts. A normalizing rule would need two correct
  implementations, one per side. A false change costs at most a re-plan, or only a flag on a
  closed ticket.

**What each comparison reads.**

| Check | Left side | Right side | Where it runs |
|---|---|---|---|
| (a) source | card `plan` texts and order, as read at resume | `Ticket.acceptance_criteria` pulled at open | resume only |
| (b) coverage | every review of the revision: `plan_revision`, `criteria`; every plan go of the revision: `criteria` | card `plan.revision` and texts | resume, `record_go("outward")`, `require_outward_go`, `close` and both read-backs, closed resume |
| (c) drift | card `plan` revision and texts, re-read at that moment | revision and texts the pass holds | `record_go("outward")`, `require_outward_go`, `close` and both read-backs |
| (d) plan gate | plan go `criteria` | card `plan` texts | `approved("plan")`, so `begin_build`; resume; `close` and closed resume |

At resume, the pass holds the card's stored plan. Check (c) is therefore trivially true at resume
time; it guards against edits made later in the pass. A live pass never pulls the source again;
the next open sees a source edit.

## 2. What a change does at each state

There are two message classes. A **flag** is a line in `reconciliation`: the resume succeeds and
never raises over a plan change. A **refusal** is an exception. `StoreUnavailable(code=
"review_changed")` means the card changed under a live pass, and it ends the pass.
`StoreUnavailable(code="plan_unidentifiable")` means the stored plan cannot be read at all; the
open ends the pass. `TicketError` means a wrong step or value, and the pass stays live.

On resume, the checks run in this order and the first match wins: an unidentifiable stored plan
(refusal), then closed and valid, then a stored cap stamp, then a change (from check a or b),
then the existing ladder.

| State when the change is seen | Resulting state | Passing review survives? | Message |
|---|---|---|---|
| Stored plan cannot be identified, in any state | unchanged card; the pass ends | not read | refusal `plan_unidentifiable` |
| Open, plan stored, no plan go (`plan` / `plan_go`) | `plan` (present revision N+1) | none exists | flag: what changed, and that revision N must be presented again as a new revision |
| Open, building (plan go held, no passing round of revision N), rounds 0–2 | `plan`. Earlier rounds stay stored and count toward the cap | none exists | flag. A plan go whose `criteria` differ from the card's plan is itself the change (check b) when no review of revision N exists |
| Open, round 3 stored and did not pass | cap stamp stored: `stopped`. Not stored: `cap_stop` as today | none exists | flag, plus the existing cap message |
| Passing round held, waiting at outward go or `outward_stop`, round 1 or 2 | `plan` | No. It stays on the card as history and covers nothing | flag: "review round N passed plan revision M; the criteria changed since (…); that round covers nothing in a new plan" |
| The same at round 3 | `stopped`. Resume writes nothing; `open` ends the pass | No | flag: "…review round 3 passed plan revision M; the criteria changed since (…); no review round is left for a new plan; a person restores the criteria or opens a new ticket". It must never claim that the card lacks the round. |
| Outward go recorded, not closed (includes an unconfirmed close stamp) | As the two rows above, by round | No, and the outward go is spent with it | flag also says the outward go of revision M authorizes nothing now and actions already taken are not undone |
| Live pass, card edited before an outward step | unchanged phase; the step is refused | not for this pass | refusal `review_changed`; the pass ends |
| Live pass, card plan edited after the plan go, before build | `plan_go` | none exists | `begin_build` raises `GateRequired`: the plan go names other criteria (check d) |
| Closed, and the close validates on the card alone | `closed`; the pass ends | Yes. It is the close's evidence | flag: "the ticket's criteria changed after close (…); the ticket stays closed; follow-up work is a new ticket" |

A closed ticket never reopens on a source edit. Closed validation (`_closed_problems`) reads only
the card: the stamps, both go decisions (the plan go bound to the card's criteria), the review
count, check (b), and every criterion citing the final round. A card edit that breaks those
checks still resumes as not closed, which is the existing behaviour and is correct, because the
card, not the source, was changed.

## 3. Cases the rule must cover

IDs: `Rn.k` is issue k of review round n of this change; `Rn.nk` is minor note k. `R1a` is an
earlier first-round review. `D5` and `D8` are mutations of the code that survived an earlier
test suite. `G1` and `G2` are the two design-review findings above. "Source X" means a test that
resumes with a `Ticket` whose criteria are edited (reorder, text, added, removed).

| ID | Finding | Under the rule | Test: shape |
|---|---|---|---|
| R1a.2 | close checked only the round count | close runs (b) and (c) before the stamp, after read-back, after confirm | `test_close_refuses_a_card_whose_final_review_is_changes_and_writes_no_close` (exists): card's final verdict set to changes, close refuses, no close stamp |
| R1a.3 | three rounds read as stopped without the cap stamp | `cap_stop` until the stamp is stored | `test_a_lost_cap_stop_stamp_resumes_retryable_until_the_stamp_is_written` (exists) |
| R1.3 | an unstamped pass on revision 1 finished revision 2 after a source change | source change → `plan`; after revision 2 and its go → `build`, never `outward_stop` | `test_an_old_plans_passing_round_does_not_finish_a_new_plan` (exists, edits source) |
| R1.4 | `record_outward_stop` evidence check unreached | it re-reads the final round and refuses a changed one | `test_record_outward_stop_refuses_a_stored_round_whose_evidence_changed` (exists); `test_an_outward_stop_whose_stored_round_reviewed_other_criteria_is_refused` (new): the final round's `criteria` reordered, refusal `review_changed`, pass ends, no stamp |
| R1.5 | close's outward-stamp clause untested | part of (b); missing stamp refuses close and closed resume | `test_a_card_without_its_outward_stop_stamp_neither_closes_nor_resumes_closed` (exists) |
| R1.n2 | "only an edited card" message reached with no edit | round 3 passed + source change → `stopped`, with the true message | `test_a_source_change_after_round_three_passed_stops_naming_the_change[reorder,text,added,removed]`: three rounds, round 3 passes, source X, phase `stopped`, flag names the change and no round left, flag lacks "does not carry that round", card unchanged |
| R2.n2 | wrong cap message after round 3 passed and revision 2 got its go | `cap_stop` naming the earlier revision (a card from before this rule) | `test_a_round_three_pass_of_an_earlier_plan_names_why_the_cap_stop_is_missing` (changed: the card is built directly, since a resume on the changed source now stops at round 3) |
| R2.2 | relabelled `plan_revision` still authorized outward work and close | (b) reads `plan_revision` at every point of reliance | the five `test_a_relabelled_final_review_does_not_*` tests (exist) |
| R2.n3, R3.n2, R4.n2 | final review without a whole-number round: KeyError, then a live pass with no way forward | the card changed under the pass: refusal `review_changed`, pass ends | `test_an_outward_stop_whose_stored_round_lost_its_number_is_refused_as_invalid`: flip to `StoreUnavailable` with that code, `not resumed.live` |
| R3.1 | cached revision and a subset of criteria passed the check; a deleted criterion resumed closed | (c) in a live pass; (b) on resume and on a closed card | `CARD_PLAN_EDITS` live-pass tests (exist); `test_a_closed_card_whose_criteria_were_edited_does_not_resume_as_closed` (exists, now fails by (b) and asserts the review-coverage message) |
| R3.2 | refusal in `require_outward_go` did not end the pass | `review_changed` refusal ends the pass | `test_a_relabelled_final_review_does_not_authorize_outward_actions` asserts `not session.live` |
| R4.1a | closed + source reorder reopened the ticket | stays `closed`, flag | `test_a_source_change_after_close_keeps_the_ticket_closed[reorder]`: close, source X, phase `closed`, flag names the change, card untouched |
| R4.1b | closed + source text edit reopened at `plan` | stays `closed`, flag | the same test `[text]`, plus `[added]` and `[removed]` |
| R4.1c | below the cap, a source reorder resumed at `build` with no flag | `plan` with a flag naming the passing round | `test_a_source_change_after_a_passing_review_resumes_at_plan_with_a_flag[reorder,text,added,removed]`: round 1 passes, source X, phase `plan`, flag names round 1, `approved("outward")` is false, the pass holds the stored texts, then revision 2 reaches `build` |
| R4.1d | at round 3, a source reorder stopped with a false message | `stopped` with the true message | covered by the R1.n2 test `[reorder]` |
| S1 (state) | outward go recorded, then the source changes | `plan`, flag names the spent outward go | `test_a_source_change_after_the_outward_go_resumes_at_plan_naming_the_go`: approved for close, source X, phase `plan`, flag names the outward go |
| D8 | resume compare checked the card against its own texts, and all tests stayed green | the meaningful resume compares are (a) and (b). Either one replaced by card-vs-card must go red | `test_a_source_change_after_a_passing_review_resumes_at_plan_with_a_flag[reorder]` (kills a mutated (a)); `test_a_card_and_source_reordered_together_after_a_passing_review_resume_at_plan`: reorder card and source alike, (a) passes, (b) fails, phase `plan`, a second resume lands at `plan` again (no loop), then revision 2 reaches `build` |
| D5 | dropping `type(plan.get("revision")) is not int` stayed green | revision must be an `int`, read strictly everywhere | `"plan revision true"` and `"plan revision 1.0"` in `CARD_PLAN_EDITS` (each live-pass test refuses); `test_a_plan_go_is_not_recorded_against_a_non_integer_plan_revision[True,1.0]`; a closed card with such a revision is refused as unidentifiable (G2) |
| (b) reviews | a review whose `criteria` differ from the card's plan still covered it | (b) on resume | `test_a_review_round_covers_nothing_once_the_card_plan_differs_from_its_criteria`: card plan and plan go edited alike, review not, phase `plan`, flag names the review round |
| G1 | plan go not bound to the criteria before any review exists | (b) and (d) read the plan go's `criteria` | `test_a_plan_go_is_not_reused_for_criteria_edited_on_the_card_and_the_source` (zero reviews: plan go held, card and source edited alike, phase `plan`, flag names the plan go, `approved("plan")` false); `test_a_plan_go_is_not_reused_when_only_an_earlier_revision_was_reviewed` (the same at revision 2, whose only reviews are of revision 1); `test_a_plan_go_opens_no_build_for_criteria_the_card_no_longer_holds` (live, `begin_build` refused); `test_a_plan_go_for_other_criteria_authorizes_no_outward_step[outward go,outward action,close]` (refusal, pass ends, nothing written) |
| G2 | a malformed stored identity was a flagged change needing revision N+1 of an unreadable plan | refusal `plan_unidentifiable`, the pass ends, nothing written | `test_a_stored_plan_that_cannot_be_identified_is_refused_and_writes_nothing[stage × shape]`: each shape in `UNIDENTIFIABLE`, at plan go, at outward go (both go decisions present) and closed; `test_a_live_pass_whose_stored_plan_cannot_be_identified_is_refused_and_ends` |
| (schema) | review and plan go `criteria` must be well formed | stored only as a non-empty list of non-empty text | `test_a_review_or_go_with_invalid_criteria_is_not_stored` |

The outward-go fault "no criteria" (an empty criteria list on the card) moves from
`OUTWARD_GO_FAULTS` to `UNIDENTIFIABLE`: it is now a refusal, not a resume short of outward go.

Out of scope (the rule does not touch these):

- R1a.1: `-->` in recorded text breaks the HTML comment. This is card rendering.
- R1a.n1: an empty ledger selector counts as unset. This is store choice.
- R1a.n2: `release_pass` order of operations. This is pass-record handling.
- R1.1 and R2.1: plan step and `diff_statement` validation. This is snapshot schema.
- R1.2: thread-shared lock depth. This is store locking.
- R1.n1 and R2.n1: wording of who flips `passes`. This is documentation.
- R3.n1 and R4.n1: an empty ticket title is accepted at the source. This is source validation.
- R3.n3: three mutations were equivalent. Nothing changes.

## 4. What changes in the code

- `_plan_identity` (new): reads the stored revision strictly and the stored texts in order, or
  returns `None` for an unidentifiable plan.
- `_resume`: refuses an unidentifiable stored plan with `plan_unidentifiable` before anything
  else. Otherwise it reads the revision from the identity, without `int(...)` coercion, and
  applies the precedence in section 2. A change at round 3 or later goes to `stopped` with the
  true flag. The "only an edited card" message stays only where it is true: a source change no
  longer reaches it. Source differences on a closed card are a flag.
- `_reconcile`: builds the held plan from the card's stored criteria in stored order (keeps the
  `_verified` reset) and returns the source differences (added, removed, changed, reordered)
  as their own list.
- `ReviewRecord`, `_validate_review`, `record_review`: add `criteria`, the ordered texts from
  the plan the pass holds. When present it must be a non-empty list of non-empty strings.
- `DecisionRecord`, `_validate_decision`, `record_go("plan")`: add `criteria` the same way; the
  plan go stores the texts of the plan the pass holds.
- `_coverage_problems` (new, check b): every review and every plan go of the revision must
  carry `criteria` equal to the card's plan texts. `_review_problems` includes it, and resume
  runs it before the ladder.
- `_has_go` (check d): a plan go counts only when its `criteria` equal the card's plan texts.
- `_passing_final_review`: also requires the final round's `criteria` to equal the texts the
  caller passes (the card's, or in `record_outward_stop` the pass's).
- `_card_plan_problems`: keep the strict `int` check. Callers pass only the texts the pass holds,
  never `self._ticket.acceptance_criteria`.
- `_closed_problems`, `_close_card_problems`: read only the card and card-derived values.
- `record_go` (plan branch): compare the stored revision with `type(...) is int`.
- `record_outward_stop`: a final review with no whole-number round, or with mismatched
  `criteria`, raises `StoreUnavailable(code="review_changed")`.
- `TicketPass.plan` after a changed resume returns the stored plan. Update
  `test_resume_flags_changed_acceptance_criteria_and_never_keeps_them_passing` to match.
  `present_plan` is unchanged and is still where the texts are captured.
- `SKILL.md`: update Orient step 4 (the resume rule and the corrupt-card refusal), Plan go (the
  go names its criteria), Build and review (the cap behaviour) and Outward go (a closed ticket
  stays closed), describe the review and decision `criteria` fields in the store contract, and
  point to this note. The plan card schema file describes only the plan snapshot, which does
  not change; review and decision records have no schema file.

## 5. Non-goals

- Pulling the source again during a live pass, or before each outward action.
- Treating a change to status, title, description, links or `updated_at` as a plan change.
  These stay flags.
- Recovering from a card someone edited consistently in every place (plan, reviews, decisions,
  stamps). The store remains what the skill trusts.
- Reopening a closed ticket automatically, or undoing outward actions already taken.
- Normalizing criterion text beyond the strip the source already applies.
- Migrating older cards. The skill is unreleased, so no card exists without review and plan go
  `criteria`.
- A fourth review round, or any change to the round cap.

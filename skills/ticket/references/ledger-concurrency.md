# Ledger store: one writer per ticket by compare-and-set

## Problem

The ledger store writes a ticket's stamps, decisions, reviews and close through the ledger CLI, one subprocess per event. Earlier versions checked the ticket's newest event, then appended, then read the events back to see whether anything had landed in between. That check and that append were two calls, so another writer could always slip between them, and every repair (void notes, state restores) opened a new window.

## Rule

The ledger CLI's write verbs take `--expect-revision <event id | none>`: the append lands only if the item's newest event id equals the expected one, checked in the same database transaction as the insert, and otherwise refuses with exit code 5 (`stale_revision`) naming the newest event's writer. The ledger store uses it on every write.

1. **Revision.** A store handle holds the ticket revision it has accounted for: the newest event id it read (or `none` for a ticket with no events), advanced only by its own successful appends.
2. **Every write is one conditional append.** `write_stamp`, `record_decision`, `record_review`, `record_plan`, `record_tried`, the pass claim and the close each append with `--expect-revision <held revision>`. On success the held revision becomes the new event's id. On exit 5 the write is refused: the store raises `TicketHeld` naming the writer from the CLI's message, writes nothing else, and the pass must re-read before writing again.
3. **The claim.** Starting a pass reads the ticket, refuses if a pass is live (printing its session id, host and start time), and otherwise appends the `session.opened` claim conditionally on the revision it read. Two sessions that read the same revision cannot both claim: the second append is refused. A refused claim wrote nothing, so there is nothing to release. Releasing a pass (close, the person's `release` command, or an error exit) appends `session.closed` without a precondition; giving up the ticket is safe whatever landed since.
4. **The close.** Closing is the pending-close stamp (one conditional append), then one conditional `item.state` append marking the ticket closed, expected at the stamp's event id. If anything lands between them, the state append is refused and the ticket stays open with an unconfirmed pending close; resume reports it and resumes at the outward go. A `closed` status is trusted only when the event immediately before it in the ticket's history is this pass's pending-close stamp.
5. **What is deleted.** The read-back after every append, the close-void note, the state restore and the revision-keyed idempotency key: compare-and-set makes them unnecessary. Appends keep a unique idempotency key for retry safety only.
6. **Old CLIs.** A ledger CLI that does not accept `--expect-revision` is refused at store selection with a one-line message naming the missing option; the store never writes without the precondition.

## Inputs and invariants

- Inputs: the item's events from the CLI's export; the CLI's exit status and error code on each append.
- Invariant: every event the store appends was appended at exactly the revision the store last observed, so no write ever lands on top of a change the store did not see.
- Fail-closed default: an unknown exit status, an unparseable response, or a missing `--expect-revision` option is `StoreUnavailable`, never a write.

## Tests

- Two handles read revision N; the first stamp lands; the second is refused naming the first writer, and the ticket's event count grows by one.
- Two sessions race to claim from the same revision: exactly one claim lands; the other is refused and leaves no claim.
- A state event inserted between the pending-close stamp and the closed write: the closed write is refused, the status is unchanged, and resume reports an unconfirmed close.
- A `closed` status not immediately preceded by this pass's pending-close stamp is not trusted on resume.
- A CLI without `--expect-revision` is refused at selection.
- Mutation targets: omit `--expect-revision` on one write; trust a closed status without the adjacency check; accept exit 5 as success.

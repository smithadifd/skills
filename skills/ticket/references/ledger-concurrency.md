# Ledger store: one writer per ticket by compare-and-set

## Problem

The ledger store writes a ticket's stamps, decisions, reviews and close through the ledger CLI, one subprocess per event. Earlier versions checked the ticket's newest event, then appended, then read the events back to see whether anything had landed in between. That check and that append were two calls, so another writer could always slip between them, and every repair (void notes, state restores) opened a new window.

## Rule

The ledger CLI's write verbs take `--expect-revision <event id | none>`: the append lands only if the item's newest event id equals the expected one, checked in the same database transaction as the insert, and otherwise refuses with exit code 5 (`stale_revision`) naming the newest event's writer. The ledger store uses it on every write.

1. **Revision.** A store handle holds the ticket revision it has accounted for: the newest event id it read (or `none` for a ticket with no events), advanced only by its own successful appends.
2. **Every write is one conditional append.** `write_stamp`, `record_decision`, `record_review`, `record_plan`, `record_tried`, the pass claim and the close each append with `--expect-revision <held revision>`. On success the held revision becomes the new event's id. On exit 5 the write is refused: the store raises `TicketHeld` naming the writer from the CLI's message, writes nothing else, and the pass must re-read before writing again.
3. **The claim.** Starting a pass reads the ticket, refuses if a pass is live (printing its session id, host and start time), and otherwise appends the `session.opened` claim conditionally on the revision it read. Two sessions that read the same revision cannot both claim: the second append is refused. A refused claim wrote nothing, so there is nothing to release. Releasing a pass (close, the person's `release` command, or an error exit) appends `session.closed` without a precondition; giving up the ticket is safe whatever landed since.
4. **The close.** Closing is the pending-close stamp (one conditional append), then one conditional `item.state` append marking the ticket closed, expected at the stamp's event id and carrying the idempotency key `ticket-close/<ticket key>/<stamp event id>` and this pass's writer name. If anything lands between them, the state append is refused and the ticket stays open with an unconfirmed pending close; resume reports it and resumes at the outward go. A `closed` status is trusted only when all three hold: the event immediately before it is a pending-close stamp written by this ticket's store; its idempotency key is exactly `ticket-close/<ticket key>/<that stamp's event id>`; and its writer name matches the stamp's. An outside writer that appends `closed` right after the stamp (winning that revision) fails the key check, so resume reports the close as not this pass's and resumes at the outward go.
5. **Idempotency key per write.** Every conditional append carries a deterministic key, `ticket-write/<ticket key>/<expected revision>/<kind>` (the close uses its own key, rule 4). The ledger CLI checks a stored key before the revision precondition: a retry of a request that already committed returns the stored event unchanged instead of `stale_revision`, and the same key with a different event is refused as a conflict. So a retry after a lost response is safe and lands at most once.
6. **Ambiguous outcomes.** An unknown exit status, a crash, or an unparseable response does not prove the append did not commit. The store then reads the ticket's events and looks for the write's key: found means it landed (the held revision becomes that event's id and the write returns normally); absent means it did not, and the write raises `StoreUnavailable` with the CLI's output. The store never retries with a new key.
7. **What is deleted.** The read-back after every successful append, the close-void note and the state restore: compare-and-set makes them unnecessary. The only read-back left is rule 6's lookup by key after an ambiguous outcome.
8. **Old CLIs.** A ledger CLI that does not accept `--expect-revision` is refused at store selection with a one-line message naming the missing option; the store never writes without the precondition.

## Inputs and invariants

- Inputs: the item's events from the CLI's export; the CLI's exit status and error code on each append.
- Invariant: every event the store appends was appended at exactly the revision the store last observed, so no write ever lands on top of a change the store did not see.
- Fail-closed default: a missing `--expect-revision` option is refused before any write; an unknown exit status or unparseable response is resolved only by finding the write's own key in the ticket's events (rule 6), and otherwise is `StoreUnavailable`.

## Tests

- Two handles read revision N; the first stamp lands; the second is refused naming the first writer, and the ticket's event count grows by one.
- Two sessions race to claim from the same revision: exactly one claim lands; the other is refused and leaves no claim.
- A state event inserted between the pending-close stamp and the closed write: the closed write is refused, the status is unchanged, and resume reports an unconfirmed close.
- A `closed` status not immediately preceded by this pass's pending-close stamp is not trusted on resume.
- An outside writer appends `closed` immediately after the pending-close stamp (winning that exact revision, so this pass's closed write is refused): resume does not trust it, because its idempotency key is not `ticket-close/<key>/<stamp id>`.
- A CLI without `--expect-revision` is refused at selection.
- Lost response: the CLI commits the append but returns unparseable output; the store finds the key, advances its revision, and the next write lands. A retry of the same request returns the stored event, not `stale_revision`.
- Ambiguous failure with nothing committed: the key is absent, the write raises `StoreUnavailable`, and the revision is unchanged.
- Mutation targets: omit `--expect-revision` on one write; trust a closed status without the adjacency check; accept exit 5 as success.

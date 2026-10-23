# Ledger store: one writer per ticket by compare-and-set

## Problem

The ledger store writes a ticket's stamps, decisions, reviews and close through the ledger CLI, one subprocess per event. Earlier versions checked the ticket's newest event, then appended, then read the events back to see whether anything had landed in between. That check and that append were two calls, so another writer could always slip between them, and every repair (void notes, state restores) opened a new window.

## The CLI guarantee this rests on

The ledger CLI's write verbs take `--expect-revision <event id | none>`. The append lands only if the item's newest event id equals the expected one, checked in the same database transaction as the insert; otherwise it exits 5 (`stale_revision`), names the newest event's writer, and writes nothing. A stored idempotency key is checked first: the same key with the same event returns the stored event; the same key with a different event is refused as a conflict.

## Rules

1. **Held revision.** A store handle holds the ticket revision it has accounted for: the newest event id it read (or `none` for a ticket with no events). Only its own successful appends advance it.
2. **Pass ownership on every write.** Before every write, the handle reads the ticket's events and refuses, writing nothing, when a pass is live that is not the handle's own (printing that pass's session id, host and start time), or when the handle holds a pass that is no longer live. A handle outside any pass may write only while no pass is live.
3. **Conditional writes.** Every write except a pass release (rule 6) is one append with `--expect-revision <held revision>`: `write_stamp`, `record_decision`, `record_review`, `record_plan`, `record_tried`, the pass claim and the closed state. On success the held revision becomes the new event's id. On exit 5 the store raises `TicketHeld` naming the writer and writes nothing else; the pass must re-read before writing again.
4. **Write keys.** Every conditional append carries a key unique to that logical write, generated once and reused only to retry that same write:
   - ordinary writes: `ticket-write/<ticket key>/<expected revision>/<kind>/<request token>`, with a random request token;
   - the closed state: `ticket-close/<ticket key>/<stamp event id>/<pass id>`, where the stamp is this pass's pending-close stamp.
   Two independent handles writing from the same revision never share a key, so the second is refused by the revision check, not taken for a retry.
5. **Claim.** Starting a pass reads the ticket, refuses if any pass is live, and appends the `session.opened` claim (session id, host, started-at) with the expected revision it read. Two sessions racing from the same revision cannot both claim: the second append is refused and leaves no claim.
6. **Release.** Releasing a pass (at close, on the person's `release` command, or on an error exit) appends `session.closed` naming the pass, without a revision precondition: giving up the ticket is safe whatever landed since, and it must succeed after the handle's revision has gone stale. Its key is `ticket-pass/<ticket key>/<pass id>/released`, so a retried release lands once.
7. **Close.** Closing is this pass's pending-close stamp (a conditional write), then the closed state, appended with `--expect-revision <the stamp's event id>` and the close key from rule 4. If anything lands between them, the closed write is refused and the ticket stays open with an unconfirmed pending close; resume reports it and resumes at the outward go.
8. **Trusting a closed status on resume.** A `closed` status is trusted only when all of these hold:
   - the event immediately before the closed-state event is a pending-close stamp written by this ticket's store;
   - the closed-state event's key is exactly `ticket-close/<ticket key>/<that stamp's event id>/<the pass id that wrote the stamp>`, and its writer name matches the stamp's;
   - that pending-close stamp is still the latest `item.stopped` event (a later stop invalidates the close), and every event after the closed state is pass bookkeeping (`session.opened` or `session.closed`); any other later event invalidates the close. That bookkeeping must leave no pass live: the closing pass's own release, and complete open/close pairs matched by pass id. A `session.opened` with no matching `session.closed`, or a `session.closed` for a pass that was never opened after the close, means the card is not validly closed.
   Otherwise resume reports why and resumes at the outward go, or earlier when an earlier check fails.
   Resume reads and validates the card **before** it claims a pass: a validly closed ticket is reported closed and no claim is taken; only a ticket that is not validly closed is claimed and resumed.
9. **Ambiguous outcomes.** An unknown exit status, a crash, or an unparseable response does not prove the append did not commit. The store reads the ticket's events and looks for the write's exact key with the same kind and payload: found means it landed (the held revision becomes that event's id and the write returns normally); absent means it did not, and the write raises `StoreUnavailable` with the CLI's output. The store never retries a write under a new key.
10. **Old CLIs.** A ledger CLI that does not accept `--expect-revision` is refused at store selection with a one-line message naming the missing option.
11. **Deleted.** The read-back after every successful append, the close-void note and the state restore. The only read-back left is rule 9's lookup by key after an ambiguous outcome.

## Invariants

- Every event the store appends, except a pass release, was appended at exactly the revision the store last observed, by a handle that owned the live pass (or wrote while none was live).
- A closed status is trusted only under rule 8.
- Fail-closed: a missing `--expect-revision` option is refused before any write; an ambiguous outcome is resolved only by finding the write's own key (rule 9), and is otherwise `StoreUnavailable`.

## Tests

- Two handles read revision N; the first stamp lands; the second is refused naming the first writer, and the ticket's event count grows by one.
- Two handles at the same revision issue the same kind of write with identical payloads: the first lands; the second is refused with `stale_revision`, never returned the first one's event.
- A fresh handle (no pass) writes after another pass has claimed the ticket: refused by rule 2, nothing written.
- Two sessions race to claim from the same revision: exactly one claim lands; the other leaves no claim.
- Release succeeds after the handle's revision has gone stale, and a retried release lands once.
- A state event inserted between the pending-close stamp and the closed write: the closed write is refused, the status is unchanged, and resume reports an unconfirmed close.
- An outside writer appends `closed` immediately after the pending-close stamp (winning that revision): resume does not trust it, because its key is not `ticket-close/<key>/<stamp id>/<pass id>`.
- A later `item.stopped` after a trusted close: resume no longer trusts the close.
- Resuming a validly closed ticket reports it closed and appends no claim; a close followed only by another session's `session.opened`/`session.closed` pair is still trusted; a close followed by an unmatched `session.opened` is not.
- Lost response: the CLI commits but returns unparseable output; the store finds the key, advances its revision, and the next write lands. A retry of the same request returns the stored event, not `stale_revision`.
- Ambiguous failure with nothing committed: the key is absent, the write raises `StoreUnavailable`, and the revision is unchanged.
- A CLI without `--expect-revision` is refused at selection.
- Mutation targets: omit `--expect-revision` on one conditional write; skip the pass-ownership check on one write; trust a closed status without the key check; trust a close after a later stop; accept exit 5 as success.

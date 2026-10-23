## Wikilink hyphen/underscore repair (suggestion-only, in `scan`)

A real bug class: a `[[wikilink]]` written with hyphens pointing at a file
named with underscores (or vice versa) is dangling by exact match but would
resolve if the separator style were normalized —
e.g. `[[feedback-self-review]]` when the file on disk is
`feedback_self_review.md`. `scan`'s top-level `hyphen_underscore_repairs` key
surfaces these as **suggestions only**; nothing is auto-applied.

- **Scoped to one memory dir**, same as `dangling_links` — wikilinks are
  expected to resolve within their own project's store, not across stores.
- **Never fabricates a repair.** Only a PURE separator-case difference against
  the SAME normalized stem is suggested. A link missing an entire prefix (e.g.
  pointing at a differently-named file in a different project) is left as a
  plain dangling link, not force-matched.
- **Feed it into the existing mechanism.** A suggestion the model verifies
  becomes a normal plan entry's `wikilink_repairs: [{"from": ..., "to": ...}]`
  — no new apply-side code; `repair_wikilinks` already handles it.
- **Verified against a real corpus**: two of the six reported targets were genuine
  mismatches in a real store (for example `[[feedback-review]]` →
  `feedback_review.md`); the other four are dangling for reasons
  this normalizer correctly declines to "fix" (a missing prefix, a cross-store
  reference, or no matching file at all).

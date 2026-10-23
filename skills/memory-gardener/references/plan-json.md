## The plan JSON (what the model writes)

After reading `scan`, write a `plan.json`. One entry per file; the fields the
helper consumes:

```json
{
  "files": [
    {
      "file": "some-note.md",
      "verdict": "SUPERSEDED",
      "sha256": "<copy verbatim from the scan output for this file>",
      "rationale": "why (shown in the report)",
      "index_line": "[Title](some-note.md) — one scannable line",
      "frontmatter": {"status": "current", "supersedes": ["other-note"], "description": "set ONLY when correcting it — omit otherwise"},
      "body": "the rewritten body (null / omit to leave the body untouched)",
      "changelog": ["2026-04 first theory — WRONG", "2026-05 confirmed root cause"],
      "archive": {"reason": "tombstone line — required for DEAD"},
      "wikilink_repairs": [{"from": "old-note", "to": "archive/old-note"}],
      "pointerize": {"full_body": "the full document body to extract to docs/some-note-full.md"}
    }
  ]
}
```

- `sha256` is the **stale-plan drift guard**: copy it verbatim from `scan`
  output. The report-review-apply gap can be hours; if the file changes on disk
  in between, validation fails with `STALE PLAN` instead of silently
  overwriting the newer content. `apply` REQUIRES it on every mutating verdict
  (UPDATE / SUPERSEDED / DEAD); `report` checks any that are present.
- `frontmatter` may contain ONLY `status` / `last_verified` / `supersedes` /
  `description` — the helper rejects anything else. `last_verified` is
  auto-stamped to today on UPDATE/SUPERSEDED if you omit it. Set `description`
  explicitly and only when you're correcting it (e.g. a SUPERSEDED/UPDATE body
  rewrite that leaves the old description contradicting the new body) — never
  auto-derive one just because the file is otherwise being touched.
- `body` replaces the markdown after the frontmatter; the helper appends the
  `changelog` as a `### Changelog` block. Omit `body` to leave content as-is
  (typical for a pure index-line UPDATE).
- `index_line` rebuilds that note's line **in whichever index file owns it** —
  `MEMORY.md` for a core-resident note, `MEMORY.<topic>.md` for one that lives in
  a JIT shard (`scan` reports the owner as `index_file`, and the report names it
  above the diff). A note listed in no index at all is appended to the core, the
  documented never-silently-dropped behaviour. DEAD files have their index line
  dropped automatically, again from the file that holds it.
  **Rewriting a SHARD's lines also refreshes that shard's JIT pointer in the
  core** (`refresh_shard_pointer`) — changing a shard changes
  its load cost, and the always-loaded core advertises that cost. Nothing else
  refreshes it: `propose_extend` refreshes a pointer only for a shard it MOVES
  bullets into and explicitly leaves an untouched shard's pointer byte-identical,
  so before this fix a trim run's savings never reached the core's own numbers.
  Live case: trimming 21 ballooned lines across three shards left the core
  claiming 6912 tok of JIT shards against a real 5578 — 24% over, the largest
  error (1155 tok) on a shard `extend` would never have touched. The refresh is
  **surgical** — only the `· N notes · ~T tok ·` segment is rewritten, because
  live pointers carry hand-added prose after the generated suffix that
  regenerating through `_shard_pointer` would silently delete. A pointer edited
  past recognition is left alone rather than guessed at.
  This used to rebuild `MEMORY.md` unconditionally, which **failed open** on a
  shard-resident note: `rebuild_index` found no matching core line, APPENDED one,
  left the shard's stale line untouched, and reported `1 line(s) updated`. The
  note ended up listed twice. `_assert_no_duplicate_index_targets` now runs on
  every rebuilt index before it is written and refuses rather than duplicate — it
  is the backstop for the whole class, since every way this path can go wrong
  (an unparsable title, a mis-resolved home, a shard note handed to the core)
  converges on that same doubled listing.
- `archive.reason` is REQUIRED for DEAD (it becomes the tombstone).
- `pointerize` mechanizes the pointer-memory refactor (see the dedicated
  section below) — only valid on UPDATE/SUPERSEDED. Pair it with `body` set to
  the pointer text left at the original basename.

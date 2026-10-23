## Gotchas

- **The signals are hints, not verdicts.** `correction_chain` fires on marker
  words (WRONG, CORRECTION, red herring, dead-end); a note can mention those
  while still being current. Always read the file.
- **`balloon_index` is a length heuristic** (index summary > ~320 chars). A
  genuinely dense one-liner near the threshold is fine — judge it. It reads the
  note's line from whichever index owns it, so shard-resident notes are covered;
  before that fix `index_summary_len` was 0 for every note outside `MEMORY.md`,
  which made the signal structurally blind to every note held in a shard —
  including the ones with the worst lines, since the shards are gardened least.
- **KEEP writes nothing *to the fact file* — but it can still fix an index
  line.** A `KEEP` entry carrying an `index_line` rebuilds that line and leaves
  the note byte-identical: no body rewrite, no `last_verified` stamp. That is the
  right verdict for pure index-sickness, where you are compressing a bloated
  summary without re-verifying the fact. Reserve `UPDATE` (which auto-stamps
  `last_verified`) for entries where you actually touched or re-verified the
  file. Trimming a whole index under `UPDATE` would stamp every note as
  verified-today and blind the `unverified_over_90d` tripwire storewide.
- **Otherwise KEEP writes nothing.** Resist the urge to "tidy" or stamp untouched
  files; that's how you'd mass-rewrite the whole dir and blow away the
  additive-only discipline.
- **`supersedes` names the OTHER file.** Put it on the canonical note pointing
  at the one it absorbs; the absorbed note gets `status: superseded` and a stub
  body, and stays on disk (it's a valid link target).
- **Archive breaks links by design.** When you archive X, repair every `[[X]]`
  to `[[archive/X]]` (or to the replacement note) in the same plan — `apply`
  warns on any it can't resolve.
- **`STALE PLAN` means re-scan, not force.** If `apply` (or `report`) refuses
  with a sha256 mismatch, a memory file changed after the plan was written —
  maybe by another session. Re-run `scan`, refresh the entry (and its hash),
  re-report, get a fresh go. There is deliberately no override flag.
- **One memory dir per run.** The default `--dir` is the current user's home-project memory dir (override it with
  `$MEMORY_GARDENER_DIR` or `--dir`); pass `--dir`
  explicitly to garden a different project's memory. Don't widen a run across
  projects.
- **`--today` is for tests/determinism.** Real runs use the system date; the
  fixture tests pin `--today 2026-07-08` so age math is stable.
- **`budget` is chars/4, not a real tokenizer.** The estimate can be off by
  10–20%; that's fine for a tripwire. Don't swap in a heavyweight tokenizer —
  raise/lower `--max-tokens` instead if the ceiling feels wrong.
- **`budget` only re-files index bullet lines.** Only lines matching the
  `- [name](file.md) — ...` index format are ever moved into a shard.
  Everything else — the title, blank lines, headings, prose, HTML comments,
  malformed/non-link bullets — is retained in the proposed core index, in its
  original relative order, whether it sits before or after the first index
  bullet (`non_index_lines_retained` in the JSON output reports the count).
  Nothing after the header is silently dropped.
- **Taxonomy selection is store-aware and fail-closed.** `budget` and `shard`
  select explicit `--taxonomy`, then `<memory-dir>/shard-taxonomy.json`, then
  built-in defaults, and print which source won. A malformed selected file —
  including a malformed store-local file — is a clean `error: ...` refusal
  (exit 2), never a silent fallback. A missing `"shards"` key, a non-object
  shard entry, a duplicate shard name, and an empty-string keyword (which would
  substring-match every filename) are likewise refused.
- **`--max-tokens`/`--max-lines` must be positive.** A zero or negative
  ceiling is rejected (exit 2) rather than turning even an empty `MEMORY.md`
  into a bogus "over budget" alert.
- **Over budget ≠ auto-fix.** Exit `3` is an *alert*. Actually sharding is a
  separate supervised `shard` dry-run → review → `shard --apply` sitting —
  never chained off the alert.
- **`budget` and `filebudget` are DIFFERENT ceilings — don't conflate them.**
  `budget` (4500 tok / 100 lines, MEMORY.md only) is about JIT-sharding
  economics. `filebudget` (200 lines / 25,000 bytes, every file, every store)
  is the harness's own hard truncation limit. A file can be fine on one and
  over on the other.
- **`filebudget`'s `docs/`/`archive/` exclusion is deliberate, not a bug.**
  If you ever see a `docs/*-full.md` or `archive/MEMORY.pre-shard-*.md` file
  NOT excluded, that's the regression to fix — don't "fix" the files
  themselves; they're supposed to be oversized.
- **`pointerize` never guesses the pointer text.** `full_body` is the ONLY
  mechanized part; the pointer `body` (headline, kept facts, `Related:` line)
  is model-authored judgment, same as any other verdict's `body`. Read the
  five hand-made pointers before writing one.
- **Hyphen/underscore suggestions are not repairs.** `hyphen_underscore_repairs`
  in `scan` output is a hint list — verify the target file actually is the
  right note before adding it to a plan's `wikilink_repairs`. It will not
  suggest anything for a link that's dangling for a different reason (missing
  prefix, wrong store, no such note at all).

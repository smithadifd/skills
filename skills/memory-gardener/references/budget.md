## Index load-cost budget + topic-sharding (`budget` — read-only, schedulable)

`MEMORY.md` is loaded **wholesale into every session's context**, so its size is
a standing per-session token tax. Two failure modes inflate it: index-sickness
(individual lines ballooning — handled per-file by the rubric above) and sheer
**growth in note count** (the index just gets long). The `budget` subcommand
polices the second.

```bash
# read-only: measure + (if over budget) print a shard proposal. Writes nothing.
python3 memory_gardener.py budget --dir ~/.claude/projects/<proj>/memory
python3 memory_gardener.py budget --max-tokens 4500 --max-lines 100  # tune the ceiling
python3 memory_gardener.py budget --json                            # machine-readable
python3 memory_gardener.py budget --taxonomy my-shards.json         # override groupings
```

- **Token cost method.** A dependency-free **chars/4** approximation (this repository
  ships no tokenizer and prefers zero-dep helpers). It's a tripwire, not an
  invoice — the ceiling is generous enough to absorb the imprecision.
- **Budget.** Configurable `--max-tokens` (default **4500**) and `--max-lines`
  (default **100**). Over on *either* axis = over budget.
  **Retuned from 2500/40**, which assumed a ~40-note index landing at
  800–1200 tok. Once a store is sharded, the CORE holds the always-loaded working
  set, and at ~80 notes the old ceiling was arithmetically unreachable: the bullet
  + `[title](file.md)` link overhead alone runs ~86 chars/line, so the links with
  *no summary text at all* already cost ~1730 tok — 69% of 2500. Clearing it would
  have needed ~38-char summaries. A red `budget` had stopped meaning "the index is
  sick" and started meaning "the store is 4x the size this number was written
  for", and the only fix it pointed at — sharding the working set into JIT files
  that aren't loaded by default — trades facts-you-need-going-unseen for a number.
  Re-derive the ceiling if the core's note count changes materially; **diagnose
  with tokens-per-line before trimming or sharding**, and subtract link overhead
  first (a store with long bolded titles reads falsely fat against a raw
  tokens-per-line rule of thumb).
- **Exit code = the alert.** `0` under budget, **`3`** over budget (distinct
  from the `2` used for config/plan errors), plus a one-line headline on stderr.
  A scheduled wrapper alerts only on exit `3`. This is the whole point: scanning
  and reporting are safe, so the *check* is schedulable even though the *fix*
  isn't.
- **Topic-sharding proposal.** When over budget, `budget` proposes splitting
  `MEMORY.md` into a small always-loaded **core index** plus topic sub-indexes
  (`MEMORY.workflow.md`, `MEMORY.infra.md`, `MEMORY.automation.md`,
  `MEMORY.secrets.md`, …) pulled **just-in-time**. Each shard gets a one-line
  JIT pointer in the core index. It stays **markdown** on purpose — grep/Explore
  displaces an embedding index, so there's no vector store to maintain. Notes
  are grouped by the first available taxonomy in this order: explicit
  `--taxonomy`, `<memory-dir>/shard-taxonomy.json`, then the built-in defaults.
  Both `budget` and `shard` print the selected source. A present but malformed
  store-local file is a config error (exit 2), never permission to fall through
  to defaults. Taxonomy JSON has the shape
  `{"shards":[{"name","label","keywords":[…]}]}`; unmatched notes stay in core.
  Brand-specific keywords belong in a user's own `--taxonomy` file; the
  built-in defaults use generic category words.
- **It's a PROPOSAL, not an edit.** `budget` never writes the shard files or
  touches memory. The actual split — moving lines into `MEMORY.<topic>.md`,
  writing the core pointers — is performed by the **`shard`** subcommand below
  (its writing sibling), a **separate supervised sitting** (and, for the live
  memory dir, a human-reviewed one-off). `--apply` stays supervised,
  interactive-only.
- **Already-sharded core.** If `MEMORY.md` is over
  budget but already carries JIT shard pointers, `budget` does NOT show the
  fresh-split proposal above — it has no notion the shard files already
  exist, so it would mint a colliding second pointer per shard with the
  wrong (overflow-only) note count. It shows a **re-shard proposal**
  instead: the overflow core bullets that classify to an EXISTING shard,
  and exactly what `shard --extend` would move. Plain `shard --apply` (no
  `--extend`) refuses outright on an already-sharded core — `--force` does
  not override that — so the proposal's "next step" always names
  `shard --extend`, never plain `shard --apply` (see `shard --extend` below).

Scheduling it (optional, the owner's call) is a job that runs
`budget` on a cadence and alerts on exit `3` — never `apply`.

### Executing the split (`shard` — dry-run default, `--apply` mutates)

`budget` proposes; `shard` writes. `shard` is the executor for the topic-split,
built to the same report-first discipline as the rest of the skill.

```bash
# DRY RUN (default): print exactly what WOULD be written — the new core
# MEMORY.md and each MEMORY.<topic>.md — plus a summary and the current
# MEMORY.md sha256 to copy into --expect-sha256. Writes nothing.
python3 memory_gardener.py shard --dir ~/.claude/projects/<proj>/memory --taxonomy my-shards.json

# APPLY (mutating, supervised): performs the split behind four guards.
python3 memory_gardener.py shard --dir … --taxonomy my-shards.json \
    --apply --expect-sha256 <hash printed by the dry run>
```

- **Dry-run is the default.** No `--apply` = a read-only preview built from the
  SAME `propose_shards` grouping `budget` uses, so the preview is identical to
  what `budget` proposed and to what `--apply` will write. It prints the full
  proposed content of every file and writes nothing.
- **`--apply` requires `--expect-sha256`.** MEMORY.md is read exactly once; its
  sha256 over those bytes must match the hash you reviewed in the dry run, or
  `shard` refuses with a `STALE PLAN` message (exit 2). Omitting `--expect-sha256`
  is refused outright (never blind-applies). The layout is built from the SAME
  bytes that were hashed — no TOCTOU re-read.
- **Sharding is a one-time op — plain re-sharding is REFUSED.** After a split
  the core holds JIT pointer lines (`- [MEMORY.<topic>.md](…) — … load JIT …`).
  A second plain `--apply` (no `--extend`) detects those pointers and refuses
  (exit 2) rather than re-grouping them — a fresh split has no notion the
  shard files already exist. **`--force` does NOT override this**; to redo a
  fresh split from scratch, restore MEMORY.md from its
  `archive/MEMORY.pre-shard-*.md` backup first. To grow the shards that
  already exist instead, use `--extend` (below) — that's the sanctioned path,
  not a workaround.
- **`--extend` — move overflow lines into an EXISTING shard.** The inverse precondition of plain `shard`: `--extend`
  refuses when the core is NOT already sharded (run plain `shard --apply`
  first). It finds core bullets that classify (same taxonomy) to a shard
  file that ALREADY exists on disk and moves them in, refreshing that
  shard's JIT pointer note-count/token estimate from disk on every run.
  Never creates a new shard — a taxonomy match against a shard with no
  existing `MEMORY.<name>.md` file is left untouched in the core; growing
  the taxonomy into a brand-new topic is plain `shard`'s job.
  ```bash
  python3 memory_gardener.py shard --dir … --taxonomy my-shards.json --extend
  python3 memory_gardener.py shard --dir … --taxonomy my-shards.json \
      --extend --apply --expect-sha256 <hash printed by the dry run>
  ```
  Same dry-run-default / `--apply`-is-sha256-guarded shape as plain `shard`,
  its own pre-change core backup, and its own no-loss invariant (every
  non-pointer bullet across {core + every shard file on disk} survives the
  move). This is the mechanism `budget`'s already-sharded proposal (above)
  points at.
- **The DESTINATION shard is checked against the harness ceiling, and an
  over-cap move is REFUSED.** A shard is a memory file, so `filebudget`'s
  200-line/25,000-byte truncation cap applies to it exactly as it does to
  `MEMORY.md`. Curing the core by pushing a shard past that cap does not fix
  anything — it relocates the same silent truncation into a JIT-loaded file,
  where it is *less* likely to be noticed. `FILE_BUDGET_MAX_BYTES` used to be
  consulted by `filebudget` alone, so `shard`/`--extend` would happily propose
  exactly that move. Both the dry run and `--apply` now judge every destination:
  the proposal table carries a **Post-move vs harness cap** column (bytes used,
  plus how many more notes fit at that shard's own line density), a `warn`
  destination is surfaced but allowed, and an `over` one makes `--apply` refuse.
  The dry run states the refusal it would hit, so a preview can never look green
  for a move apply rejects. There is deliberately no override: the fix is to trim
  the destination's index lines, or split the taxonomy so the overflow spreads
  across more shards.
  The notes-headroom figure is the one that matters. On a real store an
  extend put one shard at 20,038 B — comfortably inside
  25,000, and *room for ~18 more notes* against a shard that gains roughly one a
  day. "Inside the cap" and "worth doing" are different questions.
- **Backs up first, and the backup is sacrosanct.** The memory dir is
  Syncthing-synced, not git-tracked — no VCS undo. `--apply` copies MEMORY.md to
  `archive/MEMORY.pre-shard-<today>.md` (or `-2`, `-3`, … if that name is taken)
  and **never overwrites an existing backup under any flag**. Nothing is deleted.
- **No-loss invariant.** Before writing, `shard` asserts the multiset of index
  bullet lines across {new core + all shard files} exactly equals the original
  MEMORY.md's bullets (JIT pointers excluded — they point at the new shard
  files). Any mismatch writes NOTHING and exits non-zero. The load-bearing
  safety check: the split can never drop, duplicate, or mangle a memory line.
- **Atomic commit.** Every output (backup + shards + new core) is staged to a
  sibling temp file; only once all temps are written are they `os.replace`'d
  into place. Any failure mid-write removes the temps and leaves the real dir
  byte-identical to the start — never a half-written, inconsistent index.
- **No silent clobber.** If any `MEMORY.<topic>.md` output file already exists,
  `shard` refuses unless `--force` (which governs shard OUTPUT files only, never
  the backup).
- **Only index files change.** `shard` never touches a fact `*.md` file — only
  MEMORY.md (rewritten as the core) plus the new `MEMORY.<topic>.md` shards and
  the archive backup. Fact-file frontmatter stays additive-only, untouched here.

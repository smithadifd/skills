---
name: memory-gardener
kind: portable
version: 1.3.0
description: >
  SUPERVISED, report-first reconciliation of the Claude memory dir
  (~/.claude/projects/<proj>/memory/): fact files classified for staleness, resolved
  correction-sagas rewritten into one current-truth statement, "index-sickness" in
  MEMORY.md cured, dead facts archived (never deleted), [[wikilinks]] repaired, and the
  MEMORY.md index load-cost budget guarded with topic-sharding proposed when it grows
  over. DEFAULT IS A REPORT (per-file verdict + diff); `--apply` runs only after the owner
  reviews it. Use when the user says "garden the memory", "reconcile my memories", "clean
  up MEMORY.md", "the memory index is bloated", "check the memory budget", "is the index
  over budget", "shard the memory index", "prune dead memories", or "run the memory
  gardener". NEVER run unattended, from a queue, or with --apply before the report
  is reviewed — the sole exception is the read-only `budget` check.
---

# Memory Gardener

The memory dir accretes. Facts get corrected in place (so a note carries its
whole WRONG→CORRECTED chain), the same fact lands in two files, resolved sagas
go stale, and the `MEMORY.md` index — meant to be one scannable line per memory
— swells into paragraph-length state dumps that duplicate the files. This skill
reconciles all of that, but it treats the memory dir as **production data owned
by its owner**: it proposes, the owner disposes.

The heavy, error-prone mechanics (YAML-safe frontmatter edits, index rebuilds,
diff assembly, wikilink repair, archive moves) live in a deterministic helper,
[`memory_gardener.py`](memory_gardener.py). The **judgment** — reading each
file and deciding its verdict, writing the unified truth — is the model's. The
helper never classifies; the model never hand-edits YAML.

## Absolute guardrails (read before running anything)

- **Report-first, always.** The default output is a review report. `--apply`
  mutates the memory dir and runs ONLY after the owner has read the report and said
  go. Never chain report→apply in one breath.
- **NEVER unattended.** Do not run `scan → report → apply` from a task queue,
  a scheduled job, or any headless/queued context. It edits the owner's personal
  memory — a live, supervised surface.
  Report generation is read-only and safe; `--apply` is interactive-only.
- **Two scheduled exceptions: `budget` and `filebudget`.** Both are pure
  read-only checks — no `--apply`, cannot mutate. `budget` measures MEMORY.md's
  own wholesale-load token/line cost and, when over, *prints* a shard proposal.
  `filebudget` checks the DIFFERENT, harder ceiling the
  harness itself enforces on EVERY memory file across ALL stores — see
  "Per-file harness truncation budget" below. Both are safe to run on a
  schedule: periodic dry-runs that alert on their own exit code. What `budget`
  proposes is executed by its writing sibling `shard` (`shard --apply`,
  supervised, sha256-guarded); what `filebudget` flags is fixed by the
  `report → apply` fact-file gate (an `UPDATE` verdict, optionally with
  `pointerize` — see below) or, for a genuinely oversized index, by `shard`.
  None of the mutating paths are triggered automatically by either check.
- **Additive-only frontmatter.** The ONLY keys this tool writes are `status`,
  `last_verified`, `supersedes`, and `description`. It **never** removes or
  renames `name` or `metadata.*` — the harness reads `name` / `description` /
  `metadata.type` for recall, and that must keep working. `description` is
  writable (issue #177) precisely because it's part of that recall
  surface: a `SUPERSEDED`/`UPDATE` body rewrite with no way to correct a
  `description` that now contradicts it left notes retrievable under a false
  summary, or unretrievable at all. Same guardrails as the other three keys —
  report-first, `--apply`-gated, spliced as text (not reparsed-and-
  reserialized) — and same discipline: don't set it unless you're actually
  correcting it (never auto-derive one just because a file was touched). The
  helper enforces the whitelist mechanically (it refuses any other key); don't
  work around it.
- **Never delete. Archive only.** DEAD facts are MOVED into `archive/` with a
  one-line tombstone reason in frontmatter; git records the rename on commit.
  There is no delete path, by design.
- **Preserve `[[wikilinks]]`.** If a rename or archive would dangle a link, the
  plan must carry the repair; `apply` warns loudly about any link left dangling.
- **The report IS the plan.** `report` and `apply` build proposed content from
  the SAME plan JSON through the same function — nothing is re-derived between
  what the owner reviewed and what gets written. Don't edit files by hand between
  the two steps; edit the plan and re-report.
- **Don't mass-rewrite untouched files.** A KEEP verdict writes nothing to the
  fact file — not even a `last_verified` stamp. Frontmatter is stamped only on
  files the run actually touches (UPDATE / SUPERSEDED / DEAD). A KEEP entry MAY
  still carry an `index_line`: the index is not the fact, and fixing a bloated
  index line is not a claim to have re-verified the note.

## Sources

| Source | Path | Used for |
|---|---|---|
| Memory dir | `~/.claude/projects/<project>/memory/` | the fact files + `MEMORY.md` index (the thing being gardened) |
| Helper | `~/.claude/skills/memory-gardener/memory_gardener.py` | scan / report / apply / budget / shard / filebudget (stdlib-only, offline) |
| Plan | a scratch `plan.json` you write from the scan | the reviewed set of verdicts; shared by report and apply |
| Reports | `~/.claude/logs/memory-gardener/` | the only write target of `report`; a same-day re-run claims `<date>-2.md`, never overwrites |
| Fixture | `skills/memory-gardener/tests/fixtures/memory/` | invented fake memories for the tests (no real content) |

## The classification rubric

One verdict per file. The rubric is grounded in the pathologies actually present
in the memory dir (examples below are **anonymized by name** — the repo is
public, so recognize the *shape*, not the private specifics).

### `KEEP`
Single current-truth statement, no correction chain, verified recently enough.
**Write nothing to the file** (not even a stamp).

This is also the verdict for a pure **index-line trim**: pair `KEEP` with an
`index_line` to rebuild a bloated line while leaving the note byte-identical.
Compressing a summary is index hygiene, not re-verification, so it must not
stamp `last_verified` — an index-wide trim run under `UPDATE` would mark every
note verified-today and blind the `unverified_over_90d` tripwire across the
whole store for 90 days.
*Shape:* a short reference note ("how the compost rotation works") whose index
line is one sentence — or one whose line has ballooned but whose content you
did not read closely enough to re-verify.

### `UPDATE`
The file's content is fine and single-truth, but something mechanical is wrong
**in the file**: the frontmatter needs an additive stamp, `description` has
drifted from the body, or a wikilink needs repair. Correct `description` if (and
only if) it's actually wrong, and/or stamp `last_verified`; leave the body alone
otherwise. Use this over `KEEP` when you actually opened and re-verified the
note — the `last_verified` stamp is that claim, so don't make it idly.
*Real shape — "index-sickness":* one roadmap note's `MEMORY.md` line had grown
into a ~300+-word paragraph reproducing trademark-conflict details, commit
hashes, and wire-namespace decisions that all already live in the file body.
The index is supposed to be one line you can scan; when it becomes a second copy
of the file, that's index-sickness — cure it with `KEEP` + `index_line` above
unless you're changing the file too.
*Real shape — "index-sickness":* one roadmap note's `MEMORY.md` line had grown
into a ~300+-word paragraph reproducing trademark-conflict details, commit
hashes, and wire-namespace decisions that all already live in the file body.
The index is supposed to be one line you can scan; when it becomes a second copy
of the file, that's index-sickness. Real healthy index lines are ~1 sentence.

### `SUPERSEDED`
A **resolved saga**: the file carries a correction chain
(WRONG → CORRECTED → "red herring" → dead-ends), and the current one-line truth
is buried under the history — OR the same fact is **duplicated across files**.
Rewrite the body to ONE current-truth statement and move the history into a
short appended **changelog** (dated one-liners). If the fact now lives
canonically in another note, set that note's `supersedes: [this-one]`, rewrite
this one to a concise stub that points at the canonical note, and stamp
`status: superseded`. **Check `description` against the new body** — it's the
field the harness reads for recall, so a rewrite that leaves it describing the
retired state (issue #177) makes the note misleading or unfindable
even though its body is now correct. Set `frontmatter.description` in the plan
entry ONLY when it actually needs correcting; don't rewrite it reflexively.
*Real shape — superseded saga:* an infra-diagnosis note carried "CORRECTION to
my earlier diagnosis (I got this wrong)", a "fooled me THREE times" trap, and a
"dead-ends — do NOT re-try" list; its sibling note was explicitly marked
"UNIFIED … the earlier conclusion was WRONG." Two files, one root cause, both
front-loading the wrong turns. Consolidate: canonical note keeps the truth +
changelog and gains `supersedes`; the sibling becomes a stub.

### `DEAD`
The fact is no longer true — the thing was decommissioned, sold, or fully
replaced. **Archive** it (move to `archive/` with a tombstone reason); never
delete. Repair every inbound `[[wikilink]]`.
*Shape:* a note about a tool/host that has since been retired; keep it for
history, get it out of the live set.

### `UNVERIFIED>90d`
`last_verified` (or, absent that, file mtime) is older than 90 days. **Flag for
human re-verification; change nothing.** This is a prompt to the owner, not an edit.
*Real shape — resolved-then-stale:* a note stating "token expires <date>" where
the date is already in the past — the fact resolved once but nobody re-touched
it, so its truth is now suspect.

Two more things every run should notice, surfaced by `scan`:
- **Missing provenance frontmatter.** No legacy note carries `status` /
  `last_verified`; stamp them additively only on files you touch for another
  reason (don't mass-stamp — that's a KEEP violation).
- **Dangling links.** `scan` reports any `[[target]]` whose file is absent —
  fix these as part of whatever verdict owns the target.

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

## Flow

1. **Scan (read-only).** From the skill dir:
   ```bash
   python3 memory_gardener.py scan --dir ~/.claude/projects/<proj>/memory > /tmp/mem-scan.json
   ```
   Read the signals: `balloon_index`, `correction_chain` (+ the matched
   `saga_markers`), `unverified_over_90d`, `missing_frontmatter_stamp`,
   `inbound_links`, the top-level `dangling_links`, the top-level
   `hyphen_underscore_repairs` — SUGGESTED wikilink fixes (see "Wikilink
   hyphen/underscore repair" below) — and top-level `integrity`. A clean scan
   reports `integrity.clean: true`; `cross_index_duplicates` otherwise names
   every duplicated note and every reachable index file listing it. Verify each
   suggested wikilink repair before folding it into a plan entry's
   `wikilink_repairs`; never apply blind.
2. **Read the flagged files** and classify each into the rubric. The signals are
   hints, not verdicts — open the file and decide. Write `plan.json`.
3. **Report (read-only).**
   ```bash
   python3 memory_gardener.py report --dir ~/.claude/projects/<proj>/memory --plan /tmp/plan.json
   ```
   Writes `~/.claude/logs/memory-gardener/<date>.md` (and echoes it). Read it
   back to the owner: the summary table first, then walk the diffs.
4. **Gate.** The owner reviews. They may hand-edit `plan.json` and you re-report.
   Loop until he approves. **Do not proceed without an explicit go.**
5. **Apply (mutating, only after the go).**
   ```bash
   python3 memory_gardener.py apply --dir ~/.claude/projects/<proj>/memory --plan /tmp/plan.json
   ```
   Executes exactly the reviewed plan: additive stamps, body rewrites, index
   rebuild, archive moves + tombstones, wikilink repair. It prints each action
   and exits non-zero if any link is left dangling.
6. **Show the result.** `git diff` (or `git status`) in the memory dir if it's
   tracked; confirm the archive move and the shrunk index. The owner commits.

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

- **Token cost method.** A dependency-free **chars/4** approximation (the toolkit
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
  (`MEMORY.fleet.md`, `MEMORY.infra.md`, `MEMORY.automation.md`,
  `MEMORY.secrets.md`, …) pulled **just-in-time**. Each shard gets a one-line
  JIT pointer in the core index. It stays **markdown** on purpose — grep/Explore
  displaces an embedding index, so there's no vector store to maintain. Notes
  are grouped by the first available taxonomy in this order: explicit
  `--taxonomy`, `<memory-dir>/shard-taxonomy.json`, then the built-in defaults.
  Both `budget` and `shard` print the selected source. A present but malformed
  store-local file is a config error (exit 2), never permission to fall through
  to defaults. Taxonomy JSON has the shape
  `{"shards":[{"name","label","keywords":[…]}]}`; unmatched notes stay in core.
- **It's a PROPOSAL, not an edit.** `budget` never writes the shard files or
  touches memory. The actual split — moving lines into `MEMORY.<topic>.md`,
  writing the core pointers — is performed by the **`shard`** subcommand below
  (its writing sibling), a **separate supervised sitting** (and, for the live
  memory dir, a human-reviewed one-off). `--apply` stays supervised,
  interactive-only.
- **Already-sharded core (issue #175).** If `MEMORY.md` is over
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
`budget` on a cadence and Discord-alerts on exit `3` — never `apply`.

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
- **`--extend` — move overflow lines into an EXISTING shard
  (issue #175).** The inverse precondition of plain `shard`: `--extend`
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

## Per-file harness truncation budget (`filebudget` — read-only, schedulable)

`budget` above polices MEMORY.md's own wholesale-load token cost — a
JIT-sharding concern with its own tuned defaults (4500 tok / 100 lines).
`filebudget` checks a **different, harder** ceiling: the
Claude Code runtime itself silently truncates **any** memory file — not just
MEMORY.md — once it exceeds **200 lines OR 25,000 bytes**
(the Claude Code runtime's per-file memory load limit). Content past
the cap is loaded behind a single `WARNING` line and is otherwise
**unreachable, with no error**. `filebudget` is the tripwire for that ceiling,
across every store, every file.

```bash
# read-only: every ~/.claude/projects/*/memory store, every *.md file in it.
python3 memory_gardener.py filebudget
python3 memory_gardener.py filebudget --dir ~/.claude/projects/<proj>/memory   # one store
python3 memory_gardener.py filebudget --max-lines 200 --max-bytes 25000 --warn-lines 180
python3 memory_gardener.py filebudget --json
```

- **The OR is load-bearing, not decorative.** A file can blow the byte cap on
  dense prose well under 200 lines, or blow the line cap with short lines well
  under 25 KB. Both axes are checked independently; either one alone is `OVER`.
- **Byte-accurate, not char-accurate.** These notes are full of multi-byte
  UTF-8 (em dashes, etc.); the ceiling is measured in bytes-on-disk, not
  Python string length — the two differ by hundreds of bytes on a typical note.
- **Warn band: 180–200 lines.** Lines in `[180, 200]` are flagged `WARN` (not
  yet truncating) so a file gets attention before it silently loses content,
  not after.
- **`memory/docs/` and `memory/archive/` are EXCLUDED from `OVER`/`WARN`.**
  Verified against real stores: a naive scan hit 8 files, and
  every one was a false positive — the `docs/*-full.md` bodies are the
  pointer-refactor's own OUTPUT (deliberately oversized; that's the whole
  point of moving them out of the wholesale-loaded set) and
  `archive/MEMORY.pre-shard-*.md` are frozen backups the harness never
  reloads. Flagging either class would tell the owner to "fix" files that are
  already correctly parked outside the load path. They're still walked and
  reported (`status: excluded`) for auditability — just never counted toward
  `OVER`/`WARN` or the exit code.
- **Exit code = the alert**, same posture as `budget`: `0` clean, `3`
  (`OVER_BUDGET_EXIT`, shared with `budget`) if anything is `OVER`, `4`
  (`FILEBUDGET_WARN_EXIT`) if nothing is `OVER` but something is `WARN`. A
  scheduled wrapper can tell "must fix" from "worth a look" apart.
- **All stores by default.** Unlike every other subcommand, `filebudget` has
  no single default `--dir` — it globs `--projects-root` (default
  `~/.claude/projects`) for every `*/memory` dir that exists. Pass `--dir` to
  scope it to one store.
- **Syncthing internals are skipped.** `.stfolder`/`.stversions` (and any
  other dotdir) are never walked — they're sync machinery, not memory content.
- **Fixing what it flags** is an `UPDATE` verdict through the normal
  `report → apply` gate — usually paired with `pointerize` below to actually
  shrink the file, not just re-word it thinner.

## Pointer-memory refactor (`pointerize` plan field — mutating, via `apply`)

Mechanizes a manual sweep of the largest notes in a real memory dir (the
format spec): move an oversized note's body to `memory/docs/<stem>-full.md`
and leave a short pointer at the file's **original basename**, so inbound
`[[wikilinks]]` and index links need **zero repair**.

Add a `pointerize` block to a plan entry (verdict must be `UPDATE` or
`SUPERSEDED`) alongside `body` set to the pointer text:

```json
{
  "file": "some-note.md",
  "verdict": "UPDATE",
  "sha256": "<from scan>",
  "body": "\n**Pointer memory.** The full document (NNN lines) is at [docs/some-note-full.md](docs/some-note-full.md) — read it with the Read tool when you need the detail. It was moved out of this file on <date> because memory files are truncated on load at 200 lines / 25,000 bytes.\n\n<headline + the 3-6 settled facts worth keeping inline>\n\nRelated: [[other-note]]\n",
  "pointerize": {
    "full_body": "<the FULL original document text — verbatim; a no-loss check refuses a summary>",
    "docs_sha256": "<REQUIRED by apply only if docs/some-note-full.md already exists; report prints it>"
  }
}
```

- **Match the five hand-made pointers' shape.** Frontmatter (`name`,
  `description`, `metadata.*`) stays exactly as-is — only the additive
  `last_verified` stamp changes. The pointer body is: one **"Pointer memory."**
  sentence naming the doc's size and the reason (the 200-line/25,000-byte
  truncation), a **Headline** paragraph, a short list of the facts worth
  keeping inline (the ones the owner needs without opening the doc), and a
  closing **Related:** line carrying every `[[wikilink]]` the original body
  had. Write this by hand, the same judgment call as any other `body` —
  `pointerize` only mechanizes the file move.
- **The destination is `docs_target_path(memory_dir, stem)`: a hard,
  unit-tested invariant.** It refuses (raises, before any write) if: a stem
  contains a path separator; `memory_dir/docs` is **itself a symlink** (see
  below — this is checked before any `realpath()` resolution, deliberately,
  not skippable); or the resolved real path still falls outside a real
  `memory_dir/docs/`. Syncthing's `.stignore` negates only
  `!/projects/*/memory` for sync, so **any** destination outside that subtree
  reads as a **DELETE on the peer machine** — this is not a style preference,
  it's a data-loss guard. `validate_plan` calls this guard for every
  `pointerize` entry BEFORE either `report` or `apply` writes anything, so a
  bad plan fails closed, not mid-write.
- **Why the symlink check exists as its own step.** If `memory_dir/docs` were
  replaced with a symlink to somewhere outside the memory tree, resolving
  both the target and `docs/` through `os.path.realpath()` would make them
  agree on that external directory — the outside-`docs/` escape check would
  pass **vacuously**, and the write would land outside `memory/` entirely
  (the exact case the guard exists to prevent). `docs_target_path` therefore
  checks `os.path.islink()` on the raw `docs/` path first. This check is
  scoped to the `docs/` path component only — `memory_dir` itself is trusted
  as the caller-validated root (e.g. `discover_memory_stores`'s
  `os.path.isdir` gate), the same trust boundary every other read/write in
  this module already relies on.
- **`filebudget`'s scan follows a symlinked `docs/`/`archive/` too (read-only,
  no guard needed).** `iter_memory_files` doesn't follow symlinked
  directories in general (loop-avoidance), but a symlinked top-level `docs/`
  or `archive/` is the one deliberate exception: its `*.md` contents are
  still walked and show up in the report tagged `excluded`, same as a real
  `docs/`/`archive/` dir — never silently invisible. Unlike the write-side
  guard above, reading through the symlink here carries no data-loss risk.
- **No-loss invariant — `full_body` is checked against the source.**
  `check_pointerize_no_loss` (the pointerize twin of `shard`'s
  `check_no_loss`) asserts that **every content line of the original body
  survives** in the pointer stub ∪ the extracted `full_body`, and raises —
  writing nothing — otherwise. This exists because `full_body` is
  *model-authored prose*: nothing previously compared it to the source, so a
  400-fact, 12KB note could be replaced by a 196-byte summary and apply
  cleanly. A pointerize is a **MOVE, not a rewrite** — the body has to come
  out the other side verbatim. It is **containment, not equality** (unlike the
  shard twin): extra lines are expected — the pointer stub is new prose by
  design — only *missing* lines are a defect. If you want to reword a pointer
  stub, that's an ordinary `UPDATE`, not a `pointerize`.
  **Indentation counts as content.** Lines are compared with their *leading*
  whitespace intact, because markdown indentation is meaning-bearing: flattening
  a nested list keeps every line's text while destroying which child belongs to
  which parent, and lifting a line out of a fenced code block leaves code that
  no longer runs. Both used to pass. *Trailing* whitespace and CRLF-vs-LF are
  normalized away — they carry no fact and editors rewrite them silently, so
  refusing on them would only be a false alarm. Practical consequence: a
  `pointerize` may not re-indent or re-flow the body it moves. Reformatting is
  an ordinary `UPDATE`.
- **Atomic commit — the source is never destroyed by a failed extraction.**
  The backup, the pointer stub and the extracted body are all staged to
  `.tmp-pointerize` siblings and `os.replace`d into place **only after every
  stage succeeds**; any failure removes the temps. The extraction directory is
  created *first*, so a `docs/` that is a regular file or is non-writable fails
  while the source is still fully intact. (Previously the source was overwritten
  with the pointer stub **first** and `docs/<stem>-full.md` written second,
  unstaged and without a `try`/`except` — both of those conditions destroyed the
  body permanently.) Mirrors `execute_shard`'s commit exactly.
  **The source is replaced LAST**, because a multi-file commit cannot truly be
  atomic on POSIX — `os.replace` is atomic per *file*, and nothing makes three
  of them one transaction. The order (backup → destination → source) is what
  makes every reachable partial state safe: a failure at any point leaves the
  source at its original content, and the worst case is a stray backup and/or a
  written extraction with the source untouched — additive, harmless, and
  idempotently retryable. Committing the source before the destination would
  leave a stub pointing at a file that does not exist.
  Staging files are **exclusively created** under randomized names, so a stale
  temp from a crashed run is never truncated, two concurrent gardeners never
  share a staging file, and a symlink planted at a predictable temp path can
  never redirect the write outside `memory/`.
- **Backup — `archive/<stem>.pre-pointerize-<date>.md`.** The true
  pre-pointerize original is archived whenever the source text actually
  changes, so the move is always undoable. An existing backup is **never**
  overwritten; a repeat run claims `-2`, `-3`, … (same sacrosanct-backup rule
  as the pre-shard backup) — and the name is *reserved* the moment it is chosen,
  so a backup that appears while the commit is staging cannot be clobbered by
  it. No backup is written when the plan leaves the source unchanged — nothing
  was moved out of it, so there is nothing to recover.
- **Destination drift guard — `pointerize.docs_sha256`.** The plan's `sha256`
  guards the *source* against changing between report and apply; `docs_sha256`
  does the same for the *destination*, which was previously clobbered
  silently. A provided hash must match the file on disk (checked in `report`
  as well as `apply`), and a destination that has vanished under the plan
  refuses too. On `apply`, an **existing** destination **must** carry one —
  that is the "it appeared during the review gap" case. `report` prints the
  destination's current sha256 to copy into the plan, alongside the plain
  "OVERWRITES an existing file" flag, so the overwrite is acknowledged
  deliberately rather than by omission. A destination that doesn't exist needs
  no hash — that's the ordinary first-time extraction. The hash is re-verified
  **immediately before the destination is written**, not only when the plan is
  validated: a concurrent writer that creates or edits the destination while
  the commit is staging would otherwise slip through the plan-time check.
- **Report/apply parity, same as everything else.**
  `compute_pointerize_output_checked` — the pure computation *plus* the
  no-loss invariant, bound together so they cannot drift apart — is what both
  `report` (preview: "WOULD WRITE `docs/<stem>-full.md`") and `apply`
  (actually writes it) call. Nothing is re-derived between what gets reviewed
  and what gets written, and a plan `apply` would refuse can never be previewed
  green: `report` fails on the identical check, in the **safe** direction.

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

## Report format

`~/.claude/logs/memory-gardener/<date>.md`:

```markdown
# Memory gardener — reconciliation report — <date>

Memory dir: `…`
**This report IS the plan.** apply executes exactly these changes…

## Summary
| Verdict | File | Change |
|---|---|---|
| DEAD | `old-note.md` | → archive/ (<reason>) |
| SUPERSEDED | `saga-note.md` | frontmatter/body updated |
| UPDATE | `bloated-index.md` | frontmatter/body updated |
| UNVERIFIED>90d | `stale-note.md` | no file change |
| KEEP | `clean-note.md` | no file change |

## Wikilink repairs
- `[[old-note]]` → `[[archive/old-note]]`

## Per-file detail
### `saga-note.md` — **SUPERSEDED**
<rationale>
```diff
<unified diff of the proposed file change>
```
**Index line:**
```diff
- <old ballooned line>
+ <new one-liner>
```
```

Rows are ordered DEAD → SUPERSEDED → UPDATE → UNVERIFIED>90d → KEEP so the
consequential changes are at the top.

## Tests

The toolkit has no repo-wide Python runner; this suite is standalone and
network-free (pure logic + the invented fixture memory dir):

```bash
python3 -m pytest skills/memory-gardener/tests/
```

If your default `python3` lacks pytest, run it with any interpreter that has it
(e.g. `<project>/.venv/bin/python -m pytest …`). The
helper itself is stdlib-only; only the tests need pytest.

The fixtures under `tests/fixtures/memory/` are **invented fake memories**
(garden/yard facts) built to exhibit each pathology — index-sickness, a
WRONG→CORRECTED saga, cross-file duplication, a dead/archived fact, and an
unverified>90d note. No real memory content is in the repo.

The budget/sharding tests cover the chars/4 estimate, over/under-budget
detection on both axes, the shard-proposal grouping + core-pointer emission,
the full taxonomy precedence matrix (explicit override > store-local
`shard-taxonomy.json` > built-in defaults), the `stop-limit`/default-`op-`
misfile regression, selected-source output from both commands, malformed-local
refusal without fallback, the no-loss preservation invariant (interleaved
headings/prose/comments/malformed bullets survive the proposal, accounted for
via `non_index_lines_retained`), defensive taxonomy validation (missing
`shards` key, non-object entries, duplicate names, empty-string keywords — all
clean exit-2 refusals, never a traceback), rejection of non-positive
`--max-tokens`/`--max-lines`, and — the standing safety gate — a snapshot proof
that a `budget` run over an OVER-budget dir leaves every file byte-identical and
writes no shard/archive files (the schedulable check is provably read-only,
with no `--apply`/`--plan` surface).

The `shard` executor tests cover: the dry-run being byte-identical to `budget`'s
proposal (same `propose_shards` grouping), the `--apply` no-loss invariant
verified against the ACTUAL written core+shard files (every original bullet
reproduced), fact-note byte-identity across an apply, the drift guard
(absent/wrong `--expect-sha256` → refuse, dir byte-identical), the
no-silent-clobber guard (+ `--force` override), and the on-disk
`archive/MEMORY.pre-shard-<date>.md` backup.

The `filebudget` tests (all `tmp_path`-synthetic — no real
fixture bloat) cover: byte-vs-char measurement on multi-byte content, the OR
selection (a file over on lines only and one over on bytes only, both flagged
independently), the 180–200 warn band's edges (183 → WARN, 200 → WARN not
OVER, 201 → OVER not WARN, 170 → ok), the `docs/`/`archive/` exclusion (still
walked and reported, never counted as OVER/WARN), multi-store aggregation via
`discover_memory_stores`, non-positive/inconsistent ceiling refusals, all
three exit codes (`0`/`3`/`4`) via `main()`, and a read-only-mutation proof
(dir snapshot unchanged across a run).

The `pointerize` tests cover: the plan-field round trip (frontmatter preserved
byte-for-byte except the additive `last_verified` stamp, full body lands
verbatim at `docs/<stem>-full.md`, pointer body lands at the ORIGINAL
basename), report/apply preview parity, the overwrite-is-visible-not-silent
behavior, verdict restriction (KEEP refused), missing-`full_body` refusal, and
— the load-bearing one — the outside-`memory/` guard proven to fire against a
REAL nested file (`sub/dir.md`) through the actual `apply_plan`/`render_report`
entrypoints, with a directory-snapshot proof that nothing was written anywhere
when it refuses.

The `pointerize` **data-safety** tests cover the three invariants above, each
with a directory-snapshot proof that a refusal writes nothing at all:
**atomicity** (a mid-staging failure leaves the dir byte-identical, the source
body intact, no leftover `.tmp-pointerize`, no committed backup), plus the two
*reproduced* data-loss defects as regression tests — a `docs/` that is a
regular file, and a non-writable `docs/`, each of which previously destroyed
the body permanently and now fails with the source untouched; **no-loss** (a
400-fact note collapsed to a ~200-byte summary refuses; containment semantics
proven both ways — extra prose fine, one dropped fact fatal; `report` refuses
identically to `apply`); and **destination drift** (a `docs/<stem>-full.md`
edited under the plan, deleted under the plan, or appearing after the report
was reviewed all refuse rather than clobber, and acknowledging it with the
hash the report prints lets it proceed). The backup path is covered too: the
true pre-pointerize original lands in `archive/`, an existing backup is never
overwritten (`-2`), and no backup is written when the source is unchanged.

A second hardening pass covers the concurrency and ordering edges a single-file
view of "atomic" misses: a failure injected at **every** `os.replace` position
in turn (the source must survive all of them, and the worst partial state is
proven additive), a pre-existing staging temp left untouched, a symlink planted
at a staging path proven not to redirect the write outside `memory/`, a
destination created *during* staging refused rather than clobbered, the backup
name proven *reserved* rather than merely chosen (and an unused reservation
released), and the no-loss comparison proven to catch flattened nested lists and
de-indented code while tolerating trailing whitespace, CRLF, and to count
duplicate lines as a multiset.

The shard-aware index tests (all reproduced against a real store
before being written) cover: `INDEX_LINE_RE` matching a title that carries its
own bracketed run (``[`/model` … `[1m]`](…)``) while still keying off the FIRST
link when the SUMMARY carries a second one (a greedy match-to-the-last-`](`
would silently re-key that line to the wrong note); `rebuild_index` REPLACING
rather than appending such a line; `_assert_no_duplicate_index_targets`
refusing a doubled listing and passing a clean one; `index_homes` resolving core
vs shard residents and never claiming a home for a pointer bullet; `scan`
reporting a non-zero `index_summary_len` plus the owning `index_file` for a
shard-resident note; cross-index integrity reporting every home when a note is
listed in core plus shard or in two shards, while a single-home note remains
clean and repeated scans preserve ordering; `apply_plan` writing a
shard-resident note's trimmed line into ITS OWN shard with the core left
byte-identical (the load-bearing regression — this previously appended a
duplicate to `MEMORY.md`); and the never-silently-dropped path still appending
a brand-new entry to the core.

The destination-ceiling tests cover: `measure_text` reporting bytes distinctly
from chars (multi-byte content makes them differ by enough to matter);
`_extend_ceiling` flagging over-on-bytes and over-on-lines independently plus
the warn band; `execute_extend` REFUSING when a move would push a destination
shard past the ceiling, with a directory-snapshot proof that nothing is written
on the refusal; and the dry run stating that refusal in its own footnote, so
report/apply parity holds in the safe direction.

The wikilink hyphen/underscore tests cover: the normalizer catching the real
shape (`[[feedback-self-review]]` → `feedback_self_review.md`), leaving an
exactly-resolving link alone, refusing to fabricate a repair across a missing
prefix or a different store, flagging genuine ambiguity when two live files
share a normalized key, and `scan()` surfacing the new key end-to-end (with a
regression check that the shipped fixture — matching the real
stores — has zero suggestions).

## Gotchas

- **The signals are hints, not verdicts.** `correction_chain` fires on marker
  words (WRONG, CORRECTION, red herring, dead-end); a note can mention those
  while still being current. Always read the file.
- **`balloon_index` is a length heuristic** (index summary > ~320 chars). A
  genuinely dense one-liner near the threshold is fine — judge it. It reads the
  note's line from whichever index owns it, so shard-resident notes are covered;
  before that fix `index_summary_len` was 0 for every note outside `MEMORY.md`,
  which made the signal structurally blind to 92 of the live store's 173 notes —
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

---
name: memory-gardener
kind: portable
version: 1.4.0
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
  gardener". NOT for editing a single memory note by hand, and NOT for notes
  outside a Claude memory directory. NEVER run unattended, from a queue, or with --apply before the report
  is reviewed — the sole exceptions are the read-only `budget`, `filebudget` and
  `classify` checks.
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
helper never decides a note's keep/update/dead verdict (`classify` only sorts
notes into migration homes, as a read-only report); the model never hand-edits
YAML.

## Absolute guardrails (read before running anything)

- **Report-first, always.** The default output is a review report. `--apply`
  mutates the memory dir and runs ONLY after the owner has read the report and said
  go. Never chain report→apply in one breath.
- **NEVER unattended.** Do not run `scan → report → apply` from a task queue,
  a scheduled job, or any headless/queued context. It edits the owner's personal
  memory — a live, supervised surface.
  Report generation is read-only and safe; `--apply` is interactive-only.
- **Three read-only checks: `budget`, `filebudget` and `classify`.** All are
  pure read-only reports — no `--apply`, cannot mutate the memory dir. `budget` measures MEMORY.md's
  own wholesale-load token/line cost and, when over, *prints* a shard proposal.
  `filebudget` checks the DIFFERENT, harder ceiling the
  harness itself enforces on EVERY memory file across ALL stores — see
  "Per-file harness truncation budget" below. `classify` (see "Migration
  classification" below) sorts every note into a migration home and writes
  only an explicit `--out` outside the memory dir; its report is read by the
  owner and nothing applies it. `budget` and `filebudget` are safe to run on a
  schedule: periodic dry-runs that alert on their own exit code. What `budget`
  proposes is executed by its writing sibling `shard` (`shard --apply`,
  supervised, sha256-guarded); what `filebudget` flags is fixed by the
  `report → apply` fact-file gate (an `UPDATE` verdict, optionally with
  `pointerize` — see below) or, for a genuinely oversized index, by `shard`.
  None of the mutating paths are triggered automatically by any of the three.
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
| Helper | `${CLAUDE_SKILL_DIR}/memory_gardener.py` | scan / report / apply / budget / shard / filebudget / classify (stdlib-only, offline) |
| Plan | a scratch `plan.json` you write from the scan | the reviewed set of verdicts; shared by report and apply |
| Reports | `~/.claude/logs/memory-gardener/` | the only write target of `report`; a same-day re-run claims `<date>-2.md`, never overwrites |
| Fixture | `skills/memory-gardener/tests/fixtures/memory/` | invented fake memories for the tests (no real content) |

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

## Chapters

The long reference material is paged out of this file. Read a chapter when the step it covers comes up.

- [The classification rubric: KEEP, UPDATE, SUPERSEDED, DEAD, UNVERIFIED>90d](references/rubric.md)
- [The plan JSON the model writes](references/plan-json.md)
- [`budget` and `shard`: index load cost and topic-sharding](references/budget.md)
- [`classify`: migration classification](references/classify.md)
- [`filebudget`: per-file harness truncation budget](references/filebudget.md)
- [`pointerize`: the pointer-memory refactor](references/pointerize.md)
- [Wikilink hyphen/underscore repair](references/wikilink-repair.md)
- [Report format, with Summary, Wikilink repairs and Per-file detail](references/report-format.md)
- [Tests](references/tests.md)
- [Gotchas](references/gotchas.md)

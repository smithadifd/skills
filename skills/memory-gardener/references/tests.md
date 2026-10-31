## Tests

This repository has no repo-wide Python runner; this suite is standalone and
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

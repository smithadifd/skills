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

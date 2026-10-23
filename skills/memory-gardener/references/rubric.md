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

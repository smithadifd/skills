## Migration classification (`classify` — read-only report)

`classify` sorts every note in one memory dir into exactly one of five classes, so
a reviewed list exists before any supervised move. It is the report half only:
it never moves, rewrites, archives or deletes a note, opens nothing for writing
except an explicit `--out`, and refuses (exit 2) an `--out` whose spelled or
resolved path is inside the memory dir it is classifying, or that is a symlink.
A missing memory dir, `--rules-root`, `--ledger-root` or `MEMORY_GARDENER_DIR`
target also exits 2 and names the bad path.

```bash
python3 memory_gardener.py classify --dir ~/.claude/projects/<proj>/memory \
  --rules-root ~/.claude/rules --rules-root <dir-of-standards> \
  [--ledger-root <dir-of-ledger-items>] [--out report.md] [--stamp]
```

Notes are the top-level `*.md` files other than `MEMORY.md` and the
`MEMORY.<topic>.md` shard indexes. The report opens with a `counts:` line (one
count per class plus the total), then one line per note, sorted by filename:
`class | note | evidence [| low-confidence: why]`. The output is deterministic
(no clock in the body; `--stamp` adds a single `generated:` header line). The
class of each note is determined by the store's content alone. The report text
is not: a `low-confidence` reason can mention a quoted path that is missing, so
that one reason can change when the referenced file appears or vanishes.

| Class | Meaning | Evidence on the line |
|---|---|---|
| `personal` | about the owner; belongs in a personal store | `type=user` / `type=feedback` |
| `documented` | already written in a rule or standard; the note can go | `duplicates: <root>/<file>:<line>` |
| `decision` | a recorded choice; belongs in a decision record | `ledger: <root>/<file>:<line>`, `ledger: no match`, or `ledger: none yet` (a "ledger" here is any directory of decision records) when no `--ledger-root` is given |
| `project-gotcha` | a fact about one repo; belongs in that repo | `repo: <name>` |
| `stale` | dead; archive it | `reason: ...` |

Rules, applied in order (the first that fires wins):

1. **stale** — only an explicit marker counts: frontmatter `status` is
   resolved/superseded/obsolete/deprecated/dead/archived, or `superseded_by` is
   set, or the first heading or the `description` starts with RESOLVED/
   SUPERSEDED/OBSOLETE/replaced-by, or a body status line does: a line that
   opens the body (its first three non-blank lines) or is written `Status: ...`
   and starts with RESOLVED/SUPERSEDED/OBSOLETE/DEPRECATED/replaced-by. The same
   word mid-sentence, mid-title or deep in a long note is history, not a marker.
   A quoted path that is missing never makes a note stale; it only adds a
   `low-confidence` reason.
   Placeholders and globs (`<x>`, `*`, `{}`, `...`), URLs, API routes (a first
   directory that does not exist here) and temp or worktree paths are not
   checked.
2. **personal** — `metadata.type: user`.
3. **documented** — a line in a `--rules-root` directory (any `*.md`,
   searched recursively) is the note's duplicate only when the line and the
   note's `description` share at least 5 distinctive words and their Jaccard
   score (shared words over all words of both) is at least 0.4, so the overlap
   must hold in both directions. Stopwords, agent-tooling words (builder,
   reviewer, worktree, subagent, ...) and generic words (rule, standard, file,
   memory, ...) are not distinctive. Thresholds sit between the loose matches of
   an early review (at most 4 shared words, score 0.31 or less) and true
   restatements of a rule (5 or more, 0.45 or more). The line carries the
   `file:line` it matched. With no `--rules-root` nothing is searched, so no
   note is ever `documented`: a tooling-sounding note falls to its next class
   with `duplicates: not searched` on its line and a `low-confidence` reason.
   There is no built-in rules path.
4. **By `metadata.type`** — `feedback` → `personal`; `project` → `decision` when
   decision cues (decided, ratified, ADR, outcome, ...) outnumber gotcha cues
   (gotcha, workaround, never, fails, ...), else `project-gotcha`; `reference`
   and `mechanics` → `project-gotcha`. A missing or other type is decided by the
   same cue count.

Where the rules cannot decide, the line carries `low-confidence` and a reason:
an unmatched type, a project note with equal cue counts, a decision with no
matching ledger item, a harness-sounding note with no duplicate in the
searched roots (or no roots to search), a quoted path that is missing, or a repo taken
from the store's own name because the note names none. The repo comes from a
frontmatter `repo` field, else the first `code/<name>` or `work/<name>` path in
the note whose name has a letter or digit (`work/...` is skipped), else the store's
project slug.

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

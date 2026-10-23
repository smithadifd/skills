---
name: topic
kind: personal
version: 1.2.0
description: >
  Open, resume, or close a session note for a one-off, attended piece of work
  that will outlive the sitting. Resolves the thread's note file (project repo
  when one exists, else this skill's configured notes directory — `topics_dir`
  in `config.json` — as `<slug>.md`), creates it from that directory's
  TEMPLATE.md on first touch, or from the built-in skeleton in this file when
  the template is absent, opens EVERY touch with a reconcile pass against the
  note's external references and stamps "Last synced", maintains a Staged drafts
  rail for async continuation, and at close runs the promote-or-park ledger and
  names the close tier (LITE vs FULL). Use when the user says "/topic <slug>",
  "open a topic for this", "resume the <x> topic", "track this investigation",
  "stage this for later", or when an attended session starts with an ad-hoc ask
  that will outlive the sitting. NOT for a handoff note to a fresh session (that
  is `handoff`), and NOT for a throwaway question that ends with the sitting.
---

# Topic

Keeps one note per thread of work: open it, resume it on later touches, close it
with every follow-up accounted for. The optional convention document is
`<topics_dir>/README.md`. When that file is present it is a local reference:
read it live and follow whatever it adds (where follow-up work is recorded,
which surfaces count as tracked, extra close steps). This skill never restates
it as a second copy. When it is absent, print one line and continue with the
built-in default: each thread gets a note file, a promote-or-park ledger at
close, and a close tier.

The one line, for every absent optional read, is exactly:

`topic: <path> is absent — continuing with the built-in default stated in SKILL.md`

Do not invent a second directory when the resolved path is absent.

## Boundaries

- **Writes ONLY the note file** (and creates it from the template, or from the
  built-in skeleton below when the template is absent). Never a roadmap, memory,
  a deploy, or any other file. Promotion targets get edited by the session itself
  as ordinary work — this skill just enforces that the ledger names a disposition.
- Not a session journal — one file per *thread*, not per sitting.

## File resolution

1. Named or implied project repo with a working tree → `<repo>/.claude/topics/<slug>.md`
   — unless the thread already has a de-facto note there (a planning doc, a change-log
   entry); an existing one wins over creating a parallel one.
2. Otherwise → `<topics_dir>/<slug>.md`, where `<topics_dir>` is this skill's
   configured notes directory (`topics_dir` in `config.json`; run
   `python3 ${CLAUDE_SKILL_DIR}/topics_dir.py` to print the
   resolved path — never hardcode it). When the key is unset, that command
   prints `~/.claude/topics`.
   Path precedence is explicit `--config`, then
   `~/.claude/skill-config/topic.json`, then shipped config, then built-in defaults.
3. Creating: copy `<topics_dir>/TEMPLATE.md` when that file exists, fill the
   header, record the ask in the asker's words. When it does not exist, print
   the one line above and use the built-in skeleton in this file. Run the
   session from the note's cwd (spend attribution follows cwd).

   The same create is `python3 ${CLAUDE_SKILL_DIR}/topics_dir.py --open <slug> --ask <words>`.
   It does not overwrite an existing note file. Absent-read notices are printed
   to stderr; the note path is printed to stdout.

### Built-in skeleton

Used only when `<topics_dir>/TEMPLATE.md` is absent. Not a copy of an external template.

```
# <slug>

Status: OPEN
Last synced: <date> — created

## Ask

<asker's words>

## External refs

## Current state

## Staged

## Log

- <date> — opened

## Promote-or-park

## Close tier

LITE
```

## On open (every touch, not just the first)

1. Read the note file in full.
2. Resolve `<topics_dir>` with `topics_dir.py` (never a hardcoded directory).
   Optionally read these three paths. Each applies only when the file is
   present. When it is absent, print the one line above and continue with the
   built-in default named here — do not raise, and do not go looking elsewhere:
   - `<topics_dir>/README.md` — local convention. Built-in default: the
     paragraph at the top of this file.
   - `<topics_dir>/TEMPLATE.md` — create shape. Built-in default: the skeleton
     in this file.
   - `<topics_dir>/../standards/model-routing.md` — model guidance. Built-in
     default: the Models section below.
   `topics_dir.py --notices` prints the one line for each absent path and
   exits 0.
3. **Reconcile before acting**: check each `External refs` entry against live state —
   tickets move, devices drift, PRs merge. Note drift in the file, THEN work.
4. Stamp `Last synced: <date> — <what was reconciled>`.

## During

- `Current state` is a rewrite-in-place section; the `Log` gets one dated line per touch.
- Anything produced for a later human window — plan drafts, doc skeletons,
  questions-with-defaults — goes under `Staged`. This is the rail that makes fragmented
  attention cheap: the next touch steers a draft instead of cold-starting.

## At close

1. Fill the **promote-or-park ledger**: every emitted work item gets exactly one
   disposition — a line actually added to a tracked surface (a roadmap, backlog or
   issue tracker; `<topics_dir>/README.md` names the local one when present), or an
   explicit dated `PARKED` stamp with the reason. No third state. An item that still
   needs the user's decision stays open and is marked as waiting on them rather than
   parked.
   Every follow-up of an accepted decision (a decision record, a design choice the
   user signed off on) is always promoted in the same session to a durable tracked
   surface, never parked; only an item that still needs the user's own choice stays
   open, marked as waiting on them. The convention document may choose which tracked
   surface receives it, but it cannot turn this into a park.
2. Name the **close tier** in the file:
   - **LITE** — no emitted work, no live-system touches → this file update is the whole
     close.
   - **FULL** — touched a live system or emitted work → your normal end-of-session
     checklist applies on top.
3. Set `Status` (OPEN for a continuing thread, PARKED with date+reason, CLOSED).

## Models

Default, used when `<topics_dir>/../standards/model-routing.md` is absent: use the
session's own model. Never hardcode model names.

When that file is present it is the reference for model choice — read it live,
never hardcode model names here. When it is absent, print the one line above and
continue with the default in this section.

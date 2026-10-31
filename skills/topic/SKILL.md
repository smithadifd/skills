---
name: topic
kind: portable
version: 1.3.0
description: >
  Open, resume, or close a session note for a one-off, attended piece of work
  that will outlive the sitting. Resolves the thread's note file (project repo
  when one exists, else this skill's configured notes directory — `topics_dir`
  in `config.json` — as `<slug>.md`), creates it from that directory's
  TEMPLATE.md on first touch, or from the built-in skeleton in this file when
  the template is absent, opens EVERY touch with a reconcile pass against the
  note's external references and stamps "Last synced", maintains a Staged drafts
  rail for async continuation, and at close runs the promote-or-park ledger and
  names the close tier (LITE vs FULL). With `ledger_cli` set, the thread is a
  ledger item and no note file is written. Use when the user says "/topic <slug>",
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
  With `ledger_cli` set, the ledger item replaces the note file (Ledger route,
  below).
- Not a session journal — one file per *thread*, not per sitting.

## Ledger route

`ledger_cli` names an external ledger CLI that implements the contract below;
this repository does not ship one. It is a key in `config.json`, read
the same way as `topics_dir` (explicit `--config`, then
`~/.claude/skill-config/topic.json`, then the shipped file). Unset, null or empty
means off, and everything else in this file applies as written. A value that is
not a string is an error. When it is set,
a thread is a ledger item of type `topic` with the id `topic-<slug>`, this skill
writes no markdown note, and File resolution, the template and the built-in
skeleton below are not used.

**The contract.** Every call is `<ledger_cli> <verb> ... --json`, and the CLI
prints one JSON envelope: `{"ok": true, "data": ...}` or
`{"ok": false, "error": {"code": ..., "message": ...}}`. The verbs and flags
used:

| Call | Answers |
|---|---|
| `query --type topic --item <id>` | `data.items`, rows with `item_id` and an integer `revision`; error code `empty` or `missing_db` when there is no such item |
| `new --type topic --title <slug> --id <id>` | `data.event.id`; error code `conflict` when the id exists |
| `note <id> --text <text> --expect-revision <n>` | `data.event.id`; error code `stale_revision` when the item's revision is no longer `n` |
| `export-item <id>` | `data.events`, each with `kind` and `payload` (`item.note` events carry `payload.text`) |
| `card <id>` | a readable summary of the item and its notes |
| `link <id> --rel url --target <path>` | attaches a link to the item |
| `state <id> --status closed` | closes the item |

- **Open or resume:** `python3 ${CLAUDE_SKILL_DIR}/ledger_item.py --open <slug> --ask <words>`.
  It prints the item id, `created` or `resumed`, and the executable. It creates
  the item only when none exists, with an `Ask:` note and a `topic-rails` note
  (Rails, below). On a resume it adds whichever of those two an interrupted
  earlier call left out, and never a second copy. Two opens racing on one slug
  both end on one item. A missing or failing executable is one stderr
  line and a nonzero exit: report it and stop. Never fall back to a note file.
- **On resume:** read the item with `<ledger_cli> card <item>`, reconcile as under
  On open, and record the pass as `<ledger_cli> note <item> --text "Synced: <what was reconciled>"`.
- **Rails:** the staged-drafts rail and the promote-or-park ledger are item
  fields held in `topic-rails` notes with a fixed grammar. The note's first line is
  `topic-rails`, then one `key: value` line per rail, and a new item starts with:

  ```
  topic-rails
  staged_drafts: none
  promote_or_park: open
  ```

  `staged_drafts` is `none`, or the staged drafts, each `<draft, or where it lives>`,
  separated by ` ; `. `promote_or_park` is `open`, or entries separated by ` ; `, each
  `<work item> — PROMOTED to <tracked surface>` or `<work item> — PARKED <date>: <reason>`.
  The newest `topic-rails` note is the current state: read it in the card's Notes
  (`<ledger_cli> card <item>`; if the card shows a `(+K cut)` marker on Notes, use
  `<ledger_cli> export-item <item> --json`). To change a rail, append a newer note
  of the same grammar with both lines restated in full, with
  `<ledger_cli> note <item> --text "<the topic-rails block>"`. Never free text, and
  never an edit of an older note.
- **Staged drafts:** each one goes into the `staged_drafts` line of a new
  `topic-rails` note. A long-form write-up is a markdown document the session
  writes where the project keeps its documents, linked with
  `<ledger_cli> link <item> --rel url --target <path>`.
- **Promote-or-park:** at close, each emitted work item gets exactly one entry in
  the `promote_or_park` line of a new `topic-rails` note, `PROMOTED to <tracked
  surface>` or `PARKED <date>: <reason>`. The rules under At close apply
  unchanged. Name the tier with `Close tier: LITE` or `Close tier: FULL`.
  A closed thread gets `<ledger_cli> state <item> --status closed`.

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
   the one line above and use the built-in skeleton in this file.

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
   - `<topics_dir>/../standards/model-notes.md` — optional model notes (an
     existing `model-routing.md` there is read when `model-notes.md` is absent).
     Built-in default: the Models section below.
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

Default, used when `<topics_dir>/../standards/model-notes.md` is absent: use the
session's own model. Never hardcode model names.

When that file is present it is the reference for model choice — read it live,
never hardcode model names here. When it is absent, print the one line above and
continue with the default in this section.

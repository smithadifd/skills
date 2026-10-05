---
name: topic
kind: personal
version: 1.1.0
description: >
  Open, resume, or close the SoT topic file for a bespoke session — the attended,
  off-queue, judgment-led one-offs the 2026-08-31 convention covers
  (`<topics_dir>/README.md` when that file is present). Resolves the thread's
  topic file (project repo when one exists, else this skill's configured topics
  directory — `topics_dir` in `config.json` — as `<slug>.md`), creates it from
  that directory's TEMPLATE.md on first touch, or from the built-in skeleton in
  this file when the template is absent, opens EVERY touch with a reconcile pass
  against the file's external refs and stamps "Last synced", maintains the Staged
  drafts rail for async continuation, and at close runs the promote-or-park
  ledger and names the close tier (LITE vs FULL). Use when the user says
  "/topic <slug>", "open a topic for this", "resume the <x> topic", "this is a
  bespoke session", "track this investigation", "stage this for later", or when
  an attended session starts with an off-roadmap ask that will outlive the sitting.
---

# Topic

Executor for the bespoke-session convention. The convention document is
`<topics_dir>/README.md`. When that file is present, read it live; this skill
applies it and never restates it as a second normative copy. When it is absent,
print one line and continue with the built-in default: bespoke threads get a
topic file as SoT, a promote-or-park ledger at close, and a close tier — no wave
machinery.

The one line, for every absent normative read, is exactly:

`topic: <path> is absent — continuing with the built-in default stated in SKILL.md`

Do not invent a second directory when the resolved path is absent.

## Boundaries

- **Writes ONLY the topic file** (and creates it from the template, or from the
  built-in skeleton below when the template is absent). Never a roadmap, the
  wave SoT, the Desktop mirror, memory, a wrapper, or a deploy. Promotion targets get
  edited by the session itself as ordinary work — this skill just enforces that the
  ledger names a disposition.
- Not for roadmap-backed wave work (that's `orchestrate`/`session-handoff`) and not a
  session journal — one file per *thread*, not per sitting.

## File resolution

1. Named or implied project repo with a working tree → `<repo>/.claude/topics/<slug>.md`
   — unless the thread already has a de-facto SoT there (a planning doc, a change-log
   entry); an existing SoT wins over creating a parallel one.
2. Otherwise → `<topics_dir>/<slug>.md`, where `<topics_dir>` is this skill's
   configured topics directory (`topics_dir` in `config.json`; run
   `python3 ~/.claude/skills/topic/topics_dir.py` to print the
   resolved path — never hardcode it). When the key is unset, that command
   prints `~/.claude/topics`.
   Path precedence is explicit `--config`, then
   `~/.claude/skill-config/topic.json`, then shipped config, then built-in defaults.
3. Creating: copy `<topics_dir>/TEMPLATE.md` when that file exists, fill the
   header, record the ask in the asker's words. When it does not exist, print
   the one line above and use the built-in skeleton in this file. Run the
   session from the SoT's cwd (spend attribution follows cwd).

   The same create is `python3 ~/.claude/skills/topic/topics_dir.py --open <slug> --ask <words>`.
   It does not overwrite an existing topic file. Absent-read notices are printed
   to stderr; the topic path is printed to stdout.

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

1. Read the topic file in full.
2. Resolve `<topics_dir>` with `topics_dir.py` (never a hardcoded directory).
   Optionally read these three paths. Each is normative only when the file is
   present. When it is absent, print the one line above and continue with the
   built-in default named here — do not raise, and do not go looking elsewhere:
   - `<topics_dir>/README.md` — convention. Built-in default: the one-line
     convention in the opening paragraph.
   - `<topics_dir>/TEMPLATE.md` — create shape. Built-in default: the skeleton
     in this file.
   - `<topics_dir>/../standards/model-routing.md` — seat and model ids.
     Built-in default: the Seat section below.
   `topics_dir.py --notices` prints the one line for each absent path and
   exits 0.
3. **Reconcile before acting**: check each `External refs` entry against live state —
   colleagues move tickets, devices drift, PRs merge. Note drift in the file, THEN work.
4. Stamp `Last synced: <date> — <what was reconciled>`.

## During

- `Current state` is a rewrite-in-place section; the `Log` gets one dated line per touch.
- Anything produced for a later human window — plan drafts, doc skeletons,
  questions-with-defaults — goes under `Staged`. This is the rail that makes fragmented
  attention cheap: the next touch steers a draft instead of cold-starting.

## At close

1. Fill the **promote-or-park ledger**: every emitted work item gets exactly one
   disposition — a line actually added to a CHART-visible surface (`roadmaps/`), or an
   explicit dated `PARKED, not for CHART` stamp. No third state.
   Every follow-up of an accepted ADR is dispositioned as a line added to a
   `roadmaps/` file, never `PARKED`; only an item that still needs the user's
   election stays gated, marked `AI-able: no`.
2. Name the **close tier** in the file:
   - **LITE** — no emitted work, no live-system touches → this file update is the whole
     close. No mirror, no banner.
   - **FULL** — touched prod or emitted work → the normal global close ritual applies on
     top (mirror, banner, ledgered NEEDS-YOU).
3. Set `Status` (OPEN for a continuing thread, PARKED with date+reason, CLOSED).

## Seat

Bespoke default, used when `<topics_dir>/../standards/model-routing.md` is absent:
the throughput tier with the standard four-trigger promotion read. Never hardcode
model names.

When that file is present it is normative for seat and model ids — read it live,
never hardcode model names here. When it is absent, print the one line above and
continue with the default in this section.

## Work-harness note

The template ports to the future work repo with surfaces swapped (JIRA / work Notion /
Confluence — translation notes are inline in TEMPLATE.md). Andrew owns a marry-up pass
against his team's project-context conventions before first work use; until then this
skill is home-side only.

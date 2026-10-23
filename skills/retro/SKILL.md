---
name: retro
kind: portable
version: 1.0.0
description: >
  Read what a session did and propose changes to the environment the next
  session runs in (hooks, checks, instruction files, tools, docs), never to the
  person. Sorts findings by severity, types each one (encode in structure, a
  skill or rule edit as a diff you approve, a short repo learning, or nothing
  met the bar), and writes them to one findings file. Use when the user says
  "/retro", "run a retro", "what should we change after this session", "retro
  this PR", or "what went wrong in that session and how do we stop it". NOT for
  a blame review of the person, NOT for saving memory, and NOT a code review
  (that is `review`).
disable-model-invocation: true
---

# Retro

Looks back at one session and asks what about the environment made the work
harder or let a mistake through. The subject is the setup, not the person. It
proposes; the person decides. It never writes memory and never edits any file
except the findings file without an explicit approval turn.

Commands below run from this skill's directory (`retro.py`, standard library
only; Python 3 required).

## Inputs

Use what the person gives, in this order:

1. A transcript path, PR URL or commit range named in the request.
2. The current session's transcript, when the tooling exposes it.
3. `git log` since the session's first commit, and the review threads when a PR
   is named (`gh pr view --comments`, `gh api` for review comments).

If there is no transcript, work from the commits and threads and say so. A
transcript is a JSONL file of messages whose content blocks are text,
`tool_use` and `tool_result`.

## Steps

1. **Triage.** With a transcript, run `python3 retro.py triage <transcript>`.
   Exit code 3 means a trivial session: print its one-line message and stop.
   Without a transcript, skip if the range has fewer than two commits and no
   review thread, and say so.
2. **Scan.** Run `python3 retro.py scan <transcript>`. It lists the person's
   correction turns and the tool-and-identifier classes (a lint code or test id)
   that failed in at least two runs. These are leads for the reviewers, not
   findings. A run counts as failed only when its result is an error that
   starts with a non-zero `Exit code`; summary text is never read, so a failure
   hidden by a pipe (`pytest | tail`) is missed rather than guessed. Details:
   `references/failure-detection.md`.
3. **Review in parallel.** Start three read-only subagents in one message. Each
   reads the same inputs plus the scan output, applies all seven lenses in
   `references/lenses.md`, and returns candidate findings as JSON objects (shape
   below). They may read files and run read-only commands; they edit nothing.
   - Process: how the work was ordered, what was redone, where the person had to
     steer.
   - Tooling and environment: what a hook, check, tool or instruction file
     could have caught or made cheaper.
   - Divergent: what a different approach would have avoided entirely; also
     rules that are no-ops or whose mistake can no longer happen.
4. **Synthesize** on the main thread. Merge duplicates, then apply the bar:
   - A mistake class counts only when seen at least twice. Cite both instances
     (`ref` plus a short `excerpt`). A rule is never proposed from one slip.
   - Fix at the highest level that works. If the mistake is mechanical (a tool
     can detect it), mark it `mechanical: true`; it becomes a hook, lint, test
     or CI check, not a rule.
   - A judgment mistake becomes one diff against a skill or instruction file.
   - A rule whose mistake can no longer happen (a check now catches it) is
     proposed for removal: `obsolete_rule: true` with the removal diff.
   - A repo learning is a short note under the repository's docs folder, and
     only when nothing in code, tests or docs already says it. Set
     `already_documented` after grepping.
   - Rate each finding `high`, `medium` or `low` by how much rework it caused.
5. **Write the findings file.** Save the candidates as `draft.json` in a scratch
   directory, then run `python3 retro.py build draft.json --out <findings.md>`.
   It types each finding, drops what is below the bar, sorts by severity and
   refuses an invalid file. Run `python3 retro.py check <findings.md>`. If
   nothing met the bar, the file says exactly that; this is a valid result.
6. **Reply in at most five lines:** the findings path, the count per type, the
   top finding, and whether anything awaits approval.

## Candidate shape

```json
{"findings": [{
  "id": "short-slug", "title": "...", "severity": "high|medium|low",
  "mechanical": true, "structure": "hook|lint|test|ci", "proposal": "...",
  "instances": [{"ref": "line 15", "excerpt": "..."}, {"ref": "line 31", "excerpt": "..."}],
  "evidence": ["why this is a real, repeated class"],
  "diff": "unified diff, judgment findings only",
  "learning": "note text, repo learnings only", "path": "docs/<file>.md",
  "already_documented": false, "obsolete_rule": false
}]}
```

## Finding types

| Type | When | Carries |
| --- | --- | --- |
| encode-in-structure | The mistake is mechanical. Preferred whenever it applies. | `structure`, `proposal` |
| skill-or-rule-edit | Judgment mistake seen twice, or a rule to remove. | `diff`, `approved: false` |
| repo-learning | A fact missing from code, tests and docs. | `path` under `docs/`, `note` |
| nothing-met-the-bar | No class reached two instances. | the only result |

## Approval

Show each edit finding's diff and ask the person to approve or reject it. On
approval run `python3 retro.py approve <findings.md> <id>`, which records the
decision in the findings file, and only then apply that diff. Apply nothing
that was not approved, and do not batch approvals the person did not give.
Structure findings and repo learnings are also proposals: implement them only
when the person asks.

## Do not

- Judge or rank the person. Findings describe tools, files and checks.
- Write memory, or edit any file other than the findings file, unasked.
- Propose a rule from a single occurrence, or restate what a check already
  enforces.
- Pad the list. An empty result is the right answer for a clean session.

Lenses and the method this follows are in `references/lenses.md` and
`references/prior-art.md`.

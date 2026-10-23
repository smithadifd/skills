---
name: handoff
kind: portable
version: 1.0.0
description: >
  Write one resume note so a fresh session can continue this work. Use when the
  person says "hand off this session", "write a handoff", or "save where we are
  so I can resume". The note has fixed sections (goal, where we stopped,
  verified command and result pairs, claimed but not verified, decisions,
  failed approaches, open questions, references, skills to invoke next,
  secrets), is redacted by pattern, capped at 20 KB, and written to the
  temporary directory, not the repository. NOT for a ledger entry, ticket
  resume card, roadmap update or memory note: this skill reads and writes none
  of those. To continue from a note, use /pickup.
disable-model-invocation: true
---

# /handoff

Write one resume note for a session that is about to end. The note is for a
reader with no memory of this conversation, so every line must stand alone.

## Steps

1. **Collect the content from this conversation.** Fill each section below.
   Write `none` for a section with nothing to say; never leave one out.
   - **Goal**: what the person set out to do, in one or two sentences.
   - **Where we stopped**: the last finished step and the next concrete one.
   - **Verified**: one line per check you actually ran in this session, as a
     command and its result: ``- `command` => result``. The command must be
     runnable from the repository root and exit 0 when the claim still holds,
     because `/pickup` reruns it. Do not list a check you did not run.
   - **Claimed but not verified**: things believed true that were not checked
     here (a teammate's statement, a CI result you did not read, a guess).
   - **Decisions and why**: each choice with its reason.
   - **Failed approaches**: what was tried and why it did not work.
   - **Open questions**: what still needs an answer, and from whom.
   - **References**: paths, PR URLs and commit ids only. Never paste file
     contents, diffs or logs.
   - **Skills to invoke next**: skills the next session should load, by name.
   - **Secrets**: written by the script as `none, redacted`. Never put a
     secret in any section.
2. **Write the content as JSON** to a scratch file in the temporary directory:
   an object mapping each section name to a string, or to a list of strings
   (one bullet each).
3. **Run the script:**
   `python3 ${CLAUDE_SKILL_DIR}/handoff.py --repo <repo-name> --slug <short-topic> --input <content.json>`
   It prints the note's path and a line `truncated: yes` or `truncated: no`.
   The default is `handoff-<repo>-<slug>.md` (a repo or slug that looks like
   a credential becomes `redacted-<hex>`) in a private per-user directory
   (mode 0700) under the temporary directory; the file is mode 0600. Pass
   `--out <path>` only when the person names a path; never write the note
   inside the repository on your own.
4. **Report** the path, the truncated line, and the command to resume:
   `/pickup <path>`.

## What the script does

- Redacts secret-shaped strings by pattern (cloud access keys, GitHub tokens,
  Slack and `sk-` keys, bearer and `Authorization` headers, private-key
  blocks, `NAME=value` and `NAME: value` for key, token, secret and password
  names, and `--token`-style flags). Pattern matching is a net, not a
  guarantee, so still keep secrets out of the content.
- Caps the note at 20 KB. Over the cap it shortens the largest section bodies,
  keeps every heading, and appends a visible truncation notice.
- Never makes a commit.

## Commits

Make a `wip:` commit of the working tree only when the person asks for one.
Stage named paths, commit with a `wip:` subject, and put the commit id in
References. Do not push.

## Boundaries

This skill does not read or write a ledger, a roadmap or memory. It is the
path for session continuity when none of those is in use.

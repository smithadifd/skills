---
name: document
kind: portable
version: 1.0.0
description: >
  Write accurate documentation pages for a module, service or whole app by
  running the `doc-researcher`, `doc-writer` and `doc-verifier` agents, and
  write only under the target repository's docs folder. Use when the user says
  "document this module", "write the README for", "docs for this service",
  "document the whole app" or "refresh the docs after this change". NOT for
  code comments or docstrings (edit the code directly), NOT for commit messages
  or pull request descriptions (that is `pr`), and NOT for design notes or
  decision records the user is drafting themselves.
---

Produce documentation that matches the code. The skill coordinates three
agents so the heavy reading and drafting stay out of the main conversation:

| Agent | Role |
| --- | --- |
| `doc-researcher` | Surveys the target and returns a tight brief. |
| `doc-writer` | Drafts one page from the brief and returns the full page text and a claims list. Under this skill it never writes. |
| `doc-verifier` | Checks each claim against the code and returns confirmed and flagged lists. |

## 0. Check the agents are installed

Installing the skill does not install its agents. If any of `doc-researcher`, `doc-writer` or `doc-verifier` is not among the available agents, stop before doing anything else and print this, run from the clone of the skills repository (the plugin install includes the agents):

```sh
./install.sh --agent doc-researcher
./install.sh --agent doc-writer
./install.sh --agent doc-verifier
```

## 1. Find where files may be written

Writes go only under one folder, the output root. Settle it before anything else:

1. Use the repository's existing `docs/` folder.
2. If the person names a different folder, use it only if it is a docs folder (for example `documentation/` or `doc/`), not a source, test or config folder. Otherwise refuse, say why, and offer `docs/`.
3. If there is no docs folder, propose `docs/` and wait for a yes before creating it.
4. Edit a `README.md` only when the person asked for the README. It is the one file allowed outside the output root: pass the repository root as `--root` and `<repo>/README.md` as `--dest`.

Every writer is handed an explicit output path under the output root (or the README path when asked), as the page's intended destination. Writers never write: every `doc-writer` prompt, first draft or fix, says "return-only" so it returns its envelope and does not call Write or Edit. The envelope is the `## Page text` section (the whole page in one fenced `markdown` block), then `## Claims (for verification)`, then `## Summary`; the parent takes the page text and the claims from it.

**The parent writes only through the guard script**, once per page, after verification ends:

```sh
python3 "${CLAUDE_SKILL_DIR}/write_page.py" --repo <repo root> --root <output root> --dest <page path> --mode create|update
```

with the page text on standard input. The script refuses, with one line on stderr and nothing written, on a symlink anywhere below the repository root on the way to the page, a root outside the repository, a destination outside the root, a `..` component, create mode over an existing page (stop and ask the person; never overwrite silently), update mode on a missing page, or any destination directly under the repository root other than `README.md`. It opens the page relative to its directory without following links, so there is no gap between the check and the write. Exit 1 with a message that says "written, but" means the page is already in place and a later step failed; do not retry in create mode (it would be refused as existing), and report the message instead. Never write, edit or delete any other file: no source files, no config, no files outside the repository. If a page would belong elsewhere, refuse that path, say so in the report and leave it unwritten.

## 2. Read the request

Decide these and state them back in one line before starting, so the person can correct course cheaply: "Writing docs for X, audience Y, create mode, output root Z."

- **Scope.** One module, file or README is single-page. A repository or major subsystem is app-wide. When unsure, choose single-page; it is cheaper to widen later.
- **Audience.** End user, developer, contributor or operator. Default to a mix of end-user introduction and developer setup for a README, and developer for a module page.
- **Mode.** Update if the target page already exists or the person says "refresh" or "update"; otherwise create.
- **Format.** Plain markdown pages. If the repository already has a docs-site config (for example `mkdocs.yml` or a Docusaurus or Starlight config), match its page format and navigation instead of inventing one.

## 3. Single page

Use one of each agent, with no outline gate. Nothing touches disk until step 4.

1. Spawn `doc-researcher` with the target and audience. If its coverage notes say the target is too large or too vague, ask the person to narrow it and stop.
2. Spawn one `doc-writer` in return-only mode with the brief, page scope, audience, intended output path and mode. It returns the envelope.
3. Spawn `doc-verifier` with the envelope's complete claims list. For flagged claims, send them with their reasons back to the writer (return-only) for a revised envelope, then verify that revised envelope's complete claims list. Make at most two fix passes. If claims are still flagged at the cap, report them and write nothing unless the person says to write the page anyway.
4. Run the guard script once with the final accepted page text.
5. Report the path written, what it covers, the verification counts and anything skipped.

## 4. App-wide

1. **Research.** Spawn `doc-researcher` with the target and audience. If its coverage notes say the target is too large or too vague, ask the person to narrow it and stop.
2. **Plan and gate.** From the brief's suggested outline, list each page with its path, one-line purpose and audience, plus the output root. Show the list and wait for approval. This is the only gate; after it, work without further questions. Apply requested changes to the list and show it again if they were large.
3. **Draft.** Spawn one return-only `doc-writer` per approved page, giving it the relevant slice of the brief, the page scope, audience, intended output path and mode. Run writers in parallel for pages that do not depend on each other. Draft a page first if later pages link back to it. If a writer reports gaps, either go back to the researcher or record the gap in the final report.
4. **Verify.** Spawn `doc-verifier` with every envelope's complete claims list. For flagged claims, send the writer of that page the claims and reasons in one batch (return-only) for a revised envelope, then verify that envelope's complete claims list. Make at most two fix passes. For any page still flagged at the cap, report the claims and do not write that page unless the person says to.
5. **Write.** Run the guard script once per accepted page, using the final text. Nothing touches disk before verification ends.
6. **Report.**

```
Wrote N pages under <output root>:
- <path>: <title>
Verification: <confirmed> confirmed, <flagged> flagged and fixed, <skipped> skipped as subjective
Gaps: <pages that shipped with known gaps, or none>
```

For updates, run the researcher first, since the code may have moved. Writers read the existing page and return the full revised text with only the stale sections changed, so untouched content stays as it was. Finish with a short summary of what changed and what was preserved.

## Writing rules

- Lead with the point. No warm-up paragraph.
- Use plain verbs: "is", "has", "returns".
- Name the real function, flag, path and endpoint. No "various utilities".
- Vary sentence length. End when the content ends, with no summary paragraph.
- Sentence-case headings, unless the existing docs use title case.
- No emoji, no curly quotes, no chatbot phrases such as "I hope this helps".
- Do not attribute claims to unnamed experts.
- Match the repository's existing docs for voice, heading style and layout.

## Rules

- Never invent. A page may only state what the brief or the code supports; leave a gap and report it instead.
- Keep the main conversation lean on app-wide work. Reading the code is the researcher's job.
- Gate app-wide work on an approved outline before any page is drafted.
- Updates keep every section the change does not touch.
- The output root and the requested README are the only places this skill writes, and only through the guard script.

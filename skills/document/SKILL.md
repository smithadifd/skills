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
| `doc-researcher` | Surveys the target and returns a brief of about 500 words. |
| `doc-writer` | Drafts one page from the brief, writes it to disk, returns a summary and a claims list. |
| `doc-verifier` | Checks each claim against the code and returns confirmed and flagged lists. |

## 1. Find where files may be written

Writes go only under one folder, the output root. Settle it before anything else:

1. Use the repository's existing `docs/` folder.
2. If the person names a different folder, use it only if it is a docs folder (for example `documentation/` or `doc/`), not a source, test or config folder. Otherwise refuse, say why, and offer `docs/`.
3. If there is no docs folder, propose `docs/` and wait for a yes before creating it.
4. Edit a `README.md` only when the person asked for the README. The README is then the one file allowed outside the output root, and nothing else is.

Every writer is handed an explicit output path under the output root (or the README path when asked), and writes nowhere else. Never write, edit or delete anything else: no source files, no config, no files outside the repository. If a page would belong elsewhere, refuse that path, say so in the report and leave it unwritten.

**Path check.** A path that merely starts with `docs/` is not enough, because a symlink can redirect a write elsewhere. Before spawning each writer and before each write, run this check and refuse on any failure:

1. No component of the output root, the destination directory or the destination file (including any existing page or README) is a symlink.
2. The canonical path (for example from `realpath`) of the output root is inside the canonical repository root.
3. The canonical destination is inside the canonical output root. Check the nearest existing parent when the file does not exist yet.
4. The root `README.md`, when the person asked for it, gets the same checks against the canonical repository root.

## 2. Read the request

Decide these and state them back in one line before starting, so the person can correct course cheaply: "Writing docs for X, audience Y, create mode, output root Z."

- **Scope.** One module, file or README is single-page. A repository or major subsystem is app-wide. When unsure, choose single-page; it is cheaper to widen later.
- **Audience.** End user, developer, contributor or operator. Default to a mix of end-user introduction and developer setup for a README, and developer for a module page.
- **Mode.** Update if the target page already exists or the person says "refresh" or "update"; otherwise create. Never overwrite an existing page in create mode.
- **Format.** Plain markdown pages. If the repository already has a docs-site config (for example `mkdocs.yml` or a Docusaurus or Starlight config), match its page format and navigation instead of inventing one.

## 3. Single page

Use one of each agent, with no outline gate.

1. Spawn `doc-researcher` with the target and audience. If its coverage notes say the target is too large or too vague, ask the person to narrow it and stop.
2. Run the path check, then spawn one `doc-writer` with the brief, page scope, audience, output path and mode.
3. Spawn `doc-verifier` with the writer's claims list. Send flagged claims back to the writer to fix in place, then verify them again. Make at most two fix passes; report any claim still flagged after that.
4. Report the path written, what it covers, the verification counts and anything skipped.

## 4. App-wide

1. **Research.** Spawn `doc-researcher` with the target and audience. If its coverage notes say the target is too large or too vague, ask the person to narrow it and stop.
2. **Plan and gate.** From the brief's suggested outline, list each page with its path, one-line purpose and audience, plus the output root. Show the list and wait for approval. This is the only gate; after it, write without further questions. Apply requested changes to the list and show it again if they were large.
3. **Write.** Run the path check for each page, then spawn one `doc-writer` per approved page, giving it the relevant slice of the brief, the page scope, audience, output path and mode. Run writers in parallel for pages that do not depend on each other. Write a page first if later pages link back to it. If a writer reports gaps, either go back to the researcher or record the gap in the final report.
4. **Verify.** Gather every writer's claims list and spawn `doc-verifier` with it. For flagged claims, send the writer of that page the claims and reasons in one batch to fix in place, then run `doc-verifier` again on those claims. Make at most two fix passes; list any claim still flagged in the report.
5. **Report.**

```
Wrote N pages under <output root>:
- <path>: <title>
Verification: <confirmed> confirmed, <flagged> flagged and fixed, <skipped> skipped as subjective
Gaps: <pages that shipped with known gaps, or none>
```

For updates, run the researcher first, since the code may have moved. Writers change only the stale sections and use Edit, so untouched content stays as it was. Finish with a short summary of what changed and what was preserved.

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
- Gate app-wide work on an approved outline before any page is written.
- Updates keep every section the change does not touch.
- The output root and the requested README are the only places this skill writes.

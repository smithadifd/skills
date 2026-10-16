---
name: review
kind: portable
description: >
  Review the current changes for bugs and security issues only.
---

Review the current changes for bugs and security issues only.

Run `git diff` (or `git diff main...HEAD` for a full branch review) to see the changes, then analyze them.

Focus areas (in priority order):
1. **Bugs**: Logic errors, off-by-one errors, missing error handling for failure paths, incorrect type assumptions, race conditions
2. **Security**: Injection vulnerabilities (SQL, command, XSS), auth/authz gaps, secrets in code, unvalidated user input reaching sensitive operations, insecure defaults

Output format: one finding is one bullet, all of it on that one line — the
tag, then `file:line`, then the description and the fix. Do not split a
finding across several bullets.
- Prefix with `[BUG]` or `[SECURITY]`, then the `file:line` the finding is
  about, then one sentence description and one sentence suggested fix, e.g.
  `- [BUG] skills/example/tool.py:41 — the retry loop drops the last item on
  a short read. Add a length check before the final iteration.`
- If nothing found: "No bugs or security issues found."
- Any verdict that relies on a measured interval carries an `INTERVAL:` line: `INTERVAL: from <start event>, <start source> to <end event>, <end source> = <duration>` (every part non-empty).

Phase artifacts (optional, when a caller wants the structured result): Python 3
is needed. Write the review to a file and run `python3 ~/.claude/skills/review/review_phase.py --from-text <file>
--backend claude-fresh`. It prints a `phase_result: v1`
whose `artifacts["findings.json"]` validates against `skills/review/findings.schema.json`
(`--validate <findings.json>` re-checks one); the one-line bullet shape above,
tag and `file:line` exact, is what its `claude-fresh` adapter parses — the
worked example above is the shape it expects, verbatim.

A review run through `omp -p --model <id> --thinking=<level>` with the
reviewer agent uses `--backend omp --model <id> --thinking <level>` (in an
envelope: `inputs.model` and `inputs.thinking`). The reply must carry the
agent's verdict line, exactly `## Verdict: APPROVE` or
`## Verdict: REQUEST CHANGES`, or the result is a failure, even with
`adapters.jev_fallback` on. Both values are required and come only from the
caller: the model as a single token, the thinking level as one of `off`,
`minimal`, `low`, `medium`, `high`, `xhigh`, `max` or `auto`. The result
records them, with the lane name, as `attribution`.

`REVIEW_PHASE_JEV_SHADOW=1` enables an advisory TypeSafe pass on parse-success
returns. It sends only `jev_client.trim_payload`'s FINDINGS/VERDICT payload and
records `jev_verdict`, `confidence`, `agrees_with_regex`, and `jev_reason` on
the phase result. `jev_reason` is a secret-free failure classification when
Jev is unavailable and null otherwise; the regex verdict and verbatim
`review.md` remain authoritative. Absent/unset is the exact base path.

Do NOT comment on:
- Style, formatting, naming conventions
- Performance optimizations (unless severe)
- Code organization or refactoring opportunities
- Missing tests (unless the absence of tests is itself the bug)
- Things that are subjectively better but not objectively wrong

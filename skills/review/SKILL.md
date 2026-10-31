---
name: review
kind: portable
version: 1.0.0
description: >
  Review the current changes for bugs and security issues only. Use when the
  user says "review my changes", "review this diff", "check this branch for
  bugs", or "any security problems here". NOT for style, naming or refactoring
  feedback, and NOT for pressure-testing a design before code exists (that is
  `shape`).
---

Review the current changes for bugs and security issues only.

Run `git diff` (or `git diff <default-branch>...HEAD` for a full branch review, where the default branch is the one `git symbolic-ref --short refs/remotes/origin/HEAD` names) to see the changes, then analyze them.

Focus areas (in priority order):
1. **Bugs**: Logic errors, off-by-one errors, missing error handling for failure paths, incorrect type assumptions, race conditions
2. **Security**: Injection vulnerabilities (SQL, command, XSS), auth/authz gaps, secrets in code, unvalidated user input reaching sensitive operations, insecure defaults

Output format: one finding is one bullet, all of it on that one line — the
tag, then `file:line`, then the description and the fix. Do not split a
finding across several bullets.
- Prefix with `[BUG]` or `[SECURITY]`, then the `file:line` the finding is
  about, then one sentence description and one sentence suggested fix, e.g.
  `- [BUG] skills/example/tool.py:41 — the retry loop drops the last item on
  a short read. Add a length check before the final iteration. [reach:
  <lane|concurrent|user|owner> (<one-clause reason>); breaks:
  <gate|data|avail|docs|evidence>; evidence: <probed|quoted|read>]`
- If nothing found: "No bugs or security issues found."
- End the review with an `INTERVAL:` line, the last line of the reply:
  `INTERVAL: none`, or one measured record per interval the verdict relies on
  (`INTERVAL: from <start event>, <start source> to <end event>, <end source> = <duration>`,
  every part non-empty, the duration a number then `ms`, `s`, `m`, `h` or `d`,
  like `41m`). Only that line is read for time; durations elsewhere are never
  read.

Phase artifacts (optional, when a caller wants the structured result): Python 3
is needed. Write the review to a file and run `python3 ${CLAUDE_SKILL_DIR}/review_phase.py --from-text <file>
--backend claude-fresh`. It prints a `phase_result: v1`
whose `artifacts["findings.json"]` validates against `skills/review/findings.schema.json`
(`--validate <findings.json>` re-checks one); the one-line bullet shape above,
tag and `file:line` exact, is what its `claude-fresh` adapter parses — the
worked example above is the shape it expects, verbatim. The closing
`INTERVAL:` line is required there too: a review without it fails with
`review_interval_missing`. The grammar and every refusal are in
`references/structured-interval.md`. A return that states
two different verdicts, in either order, is a failure for the `claude-fresh`
and `grok-notools` backends.

For a review run through `omp -p --model <id> --thinking=<level>`, append this
output contract to the reviewer prompt. Parse the reply with `--backend omp`,
`--model <id>`, and `--thinking <level>`; in an envelope, use `inputs.model`
and `inputs.thinking`.

````text
Return exactly one fenced `review-result` JSON block. `verdict` is exactly
`APPROVE` or `REQUEST CHANGES`. `bugs` and `nits` contain one
`path:line — description` string per finding. `reviewed_head` is the full
lowercase 40-character commit hash you reviewed. After the block, end the
reply with one `INTERVAL:` line, `INTERVAL: none` unless the verdict relies
on a measured interval.

The block is the only verdict. Outside it, do not write the word `verdict`;
the words `approve` or `request changes` in any case (`APPROVE`,
`REQUEST CHANGES`); raw HTML (`<name>`, `</name>`, `<!--`, or a line
starting with `<`); Markdown links or images; HTML character references; or
a non-ASCII letter inside an otherwise-ASCII word. Write generics as
`Vec[T]` or in words. Raw HTML, a link or image, a character reference, or a
word mixing ASCII and non-ASCII letters is refused as `ReviewProseUnsafe`;
an `approve` or `request changes` that differs from the block, or a
`verdict` with no value, is refused as `VerdictConflict`. The
block itself is refused as `ReviewRecordMissing`, `ReviewRecordMultiple`,
`ReviewRecordUnterminated`, `ReviewRecordInvalidJSON` or
`ReviewRecordSchema` when it is absent, repeated, unclosed, not JSON, or off
this schema.

```review-result
{
  "verdict": "APPROVE",
  "bugs": [],
  "nits": [],
  "reviewed_head": "0123456789abcdef0123456789abcdef01234567"
}
```
````

The block is the OMP verdict's only source: zero or multiple blocks, invalid
JSON, unknown or incorrectly typed fields, malformed `reviewed_head`, and an
`APPROVE` record with non-empty `bugs` fail closed as record errors; a clean
sentence never substitutes for a missing or invalid record. Outside the record
fence, prose is read as written and nothing is deleted from it. Category
Cf/Mn/Me/Zl/Zp/Co/Cn/Cs characters, bidi controls, Hangul or Braille fillers,
and HTML character references fail as `ReviewProseUnsafe`, and so do raw HTML
(a tag, a `<!` or `<?` start, or a line that starts an HTML block) and
Markdown links and images, which are refused rather than stripped. Prose is
then folded (NFKC, then a Latin fold read from each letter's Unicode name); a
word mixing ASCII and non-ASCII letters fails as `ReviewProseUnsafe`, so a
lookalike key is refused rather than matched. Each paragraph (ended by the
record, a blank line or a fence line) is read for value tokens: the letters of
`approve` or `request changes` in any case, split only by non-letters. Every
value token must agree with the record, or the reply fails as
`VerdictConflict`. The key is the ASCII word `verdict` (letters may be split
by non-letters, camel-case prefixes such as `FinalVerdict` count); a line
holding a key must also hold a value token, or the reply fails as
`VerdictConflict`.
A key-only line takes the next non-blank line, or the record after it, as its
value.
The model and thinking level are required caller inputs. The level is one of `off`,
`minimal`, `low`, `medium`, `high`, `xhigh`, `max` or `auto`. The result
records both, with the lane name, as `attribution`.

Do NOT comment on:
- Style, formatting, naming conventions
- Performance optimizations (unless severe)
- Code organization or refactoring opportunities
- Missing tests (unless the absence of tests is itself the bug)
- Things that are subjectively better but not objectively wrong

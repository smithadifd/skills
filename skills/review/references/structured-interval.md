# The INTERVAL record: one structured line instead of a prose scan

DESIGN: every review return ends with an INTERVAL record. Its last non-blank line is `INTERVAL: none`, or one `INTERVAL: from <start event>, <start source> to <end event>, <end source> = <duration>` line per measured interval the verdict relies on. The review phase validates only that record and refuses a return by name when the record is missing, malformed, missing an endpoint, contradictory or duplicated (`review_interval_missing`, `review_interval_malformed`, `review_interval_endpoints_missing`, `review_interval_contradictory`, `review_interval_duplicate`). Nothing else in the return is read for time.

Why: the phase used to scan the whole return for anything shaped like a duration and demand an INTERVAL line whenever it found one. Every fix to that scan found a new way to misread prose. A short commit sha (`919960d`) read as 919960 days. A reviewer quoting the probe inputs it ran (`took 2d`, `41m`) was refused for its own evidence. Exemptions for code spans, fences, Evidence sections, section numbers, file references and configured timeouts each needed their own parser, and each of those parsers disagreed with the adapters' parsing somewhere (fence pairing, indented continuations, a clean sentinel in a fence info string). A line the reviewer writes on purpose, read in one fixed place, needs none of that.

## 1. The record

**Form: a required `INTERVAL:` line, the last non-blank line of the return.** Not a JSON field: the only JSON in the pipeline (`findings.json`, `review-findings/v1`) is produced by the adapter from the reviewer's text, never by the reviewer, so a JSON field would still be derived from a line of text; that schema is closed (`additionalProperties: false`) and versioned, so a new key would mean v2 for every consumer; and a no-tools reviewer returns markdown only. A line the reviewer writes is the one thing every review surface can emit.

Grammar (one line; `INTERVAL` and `none` are case-insensitive; leading spaces or tabs allowed; nothing else on the line):

```
INTERVAL: none
INTERVAL: from <start event>, <start source> to <end event>, <end source> = <duration>
```

- `none`: the verdict relies on no measured time interval. It is written whether or not the review mentions a duration anywhere.
- The measured form names both physical endpoints. Every part is non-empty after trimming. `<start event>` and `<end event>` say what happened; `<start source>` and `<end source>` say where each timestamp was read (`events.tsv:14`, `result.md mtime`, a log line). The parts are free text on one line. The first `,` ends the start event, the first `to` followed by whitespace after it ends the start source, the next `,` ends the end event, and the last `=` ends the end source, so an end source may itself hold `=` (`worker log status=done = 41m`).
- `<duration>` is one token, a number then a unit: `[0-9]+(\.[0-9]+)?(ms|s|m|h|d)` (`41m`, `2.5h`, `500ms`). Spelled-out or spaced units (`41 minutes`, `41 m`) are malformed.
- A verdict that relies on more than one interval writes one measured line per interval, consecutive, all at the end. The record block is the longest run of `INTERVAL:` lines that ends at the last non-blank line. A blank line ends the run: an `INTERVAL:` line above a blank line is not part of the record.

Position: the last non-blank line of the return the adapter is given. That place cannot be confused with quoted input: the phase reads exactly one place, found from the end of the text, and never examines any other line for the word INTERVAL or for a duration. An example `INTERVAL:` line quoted in a fence or a code span, and a duration in Evidence, Issues or the summary, are never read, so neither can be mistaken for a record. The record is never inside a fence: a fence's closing line is a run of backticks or tildes, so a return that ends in a fence has no record and is refused by name, never misread. A bulleted `- INTERVAL: none` or an emphasised `**INTERVAL: none**` is not an INTERVAL line (the line must start with the label) and is refused as missing.

The verdict stays where each return shape puts it (the `## Verdict:` heading of the reviewer agent's block, a `VERDICT:` line, the value line under a `## VERDICT` heading), and the record follows the whole verdict block, so the two never compete for the same line. "Last non-blank line" is one rule for every return shape, where "the line after the verdict line" would be one rule per shape plus another for the tagged-bullets shape that has no verdict line at all.

The adapters parse the return without its trailing record block. Without that, a return whose last section is Issues or FINDINGS would read the record as prose under that section and fail the findings parse. Only the trailing record is removed; an `INTERVAL:` line anywhere else stays in the adapter's input and is read like any other line (under Issues it is foreign prose and refuses the section; above a `## VERDICT` value line it takes the value line's place and the verdict parse fails).

## 2. Validation

`run_phase`, after the selected adapter has parsed the verdict and findings, checks only the record block. It performs no scan of prose, Issues, Evidence, headings or verdict lines for duration-shaped tokens, and it never asks whether the verdict really relies on an interval: the reviewer's declaration is the input, as every other claim in a verdict is.

| Condition | Refusal (`reason` = `<backend>: <name>: line <n>: <line, cut at 200 characters>`) |
|---|---|
| The last non-blank line is not an `INTERVAL:` line (no record, or a record with other text after it) | `review_interval_missing`, naming the last non-blank line |
| An `INTERVAL:` line in the block is neither `none` nor `from` plus whitespace | `review_interval_malformed` |
| A measured line has an empty part, or a `from` line the grammar cannot split | `review_interval_endpoints_missing`, with ` (missing <parts>)` after the line; a line the grammar cannot split names all five parts |
| A measured line's duration fails the unit grammar | `review_interval_malformed`, with ` (duration is not a number then ms, s, m, h or d)` after the line |
| `none` beside a measured line, or `none` twice | `review_interval_contradictory`, naming the second of the two |
| Two measured lines identical after trimming | `review_interval_duplicate`, naming the second |

Every refusal is `status: failure`, exit `1`, `artifacts["review.md"]` verbatim and `findings.json` null, exactly as an adapter parse error is; the review is retried by running it again. The first failing line wins, in text order; within one measured line the form is checked (malformed, endpoints, duration) before its relation to the lines above it (contradictory, duplicate). On success the phase result always carries `intervals`: `[]` for `none`, otherwise the parsed records in order. The Jev fallback and Jev shadow paths run the same check on the same text; nothing is quoted or exempted for them either, and the shadow runs only after the check passes.

Checks that are deliberately absent: no cross-check that the review text mentions the duration; no check that a measured record is used; no exemption for timeouts, bounds, shas, paths, section numbers, emphasis, quotes, code spans or fences, since none of those lines is read.

A reviewer can still rely on a measured time and write `INTERVAL: none`. No scan could catch that either: a reviewer who wanted to hide a time could always paraphrase it. It is handled as every other false statement in a verdict is: the record is the reviewer's attestation, an independent second review of the same change can disagree, and a caller that pins the result file's digest can audit a false `none` afterwards. The design is fail-closed on form, not on truth.

## 3. Emitters

The instruction lives in the installed contracts, never in a per-review brief: a caller that steers a gate reviewer through its brief is shaping the verdict it is about to gate on.

- The `reviewer` agent (`agents/reviewer.md`): the Output Format block ends with `INTERVAL: none`, and the rules under it say the last non-blank line of the block is an `INTERVAL:` line, give both forms and the duration grammar, and say that durations may appear anywhere else in the reply because only this line is read. The old rule "leave durations out of the whole reply" is gone.
- The `review` skill (`skills/review/SKILL.md`): its output format ends the review with an `INTERVAL:` line in the same two forms, and its `--from-text` paragraph says the line is required there too.
- A caller outside this repository that composes its own review prompt (a no-tools reviewer form, a brief composer, a contract appended to the prompt) has to ask for the same last line in its own words. Those changes belong to that caller's repository and land after this one, as described in section 4.

## 4. Migration

- This change lands the parser and every emitter text in this repository together, so a user installing this repository at any commit has a `reviewer` agent and a `review` skill that write the line the parser requires. Splitting them, parser first and prompts later, would refuse every otherwise valid review for anyone who installed between the two.
- Stored results: a caller's receipt of an earlier review carries a verdict, a result digest and the like, not an INTERVAL record, and is never re-parsed. Old receipts and old verdict outputs stay valid; nothing is rewritten.
- A review in progress across the switch: the next round is a fresh verdict under whatever adapter is installed when it runs; the earlier round reaches the reviewer as text and is not re-parsed.
- A caller that pins this repository at a tag or commit, and keeps its own review prompts, should update in this order to keep its own tests green: (1) teach its prompts to end with the INTERVAL line while still pinned to the older adapter, which is safe because the older adapter reads `INTERVAL: none` as an incomplete record only when its prose scan also finds a duration, where it refused already, and reads a measured record in the same grammar; (2) move the pin to a release that includes this change, together with any copied fixtures and test stubs of the adapter. Between (1) and (2) the old prose scan still runs, so its refusals can occur until (2) lands; keep the two adjacent.
- The phase result's `intervals` key now appears on every success. A consumer that compared the full key set of a result must expect it.

## 5. What no longer exists

Under the old scan these all had code to fix; under the record none of them has code to attach to, because no line other than the record block is read:

- a prose token misread as a claim (a seven-digit short sha read as days; a section number such as `§3d`; a path component such as `docs/3d/x.md`);
- a quoted input read as a claim (probe inputs quoted in Evidence, Issues or a fence; a duration inside a code span);
- the scan's copy of the adapters' parsing rules diverging from the adapters (indented verdict values, untagged bullets after a tagged one, indented continuation lines, one backend's section table applied to another);
- fence pairing in the scan (a `#` comment inside a fence ending it early, an unclosed indented fence pairing with a later one, a fence info string that looks like the clean sentinel);
- scan guards with no test or no effect, and the documentation of which exemptions applied where;
- a reviewer's own output refused for quoting the probes it ran.

The one question that does not disappear, whether a reviewer's `none` is true, is answered in section 2: it is an attestation, not a fact the phase can check.

## 6. What was deleted

From `review_phase.py`, every name on the prose-scan path: the duration detector (`_DURATION_TOKEN`, `_BOUND_NOUN`, `_DURATION_CLAIM`, `_EMPHASISED_DURATION` and its helper), the reference pass (section, path, file:line, link-target, URL and wrapper handling, and the path shields), the quoted-input pass (Evidence and Minor notes sections, fences, code spans), the quoted-example pass, the scan drivers (`_iter_scanned_lines`, `_text_for_duration_scan`, `_first_duration_claim_line`, `_has_duration_claim`, `_first_incomplete_interval_line`) and the multi-line `_collect_intervals`. Kept: `_INTERVAL_LINE` (now matched one line at a time), `_IntervalParts` (now one line, with the end source running to the last `=`), `_parse_interval_body`, `_named_refusal`, `_attach_intervals`, and `_require_intervals`, rewritten to section 2.

From the tests: every test of the deleted scan (reference forms, wrappers, shields, mismatched closers, link targets, quoted inputs, quoted examples, bound nouns, configured timeouts, and their growth and long-input timing cases), and the multi-line cases of the `_IntervalParts` grammar test, since a body is now always one line. Kept and rewritten: the missing-endpoint cases (the record already sat on the last line), the parsed-record case, the clean-review case (now with `INTERVAL: none` and `intervals: []`), the refusal cut at 200 characters, the copied real reviews that quote durations (now ending in `INTERVAL: none`), the heading and verdict linear-time blocks, and the `_INTERVAL_LINE` / `_IntervalParts` grammar and linearity block. Every review fixture gains a last line (`INTERVAL: none`, or the record for `duration-with-interval.md`); `duration-without-interval.md` is now the `review_interval_missing` fixture.

## 7. Build plan

| Change | Where | Contents |
|---|---|---|
| 1 | this repository | The parser per sections 2 and 6, the tests and fixtures, the `reviewer` agent and `review` skill wording per section 3, and this document. One change, so no install of this repository ever has a parser that requires a line its own prompts do not ask for. |
| 2 | a caller that composes its own review prompts | Its prompt texts ask for the last line (section 4, step 1). |
| 3 | the same caller | Its pin of this repository moves to a release with change 1, with any copied adapter fixtures and test stubs (section 4, step 2). |

Changes 2 and 3 belong to other repositories and come after change 1 and a release of it.

## 8. Non-goals and risks

Non-goals: verifying that a declared interval is true or that the review mentions it; recording intervals in a caller's receipt; a v2 of `review-findings`; any change to how a caller chooses which message carries the verdict; the Jev shadow's payload trim.

Risks: (1) A reviewer omits the line or puts it before Evidence. The refusal names the line and the review is run again; the installed contracts show the line in the block. (2) A model appends a sign-off after the line: refused as missing, retryable; the contracts say "last line". (3) A false `none`: accepted as an attestation, auditable through a pinned result digest (section 2). (4) Between a caller's prompt change and its pin move, the old prose scan still runs (section 4); keep those two changes adjacent. (5) This change deletes most of one file; read the surviving code in full rather than the diff.

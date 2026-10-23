DESIGN: the interval scan blanks quoted input (closed code spans and fences) everywhere except where the selected adapter consumed lines: on a consumed finding line closed spans are blanked but fenced content and bare claims stay visible, and a consumed verdict or heading line is scanned as written; the adapter reports those lines, so the scan holds no second copy of the parsing rules.

# Interval scope from the adapter's consumed lines

## Why

A review quotes probe inputs (`41m`, a fenced `500ms`). The scan must ignore those, yet never ignore a line the adapter turns into a verdict or a finding, or a measured interval slips past the INTERVAL rule. Three rounds on the first attempt each found one more line shape the scan's own copy of the parsing rules missed (fenced headings, indented verdicts, tagged bullets, then untagged bullets, continuations, and a Claude-only section applied to the Grok backend). The fix is structural: the parser is the only authority on what it read.

## Adapter return shape

Each adapter returns `(verdict, findings, consumed)`. `consumed` has one entry per line of `text.splitlines()` (a line number indexes it): `None`, `"decision"` or `"finding"`. It is filled by marking half-open line ranges where the parse happens, covering exactly the lines the adapter inspected to decide something (a decision mark is never downgraded to a finding mark):
- `decision`: the line that set the verdict (heading, label, value line, the clean sentinel) and each section heading it matched (`_HEADING`), since a heading moves its section boundary;
- `finding`: each bullet it collected with its continuation lines, and any clean-marker or placeholder bullet it skipped, taken from the same loop iteration that collected it (`_bullets` returns the end index it already tracks; a tagged bullet's range runs to the next heading, as `_bullets` does).

Lines the adapter ignored (summary prose, Evidence bullets, anything under a heading it does not collect) are not consumed. A line the adapter reads is consumed whether or not it sits in a fence, because the adapters know nothing of fences. Ranges are produced where the parse happens, never recomputed by a second pass.

## Ordering in `run_phase`

Parse first, then scan: `verdict, findings, consumed = selected(text)`, then `_require_intervals(text, consumed)`. When the selected adapter raises and `_try_other_adapters` succeeds, the ranges come from the adapter that succeeded. `_has_duration_claim` and `_first_duration_claim_line` take `consumed` as a required argument: a list means "this adapter's lines", `None` means "no adapter decided" (below). There is no default, so a caller cannot forget it.

## The scan rule

For each line index `i`: a `decision` line is scanned as written (a verdict line hiding a claim in a span was an accepted-with-no-INTERVAL bug). A `finding` line has the inside of its closed inline spans blanked, so a finding may name a probe in backticks, and nothing else: its bare claims stay visible, and a `finding` line inside a fence is not blanked as fenced content. Any other line has closed inline spans and closed fences blanked exactly as #59 does (CommonMark: a run of N backticks closes on the next run of exactly N on the same line; a fence closes on a same-marker line of at least the opener's length with no info string). Everything else the scan already does (emphasis, references, double-quoted examples, bound nouns, the INTERVAL line exclusion) is unchanged and runs after.

The marks are filled once. A fence opener or closer line is still a fence line; only the lines between are quoted, and the consumed ones among them keep the treatment above. The scan's double-quoted-example pass also skips `decision` lines through the marks, not through its own verdict regexes.

## JEV fallback: scan everything as prose

JEV runs only when no adapter could parse the return, so there is no verdict line, no finding bullet and no line anchor to report; its input is free prose. Decide: `consumed=None`, which blanks nothing (no span, no fence, no double-quoted example), and the whole text is scanned as written. Reason: with no parser to say which lines carry the verdict, any line could, and quoting would be the same guess the first attempt kept getting wrong. Cost: a JEV-decided review that quotes `41m` in backticks is refused; it can quote with an INTERVAL line or reword. The refusal is retryable, a silent pass is not.

## Unclosed markup

An unterminated backtick run, a mismatched run length, an escaped backtick, a span that crosses a line, a fence closed by the wrong marker or too few characters, and an unterminated fence are all prose: nothing is blanked for them, and an unterminated fence leaves every later line unblanked too (fail closed, as in #59). A fence opener ending in a number that the scan would join to the next line's unit leaves the block unquoted.

## Linear time

One pass builds the consumed flags (O(lines)); one left-to-right pass blanks spans and fences, each fence closed by one forward search never repeated, each span by the run list already used. The adapters' own loops are unchanged in cost.

## Required tests (expected outcome in parentheses)

Quoted, unconsumed lines: summary inline spans `41m` `2h` `10000000ms` (parses); `took 2d` in a span (parses); backtick and tilde fences holding `500ms` (parses); a double-backtick span holding a backtick (parses); Evidence spans and fences under every heading form, title and bare label (parse); the real SK13 review shape (parses).
Still claims: bare `41m`; `_3h_`; `"41 seconds"`; a span beside a bare duration; the same token in and out of a span (refuse). Unterminated backtick, mismatched run, escaped backtick, span across lines, unterminated fence (and a later span after it), wrong-marker closer, too-short closer, backtick in a fence info string (refuse). Backtick-wrapped path-first-component: quoted when closed (parses), claim when unclosed (refuses).
Consumed lines (all refuse): a span-quoted claim on a verdict or heading line (an indented grok value line with a backticked claim among them); a heading, `## Verdict:`, `VERDICT:` label inside a fence; indented verdicts for every form (grok value line with spaces, tab, backtick; grok label; Claude label; heading; bold heading); fenced `[BUG]` / `[SECURITY]` / indented numbered bullets at top level, under Evidence, and with no verdict heading; a fenced bullet under Issues and Minor notes; Sol r2 (a): a later untagged bullet and an indented continuation after a tagged or fenced bullet, each with a bare claim.
Backend scope: Sol r2 (b): a Grok review with a fenced quoted example under a `### Issues` heading (parses, Grok does not consume it); the same text on the Claude backend (refuses where the bullet is a finding); Grok `## FINDINGS` bullets (refuse).
JEV: forced fallback with a span-quoted duration in Evidence (refuses); with a bare duration (refuses); when `_try_other_adapters` succeeds, that adapter's ranges apply.
Paired tests for the finding-line rule: a quoted span on a consumed finding line parses (`- [BUG] a.py:3 - the probe `took 41m` is mishandled`, also a span in an Issues and a Minor notes bullet); a bare claim on a consumed finding line refuses; a fenced claim on a consumed finding line refuses (the same bullet inside a closed fence). Changes against #59 to accept knowingly: the JEV-decided review that quotes a duration in a span now refuses.
Linear growth, in the existing growth parametrization: #59's shapes (long backtick run, many unbalanced backticks, many fence-like lines, many unclosed fence openers, many spans on one line) plus many consumed bullets (every line a bullet) and many tiny ranges.

## Mutations that must turn tests red

1. Ignore `consumed` (blank everywhere): every "Consumed lines" and Sol r2 case.
2. Blank nothing: every "Quoted, unconsumed lines" case.
3. Drop the continuation lines from a bullet's range (end off by one): Sol r2 (a).
4. Drop section headings from `consumed`: the fenced-heading cases.
4b. Blank spans on `decision` lines too: the verdict-line span cases; blank fences on `finding` lines: the fenced-claim case.
5. Grok returns Claude's ranges: Sol r2 (b).
6. JEV passes a list instead of `None`: both JEV cases.
7. Treat an unterminated fence or backtick as closed: the unclosed-markup cases.
8. Rescan unclosed fences per line, or rebuild consumed flags per line: the growth cases.

## Decided

A finding that names a probe in backticks does not demand an interval (spans are blanked on finding lines), and neither does any other closed span outside a verdict or heading line. Fenced content on a finding line, and any span on a verdict or heading line, do.

---
name: sharpen
kind: personal
version: 1.3.0
description: >
  Run a conversational sharpening session — the requirements layer between a ratified
  calibration and a buildable contract. Interviews the user ONE probing question at a time
  (never a batch, never an invented option list) and challenges assumptions — including
  the drafting session's own option list and any stale quote being treated as standing
  intent — rather than validating them. Outputs only a requirements mini-brief — must-dos,
  success criteria in the user's own words, non-goals, open questions — which carries NO
  charter authority until the user ratifies it. REFUSES a topic that is really a bounded,
  reversible call with a real option set, handing back the drafted election instead. Use
  when the user says "sharpen this", "run a sharpening session", "grill me on this",
  "let's go deeper before we charter it", "interview me about what I actually want", or
  when a CHART or a close routed a SHARPEN-class question here. NOT for red-teaming a
  direction already chosen (that is `shape`), NOT for executing a decided queue (that is
  `orchestrate`), and NOT an AskUserQuestion election.
---

# Sharpen

The delegation loop has an **altitude gap**. `CALIBRATE` sets direction on
purpose at high altitude — "where the road is headed and not the details" — and
everything downstream assumes the details exist. Nothing in the loop gathers
them. So when a CHART meets a question whose answer *is* the requirement, it
does the only thing its instruments allow: it invents an option list and runs an
election, and Andrew's click ratifies **Claude's guess** with election ceremony,
which the v3 record then launders into `CONFIRMED`. The optional
`delegation_loop_standard` source names this defect when available; this skill
also carries a self-contained fallback so the interview does not depend on that
external document.

The stance is adapted from `grill-me` (github.com/mattpocock/skills), which
Andrew pointed at as the shape he wanted: relentless sequential questioning,
challenge assumptions, sharpen rather than validate. What is adapted and what is
deliberately changed is recorded in § Prior art — the changes are not cosmetic.

**The scarce resource is Andrew's attention, not the number of questions.**
*"this is only for the questions that make the cut — my time is the most scarce
but my voice matters a lot … it's about cueing me in at the right part of the
loop"* (session #134, 2026-08-14, the ratification reply). Both mis-routes —
sharpening a bounded call, and electing a requirement — spend the same channel.

## What this skill is (and is NOT)

- It is a **conversational elicitation session.** One probing question at a
  time, in the main loop, with Andrew present. No `AskUserQuestion` batch, no
  option menus, no fan-out of questions across sub-agents.
- Its only deliverable is a **requirements mini-brief** (§ The output) —
  **exactly one file**, written under `harness_root.roadmaps`. It never edits the wave-SoT,
  a queue row, a `contract: v3` block, the calibration SoT, a standard, code, or
  any live system. A CHART cites the brief later; this skill does not chart.
- **It AUTHORS a brief; it never CERTIFIES one.** The same authority separation
  the rest of the loop runs on (`delegation-loop.md` § v3): an unratified brief
  confirms nothing, however carefully it was elicited, and no amount of
  agreement inside the session substitutes for Andrew's dated ratification
  (§ Ratification). An AI artifact never upgrades itself.
- It is **not** `shape`. `shape` red-teams a direction that has already been
  chosen; this runs *before* there is a direction to red-team. It is not
  `orchestrate` (which executes a decided queue) and not `chart-wave` (which
  scopes rows). Its output feeds those; it does none of their work.
- It is **not** a question-answering session about the codebase. Facts are the
  session's job to find, never Andrew's to supply (§ The interview).

## Sources and path resolution

Run `python3 ~/.claude/skills/sharpen/paths.py` before reading or
writing anything. It prints one JSON object containing `roadmaps`, `standards`,
and the four optional-source paths below.

When `skills/_lib/harness_root.py` is present, `paths.py` delegates to its
`resolve()` and `resolve_relative()` functions. Root precedence therefore stays
shared with chart-wave: `HARNESS_ROOT`, then `harness_root.root`, then a
same-repository checkout discovered from the current working directory, then
the shared default. The `harness_root.roadmaps` directory is the mini-brief
directory; do not add `brief_dir` or another alias for it. Keeping briefs there
also keeps ratified briefs inside chart-wave's `roadmaps_glob` without extra
wiring.

When the shared resolver is absent, `paths.py` reads `config.json` beside
itself. Missing, malformed, non-object, and non-string configuration values
each emit one degradation line. Unset `harness_root.roadmaps` and
`harness_root.standards` then resolve to `~/.claude/roadmaps` and
`~/.claude/standards`. For each source key below, an absolute or `~`-prefixed
value stays explicit; a bare filename is joined to its named directory.

The shipped config preserves toolkit path behavior. The standalone defaults
make an independently copied skill usable in a clean home directory.

Every external source is optional. For each missing or unreadable source, emit
exactly one line:

`sharpen: optional source <config-key> unavailable at <resolved-path>; continuing with the built-in default.`

Then continue with the corresponding built-in default:

| Source | Resolution | Built-in default when unavailable |
|---|---|---|
| Brief dir | `harness_root.roadmaps` | Write the mini-brief here using `brief_filename_pattern`. This is the only write target. |
| Delegation-loop standard | `<standards>/delegation-loop.md` (`delegation_loop_standard`) | Apply this skill's refusal rule, authority boundary, ratification checks, and provenance format as written below; claim no external standard as authority. |
| Calibration SoT | `<roadmaps>/calibration.md` (`calibration_sot`) | Treat every quote from before this conversation as stale and verify it with the user before relying on it. |
| Wave SoT | `<roadmaps>/wave-current.md` (`sot_path`) | Continue without routing context and record `routing context unavailable` in the brief's provenance. |
| Model-routing standard | `<standards>/model-routing.md` (`model_routing_standard`) | Stay in the current conversation and launch no fact-finding sub-agents. |

## Flow

### 0. Confirm this is a sharpening topic, not an election (the refusal rule — which also re-fires mid-interview)

**Apply the routing rule in this skill.** When `delegation_loop_standard` is
available, also read its *Elicitation demarcation* section as the external
authority. The three ways a session legitimately starts:

1. **Andrew invokes it** — `/sharpen <topic>`, on anything he wants sharpened.
2. **A CHART routed a SHARPEN-class question here** — the row stays
   `INTENT: UNRESOLVED`, its question id is preserved verbatim, and the CHART
   scheduled the session instead of electing an answer.
3. **A close scheduled one** — a `NEEDS-YOU` banner entry with its four fields,
   carried until the sitting happens.

**The refusal rule (the both-directions guard, and it is enforced HERE too).**
A bounded, reversible call with a genuine, mutually-exclusive option set is an
**election**, and inflating it into a session because sharpening sounds more
careful spends exactly the channel the demarcation exists to protect. Apply the
OUTCOME-line test to the topic in front of you **before** asking question one:

> If you can already write the row's `OUTCOME` line and the question only picks
> which of a few known outcomes ships — **refuse.**

Refuse in this shape, and refuse in one message rather than negotiating:

```
SHARPEN REFUSED — this is an ELECTION, not a sharpening topic.
Why: the OUTCOME line is already writable — "<the OUTCOME as it would read>".
Drafted election, ready to pose:
  Q: <the question>
  - <(Recommended) content-specifying label> — <what ships if chosen>
  - <label> — <what ships if chosen>
  - <label> — <what ships if chosen>
  Recommendation: <which, and why, in one clause>
```

The same guard runs **mid-session**: if three or four questions in the topic has
collapsed to a bounded call, say so, stop, and hand back the election. Padding a
session to justify having opened one is the failure mode on this side.

**One topic per session.** A backlog of asks is not a topic — draining several
through one sitting is how the answers get thin and how the brief ends up
describing three things badly. Pick the one that seeds the most, run it, stop.

### 1. Run the interview (one question at a time, looped until convergence)

**One question at a time. Ask it, then stop talking.** Not a numbered round, not
"three quick ones", not a question with four sub-questions wearing a trenchcoat.
Wait for the answer before the next question exists — the next question is
*supposed* to be shaped by the last answer, and a batch forecloses that.

Question shape — a title, the question, and one stated view to push against:

```
Q3 — <short title>
<The question. One to three sentences, concrete, about THIS thing.>
My read: <one position, and why in a clause>. Push back if that's wrong.
```

- **A stated view is not an option list.** The house rule is *recommendations,
  not option menus* (global working agreement), and it holds here: offer **one**
  position with its reason, falsifiable, inviting disagreement. An enumerated
  space frames the answer and re-creates the exact defect that routed the
  question here. One view he can knock down; never three he can pick from.
- **Challenge assumptions — starting with your own side's.** The drafting
  session's option list is an assumption. The row's framing is an assumption.
  "We'd obviously do X first" is an assumption. Say which one you are testing.
- **A stale quote is a trigger, not a licence.** If `calibration_sot` is
  available, its current ratification date is the freshness boundary. Otherwise
  use the conservative built-in default above: verify every quote from before
  this conversation. Put a stale quote to the user and ask whether it still
  speaks to *this* surface.
  Quote archaeology is what produced the drift this instrument exists to stop —
  *"the more I try to lean into Claude digging up pure historical facts … the
  more drift from intent that seems to occur"* (session #134).
- **Sharpen, not validate. Disagree where the evidence disagrees.** A session in
  which Andrew agreed with everything is a session that didn't happen — that is
  the passivity failure `grill-me`'s own docs name, and it produces a brief
  carrying certainty it never earned. If an answer contradicts something on
  disk, say so with the file and the line, and ask again.
- **Facts are yours, never his.** If a question needs a fact from the
  environment (what the code does today, what the job is scheduled at, what a
  PR actually landed), look it up. A read-only sub-agent is allowed only when
  `model_routing_standard` is available to pin it; otherwise use the built-in
  current-conversation default. Never block the conversation on fact-finding:
  ask the next question that does not depend on it and fold the fact in later.
- **"I don't know" is a real answer**, and usually means the question is
  *ungrillable* — it needs something to react to rather than more talking. Log
  it as an open question, name the cheapest way to answer it (a prototype, a
  spike, a look at a real screen), and move on. Talking through an ungrillable
  question is how sessions balloon.
- **Scope is his, not yours.** If he says the scope is drifting, it is.

**The stop rule — convergence, not exhaustion.** Stop when two consecutive
questions produce no change to the draft's must-dos, success criteria, or
non-goals. Not at a question count, not when a template feels full. Roughly
8-20 questions and **15-30 minutes of Andrew's time** is the shape this is
sized for; a session heading past that is usually one topic wearing three, and
splitting it beats grinding it.

### 2. Write the requirements mini-brief

Write one file in the resolved `roadmaps` directory from `paths.py`, named
`sharpen-<topic-slug>-<YYYY-MM-DD>.md`. Present it in full for ratification.

```markdown
# Sharpening brief — <topic>

**STATUS: DRAFT — carries no ORIGIN authority until ratified.**
**Session:** <YYYY-MM-DD> · <n> questions · <duration> · routed from <CHART row id | close banner entry | Andrew direct>

## Must-dos
- <the requirement, as a thing that must be true when this is done>

## Success criteria (Andrew's words)
- "<verbatim quote from THIS conversation>" — <what it means in build terms>

## Non-goals (explicit)
- <what this is NOT, and the one line of why it was ruled out>

## Open questions that remain
- <question> — <cheapest way to answer it: prototype / spike / look at X> — <what it blocks>

## Provenance
- Routed here by: <the rule + the surface that routed it>
- Assumptions challenged and dropped during the session: <list, briefly>
- Stale quotes tested: <quote (date)> — <still speaks / superseded by his answer>
```

Section rules that make the brief usable as charter material:

- **Success criteria are quoted, not paraphrased.** Verbatim, from **this**
  conversation, each with its own line. A criterion in Claude's words is a
  criterion Claude invented — mark it as such (`[Claude's phrasing, ratified]`)
  rather than dressing it as his.
- **Non-goals are mandatory and explicit.** A brief with no non-goals did not
  sharpen anything; scope is defined by what it excludes. Each one carries the
  reason it was ruled out, so a later CHART doesn't re-propose it.
- **Open questions stay open.** Never resolve one by inference to make the brief
  look finished. Each names the cheapest way to answer it and what it blocks —
  an ungrillable question belongs here, not in the must-dos.
- **The brief is short.** Fifteen requirement lines that were actually said beat
  sixty that were mostly inferred.

### 3. Present the brief for ratification (the authority boundary)

**An unratified brief confirms nothing.** Apply the authority boundary below
whether or not `delegation_loop_standard` is available: an AI artifact never
upgrades a row to `CONFIRMED` by citing another AI artifact, and a mini-brief is
an AI artifact until Andrew says otherwise. Silence does not ratify it. Elapsed
time does not. A later session citing it does not.

- **Present the whole brief and ask for a ratification.** This is the ONE place
  this skill may pose a structured election, and it is legal precisely because
  it is now a bounded, enumerable call — *ratify as written* / *ratify with the
  named changes* / *not yet*. That is the demarcation working in both
  directions, not an exception to it.
- **Ratify-with-changes:** apply the named changes, re-present, and stamp the
  final text. Never stamp text he corrected.
- **The stamp replaces the STATUS line**, and is greppable —
  `grep -n '^\*\*STATUS: RATIFIED'`. **One prefix, two payloads**, mirroring the
  two `ORIGIN` forms below. Write whichever is *true* of how he ratified:

```
**STATUS: RATIFIED <YYYY-MM-DD>** — Andrew, verbatim: "<his approval words>" (<session ref>).
**STATUS: RATIFIED <YYYY-MM-DD>** — Andrew, structured-election(AskUserQuestion): elected "<label>" · <description in full> · <the ratification question> · rejected: <labels> (<session ref>).
```

  Use the **first** whenever he wrote or said anything of his own alongside the
  choice — the stronger record, and the usual case. Use the **second** only for
  a bare click with no words of his. **Never write an option label into the
  `verbatim` slot:** the label is Claude's phrasing, and quoting it as his words
  is the literal-quote rule's own named failure — *"a ratification token … it
  ratifies; it does not answer"* (`delegation-loop.md` § v3, FAIL-CLOSED). A
  bare-click ratification is a real, `CONFIRMED`-bearing origin; it is just an
  **election** record rather than a quotation, and rows cite it accordingly
  (below). The election payload carries **four** of the five record fields
  because the fifth — the date — is the stamp's own `RATIFIED <YYYY-MM-DD>`
  prefix; transcribing the stamp into an `ORIGIN` therefore yields the full
  five-field record, and the date is read from the prefix, never re-derived.

- **The authority check — what a consumer must verify before citing a brief.**
  The prefix grep finds the line; it does not establish that the line means
  anything. A brief is `RATIFIED` **only** when all four hold; any one failing
  means **UNRATIFIED, fail closed**, naming which condition failed (the same
  discipline `chart-wave` § Calibration binding already applies to the
  calibration's own ratification date):
  1. **Exactly one** `^**STATUS:` line in the file. Two — even DRAFT plus
     RATIFIED — is a malformed brief, not a ratified one; the stamp *replaces*
     the DRAFT line, it never joins it.
  2. The prefix matches `^\*\*STATUS: RATIFIED <YYYY-MM-DD>\*\*` — a real
     calendar date, four-digit year, zero-padded.
  3. That date is **not later than today.** A ratification cannot be in the
     future; a future date parses perfectly and is still not a fact.
  4. A **non-empty payload** in one of the two forms above. Unfilled template
     text is not a payload: literal angle brackets, empty quotes, `TBD`, or the
     word `<label>` still standing means the stamp was pasted, not written.

  That is one grep plus a date read — deliberately not a parser spec. Anything
  a cheap check cannot confirm reads as UNRATIFIED, exactly as an unparseable
  calibration field does; "I could not verify this" is never "it is fine."

- **A ratified brief is frozen.** A later change is an **amendment** with its
  own date and its own ratification, appended — never an in-place edit of
  ratified text. Rewriting what was ratified is how a ratified brief quietly
  becomes an unratified one. **An amendment never adds a second `**STATUS:`
  line:** the single top-of-file stamp is **re-dated** to the latest
  ratification, and the amendment records its own approval under its own
  heading inside the amendments section. A brief carrying two status lines
  fails condition 1 of the authority check above and reads UNRATIFIED — which
  is the check working, not a false positive, so keep the stamp singular
  rather than loosening the check.

**How a CHART cites it — no new ORIGIN token.** The standard defines four
`ORIGIN` forms and this skill mints no fifth; the brief rides the existing ones
as their **ref**:

```
ORIGIN: verbatim-Andrew "<quote from the brief's success criteria>" (sharpen-brief roadmaps/sharpen-<slug>-<date>.md § Success criteria, RATIFIED <date>)
ORIGIN: structured-election(AskUserQuestion) — Ratify as written · the mini-brief at roadmaps/sharpen-<slug>-<date>.md as presented, <description in full> · <the ratification question> · <RATIFIED date> · rejected: <labels>
```

Use the first form when the row rests on **his own quoted words**; use the
second when it rests on a criterion in Claude's phrasing that he **ratified**.
The distinction is the whole point of quoting verbatim in the first place — do
not blur them to make a row look better sourced than it is.

**The brief ref rides INSIDE an existing field — never appended after it.** In
the verbatim form it belongs in the `(ref)` parens the grammar already has; in
the election form it belongs in the **description** field, and the `<date>`
field is the ratification date. Hanging a `(sharpen-brief …)` tail after
`rejected: <labels>` silently widens the standard's five-field election record
to six, which is exactly the kind of quiet grammar drift the `contract: v3`
consumers cannot see — they do not error on it, they simply parse something
other than what was written (§ What this skill is: this skill implements the
standard, it does not extend it).

## Session mechanics

- **Who runs it:** Andrew invokes it, or a session that scheduled one opens
  with it. It is **never** self-invoked mid-ROLL or mid-CHART — a probing
  question asked mid-stream in a high-throughput session is precisely how the
  Moms-on-Call question drowned (`delegation-loop.md` § v3, NEEDS-YOU banner).
- **Fresh conversation, not stacked on the drafting session.** The drafter's
  option list is one of the assumptions being challenged, and a session carrying
  it in context anchors on it. Bring the topic and the routing reference; leave
  the drafted answers behind.
- **Model selection:** when `model_routing_standard` is available, read its
  session guidance live and follow it without hardcoding an id. When absent,
  remain in the current conversation and launch no fact-finding sub-agents.
- **Where it lands:** the resolved `roadmaps` directory from `paths.py`.
  `brief_commit` (config, default true) lands the file with a conventional
  `docs:` commit; the draft and ratification stamp are two separate commits,
  never squashed into one that makes an unratified draft look ratified.

## Safety rails (non-negotiable)

- **One question at a time.** Never an `AskUserQuestion` batch, never an option
  menu for a requirement — inventing alternatives around an unarticulated need
  is the exact defect this instrument exists to replace.
- **One file, ever.** The mini-brief under the resolved `roadmaps` directory. Never the wave-SoT,
  never a queue row or `contract: v3` block, never the calibration SoT, never a
  standard, never code, never a wrapper/plist/secret/deploy.
- **Refuse an election-class topic** (§ the refusal rule) and hand back the
  drafted election. Over-routing costs the same scarce channel as under-routing.
- **Unratified means unconfirmed — and unreadable means unratified.** No row is
  charted `CONFIRMED` off a draft brief; no brief ratifies itself; a session's
  own agreement is not ratification. Only Andrew's dated approval stamps it,
  and only a stamp that passes all four parts of the authority check
  (§ Ratification) counts as one — a prefix match alone establishes nothing.
  A draft is also **not survey material**: `chart-wave` excludes it from the
  roadmaps glob at the source, so a brief in flight is never mined for
  candidate rows and gated afterwards.
- **Quote verbatim or mark it.** Every success criterion is his words or is
  labelled as Claude's phrasing. A paraphrase presented as a quote is the
  laundering this whole layer was built to stop.
- **Never resolve an open question by inference** to finish the brief. Open is a
  legitimate terminal state for a question; a fabricated answer is not.
- **Mint no new vocabulary.** No fifth `ORIGIN` form, no third question type
  beside `BLOCKING`/`DEFAULTABLE`, no fourth kickoff session type. This skill
  implements the standard; it does not extend it — an operator doc must not
  assert authority its normative source lacks.

## Gotchas

- **Agreement is a smell, not a result.** Forty questions of "sounds right"
  produces a brief that reads authoritative and decided nothing. If nothing was
  pushed back on, ask a harder question or stop and say the session found no
  daylight.
- **The option list you arrived with is evidence about you, not about him.**
  Test it explicitly and record it in § Provenance when it drops. A brief that
  reproduces the drafted options word for word probably just re-ran the election
  in slow motion.
- **Ungrillable questions balloon sessions.** "How should this feel?" and "one
  page or three?" need something to react to. Log them as open, name the
  prototype, move on — do not keep rephrasing.
- **Don't sharpen a bounded call because it feels careful.** Refusing is the
  respectful answer when a one-minute election would do; session #134's own
  close-escalation reviewer flagged one such ask as `RESOLVABLE-NOW` and the
  election that replaced it took about a minute.
- **The brief is not a plan.** No file lists, no task breakdown, no estimates.
  Requirements, criteria, non-goals, open questions — the CHART turns those into
  rows, and a brief that pre-empts it is guessing at the same altitude the
  session just refused to guess at.
- **A brief written and never ratified is decision debt**, counted where its
  routing entry already lives (the banner entry or the gated bucket that
  scheduled the session) — not as a second item. Do not let it sit as a "done"
  deliverable; it is done when it is stamped.

## Prior art — what was adapted from `grill-me`, and what was changed

`grill-me` / `grilling` (github.com/mattpocock/skills), the concept reference
Andrew linked. **Kept:** relentless questioning as the instrument; challenge
assumptions; sharpen rather than validate; facts are the agent's job and
decisions are the user's; "I don't know" as a real answer; the grillable /
ungrillable distinction; the it-is-working-if-you-disagree test; a fresh
conversation rather than one stacked on an existing plan.

**Changed, deliberately:**

| `grill-me` | Here | Why |
|---|---|---|
| Asks the whole **frontier** per round (dozens of questions across a few rounds) | **One question at a time** | Andrew's ratified shape, and his own scarcity constraint — upstream this is an opt-in override a user adds to their global instructions; here it is the contract |
| **Stateless** — writes no files, leaves only a sharper idea in your head | Writes **one mini-brief**, ratified and dated | The output has to be *citable* by a later CHART as `ORIGIN`; a sharper idea in someone's head cannot carry charter authority through a delegation loop |
| Ends when the frontier is empty (every branch visited) | Ends on **convergence** (two questions changing nothing) | Exhaustive tree-walking is the ballooning failure its own docs warn about, and this session is budgeted in minutes of Andrew's attention |
| Every question carries a recommended answer | **One stated view**, explicitly not an option list | Reconciles the house "recommendations, not option menus" rule with the demarcation's ban on invented alternatives |
| Runs on anything, any time | **Refuses** election-class topics; one topic per session | The both-directions guard — over-routing spends the same scarce channel as under-routing |

## Files

| Path | What |
|---|---|
| `skills/sharpen/SKILL.md` | this playbook |
| `skills/sharpen/config.json` | compatibility paths, filename pattern, optional source filenames, commit behavior |
| `skills/sharpen/paths.py` | executable resolver for the write directory and optional sources |
| `<roadmaps>/sharpen-<slug>-<date>.md` | the mini-brief — the ONLY file this skill writes |
| `<standards>/delegation-loop.md` (`delegation_loop_standard`) | optional routing and lineage authority; local fallback above |
| `<roadmaps>/calibration.md` (`calibration_sot`) | optional quote-freshness boundary; conservative local fallback above |
| `<roadmaps>/wave-current.md` (`sot_path`) | optional routing context; provenance fallback above |
| `<standards>/model-routing.md` (`model_routing_standard`) | optional session guidance; current-conversation fallback above |
| `skills/chart-wave/SKILL.md` | the front bookend that routes SHARPEN-class questions here (Flow step 5) |
| `skills/session-handoff/SKILL.md` | the back bookend that schedules a session from the close banner (step 3b(a)) |

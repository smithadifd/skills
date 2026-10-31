---
name: sharpen
kind: portable
version: 2.0.1
description: >
  Run a conversational requirements interview — the step between a settled direction and
  a buildable plan. Interviews the user ONE probing question at a time (never a batch,
  never an invented option list) and challenges assumptions — including the drafting
  session's own option list and any stale quote being treated as standing intent — rather
  than validating them. Outputs only a requirements mini-brief — must-dos, success criteria
  in the user's own words, non-goals, open questions — which carries NO authority until
  the user ratifies it. REFUSES a topic that is really a bounded, reversible call with a
  real option set, handing back the drafted choice instead. Use when the user says "sharpen
  this", "run a sharpening session", "grill me on this", "let's go deeper before we plan
  it", "interview me about what I actually want", or when a requirements question
  needs the user's own words. NOT for red-teaming a direction already chosen (that is
  `shape`), and NOT a multiple-choice question.
---

# Sharpen

Plans often start from a direction ("where we are headed") and assume the details
exist. Nothing gathers them. So when a planning step meets a question whose answer
*is* the requirement, it does the only thing it can: it invents an option list and
asks the user to pick one. The pick then ratifies the **drafter's guess** with the
ceremony of a decision. This skill is the corrective: it asks the user what they
actually want, one question at a time, and writes it down.

The stance is adapted from `grill-me` (github.com/mattpocock/skills): relentless
sequential questioning, challenge assumptions, sharpen rather than validate. What is
adapted and what is deliberately changed is recorded in § Prior art.

**The scarce resource is the user's attention, not the number of questions.** Both
mis-routes — sharpening a bounded call, and picking a requirement from a drafted
menu — spend the same channel. Ask only what only the user can answer.

## What this skill is (and is NOT)

- It is a **conversational elicitation session.** One probing question at a
  time, in the main loop, with the user present. No `AskUserQuestion` batch, no
  option menus, no fan-out of questions across sub-agents.
- Its only deliverable is a **requirements mini-brief** (§ The output) —
  **exactly one file**, written under `notes_root.roadmaps`. It never edits an
  existing plan, tracker, direction record, standard, code, or any live system.
  Whoever plans next cites the brief; this skill does not plan.
- **It AUTHORS a brief; it never CERTIFIES one.** An unratified brief confirms
  nothing, however carefully it was elicited, and no amount of agreement inside the
  session substitutes for the user's dated ratification (§ 3). An AI artifact
  never upgrades itself.
- It is **not** `shape`. `shape` red-teams a direction that has already been
  chosen; this runs *before* there is a direction to red-team.
- It is **not** a question-answering session about the codebase. Facts are the
  session's job to find, never the user's to supply (§ 1).

## Sources and path resolution

Run `python3 ${CLAUDE_SKILL_DIR}/paths.py` before reading or
writing anything. It prints one JSON object containing `roadmaps`, `standards`,
and the four optional-source paths below.

Path resolution is standalone by default: `paths.py` never discovers or delegates
to a neighboring resolver. It reads `config.json` beside itself and resolves the
configured directories and source filenames directly. The
`notes_root.roadmaps` directory is the mini-brief directory; do not add
`brief_dir` or another alias for it.

Missing, malformed, non-object, and non-string configuration values each emit one
degradation line. Unset `notes_root.roadmaps` and `notes_root.standards`
resolve to `~/.claude/roadmaps` and `~/.claude/standards`. For each source key
below, an absolute or `~`-prefixed value stays explicit; a bare filename is joined
to its named directory. Path precedence is explicit `--config`, then
`~/.claude/skill-config/sharpen.json`, then shipped config, then built-in
defaults; top-level keys are replaced whole, so an override `notes_root` object
replaces the shipped one.
An override or `--config` file that still uses the earlier name for this object
(`harness_root`) is read as `notes_root` with one notice; when both are set,
`notes_root` wins.

Set `notes_root.resolver` to an absolute or `~`-prefixed Python file to opt in
to delegated resolution. Its module must provide `resolve(config, cwd=)` and
`resolve_relative(paths, subkey, value)`; `--cwd` forwards a working directory
to `resolve`. If the configured resolver reports that no root is set,
`paths.py` refuses with that resolver's message. Other import, contract, or
runtime failures emit one configuration line and degrade to the unchanged
standalone resolution.

**Install-specific process notes are optional and live outside this skill.** An install
that keeps its own process documents (how a brief is cited, which models to use) points the four source keys below at those documents
through `~/.claude/skill-config/sharpen.json`. The skill reads each document only
when it is present and follows it for the matter it covers; when it is absent the
built-in default below applies and the skill still works in full.

For each missing or unreadable source, emit exactly one line:

`sharpen: optional source <config-key> unavailable at <resolved-path>; continuing with the built-in default.`

Then continue with the corresponding built-in default:

| Source | Resolution | Built-in default when unavailable |
|---|---|---|
| Brief dir | `notes_root.roadmaps` | Write the mini-brief here using `brief_filename_pattern`. This is the only write target. |
| Process standard | `<standards>/process.md` (`process_standard`) | Apply this skill's refusal rule, authority boundary, ratification checks, and provenance format as written below; claim no external standard as authority. |
| Direction record | `<roadmaps>/direction.md` (`direction_record`) | Treat every quote from before this conversation as stale and verify it with the user before relying on it. |
| Context notes | `<roadmaps>/routing-context.md` (`routing_context`) | Continue without context notes and record `context notes unavailable` in the brief's provenance. |
| Model guidance | `<standards>/model-guidance.md` (`model_guidance`) | Stay in the current conversation and launch no fact-finding sub-agents. |

## Flow

### 0. Confirm this is a sharpening topic, not a choice (the refusal rule — which also re-fires mid-interview)

**Apply the refusal rule in this skill.** When `process_standard` is available,
also read the part of it that covers requirements interviews and follow it where it
is stricter. The three ways a session legitimately starts:

1. **The user invokes it** — `/sharpen <topic>`, on anything they want sharpened.
2. **A question that needs the user's own words** — another piece of work reached
   a requirements question and scheduled a session instead of picking an answer.
3. **An earlier session scheduled one** — a carried-forward note with the topic,
   the question, and why it needs the user's own words.

**The refusal rule (the both-directions guard, and it is enforced HERE too).**
A bounded, reversible call with a genuine, mutually-exclusive option set is a
**choice**, and inflating it into a session because sharpening sounds more careful
spends exactly the channel this skill exists to protect. Apply the outcome test to
the topic in front of you **before** asking question one:

> If you can already write the one-sentence outcome statement and the question
> only picks which of a few known outcomes ships — **refuse.**

Refuse in this shape, and refuse in one message rather than negotiating:

```
SHARPEN REFUSED — this is a CHOICE, not a sharpening topic.
Why: the outcome statement is already writable — "<the outcome as it would read>".
Drafted choice, ready to pose:
  Q: <the question>
  - <(Recommended) content-specifying label> — <what ships if chosen>
  - <label> — <what ships if chosen>
  - <label> — <what ships if chosen>
  Recommendation: <which, and why, in one clause>
```

The same guard runs **mid-session**: if three or four questions in the topic has
collapsed to a bounded call, say so, stop, and hand back the choice. Padding a
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

- **A stated view is not an option list.** Offer **one** position with its
  reason, falsifiable, inviting disagreement. An enumerated space frames the
  answer and re-creates the exact defect that routed the question here. One view
  the user can knock down; never three they can pick from.
- **Challenge assumptions — starting with your own side's.** The drafting
  session's option list is an assumption. The request's framing is an assumption.
  "We'd obviously do X first" is an assumption. Say which one you are testing.
- **A stale quote is a trigger, not a licence.** If `direction_record` is
  available, its current ratification date is the freshness boundary. Otherwise
  use the conservative built-in default above: verify every quote from before
  this conversation. Put a stale quote to the user and ask whether it still
  speaks to *this* surface. Quote archaeology is what produces drift from intent.
- **Sharpen, not validate. Disagree where the evidence disagrees.** A session in
  which the user agreed with everything is a session that didn't happen — that is
  the passivity failure `grill-me`'s own docs name, and it produces a brief
  carrying certainty it never earned. If an answer contradicts something on
  disk, say so with the file and the line, and ask again.
- **Facts are yours, never theirs.** If a question needs a fact from the
  environment (what the code does today, what a job is scheduled at, what a
  change actually landed), look it up. A read-only sub-agent is allowed only when
  `model_guidance` is available to pin it; otherwise use the built-in
  current-conversation default. Never block the conversation on fact-finding:
  ask the next question that does not depend on it and fold the fact in later.
- **"I don't know" is a real answer**, and usually means the question is
  *ungrillable* — it needs something to react to rather than more talking. Log
  it as an open question, name the cheapest way to answer it (a prototype, a
  spike, a look at a real screen), and move on. Talking through an ungrillable
  question is how sessions balloon.
- **Scope is the user's, not yours.** If they say the scope is drifting, it is.

**The stop rule — convergence, not exhaustion.** Stop when two consecutive
questions produce no change to the draft's must-dos, success criteria, or
non-goals. Not at a question count, not when a template feels full. Roughly
8-20 questions and **15-30 minutes of the user's time** is the shape this is
sized for; a session heading past that is usually one topic wearing three, and
splitting it beats grinding it.

### 2. Write the requirements mini-brief

Write one file in the resolved `roadmaps` directory from `paths.py`, named
`sharpen-<topic-slug>-<YYYY-MM-DD>.md`. Present it in full for ratification.

```markdown
# Sharpening brief — <topic>

**STATUS: DRAFT — carries no authority until ratified.**
**Session:** <YYYY-MM-DD> · <n> questions · <duration> · started from <the question | earlier session note | user direct>

## Must-dos
- <the requirement, as a thing that must be true when this is done>

## Success criteria (the user's words)
- "<verbatim quote from THIS conversation>" — <what it means in build terms>

## Non-goals (explicit)
- <what this is NOT, and the one line of why it was ruled out>

## Open questions that remain
- <question> — <cheapest way to answer it: prototype / spike / look at X> — <what it blocks>

## Provenance
- Started by: <the user, or the earlier note or question that scheduled it>
- Assumptions challenged and dropped during the session: <list, briefly>
- Stale quotes tested: <quote (date)> — <still speaks / superseded by their answer>
```

Section rules that make the brief usable as planning material:

- **Success criteria are quoted, not paraphrased.** Verbatim, from **this**
  conversation, each with its own line. A criterion in the assistant's words is
  a criterion the assistant invented — mark it as such
  (`[assistant's phrasing, ratified]`) rather than dressing it as the user's.
- **Non-goals are mandatory and explicit.** A brief with no non-goals did not
  sharpen anything; scope is defined by what it excludes. Each one carries the
  reason it was ruled out, so a later plan doesn't re-propose it.
- **Open questions stay open.** Never resolve one by inference to make the brief
  look finished. Each names the cheapest way to answer it and what it blocks —
  an ungrillable question belongs here, not in the must-dos.
- **The brief is short.** Fifteen requirement lines that were actually said beat
  sixty that were mostly inferred.

### 3. Present the brief for ratification (the authority boundary)

**An unratified brief confirms nothing.** Apply the authority boundary below
whether or not `process_standard` is available: an AI artifact never gains
authority by citing another AI artifact, and a mini-brief is an AI artifact until
the user says otherwise. Silence does not ratify it. Elapsed time does not. A
later session citing it does not.

- **Present the whole brief and ask for a ratification.** This is the ONE place
  this skill may pose a structured choice, and it is legal precisely because it
  is now a bounded, enumerable call — *ratify as written* / *ratify with the
  named changes* / *not yet*.
- **Ratify-with-changes:** apply the named changes, re-present, and stamp the
  final text. Never stamp text the user corrected.
- **The stamp replaces the STATUS line**, and is greppable —
  `grep -n '^\*\*STATUS: RATIFIED'`. **One prefix, two payloads.** Write
  whichever is *true* of how the user ratified:

```
**STATUS: RATIFIED <YYYY-MM-DD>** — <user>, verbatim: "<their approval words>" (<conversation ref>).
**STATUS: RATIFIED <YYYY-MM-DD>** — <user>, structured-choice(AskUserQuestion): chose "<label>" · <description in full> · <the ratification question> · rejected: <labels> (<conversation ref>).
```

  Use the **first** whenever the user wrote or said anything of their own
  alongside the choice — the stronger record, and the usual case. Use the
  **second** only for a bare click with no words of theirs. **Never write an
  option label into the `verbatim` slot:** the label is the assistant's phrasing,
  and quoting it as the user's words is the quote rule's own named failure. A
  bare-click ratification is a real ratification; it is just a choice record
  rather than a quotation, and anything citing it should say so. The date is read
  from the stamp's `RATIFIED <YYYY-MM-DD>` prefix, never re-derived.

- **The authority check — what a consumer must verify before citing a brief.**
  The prefix grep finds the line; it does not establish that the line means
  anything. A brief is `RATIFIED` **only** when all four hold; any one failing
  means **UNRATIFIED, fail closed**, naming which condition failed:
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
  a cheap check cannot confirm reads as UNRATIFIED; "I could not verify this" is
  never "it is fine."

- **A ratified brief is frozen.** A later change is an **amendment** with its
  own date and its own ratification, appended — never an in-place edit of
  ratified text. **An amendment never adds a second `**STATUS:` line:** the
  single top-of-file stamp is **re-dated** to the latest ratification, and the
  amendment records its own approval under its own heading inside the amendments
  section. A brief carrying two status lines fails condition 1 above and reads
  UNRATIFIED — which is the check working, not a false positive.

**Citing a brief.** Cite it by path and ratification date, and quote its success
criteria verbatim when resting a decision on the user's own words; say plainly when
a cited criterion is the assistant's phrasing that the user ratified. Do not blur
the two to make a decision look better sourced than it is. When `process_standard`
defines a citation grammar for briefs, follow it exactly and neither extend nor
reorder its fields.

## Session mechanics

- **Who runs it:** the user invokes it, or a session that scheduled one opens
  with it. It is **never** self-invoked mid-task by an automated workflow — a
  probing question asked mid-stream in a high-throughput session gets drowned.
- **Fresh conversation, not stacked on the drafting session.** The drafter's
  option list is one of the assumptions being challenged, and a session carrying
  it in context anchors on it. Bring the topic and any context notes; leave
  the drafted answers behind.
- **Model selection:** when `model_guidance` is available, read its session
  guidance live and follow it without hardcoding an id. When absent, remain in the
  current conversation and launch no fact-finding sub-agents.
- **Where it lands:** the resolved `roadmaps` directory from `paths.py`.
  `brief_commit` (config, default true) lands the file with a conventional
  `docs:` commit; the draft and ratification stamp are two separate commits,
  never squashed into one that makes an unratified draft look ratified.

## Safety rails (non-negotiable)

- **One question at a time.** Never an `AskUserQuestion` batch, never an option
  menu for a requirement — inventing alternatives around an unarticulated need
  is the exact defect this skill exists to replace.
- **One file, ever.** The mini-brief under the resolved `roadmaps` directory.
  Never an existing plan or tracker, never the direction record, never a
  standard, never code, never a wrapper/plist/secret/deploy.
- **Refuse a choice-class topic** (§ the refusal rule) and hand back the
  drafted choice. Over-routing costs the same scarce channel as under-routing.
- **Unratified means unconfirmed — and unreadable means unratified.** Nothing is
  marked confirmed off a draft brief; no brief ratifies itself; a session's own
  agreement is not ratification. Only the user's dated approval stamps it, and
  only a stamp that passes all four parts of the authority check (§ 3) counts as
  one — a prefix match alone establishes nothing. A draft is also **not source
  material**: a brief in flight is never mined for candidate work.
- **Quote verbatim or mark it.** Every success criterion is the user's words or
  is labelled as the assistant's phrasing. A paraphrase presented as a quote is
  laundering.
- **Never resolve an open question by inference** to finish the brief. Open is a
  legitimate terminal state for a question; a fabricated answer is not.
- **Mint no new vocabulary.** When `process_standard` defines record forms or
  question types, use exactly those; this skill implements them, it does not
  extend them.

## Gotchas

- **Agreement is a smell, not a result.** Forty questions of "sounds right"
  produces a brief that reads authoritative and decided nothing. If nothing was
  pushed back on, ask a harder question or stop and say the session found no
  daylight.
- **The option list you arrived with is evidence about you, not about the user.**
  Test it explicitly and record it in § Provenance when it drops. A brief that
  reproduces the drafted options word for word probably just re-ran the choice
  in slow motion.
- **Ungrillable questions balloon sessions.** "How should this feel?" and "one
  page or three?" need something to react to. Log them as open, name the
  prototype, move on — do not keep rephrasing.
- **Don't sharpen a bounded call because it feels careful.** Refusing is the
  respectful answer when a one-minute choice would do.
- **The brief is not a plan.** No file lists, no task breakdown, no estimates.
  Requirements, criteria, non-goals, open questions — the next person planning turns
  those into work, and a brief that pre-empts it is guessing at the same altitude
  the session just refused to guess at.
- **A brief written and never ratified is decision debt.** Do not let it sit as a
  "done" deliverable; it is done when it is stamped.

## Prior art — what was adapted from `grill-me`, and what was changed

`grill-me` / `grilling` (github.com/mattpocock/skills) is the concept reference.
**Kept:** relentless questioning as the instrument; challenge assumptions; sharpen
rather than validate; facts are the agent's job and decisions are the user's;
"I don't know" as a real answer; the grillable / ungrillable distinction; the
it-is-working-if-you-disagree test; a fresh conversation rather than one stacked
on an existing plan.

**Changed, deliberately:**

| `grill-me` | Here | Why |
|---|---|---|
| Asks the whole **frontier** per round (dozens of questions across a few rounds) | **One question at a time** | The user's attention is the scarce resource; upstream this is an opt-in override, here it is the contract |
| **Stateless** — writes no files, leaves only a sharper idea in your head | Writes **one mini-brief**, ratified and dated | The output has to be *citable* when someone plans later; a sharper idea in someone's head cannot carry authority |
| Ends when the frontier is empty (every branch visited) | Ends on **convergence** (two questions changing nothing) | Exhaustive tree-walking is the ballooning failure its own docs warn about, and this session is budgeted in minutes of the user's attention |
| Every question carries a recommended answer | **One stated view**, explicitly not an option list | Reconciles "give a recommendation" with the ban on invented alternatives |
| Runs on anything, any time | **Refuses** choice-class topics; one topic per session | The both-directions guard — over-routing spends the same scarce channel as under-routing |

## Files

| Path | What |
|---|---|
| `skills/sharpen/SKILL.md` | this playbook |
| `skills/sharpen/config.json` | directory keys, filename pattern, optional source filenames, commit behavior |
| `skills/sharpen/paths.py` | executable resolver for the write directory and optional sources |
| `<roadmaps>/sharpen-<slug>-<date>.md` | the mini-brief — the ONLY file this skill writes |
| `<standards>/process.md` (`process_standard`) | optional install-specific process and citation rules; local fallback above |
| `<roadmaps>/direction.md` (`direction_record`) | optional quote-freshness boundary; conservative local fallback above |
| `<roadmaps>/routing-context.md` (`routing_context`) | optional context notes; provenance fallback above |
| `<standards>/model-guidance.md` (`model_guidance`) | optional session guidance; current-conversation fallback above |

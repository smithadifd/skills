---
name: shape
kind: portable
version: 1.1.0
description: >
  Adversarially pressure-test a proposed direction BEFORE any code is written: parallel
  read-only sub-agents over one approach — unknowns, edge cases, alternatives + tradeoffs,
  failure modes (a pre-mortem), hidden dependencies — synthesized into ONE ranked
  "resolve-these-first" brief the user acts on before building. A design red-team, not a
  build step: it writes no code and changes no repo. Use when the user is about to commit
  to an approach and wants it stress-tested first. Trigger on: "shape this", "red-team
  this design", "pre-mortem this plan", "pressure-test this approach", "poke holes in this
  before I build it", "what am I missing", "stress-test this direction", "is this the
  right approach", "review this design/RFC/proposal", "what could go wrong with X". NOT
  for critiquing code that already exists (that is `review`), and NOT for executing a
  decided plan (hand the brief to whoever will build it).
---

# Shape

Kill the bad version of an idea on paper, before it costs a build. `/shape`
takes a *proposed* direction — an approach, an RFC, an architecture sketch, "I'm
going to do X" — and runs it through a parallel adversarial review whose entire
job is to surface what the proposer, mid-commitment, cannot see. The deliverable
is a single ranked brief of the things worth resolving *before* the first line
of code, not a code change.

**This skill mutates nothing.** No commits, no branches, no files in the user's
repos, no PRs. Sub-agents are read-only. The one and only artifact is the
synthesized brief you hand back to the user (printed inline; optionally saved to
a scratch path if the user asks). Say so if the user seems to expect a build —
`/shape` ends *before* implementation begins.

## When it fits (and when it doesn't)

- **Fits:** a tentative direction is chosen but not committed — the exact moment
  optimism bias and sunk-cost pressure are highest and dissent is cheapest. An
  RFC awaiting sign-off. A "here's my plan, poke holes" request. A fork in the
  road between two approaches.
- **Doesn't fit:** code already exists and you want its quality assessed → run
  `review` on the current diff, or inspect the existing code directly. A
  decision is already made and you want it *shipped* → hand the brief to whoever
  will build it. A single obvious question with a cheap answer → just answer
  it; don't spin up five agents.

## Phase 0 — nail the proposal before fanning out (main loop, cheap)

The five agents will each re-interpret a vague proposal differently and their
findings won't compose. So the main loop first pins down a **shared frame** all
of them receive verbatim:

1. **State the proposed approach in 3–6 sentences.** What is being built/changed,
   the core mechanism, and the shape of the intended solution. Paste any RFC /
   design note / ticket text the user provided.
2. **State the goal and the constraints.** What success looks like; hard limits
   (deadline, runtime, budget, must-run-headless, must-stay-backward-compatible,
   team size). A failure mode is only a failure *relative to a goal* — name it.
3. **State what's explicitly in and out of scope.** Prevents agents from
   red-teaming a strawman the user already ruled out.
4. **Gather cheap ground truth, don't make agents rediscover it.** If the
   proposal touches an existing codebase, do a quick main-loop recon (the
   relevant files, the current architecture, the manifests) and paste the
   findings into the frame. Agents reason about the *actual* system, not a
   guessed one — but they stay read-only and never assume anything the frame
   didn't establish.

If the proposal is too thin to frame (no goal, no mechanism), ask the user 2–3
sharpening questions *first*. A red-team of a strawman is worse than useless — it
manufactures false confidence.

## The five lenses (fan out in parallel)

Spawn **one read-only sub-agent per lens, all in a single message** so they run
concurrently. Each gets the identical Phase-0 frame plus its own charter and a
required structured return. Each is told: *you are read-only — investigate the
existing code if the frame points to a repo, but change nothing and open
nothing.* Give each a crisp lens so they don't all converge on the same generic
worries.

1. **Unknowns / open questions.** What must be *known* before this is safe to
   build but currently isn't? Unvalidated assumptions ("we assume the API is
   idempotent"), unanswered questions, ambiguities in the spec, things stated as
   fact that are actually guesses. Each item framed as a question with a concrete
   way to answer it (a spike, a doc to read, a person to ask, a measurement to
   take).
2. **Edge cases.** Where does the happy path stop being happy? Empty/null/huge
   inputs, boundary values, concurrency and races, partial failures, unusual but
   legal states, the zero/one/many transitions, time zones and clock skew,
   Unicode, permissions. For each: the trigger, what the proposed approach does
   today (or fails to), and whether it's a blocker or a note.
3. **Alternatives + tradeoffs.** What are the 2–4 credible *other* ways to do
   this, including "do nothing" and "buy/adopt instead of build"? For each, the
   axis on which it beats and loses to the proposal (complexity, time, blast
   radius, reversibility, operability). End with an honest read on whether the
   proposed approach is actually the right one, or whether an alternative
   dominates it on the stated constraints.
4. **Failure modes (pre-mortem).** *Prospective hindsight:* assume it's N months
   later and this shipped and failed — enumerate the specific reasons why.
   Silent-corruption paths, the ways it degrades under load or scale, operational
   failure (nobody can debug it at 3am, no rollback), the second-order effects,
   the "works in the demo, dies in prod" traps. Rank each by plausibility ×
   severity — that product, not raw scariness, is what matters.
5. **Hidden dependencies.** What does this quietly rely on that isn't in the
   plan? Upstream services/APIs and their SLAs and rate limits, library/version
   and runtime assumptions, data migrations, config and secrets, infra and
   deploy-order coupling, other teams/repos that must move in lockstep, implicit
   ordering, license/compliance constraints. For each: what breaks it and how
   load-bearing it is.

Give each agent this return contract so synthesis is mechanical:

> Return a list of findings. For **each finding**: (a) a one-line title;
> (b) 1–3 sentences of substance; (c) **severity** BLOCKER / MAJOR / MINOR;
> (d) **the cheapest next action** that resolves or de-risks it (a question to
> answer, a spike to run, a decision to make, a mitigation to add). End with the
> **single most important thing** in your lens. Be concrete and specific to THIS
> proposal — no generic best-practice boilerplate. If your lens surfaces nothing
> real, say so plainly rather than padding.

Model tiering: run the lenses at Opus-class (this is judgment work). For a
lightweight proposal the main loop MAY run the five lenses itself sequentially
instead of spawning agents — but the parallel fan-out is the default and gives
genuinely independent views (five fresh contexts don't anchor on each other).

## Synthesis — the ranked "resolve-these-first" brief (main loop)

The agents surface; **the main loop judges.** Do not just concatenate the five
reports — that is a pile, not a brief. Instead:

1. **Deduplicate and cluster.** The same root risk often shows up in three
   lenses (an unstated assumption that is also a failure mode that is also a
   hidden dependency). Merge those into one item — and note that convergence,
   because a risk three lenses independently found is a strong signal.
2. **Rank by resolve-first value**, i.e. severity × how much it would cost to
   discover *after* building. A cheap-to-answer unknown that would invalidate the
   whole approach ranks at the very top: high leverage, low cost. A minor edge
   case with an obvious guard ranks low.
3. **Emit the brief** in this shape:

   - **Verdict (one line).** One of: *Proceed* — no blockers, minor notes only;
     *Proceed with changes* — fold these specific mitigations in before starting;
     *Resolve first* — open questions/risks must be closed before building is
     wise; *Reconsider* — an alternative appears to dominate, or a failure mode
     is fatal.
   - **Resolve-these-first (the ranked list).** The top items, each with:
     what it is, why it's ranked here, the severity, and **the one cheapest
     action** that resolves it. This is the part the user acts on.
   - **Open questions to answer before building.** The unknowns, each paired
     with how to answer it (spike / doc / person / measurement).
   - **Alternatives worth a second look** (only if the alternatives lens found
     one that plausibly dominates) — with the axis on which it wins.
   - **Noted, not blocking.** The minors, briefly, so they're captured but don't
     drown the signal.

4. **Recommend the next move.** Usually one of: answer questions Q1–Q3 then
   re-shape; fold mitigations M1–M2 into the approach and hand it to whoever
   will build it; or step back and prototype the risky alternative first. Name
   it explicitly — the brief should end with a decision, not a shrug.

Keep the brief tight. A red-team that returns 40 undifferentiated worries has
failed at its actual job, which is *prioritization*: telling the user the three
things that matter before the thirty that don't.

## Safety rails

- **Read-only, always.** No agent commits, branches, opens a PR, edits a file in
  the user's repos, or runs anything with side effects. If a lens needs to
  *prove* something empirically, it may run throwaway experiments in a scratch
  dir only — never against the real system, never mutating state.
- **Frame a real proposal, not a strawman.** If the input is too vague to
  red-team fairly, sharpen it with the user first. Attacking a misread of the
  plan produces false confidence, the exact failure this skill exists to prevent.
- **Adversarial, not cynical.** The goal is a *buildable* version of the idea,
  not a body count. Every finding carries a cheapest-next-action so the output is
  a path forward, not just a list of ways to fail.
- **Prioritize ruthlessly.** The value is the ranking. Surface the vital few,
  demote the trivial many; never bury a blocker under boilerplate.

## Prior art

Distilled from established practices for parallel review:
Gary Klein's **pre-mortem** / prospective-hindsight technique (imagine the plan
has already failed, then enumerate why — HBR 2007; the failure-modes lens);
**adversarial design review / red-teaming**, which challenges an approach's
fundamental assumptions and scrutinizes its interfaces and integration points for
failure before commitment (the unknowns, edge-case, and hidden-dependency
lenses). The five read-only agents each examine one concern, return structured
findings, and leave the final ranking to the main loop.

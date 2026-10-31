---
name: humanizer-finance
kind: portable
version: 1.0.0
description: >
  Thin financial-register overlay for the humanizer skill. Use this INSTEAD of bare
  humanizer when the text is a financial or market-analytical digest — earnings notes,
  macro/rates commentary, portfolio updates, investment research, or similar dense-data
  analytical prose. Trigger on phrases like "humanize this digest", "make this market
  update sound human", "run humanizer-finance", "clean up this financial digest", or
  when humanizing text where the document type is financial/investment-analytical. If
  the text isn't financial/analytical, use the bare humanizer skill instead — this
  overlay adds nothing useful there. NOT for general prose, and NOT a replacement
  for `humanizer`, which it builds on, and which must be installed beside it.
---

# Humanizer — finance overlay

This is a thin overlay on the `humanizer` skill, which must be installed beside
it. Read `${CLAUDE_SKILL_DIR}/../humanizer/SKILL.md`; if it is missing, stop
before doing anything else and print `./install.sh humanizer`, to be run from
the clone of the skills repository (the plugin install includes it); never run
it yourself. Apply that skill in full first: its hard constraints, its editing/generation/
detect-only modes, the pattern catalog, "what human sounds like," and "what not to
flag" all apply unchanged. This file does not restate any of that. It adds exactly
three things on top, scoped to financial/market-analytical prose: a concrete
finance-terminology carve-out, data-density norms for dense figures, and the digest
voice.

Nothing here overrides a CORE hard constraint. If anything below appears to conflict
with a CORE rule, the CORE rule wins — escalate rather than silently reinterpreting it.

## 1. Finance-terminology carve-out

This makes the CORE's domain-terminology guidance concrete for this register. None of
the following are AI tells or jargon to strip — they're this register's normal,
precise vocabulary:

- Rate/curve language: basis points (bp), duration, convexity, the 2s10s spread, term
  premium, real yields
- Positioning/flow language: net length, dealer gamma, put/call skew, open interest
- Standard qualifiers: year-over-year, sequential, on a trailing basis, ex-items
- Macro shorthand: headline vs. core, base effects, the print, guidance

Flag these only when used incorrectly or as unexplained filler for a reader who
wouldn't recognize them — not because they sound technical. Generic business
buzzwords (synergy, holistic, robust) are still tells in this register; finance terms
of art are a different category entirely.

"Leverage" needs a closer look, since it's easy to miss right in this section: as a
noun for capital structure (financial leverage, operating leverage, net leverage, a
leverage ratio) it's genuine finance terminology, not a tell — the carve-out above
protects it. As a generic verb ("leverage our data," "leverage synergies"), it's the
same buzzword the CORE's vocabulary-cluster pattern already flags. Judge the part of
speech and the referent, not the word alone.

## 2. Data-density norms

Dense runs of figures, ratios, and yield-curve levels are normal and correct here —
don't treat a data-dense paragraph as robotic just because it's dense. Two things
separate a good dense passage from an AI-flattened one:

- **Every number still needs a load-bearing reason to be there** — a claim, a
  comparison, or a consequence, not a number for its own sake.
- **When the source material actually supports a real takeaway, give the dense run of
  figures a crux line** — one sentence, derived only from the figures actually given,
  saying what they mean going forward. This is what keeps a data-dense passage from
  reading sterile even when every number in it is accurate.
- **When the source doesn't support a takeaway, don't invent one to fill the gap.**
  The CORE's no-fabrication and evidence-boundary rules control here: if the material
  doesn't license a "here's why this matters" line, say so instead — flag the missing
  analysis (e.g., "no read yet on what this means going forward") rather than
  manufacture a forward-looking conclusion the source doesn't back.

## 3. The digest voice

The target register for finance/market digests: informed and direct, like an analyst
briefing a colleague who already follows the market — not a press release, not a
casual chat. These are register preferences, not new rules — the source material's
own structure, and the CORE's voice-sample calibration when the author supplies prior
digest prose, both take priority over the defaults below.

- **Digests tend to read better opening with the takeaway** — a level, a move, a
  call — with supporting data following, rather than the reverse. Apply this when the
  source's own structure allows it; the CORE's scope limit still applies here —
  reordering is restructuring, so don't impose this on a source that's already
  organized deliberately unless asked to.
- **This register tends to favor one point per section** over restating the same read
  on the data three different ways — a finance-flavored instance of the CORE's general
  judgment against padding, not a new rule.
- **Forward-looking market views usually read better attributed than asserted as
  fact** — "the strip is pricing a slower cutting path," "analysts expect," rather
  than stating an expectation as if it had already happened. This is this register's
  edge case of the CORE's evidence-boundary rule: a market's current pricing or
  expectation is real and citable, but it's a different category of claim from
  something that has already occurred.

## Output

Same output contract as the CORE skill — this overlay adds no new mode and doesn't
change the output format. Editing/generation/detect-only outputs from the CORE apply
unchanged; the "Changes:" bullets or tone note may cite the finance-specific fixes
above by name (e.g., "kept the yield-curve data dense but added a crux line after the
figures," or "left '2s10s spread' and 'dealer gamma' unflagged — correct terms of
art, not jargon").

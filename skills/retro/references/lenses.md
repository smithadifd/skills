# Lenses

Every reviewer applies all seven lenses. For each, ask what in the environment
(not the person) caused friction, and what evidence in the session shows it.

1. **Navigation.** Did the agent spend many reads or searches finding where
   something lives? Look for repeated greps for the same thing, wrong files
   opened first, and missing pointers. Fix: a short map or index in the
   instruction file, or a docs note, only if the layout is not self-evident.
2. **Automated checks.** Which failures did a tool report that a hook, lint,
   test or CI step could have stopped earlier, or that a check already exists
   for but was run late or not at all? Mechanical mistakes belong here. Fix:
   encode in structure.
3. **Coding standards.** Were conventions (naming, error handling, test style,
   dependency policy) discovered through correction rather than stated? Fix:
   a standards edit if a formatter or lint cannot enforce it; otherwise the
   check from lens 2.
4. **Instruction-file size.** Are instruction files long enough that rules get
   skipped, or do they repeat what code and checks already say? Fix: propose
   trimming, with the diff.
5. **Tool economy.** Did the session burn effort on slow, noisy or repeated
   tool calls (full test runs for a one-line change, huge outputs, retries)?
   Fix: a narrower command, a script, or a cached result.
6. **Information access.** Was something needed (a schema, a credential
   location convention, an API doc, a review comment) hard or impossible to
   reach? Fix: a docs note or a read-only tool, never a secret in a file.
7. **No-op instructions.** Which existing rules were ignored without
   consequence, restate a check, or guard a mistake that can no longer happen?
   Fix: propose removal with the diff.

## The bar

- A mistake class counts at its second occurrence. Cite both.
- Prefer the highest level that works: structure over a rule, a rule over a
  note, a note over nothing. Stop at the first level that fully prevents it.
- Mechanical means a tool can detect it from the file or command output alone.
- One finding per class. Merge near-duplicates from different lenses.
- A clean session produces "nothing met the bar", not filler.

## Severity

- **high:** caused repeated rework or reached review or CI before being caught.
- **medium:** cost the person a correction turn, or several wasted steps.
- **low:** minor friction; worth a line only if the fix is cheap.

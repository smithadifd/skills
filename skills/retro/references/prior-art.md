# Method notes

The design combines four practices, summarized here in our own words.

- **Severity-sorted environment retro.** Walk a session through a fixed set of
  lenses and produce one list ordered by how much each problem hurt. Nothing is
  written to disk unless the person asks for it. We keep the ordering and the
  lens set, and add a findings file so the result survives the chat.
- **Reflect with parallel reviewers.** Several independent readers examine the
  same session from different stances, a synthesizer merges and prunes their
  output, and any change to a skill or rule is shown as a diff that the person
  approves or rejects. We use three stances (process, tooling and environment,
  divergent) and an explicit approved flag in the findings file.
- **Correction rule.** Count a mistake class at its second occurrence, not its
  first. Fix it at the highest level that works: a mechanical check beats a rule
  that beats a note. Once the check exists and the mistake cannot recur, delete
  the rule that guarded against it. We encode the two-instance bar and the
  removal path as validation, not advice.
- **Compound step.** Record one learning, or say nothing met the bar. A clean
  session is a valid and common outcome, and padding the list is a failure.

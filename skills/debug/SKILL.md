---
name: debug
kind: portable
version: 1.0.0
description: >
  Find and fix the cause of a bug by reproducing it, testing a hypothesis,
  isolating the fault and fixing it behind a test that fails first. Use when
  the user says "why does this fail", "this test is failing", "debug this", or
  reports a crash, wrong output or regression. NOT for feature work or new
  behavior (use `tdd`, or delegate to the `builder-core` agent), and NOT for
  reviewing a diff that already exists (that is `review`).
---

Find the cause of a reported bug and fix it. Never claim a fix without a test
that failed for the reported cause and then passed.

## 1. Reproduce

Run the exact command that shows the problem. Record the command and its
output verbatim, including the exit code. If it does not reproduce, say so and
ask for the missing detail (input, environment, version) rather than guessing.
Until it reproduces, nothing below applies.

## 2. Hypothesize

State one hypothesis in a sentence, and the observation that would prove it
wrong. Example: "The cache key ignores the locale; if so, two locales will
return the same entry." Prefer the hypothesis the evidence already points to
over the one that is easiest to check. Read the error and its stack first.

## 3. Isolate

Narrow until one cause remains, using whichever fits:

- Bisect the history when it used to work: `git bisect start <bad> <good>`,
  then `git bisect run <command>` (the command exits 0 when good, 1 to 127
  except 125 when bad). Always finish with `git bisect reset`, including
  after an interrupted run.
- Instrument: add a temporary log or assertion at the suspected boundary and
  compare actual against expected values.
- Reduce the input or the setup to the smallest case that still fails.

Run the check the hypothesis predicts. If the observation contradicts it,
drop the hypothesis, note it as ruled out, and form the next one. Change one
thing at a time. Remove temporary instrumentation before the fix.

## 4. Fix behind a failing test

1. Write the smallest test that fails because of the reported cause, not for
   an unrelated reason. Run it and keep the red output.
2. Make the minimal change that addresses the cause. Do not fix symptoms, and
   do not bundle refactors or unrelated cleanups.
3. Run the same test again and keep the green output.
4. Run the wider suite to catch regressions.

If a test cannot be written (no harness, timing, hardware), say why, and give
the manual before-and-after evidence instead. Do not call that a verified fix.

## 5. Report

Keep it short:

- Cause: what was wrong and where (file and line).
- Evidence: the reproduction command, the red result, the green result.
- Ruled out: each hypothesis that was disproved and the observation that
  disproved it.
- Not verified: anything the tests do not cover.

## Stop rules

- Two failed hypotheses in a row: re-read the error and the code path from the
  start before forming a third.
- The fix needs a design change or touches unrelated behavior: stop and
  report it rather than expanding scope.

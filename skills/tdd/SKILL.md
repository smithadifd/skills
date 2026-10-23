---
name: tdd
kind: portable
version: 1.0.0
description: >
  Test-first discipline for changing behavior directly in the main thread. Use
  when you are about to write or change behavior yourself, such as "add this
  feature", "fix this bug", "make it do X", or "change how Y works", and no
  builder subagent is doing the work. Write the failing test first, see it
  fail for the right reason, then make it pass. NOT for delegated builds (a
  builder agent carries this rule already), and NOT for docs, comments, or
  configuration that has no checker.
---

A passing suite is only evidence if it would fail without your change. Apply this whenever you write or change behavior yourself. You do not need to be asked.

## The loop

Work in one vertical slice at a time: one small behavior, from the test through the code that satisfies it.

1. **Red.** Write one test for the next behavior. Run it. It must fail, and for the right reason: the missing or wrong behavior, not a typo, an import error, or a broken fixture. If it fails for another reason, fix the test first.
2. **Green.** Make the smallest change that passes. Do not add behavior the test does not ask for.
3. **Refactor.** With the suite green, tidy the code you just touched. Run the suite after each step. If it goes red, undo the last step.
4. **Repeat** with the next slice until the work is done.

A test that passes before you change the code is not evidence. It means the behavior already exists, or the test checks nothing. Find out which before you go on.

## Bug fixes

Reproduce the bug as a failing test before touching the code. The test, not your reading of the code, is what proves the cause.

## Rules

- Do not add test-only switches, flags, or branches to production code to make it testable. If the code cannot be tested without one, say so and stop.
- Do not skip, disable, or loosen an existing test to get green.
- Keep each slice small enough that a red result points at one cause.
- When no test is practical (docs, configuration with no checker), say so plainly. Do not present a green run as proof.

## Before you finish

The red run happens first, against the unchanged code, so the loop already produced the proof. Never commit, restore files, or otherwise modify the person's checkout to prove it. Do not use `git stash`.

If you need a revert proof after the fact, do it in a scratch copy. Take the base, the commit the change started from, as an exported copy outside the checkout (`git worktree add <tmp> <base>`, or `git archive <base> | tar -x -C <tmp>`). Copy the new tests there, run them, and see at least one fail. Remove the scratch copy when done.

Then run the repo's full test suite and its lint or build steps in the real checkout.

## Handing off the evidence

Give the reviewer the red-then-green pair for each behavior:

- the exact test command, and the failing output with the change reverted;
- the passing output with the change in place;
- a short table of each reverted change and the test that failed.

A reviewer can rerun the pair. A bare "tests pass" gives them nothing to check.

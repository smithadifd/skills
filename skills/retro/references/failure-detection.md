# retro `scan`: how a check run counts as failed

## Problem

`scan` groups repeated mechanical mistakes (the same lint or test failure fixed by hand twice) so `retro` can propose encoding them as a check. Earlier versions decided whether a run failed by reading its summary text ("2 errors", "Errors: 0, Failures: 2", "No errors or failures"). Summary lines have no closed grammar, and each fix to the matcher broke another wording.

## Rule

A tool run counts as a **failed check run** only when the transcript records the failure itself:

1. The tool result is marked as an error by the harness (`is_error: true` on the `tool_result` block), **and**
2. its content starts with the harness's exit-status line, `Exit code <N>`, with `N` a non-zero integer. That line is what distinguishes a command that ran and exited non-zero from a refusal before it ran.

Nothing else counts:

- A result with `is_error` false or absent is a pass, whatever its text says ("FAILED", "3 errors").
- An error result without the exit-status line (a hook or permission refusal, an interrupted call, a tool other than a shell) is not a check run at all and is ignored.
- A transcript format with neither field is not scanned for failures; `scan` says so in one line and still lists the person's correction turns.

The summary text is never parsed for pass/fail.

## Grouping (unchanged in intent)

Within failed check runs only, `scan` extracts rule identifiers (lint codes such as `E501`, `F401`, test node ids) from the output and groups by `(tool, identifier)`. A group seen in two or more distinct failed runs is a mechanical candidate. A failed run with no identifier groups under `(tool, "unclassified")`, which is reported but never becomes a candidate on its own.

`tool` is the command word (`ruff`, `pytest`, `eslint`, ...) of the last command in a simple `&&`/`;` chain, after skipping variable assignments, a leading `python`/`python3`/`python3.N` with its interpreter options before `-m` (flags such as `-I` or `-IB`, and `-W`, `-X` or `--check-hash-based-pycs` with an operand or attached) so `python -I -m pytest` is `pytest`, and the wrappers `env`, `npx`, `command`, `time` and `sudo` with their options (including the operand of an option that takes one, as in `env -u NAME`). Quoting is not parsed. Each instance's ref carries the tool-use id, so parallel runs in one record stay distinct.

## Known limit

A command whose exit status is hidden by a pipe (`pytest | tail`) exits with the last stage's status, so its failure is not counted. This fails toward under-counting (a missed candidate) and never toward a false one. The SKILL.md says so.

## Inputs and invariants

- Inputs: the transcript JSONL events; per `tool_result`: `is_error`, content text; the matching `tool_use` input `command`.
- Invariant: a candidate exists only if at least two distinct `tool_result` blocks each satisfy rule 1 **and** rule 2 and share `(tool, identifier)`.
- Fail-closed default: missing or malformed fields mean "not a failed check run".

## Tests

- Passing runs whose text says "errors"/"failures" in any wording (including "No errors or failures", "Errors: 0, Failures: 0") are never failures (is_error false).
- Two failed runs (is_error true, `Exit code 1`) sharing a lint code produce one candidate; one failed run does not.
- A hook refusal (is_error true, no exit line) is ignored.
- A failed run with a mixed summary ("Errors: 0, Failures: 2") counts because of rule 1–2, not its text.
- A transcript with no `is_error` fields produces no candidates and the one-line notice.
- Mutation targets: accept is_error without the exit line; parse summary text again; count one run as a candidate.

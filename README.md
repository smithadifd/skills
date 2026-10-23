# Skills and agents for Claude Code

This repository contains reusable Claude Code skills for common tasks and agents for focused research, writing, and review. Install the skills and agents you need, or install the full collection. `humanizer-finance` also needs `humanizer`.

## Before you start

You need Claude Code, `git`, and `bash`. Python 3 is required for `handoff`, `kickoff`, `memory-gardener`, `pickup`, `sharpen`, and `topic`, which run bundled Python scripts. It is also needed for `review`'s optional structured output. The `pr` skill needs the GitHub CLI (`gh`).

This is a private repository. If you've been invited as a collaborator, clone it using the URL provided with your access, then run the commands below from the clone:

```sh
git clone 'your-private-clone-url' skills
cd skills
```

## Skills

| Skill | What it does |
| --- | --- |
| `debug` | Finds the cause of a bug by reproducing it, testing a hypothesis and isolating the fault, then fixes it behind a test that fails first and passes after. |
| `handoff` | Writes one redacted, size-capped resume note so a fresh session can continue the work. |
| `humanizer` | Revises prose to sound natural, or points out robotic phrasing in a draft. |
| `humanizer-finance` | Applies the writing review to market and financial analysis while preserving useful technical terms and figures. |
| `kickoff` | Turns an early project idea into a researched plan through questions and review points. |
| `memory-gardener` | Reconciles a Claude memory directory. It reports stale, duplicated or oversized notes first, and only applies changes after you review the report. |
| `pickup` | Resumes from a handoff note, lists its verified commands, reruns only the ones you approve, and demotes any that fail or are not rerun. |
| `pr` | Prepares a pull request for the current branch using the project's conventions. |
| `review` | Checks code changes for bugs and security problems. |
| `shape` | Tests a proposed design for risks and missing considerations before implementation. |
| `sharpen` | Asks focused questions to clarify requirements and records the answers in a brief. |
| `tdd` | Applies a test-first loop when you change behavior yourself: a failing test, the smallest passing change, then a refactor, with the red-then-green evidence handed to the reviewer. |
| `teach` | Explains how a codebase works with a system overview, component tour, and traced example. |
| `topic` | Keeps the notes for an ongoing, focused discussion in one file. |

## Agents

| Agent | What it does |
| --- | --- |
| `api-contract-tracer` | Follows an API endpoint from its handler to its callers to find mismatched expectations. |
| `builder-core` | Builds one ticket in its own branch, following the repo's conventions, and opens a draft pull request. |
| `dependency-auditor` | Compares dependency versions across projects and flags differences. |
| `doc-researcher` | Gathers code facts and examples for a documentation draft. |
| `doc-verifier` | Checks specific documentation claims against the code. |
| `doc-writer` | Writes a documentation page from a research brief. |
| `researcher` | Answers a focused question about code or an external API. |
| `reviewer` | Independently reviews a change and checks that its tests would fail without it. |

### Agents or the built-in Explore and Plan agents

Use the built-in Explore agent for open-ended reading of a codebase, and the built-in Plan agent (or Plan mode) to outline the steps for a change. Reach for these agents when the job has a fixed contract the built-ins do not give you:

- `builder-core` builds one ticket in its own worktree under a floor of safety rules (draft PR only, never merges) and a turn cap.
- `reviewer` reviews with no prior context and backs its verdict with what it ran; raise its effort per spawn for an adversarial review.
- `researcher`, `doc-researcher`, `doc-verifier` and `api-contract-tracer` return a structured answer to a narrow question, where Explore returns free-form notes.

## Install

There are two ways to install: as a Claude Code plugin, or with `install.sh`. Use one, not both.

### As a plugin

From inside your clone, add the marketplace and install the `sdlc` plugin, which carries every skill and agent:

```sh
claude plugin marketplace add .
claude plugin install sdlc@skills
```

A plugin install prefixes every command with the plugin's name, so a command that this README writes as `/review` is invoked with that prefix.

### With `install.sh`

The installer copies skills and agents into `~/.claude/` with no plugin involved. It needs no marketplace.

Install one skill:

```sh
./install.sh pr
```

For financial writing, install both `humanizer` and its `humanizer-finance` extension:

```sh
./install.sh humanizer
./install.sh humanizer-finance
```

The optional HTML output in `teach` uses a separate `artifact-design` skill. Its standard chat walkthrough does not need that skill.

Install every available skill:

```sh
./install.sh --all
```

Install one agent:

```sh
./install.sh --agent reviewer
```

Install every available agent:

```sh
./install.sh --all-agents
```

Skills are copied to `~/.claude/skills/`. Agents are copied to `~/.claude/agents/`.

## Update

For a plugin install, run `claude plugin update sdlc`. For an `install.sh` install, after pulling changes in your clone, rerun the install command for a skill or agent. The installer replaces the installed copy. Use `./install.sh --all` and `./install.sh --all-agents` to refresh everything.

## Uninstall

Remove one skill:

```sh
./install.sh --uninstall pr
```

Remove every available skill:

```sh
./install.sh --uninstall --all
```

Remove one agent:

```sh
./install.sh --uninstall --agent reviewer
```

Remove every available agent:

```sh
./install.sh --uninstall --all-agents
```

## License

See [LICENSE](LICENSE).

# Skills and agents for Claude Code

This repository contains reusable Claude Code skills for common tasks and agents for focused research, writing, and review. Install the skills and agents you need, or install the full collection. `humanizer-finance` also needs `humanizer`.

## Before you start

You need Claude Code, `git`, and `bash`. Python 3 is required for `kickoff`, `memory-gardener`, `sharpen`, and `topic`, which run bundled Python scripts. It is also needed for `review`'s optional structured output. The `pr` skill needs the GitHub CLI (`gh`).

This is a private repository. If you've been invited as a collaborator, clone it using the URL provided with your access, then run the commands below from the clone:

```sh
git clone 'your-private-clone-url' skills
cd skills
```

## Skills

| Skill | What it does |
| --- | --- |
| `humanizer` | Revises prose to sound natural, or points out robotic phrasing in a draft. |
| `humanizer-finance` | Applies the writing review to market and financial analysis while preserving useful technical terms and figures. |
| `kickoff` | Turns an early project idea into a researched plan through questions and review points. |
| `memory-gardener` | Reconciles a Claude memory directory. It reports stale, duplicated or oversized notes first, and only applies changes after you review the report. |
| `pr` | Prepares a pull request for the current branch using the project's conventions. |
| `review` | Checks code changes for bugs and security problems. |
| `shape` | Tests a proposed design for risks and missing considerations before implementation. |
| `sharpen` | Asks focused questions to clarify requirements and records the answers in a brief. |
| `teach` | Explains how a codebase works with a system overview, component tour, and traced example. |
| `topic` | Keeps the notes for an ongoing, focused discussion in one file. |

## Agents

| Agent | What it does |
| --- | --- |
| `api-contract-tracer` | Follows an API endpoint from its handler to its callers to find mismatched expectations. |
| `dependency-auditor` | Compares dependency versions across projects and flags differences. |
| `doc-researcher` | Gathers code facts and examples for a documentation draft. |
| `doc-verifier` | Checks specific documentation claims against the code. |
| `doc-writer` | Writes a documentation page from a research brief. |
| `planner` | Investigates a codebase and outlines the steps for a change. |
| `researcher` | Answers a focused question about code or an external API. |
| `reviewer` | Independently reviews a change and checks that its tests would fail without it. |

## Install

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

After pulling changes in your clone, rerun the install command for a skill or agent. The installer replaces the installed copy. Use `./install.sh --all` and `./install.sh --all-agents` to refresh everything.

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

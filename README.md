# Skills

Standalone skills installed by directory name from `skills/`. Standalone agents installed by file name from `agents/`.

## Install

Install one skill:

```sh
./install.sh pr
```

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


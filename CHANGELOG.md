# Changelog

Release notes for the `sdlc` plugin, newest first.

## 0.12.0

This is the first tagged release (repository tag v1.2.0) that carries the session-start hook, the shared `_lib` folder and the ledger features listed under 0.11.0. The 0.11.0 notes below were written but never tagged; the previous tag, v1.1.1, is plugin 0.10.0 and has no hook.

### Session start reports ledger failures and supplies write identity

When `ledger_cli` is configured, the `SessionStart` hook now prints one bounded `ledger session-start: ...` line when `resume --brief` exits non-zero, cannot run or times out; it still exits 0 and never blocks session start. When the brief is non-empty it prints the ledger version line (when the CLI's version call succeeds) and then the brief; an empty brief prints nothing. The session environment gains `LEDGER_HARNESS=claude-code` when unset and `LEDGER_MODEL` from a non-empty hook input model id when unset, alongside the existing `LEDGER_SESSION` and `LEDGER_AGENT`; values are shell-quoted. `LEDGER_SESSION` is always written from a non-empty input `session_id`; `LEDGER_AGENT`, `LEDGER_HARNESS` and `LEDGER_MODEL` are left alone when already set. `LEDGER_OPERATOR` remains operator configuration and is not set by the hook.

### `ticket` uses the ledger by default when one is installed

With `TICKET_LEDGER_CLI` unset or empty, `ticket` now looks for a `ledger` on your `PATH`. It uses the ledger store when `LEDGER_DB` names an existing database and the CLI passes the same capability check as before. In every other case (no `ledger` on `PATH`, no usable `LEDGER_DB`, a `ledger` that fails the check) it keeps the markdown store; it does not refuse. `TICKET_LEDGER_CLI=none` keeps the markdown store whatever is installed, and a CLI you name in `TICKET_LEDGER_CLI` is still refused when it fails the check. The first line of the open card now reads `store: ledger (<reason>)` or `store: markdown (<reason>)`. `import-cards` and `release` choose the same way, so a machine that flips to the ledger on its own is told by the open card's notice to import its existing cards.

### Carrying existing ticket cards into the ledger

`ticket` has two new commands for a machine that ran it before switching to the ledger. `ticket.py import-cards <repo>` writes each `.ticket/<key>.md` card into the ledger as one item and moves the card to `.ticket/imported/<key>.md`; an open card keeps its pass so it resumes from the ledger, a card it cannot read is left in place and named, and running it again writes nothing. `ticket.py find-cards` lists the repositories that still hold cards. When a ticket opens on the ledger and the repository still holds cards, the open now prints the exact import command once.

### Upgrading

1. Update the plugin from the marketplace, then start a new session (or run `/reload-plugins` in an open one) to load the new version:

   ```sh
   claude plugin marketplace update skills
   claude plugin update sdlc@skills
   ```

   If you installed with `install.sh` instead, pull the repository and run `./install.sh --all` again; the session-start hook is part of the plugin only.
2. Upgrade the ledger CLI itself, with the install or upgrade command from the ledger project's own README.
3. Add this `env` block to `~/.claude/settings.json` (merge it with any `env` block already there):

   ```json
   {
     "env": {
       "LEDGER_DB": "/absolute/path/to/home/.local/share/ledger/<system>.db",
       "LEDGER_HARNESS": "claude-code",
       "LEDGER_OPERATOR": "<your name>"
     }
   }
   ```

   `LEDGER_DB` must be an absolute path; do not rely on `~` being expanded in the settings file. `LEDGER_HARNESS` is optional, because the hook supplies `claude-code` when it is unset. `LEDGER_OPERATOR` is the human the writes are attributed to, and the hook never sets it.
4. Delete any ledger instructions you copied by hand into `~/.claude/CLAUDE.md`: the session-start hook now prints the ledger's own contract at the start of every session, and hand-copied prose goes stale and contradicts it.
5. Check it: a new session's first lines show `ledger <version>` and the brief, or one `ledger session-start: ...` line naming what failed.

## 0.11.0

Three skills now keep what they produce in your ledger, and the plugin tells each new session where the work stands. All of it is off until you set one key. With the key unset, every skill and the plugin behave exactly as they did in 0.10.0, and the new session-start hook prints nothing.

### The switch

The key is `ledger_cli` in the `topic` skill's config: put it in `~/.claude/skill-config/topic.json` (it overrides the shipped `skills/topic/config.json`). Set it to the path or name of your ledger command. Unset, null or empty means off. There is no second setting.

### What changes when it is on

- **sharpen** links the brief it wrote to the open topic item. When you ratify the brief, it records your words and an election that quotes them, instead of a plain note. If you ratify with a bare click and no words, it records nothing, because there are no words to quote.
- **shape** now always saves its ranked brief as `shape-<slug>-<date>.md` in the same folder sharpen writes to, so it outlives the session. With the ledger on, it also links the file to the open topic item.
- **topic** ends its close with the ledger's own resume summary (`resume --item <id>`) as the last line.
- **Session start.** A new hook runs the ledger's `resume --brief` and prints what it returns, so the session opens with the current state. If the ledger command is missing, is older than v0.1.1 (no `resume`), or fails, the hook prints nothing and never blocks a session. It also adds `LEDGER_SESSION` (the session id) and, unless you already set it, `LEDGER_AGENT=claude-code` to the session's own environment file. It writes no other file and never touches your settings.

The `resume` verb, used by the session-start hook and the topic close, needs ledger v0.1.1 or later.

### Also in this release

- A small shared folder, `skills/_lib`, now holds the code that finds the ledger command. It is not a skill. `install.sh` installs it beside the skills.
- `topic` reads the same `ledger_cli` key as before; only where the reading lives has changed.

### Upgrading

From the marketplace:

```sh
claude plugin marketplace update skills
claude plugin update sdlc@skills
```

Then start a new session (or run `/reload-plugins` in an open one) to load the new version. If you installed with `install.sh` instead, pull the repository and run `./install.sh --all` again; the session-start hook is part of the plugin only.

# Changelog

Release notes for the `sdlc` plugin, newest first.

## Tags and plugin versions

The headings below are plugin versions, the number `claude plugin list` shows for `sdlc@skills`. A repository tag is a different number: tag v1.3.0 installs `sdlc` 0.13.0. This table gives each tag's plugin version, read from `.claude-plugin/plugin.json` at that tag.

| Repository tag | `sdlc` plugin version | Entry below |
|---|---|---|
| v0.1.0 to v0.9.2 | none: these tags have no plugin manifest, and install with `install.sh` only | none: this file starts at v1.2.0 |
| v0.9.3 to v0.9.5 | 0.9.2 | none |
| v0.10.0 to v1.1.1 | 0.10.0 | none |
| v1.2.0 | 0.12.0 | 0.12.0 |
| v1.3.0 | 0.13.0 | 0.13.0 |
| v1.4.0 | 0.14.0 | 0.14.0 |
| v1.5.0 | 0.15.0 | 0.15.0 |

Plugin 0.11.0 was never tagged; its entry describes work that first shipped in 0.12.0 (tag v1.2.0). Only tags from v1.2.0 on have an entry, because this file did not exist at earlier tags.

## Unreleased

### The ticket pass writes its decisions and its builder as item facts

On the ledger store, each recorded decision now also lands a `decided:` item note, the next element of the same batch, under its ordinary key plus `/decided`: `decided: <what was decided> (by <who>)` on one line (TL4 build row 5, CP1 3.5). The ticket-decision note beside it is unchanged, so the card and every reader of it are as before, and a retry of the batch writes the note once. `TicketPass.record_build(model, harness=, requested=, source=)` is new: while building, it writes who built the round as one item note under `row-item:builder:<item>:r<round>:<model>` with the text `built-by: harness=H model=M requested=R source=S round=N`, the grammar of the toolkit's `row-item.sh facts`, so after a decision and a build `facts` counts `decided` of at least 1, `builder` 1 and `builder_malformed` 0. It fails closed and raises nothing for a bad value: a result that names no served model, a value outside the grammar, or the markdown store writes nothing and returns `written: False` with the reason. The markdown store is unchanged. The ledger digest of `test_L2` moved, because a default pass to S6 records two decisions; the markdown digests did not. Section 6.2.5 of the design note gives the rules and section Q of the test matrix the tests; SKILL.md section 5 tells the pass to call `record_build` once the builder returns. No plugin or skill version changes.

### The ticket pass accepts its open hand-off controller's go

A settled role go now opens a plan or outward gate only when its `who` exactly
equals the controller derived from the card's current open hand-off.
`relayed_by` remains attribution, not authority; a person's go is unchanged.
`record_role_go` records one decision note at either stop under a fresh read's
revision, without an election or binding. It refuses absent, closed or unreadable
hand-offs, another role, or an answer for another plan revision, writing nothing
and keeping the pass live. No answer writes nothing: the pass adds no clock or
hold. A seat roll under the same controller keeps the go; a handback or a different
controller makes it count for nothing. Merge rules, declines and record shapes
are unchanged.

### The ticket skill opens a parent's hand-off

`ticket` now takes a ledger item a parent handed off (TL4 build row 3, the parent hand-off adapter). `ticket.handoff_source(key, implementations=)` is a fifth source, used only by a parent's adapter and not in the `/ticket <KEY>` order: the ledger-item source plus the hand-off's conditions. It reads the item's one open `loop.intent` of payload version 2, which ledger 0.7.0's loop writer records, for the controller and the three seams, and opens the pass with that seam set. Nothing records the controller: the ledger store derives a `handoff` field (controller, seams, operation id and event id) from that event on every read, and `ticket.seam_set(card)` returns its seams; a handback removes it on the next read. The open requires `holder=` and only joins the lease the loop holds under it: it writes one pass record saying `joined`, a resume writes nothing, and no lease, link, spec or loop event is ever written for the hand-off. A hand-off protects the holder name it was handed under, the holder of the item's latest claim before the hand-off: a pass whose own holder is that name (or whose hand-off's holder cannot be read) never releases a lease under it, while the hand-off is open or after the handback, a re-take under that name included, and on no end path (its own end, a resume's end through any source, or `ticket.py release` on a crashed pass). A pass whose lease was taken over and handed off under another name, and which takes its own name back after the handback, releases what it took back, and a pass on an item not handed off in its life releases a lease it took as before. Every end of a bound handle now decides from a fresh read of the item, never from what that handle took or joined, so an original handle that joined releases a lease a later handle took again, and a late handle never touches the next pass's claim. Refused by name with nothing written: `handoff_no_ledger`, `handoff_ledger_too_old` (a `--version` line below `ledger 0.7.0`, or one that cannot be read), `handoff_not_managed`, `handoff_unreadable`, `handoff_ambiguous` (two open hand-offs; never the newer), `handoff_intent_v1`, `handoff_no_intent`, `handoff_seam_unknown` (a seam naming an implementation the opener did not declare; never run as the default), `handoff_not_leased` (an item the pass would have to take the lease of), and the ledger-item source's refusals. Every other source on an item with an open loop intent of a version other than 1 is refused `handoff_source_required`. A stored markdown card carrying a `handoff` key is `card_unreadable`. A default open on every source makes the same ledger calls and writes the same bytes as before; the end of a pass that joined, or of an old-shape pass, makes the one fresh read it used to skip. SKILL.md gains the source's section and the fail-closed line (where a seam's name is not the default, the session does not run that seam's default), section 6.8 of the design note gives the rules, and section O of the test matrix the tests. Until the controller's go lands, a hand-off pass waits at the plan go for the person. No plugin or skill version changes.

### The ticket skill takes an ad-hoc ask and writes a tracker's address as a `url` link

`ticket` now takes a change that has no ticket (TL4 build row 3, the intake half; the parent hand-off adapter is a separate row). `ticket.AskTicketSource(ask, title=, description=, acceptance=)` drafts the person's ask into a ticket. Its item is `ask-<12 hex>` and its spec's source is `ask:<12 hex>`, the first 12 hex digits of the SHA-256 of the ask's text as written, so the same ask lands on the same item on every try, whoever asks; `ticket.ask_key(ask)` is the only way to the id, the source is pulled only under that key, and an open given the store of another item is refused `source_store_mismatch`. The drafted criteria are marked on the plan card (`Drafted from the ad-hoc ask ...`) and bind only through the plan go, recorded against them as for any ticket; a redraft with other criteria is a changed criterion, so the pass returns to `plan`. An empty ask (`ask_empty`, or `ask_not_utf8`), a draft with no one-line title, no body, no criterion or a criterion over more than one line (`ask_draft_invalid`), and an item with the ask's id that is not a ticket (`ask_item_not_ticket`) are refused by name before anything is written. A ticket with a `url` (a tracker key or a JSON file) now also gets that address as one `url` link on its ledger item, in the write that creates the item, or alone under the revision the open read when the item exists; an item that already links the address gets no second link, and an item with a `url` link to another address is refused `url_link_conflict` before anything is written. A ledger id and a ticket with no `url` behave as before. SKILL.md's description no longer says the skill is not for a change with no ticket (a throwaway question still belongs outside it), and section N of the test matrix and section 6.7 of the design note describe the adapters and their failure paths. No plugin or skill version changes.

### The ticket pass takes or joins the ledger lease

On the ledger store a new ticket pass now holds its item through the ledger's own lease instead of a `session.opened` claim (TL4 build row 2, claim half). Its one claim write, under `--expect-revision` of the revision it read, holds the lease (an `item.state` with status `claimed`, `owner` and `claim` both the holder, a UTC millisecond `expires_at` four hours out, and `prior_holder` when it replaces an expired lease) and a `ticket-pass:v2` pass record naming the pass, its holder and whether the lease was taken or joined. `TicketPass.open` takes `holder=`; with none the holder is `ticket-pass/<pass id>`. Another holder's live lease refuses the open as `lease_held` and writes nothing, a holder that is not a name is refused as `holder_invalid` before any ledger call, and a blocked or closed item is refused as `item_not_claimable`. A pass already leased to its holder joins the lease and never releases it; a pass that took the lease releases it at its end, in one write with the pass's end, only while a fresh read shows it still holds it, and `ticket.py release` follows the same rule. A joined pass whose resume takes the lease again, after its holder released it or it expired, has taken it and releases it at its end by the same rule. The holder an open names travels with that open's own claim or join, never kept on the store handle, so two opens racing on one handle each claim as the holder they named. A pass claimed in the old shape still holds the ticket against every new pass, takes no lease when it resumes, and another holder's lease refuses that resume. A closed item reads no live pass. The markdown store is unchanged. No plugin or skill version changes.

### The ticket skill names its build, review and outward seams

`ticket.py` now names the three seams of a ticket pass and the default implementation of each: `ticket.SEAMS` maps `build` to `builder-core`, `review` to `reviewer` and `outward` to `push-and-open-draft`, and `ticket.seam_set(card, *names)` returns the set for a card (an unknown seam name raises `TicketError` naming it). A card carries no seam record yet and a default pass writes none, so card bytes, the card token, ledger events, refusal codes and the resume output are unchanged; SKILL.md section 5 gains one paragraph that names the seams. Three new tests pin concurrent stops against the close: a stop landed before the close is refused `stale` on both stores, and a stop that arrives while the close is being saved is refused `stale` and leaves the close whole. Each is shown red under a mutation of the store's token or lock check. No plugin or skill version changes.

### `setup --replace` moves a pin to a new install of the ledger

`python3 ledger_cli.py setup --replace` replaces an existing `ledger_cli` that is an absolute path even when that path still runs, the case of a machine that moved from one install of the ledger to another (plain `setup` refuses it as `ledger_cli_setup_exists`, which now names `--replace`, and the manual workaround was to copy `topic.json` aside and clear the key). It resolves the new `ledger` and runs the compatibility check first, then replaces the value under the same lock, keeping every other key, and prints the old and the new path. A `ledger_cli` of `none` or a bare command name is a deliberate setting: `--replace` refuses it as `ledger_cli_setup_replace_not_path` and the file stays byte-identical, as it does for an incompatible `ledger`, one in Claude Code's plugin cache, or none on `PATH`. Plain `setup` is unchanged. The README's setup step and the `topic` skill describe it.

### README: `LEDGER_HARNESS` is set in Claude Code's settings

The convention changes from 0.15.0: Claude Code's settings `env` block carries `LEDGER_HARNESS=claude-code`, the shell profile keeps `export LEDGER_HARNESS=cli`, and nothing is cleared from the shell before Claude Code starts. The settings value wins over a `cli` inherited from the shell, as measured with Claude Code 2.1.328. The terminal route's two settings examples now include the key, and the uninstall list names it with the other environment keys. Docs only.

### README: the marketplace remove is only for an existing install

The tag route's `claude plugin marketplace remove skills` exits 1 with `Marketplace 'skills' not found` on a machine that never added the marketplace; the README now says the remove is only for an existing install. Docs only.

## 0.15.0

This is the release tagged v1.5.0 in the repository. It mainly carries the `ticket` skill closing kept local when the person declines the outward go, and the README's single `LEDGER_HARNESS` convention with its note on when the database is created. The sections below list every change.

### The ticket skill closes kept local on a declined outward go

When the person says no at the outward go, `/ticket` records their words as a ticket decision of its own (`record_outward_declined`), bound to the captured prompt the way a go is, and `close_local` closes the ticket kept local with nothing pushed. After the no, no outward step is allowed. Before the outward go the skill reads the repository's remotes (`outward_route`): with none it proposes no push or pull request and offers the kept-local close instead. Both closes take the `builder-core` worktree and name it in the close output without removing, moving or writing anything: the path, its branch (left in place, not deleted), what `git status` listed there, and the command `git worktree remove <path>` for the person to run, shell-quoted and never forced. A path that goes through a symbolic link is named with no command. A worktree under `.claude/worktrees/<name>` stays in the repository until the person runs that command. `builder-core` now reports the worktree it built in.

### README: one `LEDGER_HARNESS` convention, and when the database is created

The terminal route's settings examples no longer set `LEDGER_HARNESS`, matching the ledger's docs: leave it out of Claude Code's settings, because the ledger's plugin sets `claude-code` when it is unset; the shell block keeps `export LEDGER_HARNESS=cli`. The default-tier paragraph and step 2 now say that the first submitted prompt the ledger plugin's prompt capture does not skip creates the database when `LEDGER_DB` names a missing file, not only a ledger CLI write. Docs only; no skill, script or setting changed.

## 0.14.0

This is the release tagged v1.4.0 in the repository. It mainly carries the `ticket` skill binding a plan go and an outward go to the captured prompt, the README and this file mapping each tag to its plugin version, `setup` refusing to pin a ledger in Claude Code's plugin cache (with the README naming the no-pin route), changes to the README's Install section, and the README command that finds the installed skills folder. The sections below list every change.

### The ticket skill binds a plan go and an outward go to the captured prompt

After the ticket-decision note lands, `/ticket` runs `ledger elect` then one
`decision.recorded` citing the election, quoting an exact span of this
session's captured person prompt. The note stays the gate truth: a missing
session, no matching utterance, a refused elect or append, or the markdown
store reports `bound=False` and never says a ledger gate opened. The seat
passes the go words as a span copied from the prompt and reports the bound
election or the line `go recorded, not bound to a captured utterance`.

### The README and this file map each tag to its plugin version

A repository tag and the plugin version it installs are different numbers: tag v1.3.0 installs `sdlc` 0.13.0, and the changelog's headings are plugin versions. This file now opens with a table that gives each tag's plugin version, and the README's Install says the same where it tells the reader to pick a tag, so a reader comparing `claude plugin list` with a tag is not left to find the mismatch. The README no longer says every tag has a changelog entry: tags before v1.2.0 predate this file. Docs only; no skill, script or setting changed.

### Setup refuses to pin a ledger in Claude Code's plugin cache, and the README names the no-pin default-tier route

With the ledger's plugin installed, the four ledger-using skills find `ledger` on the session's PATH and `LEDGER_DB` in its environment with no `ledger_cli` pin. `python3 skills/_lib/ledger_cli.py setup` now refuses, exit 1 with one stderr line starting `ledger_cli_setup_plugin_command:`, when the resolved `ledger` path (made absolute) has a `plugins/cache/` folder segment under a `.claude` folder, and writes nothing: that path changes with every plugin update, so a pin would become `ledger_cli_moved`. A path that cannot be made absolute is not treated as in the cache. When the existing `ledger_cli` setting is a moved pin inside that cache and the `ledger` now on PATH is also inside it, setup removes that one key, keeps every other key, and prints that the stale plugin pin was cleared (exit 0). A fresh setup with no pin, a live pin, or a moved pin outside the cache still refuse as before. The README's "Using the ledger" names this default-tier route beside the existing four terminal steps and says to skip setup on it. The README's Install preface names the two routes (plugin route skips setup; terminal route keeps the four steps). The resolver's rules 1 to 3 are unchanged.

### The README's Install section prints the newest tag, opens with the ledger setup steps, and covers a missing settings file

The README's Install now prints one command that gives this repository's newest release tag, so the tag after `#` no longer has to be found on the tags page by hand. The four ledger setup steps are the first thing Install says, followed by the two marketplace commands that are step 4, so a reader starting there is not sent down the page first. Step 2 now says to create `~/.claude/settings.json`, and the `~/.claude` folder, as a whole file when the settings file does not exist yet, and shows that file. Docs only; no skill, script or setting changed.

### The README prints the command that finds the installed skills folder

Step 4 of the README's setup steps now prints one command that reads the `installPath` recorded for `sdlc@skills` in Claude Code's installed-plugins file and appends `/skills`, then shows the setup step run with its output, so a plugin install no longer needs the cache's version folders listed by hand; when the file records more than one install, it takes the last one whose scope is `user`, or the last one listed if none has that scope. Docs only; no skill, script or setting changed.

## 0.13.0

This is the release tagged v1.3.0 in the repository. It mainly carries the ledger-on path for `ticket` (a ledger item can be the ticket, and the open records the ticket as the item's spec), `setup` pinning the ledger CLI's absolute path, the session brief moving from this plugin to the ledger's own plugin (this plugin no longer declares a hook), and the README's one setup order. The sections below list every change.

### The ticket skill states the pass id, shows its claim on the plan card, and says when orient and research start again

Three findings from a walk of the ticket skill as a newcomer. The last message of a session now ends with a `Pass: <id>` line (SKILL.md, "The final message"), and at a go stop adds how to resume with it, so a person can continue the same pass the next day. The rendered plan card names the live claim beside the plan, on a `Claim: pass <id>, host <host>, started <time>` line under the title; it is not shown once the pass has ended, nor on a stopped or closed ticket. A resume with no plan on the card repeats orient and research, because they write nothing to the card, and `reconciliation` now says so (`no plan is stored for this ticket, so orient and research start again: ...`) when the card shows an earlier pass or the open joined a live pass; a first open on a blank card prints nothing. This does not record how far a pass got: that would need a write at each of those steps and so a change to the store contract.

### The README's setup path needs no hand edit

- The README's setup steps are one ordered list that matches the ledger's own docs: install the ledger CLI at a tag, set the environment (`LEDGER_DB` as an absolute path, `LEDGER_OPERATOR`, optional `LEDGER_HARNESS`) in the shell and as keys added to the `env` block of Claude Code's settings (no whole `settings.json` any more), install `ledger-hooks`, then install this plugin and run its setup step, with the ledger's first write, which creates the database, before any check. The install adds the marketplace from GitHub at a release tag, with `claude plugin marketplace add` followed by `https://github.com/smithadifd/skills.git#vX.Y.Z`, in place of `claude plugin marketplace add .`, which Claude Code refuses as `Invalid marketplace source format`; the setup step's folder is named for a plugin install; the ledger repository is named in words, under the same GitHub account as this one, with no link; command examples give the plugin-prefixed form beside the bare one; a new section lists what to remove to uninstall the plugin route (the plugin, the marketplace, the environment keys, the skill configuration file, and what Claude Code's plugin cache keeps); and the README says the question pickers and the ledger's pane need an interactive session, not `claude -p`. Docs only; no skill, script or setting changed.

### The README names a ledger item as a source for `ticket`

The `ticket` row of the README's skills table now says that, with the ledger on, a ledger item with a spec can be the ticket, as `/ticket <ITEM-ID>`, and links the walkthrough in `docs/lifecycle.md` that shows how it is read and what is refused. Docs only; no skill, script or setting changed.

### The ledger-e2e step waits for each install's processes before it checks the footprint

The ledger-e2e step of `ci/check.sh` ran `install.sh --all` and `install.sh --all-agents` and then checked what they left in the throwaway home, but nothing waited for processes an install started in the background: one could write a forbidden file, such as `.claude/settings.json`, after the footprint check had passed, and the step still passed. Each install now runs as the leader of a fresh process group. Once it exits, the group is sent SIGTERM, then SIGKILL after a one-second grace, and must be empty within five seconds; only then do the source-tree, decoy, footprint and frontmatter checks run. A group that is still not empty at the deadline, or that cannot be started, signalled or probed, fails step `sdlc-install` naming the install. Known limit: a process that leaves the install's process group on its own (with `setsid` or `setpgid`) is not waited for, and a write it makes later is not seen.

### The ticket tests choose the ledger from a home and an environment they own

The ticket tests' autouse fixture cleared only `TICKET_LEDGER_CLI`, but `ticket` also reads `ledger_cli` from `~/.claude/skill-config/topic.json` and, with a `ledger` on `PATH`, `LEDGER_DB`. On a machine with a configured ledger, the setup of a markdown-store test wrote markdown while the `ticket.py release` it ran in a subprocess selected the ledger, so `test_E11d` and `test_E11f` failed. The fixture now sets `HOME` to a fresh directory under the test's temporary directory and clears `LEDGER_DB` as well, so the test and every subprocess it starts decide the ledger from what the test sets, and a test that wants a ledger sets these itself. This changes the tests only. CI runs with a fresh `HOME` and was not affected.

### `setup` pins the ledger CLI's absolute path, and a moved CLI is refused by name

A skill with no `ledger_cli` setting runs the first `ledger` on `PATH`, so a writable folder put on `PATH` ahead of the real install could plant one. `python3 skills/_lib/ledger_cli.py setup` (the `_lib` folder beside the skills, in the plugin and in an `install.sh` copy alike) now resolves `ledger` once, checks it against the same compatibility check, and writes its absolute path as `ledger_cli` in `~/.claude/skill-config/topic.json`, keeping every other key. It refuses by name, with nothing written, when no `ledger` is found (`ledger_cli_setup_not_found`), when it is incompatible (`ledger_cli_setup_incompatible`), when the file cannot be read (`ledger_cli_setup_config_unreadable`), changed while setup ran (`ledger_cli_setup_config_changed`) cannot be written (`ledger_cli_setup_write_failed`), or its lock cannot be taken (`ledger_cli_setup_lock_failed`), or its folder does not take hard links (`ledger_cli_setup_no_hardlinks`), and when `ledger_cli` is already set (`ledger_cli_setup_exists`): a live setting, `none` or a bare name is never changed. A pinned absolute path that is no longer an executable file is the one value it replaces.

- With a setting in force no skill reads `PATH` for `ledger`; with none set, the invoked skills keep the `PATH` rule as before.
- Two setup runs at once serialize on an exclusive lock on `topic.json.lock` beside the override, held from the first read of the file through its commit: the second run finds the first run's pin and refuses `ledger_cli_setup_exists`, so neither pin is silently overwritten.
- Setup never removes or overwrites a version of the override file it did not read. It commits only by creating the file when absent, or by moving the existing file aside to `topic.json.setup-backup`, confirming its bytes equal what setup read, and creating the new file without replacing anything; while it does, the path is briefly absent. When an editor saves the file meanwhile, the editor's file is the one left at the path and setup refuses `ledger_cli_setup_config_changed`, naming the backup when it keeps one. A run that stopped with the file moved aside is put right by the next run, which restores `topic.json.setup-backup` before reading. After a successful commit setup gives that file a kept name, `topic.json.setup-previous.<UTC timestamp>-<pid>` (with a `-<counter>` suffix when that name is taken), by linking it there without replacing anything and then removing the in-flight name; its success line names the kept file and says it may be removed once no editor has it open. Setup never replaces, reads, prunes or removes a kept file, and never removes the last name of a file that has been at the override path, so a later run keeps its own displaced version under a new name and deleting the override later never brings the displaced keys back. An editor that writes in place, through a file it opened before the move, lands its late save in that side file, not in the override: setup exits 1 naming the file when it sees the edit, and a later save is in the kept file too; merge it by hand. An in-flight `topic.json.setup-backup` beside a present file is never restored, and a later run that would move the file aside refuses `ledger_cli_setup_config_changed` naming it until you remove it.
- Setup checks that a hard link works in the override's folder before it creates or moves anything: it links its own temp file to a probe name beside it and removes the probe. On a mount without hard links it refuses `ledger_cli_setup_no_hardlinks` with the override untouched and no temp, probe or backup file left, instead of moving the file aside and then failing to put either version back; a probe file that cannot be removed refuses `ledger_cli_setup_write_failed` naming it, also before anything moves. The `ledger_cli_setup_write_failed` line for a version that could not be linked back no longer promises that a rerun puts it back: it names the kept backup and says to move it back by hand when a rerun fails the same way.
- A pinned CLI that has moved is refused before anything runs, in one line starting `ledger_cli_moved:` that names the path and the setup command to rerun, by every skill that runs the CLI through the shared `call` and by `ticket`'s own runs of it.
- `ledger-hooks`, the ledger's own plugin, still runs the first `ledger` on `PATH` and takes no `ledger_cli` setting; this change does not reach it.

### The source-tree shape check names skills by the rule `install.sh` uses

`ci/source_shape.py`, which the ledger-e2e step of `ci/check.sh` uses to list the skills the installer should leave behind, counted every non-dot directory under `skills/` as a skill, so a local `skills/__pycache__` was expected in the throwaway home and the step failed when `install.sh` rightly did not install it. It now names a skill by the same rule as `install.sh`: a directory with a `SKILL.md`, plus `_lib`, and never a dot-name, `__pycache__` or any other directory without a `SKILL.md`. A skill directory that vanishes or is replaced while the check reads it raises instead of being dropped from the list. The step now reads the skill and agent names once, from the source copy before the first install runs, and the footprint check allows only those names: an install that adds or swaps an entry in the source copy, at any point, cannot add a name to the allowlist, and a skill it installs under a new name fails the step. With a local `skills/__pycache__` present, a full `ci/check.sh` now passes. CI runs on a clean checkout, so only local runs were affected.

### `ci/check.sh` discovers skills by the rule `install.sh` uses

`ci/check.sh` listed skills with the bare glob `skills/*`, so a leftover `skills/__pycache__` from a local test run was counted as a skill, `install.sh --all` correctly skipped it, and the `install-all` and `install-one` checks failed. It now discovers a skill exactly as `install.sh` does: a directory with a `SKILL.md`, plus `_lib`, and never a dot-name, `__pycache__` or any other directory without a `SKILL.md`. The `install-all` check also fails when `install.sh` installs an entry the script did not discover, so the two copies of the rule cannot drift apart unseen. CI runs on a clean checkout, so only local runs were affected.

### `install.sh --all` skips directories that are not skills

`install.sh --all` and `install.sh --uninstall --all` used to treat every directory under `skills/` as a skill. A leftover `skills/__pycache__` from a test run made `--all` stop with an error from `find`. They now act only on skills: a directory with a `SKILL.md`, plus `_lib`, the shared helper directory, which installs beside every skill as before. A dot-name, `__pycache__` and any other directory without a `SKILL.md` are skipped, and naming one (`install.sh __pycache__`, `install.sh --uninstall notes`) is refused as `Unknown skill: <name>` with nothing copied or removed.

### The ledger's brief at session start comes from the ledger's own plugin

This plugin no longer declares a `SessionStart` hook: `hooks/hooks.json` and `hooks/session-start.py` are gone, and the plugin declares no hooks at all. The ledger's brief at the start of a session now comes from the ledger's own plugin, `ledger-hooks`, from ledger v0.2.0 or later, which also records your submitted prompts and question-picker answers. Install it from the ledger repository, following the Claude Code plugin section of its README.

- With an earlier release of this plugin (0.12.0) still installed beside the ledger's plugin, the brief may print twice, once from each. Update this plugin to this release, or remove the older one, and it prints once.
- The ledger's plugin takes no `ledger_cli` setting: it runs the first `ledger` on the `PATH` Claude Code runs hooks with, when `LEDGER_DB` names a database. `ledger_cli` still decides which program `topic`, `sharpen`, `shape` and `ticket` run.
- The ledger's plugin sets `LEDGER_SESSION` and, when unset, `LEDGER_AGENT` and `LEDGER_HARNESS`. It does not set `LEDGER_MODEL`, which this plugin's hook used to take from the session's model id; set it in the `env` block of `~/.claude/settings.json` if you want it.
- The shared rule's `decide` no longer takes a `path_rule` argument, which only the removed hook used.

### `ticket` records the ticket it pulled in the ledger

On the ledger store, every open of a tracker or JSON-file ticket now records that ticket as the item's spec with `ledger spec set`: its title, description and acceptance criteria, with `ticket:<key> <url>` as the source. An unchanged ticket writes nothing. A changed one writes the next version, and the open adds the `ledger spec diff` from the version before it to `session.reconciliation`. The tracker's status is not part of the spec, and the ledger never records it.

- Give a tracker ticket its URL as `url` in the ticket mapping. A `url` that is empty or holds whitespace is refused.
- When the ledger has no item for the key yet, the open creates it before it records the spec, so the claim no longer creates it.
- A `spec set` that fails stops the open as `spec_set_failed` before the card is read, and a `spec diff` that fails stops it as `spec_diff_failed`. Each names the command and what the ledger said. A ticket with an empty description cannot be recorded, because a spec needs a body, so on the ledger store it stops the open.
- After it reads the card, the open checks that the spec it recorded is still the item's current one. When another open recorded a newer version in between, the open stops as `stale` and plans nothing. A spec read that fails stops the open by name.
- A ledger item as the ticket, and the markdown store, record nothing.
- The ledger CLI must have `spec set` and `spec diff`, as ledger 0.1.22 does.

### `ticket` works a ledger item

`/ticket <item-id>` can now work an item you filed in the ledger. When no tracker key and no local JSON file matches the key, the ledger is on and it holds an item with that id, `ticket.ledger_item_source(key)` gives a source that reads the item's title and status, its current spec (body and acceptance criteria) and its links. The pass keeps its card on that same item.

- Every open and resume reads the spec again. A spec that changed since the plan was stored (a new version, or other content) sends the pass back to planning, as a changed acceptance criterion on a tracker ticket does. A spec set while a pass opens or runs makes that pass stop as `stale`, and the next open sees the change.
- An item with no spec is refused as `no_spec`, and a spec with no acceptance criteria as `no_acceptance`. Each refusal names the `ledger spec set` command that fixes it, and nothing is written.
- Acceptance text is either `- ` bullets, one criterion per line, or one text with no list markers. Anything between the two is refused as `acceptance_unparseable`, naming the line.
- A spec read that fails, or that answers a version it cannot parse, is refused. An earlier read is never used instead.
- The ledger CLI must have `spec show` and `links`, as ledger 0.1.22 does.

### One rule decides whether the ledger is on

`topic`, `sharpen`, `shape` and `ticket` now decide whether the ledger is on the same way, with one rule in `skills/_lib/ledger_cli.py`. Before, `ticket` used a `ledger` on your `PATH` when `LEDGER_DB` named a database, while the others needed `ledger_cli` set in the topic config, so `ticket` could keep its cards in the ledger while `topic`, `sharpen` and `shape` stayed on files.

- An explicit setting wins: `ledger_cli` in the topic config and, for `ticket`, its own `TICKET_LEDGER_CLI`, which comes first. An explicit CLI that is missing or fails is reported as before.
- `none`, in any case, turns the ledger off. `"ledger_cli": "none"` keeps every skill, `ticket` included, on files.
- With no setting, `topic`, `sharpen`, `shape` and `ticket` turn the ledger on when you invoke them, a `ledger` on `PATH` passes the check `ticket` already ran (`ledger append-many --help` names `--expect-revision`), and `LEDGER_DB` names an existing file. Anything else leaves it off, silently.
- A `ledger_cli` value that is not a string, or a topic config file that exists but cannot be read, leaves the ledger off; it never falls through to the `PATH` rule. A broken `~/.claude/skill-config/topic.json` leaves the ledger off even when the shipped config names a CLI.
- Each skill's first ledger line names the rule that chose, as `ledger on (<rule>: <detail>)`, where `<rule>` is `explicit setting`, `off by setting`, `ledger on PATH with LEDGER_DB` or `not configured`. `topic` prints it before the item line, and `sharpen` and `shape` with a successful link or election. `ticket`'s open card reads `store: ledger (<rule>: <detail>)` or `store: markdown (<rule>: <detail>)`. `python3 skills/_lib/ledger_cli.py` adds `rule` and `line` to its JSON.

What changes for you:

- With `ledger` on your `PATH` and `LEDGER_DB` set, `topic`, `sharpen` and `shape` now use the ledger with no `ledger_cli` when you invoke them. Like `ticket` since 0.12.0, such a skill runs the first `ledger` on your `PATH`; setting `ledger_cli` to an absolute path, or to `none`, avoids that lookup. To keep them on files, set `"ledger_cli": "none"` in `~/.claude/skill-config/topic.json`.
- `ticket` now also follows `ledger_cli`: a CLI named there is used (and refused when it fails the check), and `none` there keeps the markdown store.
- `"ledger_cli": null` in your override file no longer forces the ledger off. It clears the setting, and the `PATH` rule decides.

### A ticket card's plan steps show what its stored events prove

A closed ticket card showed its `plan`, `build`, `review` and `outward` steps as pending, because the plan snapshot is written once, when the plan is presented, and the card rendered the status stored in it. The card now derives each step's status from its stored events, the way it already derives each criterion's result from the stored rounds. It fails closed: a step shows complete only when the stored events prove it (`plan` needs a plan go of the presenting pass, `build` a stored review round, `review` a passing last round, `outward` a closed card and an outward go), each step from `plan` on only after the step before it, a step the plan skipped shows skipped, and every other step shows pending, whatever the snapshot holds. Nothing derived is stored: the snapshot, both stores and the ledger notes are unchanged.

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
2. Upgrade the ledger CLI itself, with the install or upgrade command from the README of the [ledger repository](../../../ledger), which sits beside this one under the same GitHub owner.
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

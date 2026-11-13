# Where each skill fits

The skills in this repository sit at the points in a piece of work where the quick path and the safe path split: before you build, before you claim a fix, before anything leaves your machine, and when you stop for the day. This page shows those points on two clocks. The session clock runs from opening Claude Code to closing it. The project clock runs from an idea to shipped, documented code, over many sessions.

On this page: [the session view](#the-session-view) (start, middle, end), [the project view](#the-project-view) (idea to documented code), [which one when](#which-one-when) (three tables for the skills that overlap), and [four walkthroughs](#walkthroughs) (a new idea, a bug report, stopping for the day, picking up tomorrow), then [the same four situations with the ledger on](#the-same-four-situations-with-the-ledger-on). If you read one section, read the walkthroughs.

Each claim here comes from the skill's own `SKILL.md`. Where this page and a skill file disagree, the skill file is right; open an issue.

Four skills run only when you type their command, never on Claude's own initiative: `handoff`, `pickup`, `retro` and `humanizer`. The rest can be picked up from what you say ("why does this fail", "open a PR").

## The session view

A session has a start, a middle and an end. The start decides what you are continuing from. The middle is the work. The end decides how the work survives you closing the terminal.

```mermaid
flowchart TD
    subgraph start["Start: what am I continuing?"]
        S1["/pickup<br>a handoff note from last time"]
        S2["/topic<br>an ongoing thread"]
        S3["ticket<br>resumes from its card"]
        S4["a fresh ask"]
    end
    subgraph middle["Middle: the work"]
        M1["teach: understand first"]
        M2["ticket: has a ticket"]
        M3["tdd: changing behavior yourself"]
        M4["debug: a reported bug"]
        M5["review, then pr"]
    end
    subgraph finish["End: how does it survive?"]
        E1["/handoff<br>one note for a fresh session"]
        E2["close the topic note"]
        E3["the ticket card is already written"]
        E4["/retro<br>what should change in the setup"]
    end
    start --> middle --> finish
    finish -.->|next session| start
```

### Start

- **You have a handoff note.** Run `/pickup`, with the note's path or with nothing to take the newest note. It reads the note without running anything, warns when the repository has a commit newer than the note, and lists the note's verified commands by number. You choose which to rerun. A command that fails, or that you did not rerun, is demoted to a claim, so the session starts from what is true now, not what was true yesterday.
- **You are continuing a thread of work** that is not a ticket: an investigation, a design discussion, a migration you touch once a week. Run `/topic <slug>`. Every touch starts with a reconcile pass over the note's external references (tickets move, PRs merge) and stamps `Last synced` before any work happens.
- **You have a ticket.** Run the `ticket` skill with the key, and with the pass id from last time if that pass is still live. Its first act is to pull the live ticket, then read the card it kept from the last pass, and tell you which phase it resumes at and why. A changed acceptance criterion sends the pass back to planning; a stored plan is never trusted blindly.
- **You have a fresh ask.** Start the work. If it will outlive the sitting, open a topic for it now rather than at the end.

### Middle

- **You need to understand code before changing it.** `teach` gives a mental model, a component tour and one traced request, every claim cited to a file and line. It writes nothing durable; `document` does that.
- **The work has a ticket.** `ticket` runs orient, research, a plan card, a build-and-review loop, then close. It stops twice for your word: before anything is built (the plan go) and before anything leaves the machine (the outward go). The build goes to the `builder-core` agent in its own worktree, where it commits on a branch; the push and the draft pull request wait for the outward go. The review goes to a `reviewer` agent with no context of the build.
- **You are changing behavior yourself**, with no ticket and no builder agent. `tdd` applies: one failing test, seen failing for the right reason, then the smallest change that passes, then a refactor with the suite green. A test that passes before the change is not evidence.
- **Someone reported a bug.** `debug` reproduces it first, states one hypothesis and the observation that would disprove it, isolates the cause, and fixes it behind a test that failed for that cause. It will not call a fix verified without that test.
- **The diff exists and you want it checked.** `review` reads the current changes for bugs and security problems only, one finding per line with a file and line. For a design that has no code yet, that is `shape`, not `review`.
- **The branch is ready.** `pr` pushes it and opens a pull request in the project's own style, with a summary of every commit and a checkable test plan. It does not merge.

### End

Three mechanisms carry work across sessions. Use one per piece of work, not all three.

| You are stopping... | Use | What persists | Who reads it next |
| --- | --- | --- | --- |
| ...in the middle of work that has no ticket and no topic note | `/handoff` | One note with fixed sections: goal, where we stopped, verified command-and-result pairs, claims, decisions, failed approaches, open questions, references, skills to load next. Redacted by pattern, capped at 20 KB, written to the temporary directory, and never inside the repository unless you name that path. | `/pickup` in a fresh session, which reruns only the commands you approve. |
| ...on a thread you have touched before and will touch again | `/topic <slug>` | One note per thread in `.claude/topics/` (or the configured notes directory), with a rewrite-in-place current state, a dated log line per touch, staged drafts for later, and a promote-or-park ledger at close; or, with the ledger on, one ledger item per thread and no note file. | The next `/topic <slug>`, which reconciles the note before working. |
| ...inside a ticket pass | Nothing extra; stop at a plan go or an outward go | The ticket's card under `.ticket/` (git-ignored): the plan snapshot, go decisions, review rounds, what was tried, and stamps. With the ledger on, the same record is the events of one ledger item, not a file. | The next `ticket` open, which pulls the ticket and reports the phase it resumes at. |

The `handoff` skill reads and writes no ticket card, ledger, roadmap or memory, and says so. If you find yourself writing a handoff note for a ticket, the card already holds what you are about to write.

Two more end-of-session skills are periodic rather than per piece of work:

- **`retro`** reads what the session did and proposes changes to the environment the next session runs in: a hook, a check, an instruction file. A mistake class counts only when it is seen twice. Edits to skills or rules are shown as diffs, and nothing is applied without your approval. It never writes memory and is never about the person.
- **`memory-gardener`** reconciles Claude's memory directory: stale facts, correction chains, a bloated index. It reports first and applies only after you have read the report. Run it when memory has accreted, not every day.

## The project view

A project moves from an idea to shipped code to documentation, with the ticket loop in the middle repeating once per change.

```mermaid
flowchart LR
    K["kickoff<br>idea to written plan"] --> S["sharpen<br>requirements, in your words"]
    K --> H["shape<br>red-team the chosen direction"]
    S --> T["tickets<br>in your tracker"]
    H --> T
    T --> L["ticket loop<br>ticket / tdd / debug<br>builder-core + reviewer"]
    L --> R["review"] --> P["pr"] --> D["document"]
    D -.-> T
    subgraph periodic["Periodic"]
        RT["retro"]
        MG["memory-gardener"]
    end
    L -.-> RT
```

- **An idea.** `kickoff` interviews you one question at a time on requirements, objectives, constraints, users and success criteria, then researches the technical, market and alternative landscape with cited sources, and writes one plan file. It stops there. It never writes application code, and scaffolding only when you ask for it. It has three checkpoints where you confirm or correct what it heard.
- **A requirement that is still fuzzy.** `sharpen` runs a one-question-at-a-time interview and writes a short requirements brief: must-dos, success criteria quoted in your own words, non-goals, open questions. The brief has no authority until you ratify it, and the skill refuses a topic that is really a bounded choice between a few known options.
- **A direction you are about to commit to.** `shape` fans out five read-only lenses (unknowns, edge cases, alternatives, a pre-mortem, hidden dependencies) and returns one ranked list of what to resolve before building. It changes no code, and it saves the list as one dated file beside your sharpen briefs.
- **Tickets.** Writing them is your tracker's job. This repository has no ticket-writing skill; `ticket` pulls a ticket by key from a tracker connector or from a local JSON file.
- **The loop.** For each ticket, `ticket` runs the pass described above. For a change with no ticket, `tdd` or `debug` carry the same discipline without a builder agent. `ticket` always reviews before the outward go; outside a ticket, run `review` yourself before `pr`.
- **Documentation.** `document` writes pages under the repository's docs folder, and the README when you ask for it, through a researcher, a writer and a verifier agent, and writes only through a guard script. It is for durable pages. A code comment is edited directly; a pull request description is `pr`.
- **The periodic loop.** `retro` after a session that taught you something about the setup. `memory-gardener` when the memory index stops being one line per note.

## Which one when

The last column of each table says what changes for those skills when the ledger is on, or that nothing does. What turns the ledger on, and what an `install.sh` install lacks, is under [the ledger walkthrough](#the-same-four-situations-with-the-ledger-on).

### kickoff, sharpen or shape

| | `kickoff` | `sharpen` | `shape` | With the ledger on |
| --- | --- | --- | --- | --- |
| Start from | An idea, a sentence, a blank page | A known topic whose requirements are not pinned | A direction already chosen | `kickoff`: unchanged. `sharpen` and `shape` also work on the open `topic-<slug>` item, and ask once which item when it is not clear. |
| Does | Interviews, then researches, then writes a plan file | Interviews only, one question at a time | Red-teams in parallel, read-only | Unchanged. `kickoff` has no ledger step. `sharpen` and `shape` make ledger calls once their file is written, and write no other file. |
| Output | One plan file, with cited sources and follow-ups | One requirements brief, a draft until you ratify it | One ranked brief: resolve these first | The same files. `sharpen` and `shape` link the saved file to the item with `ledger link <item> --rel url --target <path>`. `sharpen` also records your ratification as an election (`ledger elect`, question `Ratify <brief file name>`, `ratified` against `not-ratified`), after capturing your words as an `utterance.captured` event. |
| Refuses | To implement anything real | A topic that is really a choice between known options | Not for code that already exists (that is `review`) | Unchanged. A failed ledger call leaves the saved file in place, and `shape` ends `Brief saved, not linked: <path>; <reason>`. `sharpen` never writes words for you: with no words of yours to quote it skips the election, and it records nothing when its quoted phrase is not part of your words. |
| Next | `sharpen` or `shape` where it points, or build | Whoever plans next cites the brief | Fold the mitigations in and hand it to whoever builds | Unchanged with the ledger on. |

Each of these is standalone. `kickoff` recommends `sharpen` and `shape` by name but never requires them.

### handoff and pickup, topic, or the ticket card

| | `handoff` + `pickup` | `topic` | `ticket` card | With the ledger on |
| --- | --- | --- | --- | --- |
| Unit | One session | One thread of work, over many sittings | One ticket, over one or more passes | `handoff` + `pickup`: unchanged. `topic`: one thread is one ledger item, `topic-<slug>`. `ticket`: one ticket is one ledger item, with the ticket's key as its id. |
| Lives in | The temporary directory, mode 0600 | `.claude/topics/<slug>.md` in the repository, or the configured notes directory | `.ticket/<key>.md` in the repository, git-ignored | `handoff`: unchanged, the temporary directory; it reads and writes no ledger. `topic`: the item, and no note file. `ticket`: the events of the item, and no `.ticket/<key>.md`. |
| On resume | Verified commands rerun only with your approval; failures demoted to claims | Reconcile pass over external references, then `Last synced` stamped | The live ticket pulled first, then the phase reported with its reason | `pickup`: unchanged, it reads no ledger. `topic`: reads `ledger card <item>`, reconciles, and records `Synced: <what was reconciled>` as a note on the item. `ticket`: pulls the live ticket, records it as the item's spec with `ledger spec set`, then reads the card from the item and reports the phase with its reason. An item worked as the ticket records nothing, because its spec is already the ledger's. |
| Written by | You, when you run `/handoff` | Every touch of `/topic` | Every step of the pass, atomically | `handoff`: unchanged. `topic`: every touch, as notes on the item, and `ledger state <item> --status closed` at close. `ticket`: every step, as one `append-many` under `--expect-revision`; a write that loses a race is refused `stale`. |
| Not for | A ticket card, a tracker entry, a roadmap, memory | A throwaway question; a note for a fresh session (that is `handoff`) | Work with no ticket; a session resume note | Unchanged with the ledger on. |

### debug, tdd or ticket

| | `debug` | `tdd` | `ticket` | With the ledger on |
| --- | --- | --- | --- | --- |
| Start from | A reported failure: a crash, wrong output, a regression | A behavior change you are making yourself | A ticket in a tracker or a local JSON file | `debug` and `tdd`: unchanged, they have no ledger step. `ticket`: also a ledger item with a spec, as `/ticket <ITEM-ID>`, tried after a tracker ticket and a local JSON file with that key. |
| Who writes code | You, not a delegated agent | You, on the main thread | The `builder-core` agent, in its own worktree | Unchanged with the ledger on. |
| Evidence | A reproduction, a test that fails for the reported cause, then passes | A red run before the change and a green run after, per behavior | Review rounds recorded on the card, up to three | `debug` and `tdd`: unchanged. `ticket`: the review rounds are recorded on the item, not in a card file. |
| Stops for you | When the bug will not reproduce (it asks for what is missing), or when the fix needs a design change or touches unrelated behavior | Only when the code cannot be tested without a test-only switch in production code | Twice: before building and before anything leaves the machine | `debug` and `tdd`: unchanged. `ticket`: still the two gates, and an open also stops by name when the ledger cannot supply or take the ticket: `no_spec`, `no_acceptance`, `acceptance_unparseable`, `spec_set_failed`, `stale`. |
| Not for | New behavior; reviewing an existing diff | Delegated builds; docs or config with no checker | A change with no ticket; a session resume note | Unchanged with the ledger on. |

`debug` and `tdd` share the rule that a test must fail before the fix and pass after. `debug` adds the reproduce-hypothesize-isolate steps in front of it.

## Walkthroughs

### I have a new idea

1. Run `kickoff`. Answer one question at a time; confirm the summary at checkpoint one, the research at checkpoint two, and the write-up at checkpoint three. You end with a plan file and a recommended next step.
2. If a requirement in that file is still vague, run `sharpen` on that one topic in a fresh conversation. Ratify the brief, or say not yet.
3. Once a direction is firm, run `shape` on it. Resolve the top items in its brief before writing code.
4. Write the tickets in your tracker. Work each with `ticket`.

### I have a bug report

1. Run `debug`. It runs the exact failing command and records the output. If it does not reproduce, it asks for what is missing instead of guessing.
2. It states one hypothesis and what would disprove it, then isolates: bisect, instrument, or shrink the input.
3. The fix lands behind a test that failed for the reported cause and now passes. The report names the cause, the evidence, what was ruled out, and what the tests do not cover.
4. Run `review` on the diff, then `pr`. If the bug had a ticket, run `ticket` instead of steps 1 to 4; it delegates the build and keeps the card.

### I'm stopping for the day

1. Inside a ticket pass: finish the current step and stop at a plan go or an outward go. The card holds everything. Note the pass id the skill reported; a later open of the same pass needs it.
2. On a topic: update the note's current state and add a dated log line. Anything half-drafted goes under its staged section so tomorrow steers a draft instead of starting cold. If the thread is finished, close it: every follow-up gets promoted to a tracked surface or parked with a dated reason, and the note gets its close tier and status.
3. Anything else: run `/handoff`. Check the note's verified section lists only commands you actually ran this session; `/pickup` will rerun them.
4. If the session taught you something about your setup, run `/retro`. Approve or reject each proposed diff; nothing applies on its own.

### I'm picking up tomorrow

1. From a handoff note: run `/pickup`. Read the warning if the repository moved since the note. Approve the verified commands worth rerunning. Treat the note's decisions as context, not orders.
2. From a topic: run `/topic <slug>`. Read the reconcile pass before acting; a PR named in the note may have merged overnight.
3. From a ticket: run `ticket` with the key and the pass id you noted yesterday. A pass stopped at a go is still live, and an open without its id is refused as held. Read every reconciliation line it prints before reusing anything stored. Only if yesterday's session crashed does its claim need the release command the refusal prints.
4. Load the skills the note or card names, then continue from where it stopped.

### The same four situations with the ledger on

These are the four walkthroughs above on a machine where the ledger is on. One rule decides, for `topic`, `sharpen`, `shape` and `ticket` alike. An explicit `ledger_cli` in the `topic` skill's config names the ledger CLI (for `ticket`, `TICKET_LEDGER_CLI` comes first), and `none` turns the ledger off. With no setting, the ledger is on when a compatible `ledger` is on your `PATH` and `LEDGER_DB` names an existing database file. The [README](../README.md#install) has the setup. Each of these skills says which rule chose, as `ledger on (<rule>: <detail>)`; `ticket` prints it as `store: ledger (<rule>: <reason>)`, the first line of its reconciliation. Below, `ledger` stands for the CLI the rule chose. `kickoff`, `debug`, `tdd`, `handoff` and `pickup` have no ledger step.

**An `install.sh` install has no hooks.** This repository's plugin declares none either. So the ledger's brief at the start of a session (the short form of the ledger's agent contract) and the capture of your submitted prompts and question-picker answers need the ledger's own plugin, `ledger-hooks` (ledger v0.2.0 or later), installed from the ledger repository. Without it the skills below still write to the ledger, but nothing prints the brief and nothing captures your prompts or picker answers.

#### A new idea, with the ledger on

1. Run `kickoff`. Nothing changes: it has no ledger step, and you end with a plan file.
2. If the thread will outlive the sitting, run `/topic <slug>` before `sharpen` or `shape`, which attach their brief to its item. It prints the ledger line, then the item id `topic-<slug>`, `created` and the executable. It creates the item with an `Ask:` note and a `topic-rails` note, and writes no note file.
3. Run `sharpen` on a vague requirement as before. Once the brief file is written it links the file to the item: `ledger link <item> --rel url --target <path of the brief>`. When you ratify, use words of your own. The skill captures them as an `utterance.captured` event, then records an election, `Ratify <brief file name>`, `ratified` against `not-ratified`, with the brief's path as its scope and your ratifying phrase as the quote. A bare click leaves nothing to quote, so no election is recorded and the skill says so. A failed ledger call leaves the brief and its ratification stamp as they are.
4. Run `shape` on the chosen direction as before. It saves the brief and links it to the item the same way. Its last line is `Brief saved: <path>`, or `Brief saved, not linked: <path>; <reason>` when the link failed.
5. Write the tickets in your tracker as before, or file one as an item in the ledger and give it a spec: `ledger spec set <ITEM-ID> --title <title> --body-file <body.md> --acceptance <criteria>`. The acceptance text is either one `- ` bullet per criterion, or a single text with no list markers. Work either kind with `ticket`.

#### A bug report, with the ledger on

1. Run `debug`, then `review` and `pr`. Nothing changes: none of them has a ledger step.
2. If the bug has a ticket, run `ticket <KEY>` as before. On the ledger store, the open pulls the ticket, then records what it pulled as the item's spec before it reads the card: `ledger spec set <KEY>` with the ticket's title, description and criteria, and `ticket:<KEY> <url>` as the source. An unchanged ticket writes nothing. A changed one writes the next version, and the open adds `ledger spec diff <KEY> N-1 N` to its reconciliation, naming each field that changed. Read it before you go on. The tracker's status is never recorded.
3. The first line of the open's reconciliation says which store was chosen. `store: markdown (<rule>: <reason>)` means the ledger is off, and the reason says what to fix.
4. If the bug is an item you filed in the ledger, run `ticket <ITEM-ID>`. It is tried when no tracker ticket and no local JSON file matches the key. Every open reads the item's title and status, its current spec and its links, and records nothing. An item with no spec is refused `no_spec`, and a spec with no acceptance criteria `no_acceptance`; each refusal names the `ledger spec set` command that fixes it, and nothing is written.
5. A ticket with an empty description cannot be recorded, because a spec needs a body. The open stops as `spec_set_failed` and does not read the card.

#### Stopping for the day, with the ledger on

1. Inside a ticket pass: finish the current step and stop at a plan go or an outward go. The card is the events of the item, so there is nothing to write by hand. Note the pass id the skill reported.
2. On a topic: there is no note file, so no `Current state` or `Log` section to update. The rails are notes on the item. Staged drafts go on the `staged_drafts` line of a new `topic-rails` note that restates both lines in full; an older note is never edited. A long write-up is a document in your project, linked with `ledger link <item> --rel url --target <path>`. To close the thread, give each emitted work item one entry on the `promote_or_park` line of a new `topic-rails` note, `PROMOTED to <tracked surface>` or `PARKED <date>: <reason>`. Name the tier, `Close tier: LITE` or `Close tier: FULL`, and run `ledger state <item> --status closed`. The close ends with the ledger's resume summary, printed exactly as it came back (`ledger resume --item <item>`, ledger v0.1.1 or later).
3. Anything else: run `/handoff`. It is unchanged: the note goes to the temporary directory, and the skill reads and writes no ledger.
4. If the session taught you something about your setup, run `/retro`. It is unchanged.
5. Your submitted prompts and question-picker answers are captured by the ledger's plugin as you submit them, so no step at the end covers them. An install without that plugin does not capture them.

#### Picking up tomorrow, with the ledger on

1. From a handoff note: run `/pickup`. It is unchanged and reads no ledger.
2. From a topic: run `/topic <slug>`. It prints the ledger line, then the item id and `resumed`. If an interrupted earlier open left out the `Ask:` or `topic-rails` note, it adds the missing one and never a second copy. It reads the item with `ledger card <item>`, reconciles the external references, and records the pass as a `Synced: <what was reconciled>` note. Read the reconcile pass before acting.
3. From a ticket: run `ticket` with the key and the pass id you noted. The open pulls the live ticket, records it as the item's spec, then reads the card from the item. Read every reconciliation line, including the `store:` line and any spec diff. A spec whose version or content changed since the plan was stored sends the pass back to planning, as a changed criterion does. If another open recorded a newer version in between, the open stops as `stale` and plans nothing; open the ticket again. An open without the pass id is refused as held, and on the ledger store it has already recorded the ticket's spec by then. If the repository still holds `.ticket/<key>.md` cards from before the ledger, the open adds a line naming the exact `import-cards` command; run it when you agree.
4. With the ledger's plugin installed, the session also starts with the ledger's brief. Without it nothing prints a brief, and the steps above are how you find the work.
5. Load the skills the note or card names, then continue from where it stopped.

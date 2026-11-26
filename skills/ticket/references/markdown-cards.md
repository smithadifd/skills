## Moving markdown cards into the ledger

A machine that ran `/ticket` before it switched to the ledger store still holds
`.ticket/<key>.md` cards. Once the store choice picks the ledger (a ledger on
`PATH` with `LEDGER_DB` set, or `TICKET_LEDGER_CLI` or `ledger_cli` naming one), run these
once, from this skill's folder or by its full path:

- `python3 ${CLAUDE_SKILL_DIR}/ticket.py find-cards [<root>...]` lists, one
  absolute path per line, every git repository under the roots (default: the
  current directory) whose `.ticket/` holds a card that is not imported yet.
  It follows no links (a root that is itself a link is skipped), skips `.git`,
  `node_modules` and `.ticket/imported`, and exits 0 whether or not it found any.
- `python3 ${CLAUDE_SKILL_DIR}/ticket.py import-cards <repo>... [--dry-run]`
  writes each card as the events of one ledger item and moves the card to
  `.ticket/imported/<key>.md`. Closed cards import as history; an open card
  keeps its pass id, so `open(..., pass_id=<id>)` resumes it from the ledger.
  Each card prints one line: `imported <key>`, `already imported <key>`, or
  `skipped <key>: <reason>`. A card the reader or a validator refuses (or an older
  card format) is left where it is and named, never half-imported. A rerun
  writes nothing. `--dry-run` writes and moves nothing, prints `would import
  <key>`, and reports the same `skipped` a real run would for a taken
  `.ticket/imported/<key>.md`. Exit 0 when every card imported or was already imported, 1 when any
  was skipped, 2 when the store choice above is not the ledger (`none`, or no
  usable ledger found; the message names the reason) or the named CLI fails the
  capability probe (before any card is read).
  `.ticket/imported/` must be git-ignored like the card; if it is not, the
  repository is refused and no ignore file is edited.

When a ticket opens on the ledger store and the repository still holds markdown
cards, `open` adds one line to `session.reconciliation` naming the exact
`import-cards` command. Read it to the person; run the import when they agree.
Never run the import against a repository you were not asked to work in.


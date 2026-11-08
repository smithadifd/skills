## Verdict: REQUEST CHANGES

I reviewed the round-2 delta 77c04820..f6eaabfa (head confirmed), which changes only `designs/LS5a/design.md`. The fold does four things: it moves the write point after the `workspace.txt` write at `:4814`, adds a `refuse_claimed_launch` branch that ends the seat, rewrites the mutations for T3 and T4, and pins stdlib-only imports for `launch_rows.py` (T15 iii). It also adds R13 and R14. Almost every code citation checks out against `launch-seat.sh` and `row_item.py` at the head. The one that doesn't is mutation (a)'s reasoning: it misdescribes the shell state at the mutated site, so T3's core assertion is never shown to fail.

### Issues
- `designs/LS5a/design.md:416` — Mutation (a) for T3 places `claim_launch_rows` before `:4707`. The note says the entry re-check then finds `session_id` "emptied" by `:4718` and refuses `launch_seat_rows_claim_refused` before writing anything. That is wrong in two ways:
  - Before `:4707`, `:4718` has not run yet, and nothing in the launch flow assigns `ready_session` before `:4787`. (The only other assignment is a `local` at `:3789` inside the watch subcommand.)
  - The launcher runs under `set -euo pipefail` (`:3`). Probed: referencing an unset variable under `set -u` aborts with `unbound variable` and exit 1, not a named refusal with exit 2.
  
  T3 still goes RED, because the path's own refusal name never prints. But the invariant T3 exists for is "leaves no item and no new ledger event" on every refusal path, and no mutation ever exercises it: the claim is never reached under mutation (a). A broken item-absence check would pass the whole sweep. The reason given for rejecting an alternative ("moving the claim calls past the re-check… mutates two sites") doesn't hold. The re-check lives inside `claim_launch_rows` and passes anywhere after `:4787`. So putting the one call directly after `:4808` (the cycle-2 r0 placement) is a single-site mutation, and it should turn T3's new "watch pointer send" variant RED on the no-item assertion itself. Fix: add that mutation as (a2), naming the no-item assertion as what goes RED, and correct the reasoning text for (a). [reach: lane (the design note and its test table are the author's own committed text); breaks: evidence; evidence: probed; weight: m/l/m; if-minor]

### Minor notes
- `design.md:204-206`: step 2 says the identity check covers "harness, session id, cwd, model". For a Claude seat, `retire_session_process_matches` (`:2498-2512`) checks only that argv0's basename is `claude` and that `--session-id` matches. cwd, model and thinking are checked only for omp (`:2517-2527`). This is a docs inaccuracy only; the gate itself is reused unchanged.
- `design.md:204-210`: requirement gap, unverified. No test or mutation pins the identity gate before `SIGTERM`/`SIGKILL`. Whether T9 (ii-b) goes RED when that gate is dropped depends on how the fake seat makes "exit not proven", and the note doesn't say. The SIGKILL after the 5 s wait also isn't stated to re-confirm identity. Suggest naming a mutation such as "signal without `retire_process_identity_matches`" and the test that catches it. [weight: m/l/m]
- `design.md:431-432`: "each must turn exactly its named test RED" is ambiguous for mutation (a). Placed before `:4707`, it aborts every `ROWS` launch, so T2, T4, T9 (ii) and T12 also go RED. If "exactly" means "only", (a) breaks the rule as written.
- These citations were checked and are accurate:
  - Step 4's ledger claims: `ledger` sets `launch_exit_recorded=1` for any status except `launched` (`:1832`), and `effective_lifecycle_rows` drops the matching `launch` row on `launch_refused` (`:680-682`).
  - R13's name-reuse reasoning (`:4584-4594` still sees the `watch` row).
  - The T4 mutation: `link-session` rejects a target that isn't a UUID (`row_item.py:317-318`), and `:4790` makes `$session_id` equal field 2.
  - The R14 lease no-op (`row_item.py:755-765`).
  - The new refusal paths listed at `:4810`, `:4811` (`lifecycle_append` refuses a symlink at `:791`) and `:4814`.

### Evidence
- Ran: `bash probes/pyiso.sh` (Python 3.14.7). With `-I`, the script directory and cwd are both absent from `sys.path` and a sibling import fails. Without `-I`, the script directory is on the path and the sibling import loads. Inline `-c` without `-I` loads a module planted in the cwd. This confirms the corrected `-I` reasoning at `design.md:80-86` and that T15 (iii) is needed.
- Ran (failure path, error return): `bash probes/run-nu3.sh`. Under `set -u`, `test -n "$rs_unset_var"` prints `unbound variable` and exits with rc=1. Combined with grep showing `ready_session` is first assigned at `:4787` in the launch flow, this shows mutation (a) aborts rather than refusing `launch_seat_rows_claim_refused`.
- Mutation: not run, because the delta is docs-only (a design note with no code to mutate). The mutation-validity finding above rests on the nounset probe plus the cited lines.
- Regression check: n/a. The delta touches only `designs/LS5a/design.md`, and no test parses that file's content for LS5a.
- Sinks checked: the launch-ledger (`ledger`), the lifecycle (`lifecycle_append` and `effective_lifecycle_rows`), cmux `close-workspace`, signals to the seat and watcher pids, and stderr refusal tokens.
- Trust boundary: the brief and `--rows` enter at preflight (the parse runs under `-I`, with an `O_NOFOLLOW` descriptor plus `fstat`). `watcher.ready` field 2 is checked at `:4789-4792` before the moved write point. Seat pid identity is checked by `retire_process_identity_matches` before any signal.
- Probe-creation note: several `printf` probe creations containing `if`/`then`, `||` or redirects to stderr were denied by the command guard. The probe was reduced to the minimal `nu3.sh` form shown above. The worktree under review was not touched.

INTERVAL: none

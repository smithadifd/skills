I found no blocking issues in the round-3 delta; the verdict is APPROVE, with three minor notes on the design note's accuracy. The delta changes only `designs/LS5a/design.md`, so no code mutation applies.

## Verdict: APPROVE

The delta f6eaabfa..4e3b85bd changes only `designs/LS5a/design.md` (head confirmed as 4e3b85bd4d450d0449c2220da5f2bb89a7d9a371). It rewrites step 2 of `refuse_claimed_launch` as an identity gate checked before each signal. It lists T3's variants and how each one fails, and adds mutation (a2). It adds T9 (ii-b) to (ii-d), restates the mutation rule with which tests each mutation turns red, and adds a note on how the round-1 finding was resolved. Every line reference I checked matches the code at head, and the two mechanism claims I could probe hold.

### Issues
- none

### Minor notes
- T9 (ii-b) and (ii-d), as written, cannot reach the write point. In (ii-b) the fake `ps` "reports `claude --session-id <another uuid>` for the dummy". In (ii-d) it reports the matching identity only "on its first query".
  - The launcher reads the seat's command line through `LAUNCH_SEAT_PS` long before the claim. `select_launch_pid` (`scripts/launch-seat.sh:1059-1138`) runs first, then `discover_claude_session_id` (`:1020-1043`, compared with the minted id at `:4723`).
  - Probe p2 runs both functions taken from the head file. With the other uuid, `select_launch_pid` prints no pid, so the launch refuses `launch_seat_not_ready` at `:4687` and never reaches `refuse_claimed_launch`. In (ii-d), the "first query" is the selection query.
  - The test can't pass by accident: its `MANUAL CLOSE:` / `_workspace_leftover` assertions would fail on the green run. But the design should say the fake `ps` changes its answer only after the write point, for example once the stub claim writes a marker. [weight: m/l/l]
- The delta pins "exit proven" to `${LAUNCH_SEAT_PS:-ps} -p "$pid"` no longer finding the pid, and step 3 calls this "retire's rule".
  - Retire's own rule is stricter. `retire_pid_state` (`:2257-2270`) counts only a silent exit 1 as proof the pid is gone; an error or timeout means unknown.
  - Probe p1 shows the plain `-p` check reads a `ps` that errors (rc 2) as gone. Under the note's wording, a `ps` error or bounded-run timeout after the identity gate passed would allow the `--force` retry.
  - The base had the same wording ("`ps -p` no longer finds"). The `--force` reaches only this launch's own workspace after the gate matched its seat, so I don't count this as a bypass.
  - Suggested wording: "exit proven = `retire_pid_state` prints `absent`". [weight: m/l/l]
- The note says the gate fails safe in two cases. There is a third: a sandboxed Claude seat whose recorded pid is the `codex` wrapper. `select_launch_pid` (`:1127-1136`) can print the wrapper's pid, and `retire_session_process_matches` (`:2498-2512`) requires argv0 to be `claude`. This also fails safe (no signal is sent); only the note's description is incomplete.
- Formatting: in the mutation-rule section, the sweep sentence and the following "T5's mutation (b) was changed…" paragraph now render as part of the last bullet. The doubled "exactly one / one-site" wording was already in the base.

### Evidence
- Ran: no test suite. The change is docs-only (one design note), so there is nothing to regression-test; I checked the note's claims against the head code instead. All of these match:
  - line references `:4651`, `:4694`, `:4707`, `:4718`, `:4787-4792`, `:4801-4808`, `:4810`, `:4814`, `:1531`, `:2495-2570`, and the note's own references at lines 194, 202-240, 242 and 440;
  - `ready_session` is assigned only at `:3789` (a `local` in the watch subcommand) and `:4787`;
  - `refuse` exits 2; `denied` prints `DENIED at send-<stage>` and exits 3;
  - `launch_exit_guard` prints `launch_seat_launch_incomplete` (`:1843-1866`);
  - `thinking` and `retire_deadline` are initialised globally (`:4029`, `:32`), so the new gate call is safe under `set -u`.
- Probe p1 (failure-path probe):
  - A function that reads an unset variable under `set -euo pipefail` exits 1 even when called on the left of `||`, and the EXIT trap still runs. This confirms mutation (a)'s "exit 1, not a named refusal".
  - Writing to a path that is a directory exits 1 through the EXIT trap. This confirms T3's workspace.txt variant.
  - A `ps` stand-in that exits 2 is read as "gone" by the `-p` check (minor note 2).
- Probe p2: `select_launch_pid` and `discover_claude_session_id` taken from the mutation clone at 4e3b85bd and run against a fake `ps`. With the matching id: pid=4242, rc 0. With another uuid: no pid, rc 0, and discovery returns the other uuid (minor note 1).
- Mutation: not run, docs-only. The delta changes no executable code.
- Sinks checked: n/a (docs-only). The ledger, launch-ledger and cmux close outputs described in the note were checked against the code.
- Trust boundary: n/a (docs-only). The note's identity gate reads the seat's command line and start time through `LAUNCH_SEAT_PS`, and I verified that against `:2558-2570`.

INTERVAL: none

## FINDINGS
- `portfolio-health-run.sh` `_ph_report_writable` live path writes `${REPORT_FILE}.write-probe.$$` in the wrapper process. That is host writability, not Codex sandbox writability. The claimed failure (“Codex could not write the report from its sandbox”) still dispatches Codex whenever the wrapper can write; `PH_CODEX_WRITE_PROBE_CMD` is test-only.
- `_ph_default_codex_meter`: `tolower($0) ~ /off/` is an unanchored substring. A Codex usage line containing `cutoff`/`offset`/`offline` is classified `off` and skips the fallback (`pool-off`) even when the pool is on.
- Same function: `usage="$(omp usage 2>/dev/null)"` has no timeout. A hung/slow `omp usage` blocks the weekly job before `lk_run_chain`, so the new 900s chain timeout never applies.
- `LK_CHAIN_TIMEOUT_SECS` default `900` equals the stated 15 min upper bound with no startup/wrapper slack; the slow runs this change is meant to cover can still hit `claude:timeout`.
- Tests never execute `_ph_default_codex_meter` or the live write probe (`PH_CODEX_METER_CMD` / `PH_CODEX_WRITE_PROBE_CMD` always injected). The production gate is untested; the `/off/` false-positive cannot fail CI.
- `test_wrapper_exports_chain_timeout_at_least_900` only reads an env dump the test stub writes — it does not prove `lk_run_chain` honors 900s. Skip tests use `sleep 5` + 1s timeout instead of a Claude hard-fail, and assert `"chain_fallback:"` and `codex:skipped(...)` as separate substrings rather than the claimed single line `chain_fallback: <claude-reason> · codex:skipped(<reason>)`.

## VERDICT
flagged — Codex sandbox non-writes are not gated, the live meter parser misclassifies, and CI only exercises the injectable doubles.

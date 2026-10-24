## FINDINGS
- `sample-monitor-run.sh` `_sm_report_writable` live path writes `${REPORT_FILE}.write-probe.$$` in the wrapper process. That is host writability, not worker sandbox writability. The claimed failure (“the worker could not write the report from its sandbox”) still dispatches the worker whenever the wrapper can write; `SM_WORKER_WRITE_PROBE_CMD` is test-only.
- `_sm_default_worker_meter`: `tolower($0) ~ /off/` is an unanchored substring. A worker usage line containing `cutoff`/`offset`/`offline` is classified `off` and skips the fallback (`pool-off`) even when the pool is on.
- Same function: `usage="$(workerctl usage 2>/dev/null)"` has no timeout. A hung/slow `workerctl usage` blocks the weekly job before `run_job`, so the new 900s chain timeout never applies.
- `SM_JOB_TIMEOUT_SECS` default `900` equals the stated 15 min upper bound with no startup/wrapper slack; the slow runs this change is meant to cover can still hit `reviewer:timeout`.
- Tests never execute `_sm_default_worker_meter` or the live write probe (`SM_WORKER_METER_CMD` / `SM_WORKER_WRITE_PROBE_CMD` always injected). The production gate is untested; the `/off/` false-positive cannot fail CI.
- `test_wrapper_exports_job_timeout_at_least_900` only reads an env dump the test stub writes — it does not prove `run_job` honors 900s. Skip tests use `sleep 5` + 1s timeout instead of a reviewer hard-fail, and assert `"chain_fallback:"` and `worker:skipped(...)` as separate substrings rather than the claimed single line `chain_fallback: <reviewer-reason> · worker:skipped(<reason>)`.

## VERDICT
flagged — worker sandbox non-writes are not gated, the live meter parser misclassifies, and CI only exercises the injectable doubles.

INTERVAL: none

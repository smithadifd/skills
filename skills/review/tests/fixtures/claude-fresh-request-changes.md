## Verdict: REQUEST CHANGES

The change sizes the weekly chain timeout and gates the worker step behind a meter read and a write probe.

### Issues
- `skills/sample-monitor/sample-monitor-run.sh:212` — the write probe runs in the wrapper process, so it proves host writability, not the sandbox's; the gate it claims to add never fires on the failure the PR describes.
- `skills/sample-monitor/sample-monitor-run.sh:188` — `/off/` is an unanchored substring match; a usage line containing `cutoff` classifies the pool as off.

### Minor notes
- `skills/sample-monitor/tests/test_sample_monitor_run.py:41` — the timeout assertion reads the stub's env dump rather than proving the chain honors it.

FINDINGS:
1. `skills/example/tool.py:41` — the retry loop never resets the backoff counter after a success (major)
2. `skills/example/tool.py:58` — the timeout value is read from an unvalidated env var (minor)

VERDICT: flagged — the retry logic needs the backoff reset before merge.

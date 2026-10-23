## FINDINGS
1. `skills/example/queue.py:19` — the queue never drains on shutdown, so pending jobs are dropped (blocker)
2. `skills/example/queue.py:44` — the retry count is off by one (minor)

## VERDICT
flagged — the shutdown path drops pending jobs.

INTERVAL: none

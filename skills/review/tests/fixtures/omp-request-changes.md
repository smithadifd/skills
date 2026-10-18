I reviewed the diff against the base branch; here is the result.

```review-result
{
  "verdict": "REQUEST CHANGES",
  "bugs": [
    "src/export.py:58 — the limit is applied after the rows are written, so `--limit 10` still writes every row; slice the query result before the write loop.",
    "src/export.py:31 — a negative `--limit` is accepted and silently drops the last row; reject values below 1 at parse time."
  ],
  "nits": [
    "tests/test_export.py:12 — the test checks the exit code only, not the number of rows written."
  ],
  "reviewed_head": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}
```

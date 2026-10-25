I looked at the change to the invoice export command.

### Issues
- `src/export.py:58` — the limit is applied after the rows are written.

## Report format

`~/.claude/logs/memory-gardener/<date>.md`:

```markdown
# Memory gardener — reconciliation report — <date>

Memory dir: `…`
**This report IS the plan.** apply executes exactly these changes…

## Summary
| Verdict | File | Change |
|---|---|---|
| DEAD | `old-note.md` | → archive/ (<reason>) |
| SUPERSEDED | `saga-note.md` | frontmatter/body updated |
| UPDATE | `bloated-index.md` | frontmatter/body updated |
| UNVERIFIED>90d | `stale-note.md` | no file change |
| KEEP | `clean-note.md` | no file change |

## Wikilink repairs
- `[[old-note]]` → `[[archive/old-note]]`

## Per-file detail
### `saga-note.md` — **SUPERSEDED**
<rationale>
```diff
<unified diff of the proposed file change>
```
**Index line:**
```diff
- <old ballooned line>
+ <new one-liner>
```
```

Rows are ordered DEAD → SUPERSEDED → UPDATE → UNVERIFIED>90d → KEEP so the
consequential changes are at the top.

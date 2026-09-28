---
name: pr
kind: portable
description: >
  Create a pull request for the current branch following the project's
  conventional style.
---

Create a pull request for the current branch following the project's conventional style.

Steps:
1. Run `git status`, `git diff main...HEAD`, and `git log main...HEAD` in parallel to understand all changes since branching from main
2. Check if current branch tracks a remote and whether it needs to be pushed
3. Push to remote with `-u` if needed
4. Draft a PR title (under 70 characters, imperative mood) and body using this template:

```
gh pr create --title "<title>" --body "$(cat <<'EOF'
## Summary
- <bullet 1>
- <bullet 2>
- <bullet 3 if needed>

## Test plan
- [ ] <manual test step 1>
- [ ] <manual test step 2>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Rules:
- Title uses conventional prefix matching the commits (feat:, fix:, etc.)
- Summary covers ALL commits in the branch, not just the latest
- Test plan is concrete and checkable, not vague
- Do not include style commentary or implementation details in the summary

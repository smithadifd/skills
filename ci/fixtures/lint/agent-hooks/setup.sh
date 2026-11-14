#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
printf -- "---\nname: helper\nhooks:\n  PreToolUse:\n    - matcher: Bash\n      hooks:\n        - type: command\n          command: true\n---\n\nHelp.\n" > agents/helper.md

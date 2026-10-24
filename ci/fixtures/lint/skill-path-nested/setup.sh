#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
mkdir -p skills/demo/references/sub
printf "Run python3 ~/.claude/skills/demo/demo.py now.\n" > skills/demo/references/sub/x.md

#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
grep -v "^version:" skills/demo/SKILL.md > skills/demo/SKILL.tmp
mv skills/demo/SKILL.tmp skills/demo/SKILL.md

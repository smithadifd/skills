#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
head -c 33000 /dev/zero | tr "\\0" "x" >> skills/demo/SKILL.md
printf "\n" >> skills/demo/SKILL.md

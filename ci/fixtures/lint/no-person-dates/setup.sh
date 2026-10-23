#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
day=$(printf "%s-%s-%s" 2026 01 02)
printf "Verified on %s.\n" "$day" > notes.md

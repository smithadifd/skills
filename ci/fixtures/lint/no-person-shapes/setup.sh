#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
home="/Users""/bob"
printf "Saved under %s/work.\n" "$home" > notes.md

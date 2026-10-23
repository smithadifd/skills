#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
term="zz""planted-name"
printf "Written by %s today.\n" "$term" > notes.md

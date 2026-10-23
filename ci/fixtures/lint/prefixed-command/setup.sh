#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
prefix="/sdlc"":"
printf "Run %sreview to start.\n" "$prefix" > notes.md

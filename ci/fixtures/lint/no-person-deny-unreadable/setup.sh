#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
# A tracked symlink to a missing target cannot be read; a denied term sits elsewhere.
set -eu
term="zz""planted-name"
printf "Written by %s today.\n" "$term" > notes.md
ln -s missing-target.txt dangling.txt

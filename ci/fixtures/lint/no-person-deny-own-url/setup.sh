#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
git remote add origin https://github.com/zzowner/zzrepo.git
term="zz""planted-name"
printf "See https://github.com/zzowner/zzrepo and %s here.\n" "$term" > notes.md

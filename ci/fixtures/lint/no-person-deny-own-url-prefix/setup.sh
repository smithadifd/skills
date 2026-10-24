#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
owner="zz""planted-name"
git remote add origin "https://github.com/$owner/zzrepo.git"
printf "See https://github.com/%s/zzrepo-other for more.\n" "$owner" > notes.md

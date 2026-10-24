#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
owner="zz""planted-name"
git remote add origin "https://github.com/$owner/zzrepo.git"
printf "See %s for more.\n" "xgit@github.com:$owner/zzrepo" > notes.md

#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
grep -v "^| \`demo\` |" README.md > README.tmp
mv README.tmp README.md

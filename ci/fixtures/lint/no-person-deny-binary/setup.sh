#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
term="zz""planted-name"
printf 'A\000B\000%s\000\377\n' "$term" > blob.bin

#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
filler=$(awk "BEGIN { for (i = 0; i < 400; i++) printf \"word \" }")
{
  printf -- "---\nname: demo\nkind: portable\nversion: 1.0.0\ndescription: >\n"
  printf "  Use when the person says \"run the demo\". NOT for real work. %s\n---\n\nBody.\n" "$filler"
} > skills/demo/SKILL.md

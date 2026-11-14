#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
printf -- "name: helper\n\nHelp.\n" > agents/helper.md

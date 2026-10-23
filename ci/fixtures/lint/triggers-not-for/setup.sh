#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
printf -- "---\nname: demo\nkind: portable\nversion: 1.0.0\ndescription: >\n  Use when the person says \"run the demo\".\n---\n\nBody.\n" > skills/demo/SKILL.md

#!/usr/bin/env bash
# Run inside a copy of the base tree; make it break one rule.
set -eu
# The indented --- sits inside the description block scalar, so it is body
# text, not the closing marker; the top-level hooks key after it is still
# frontmatter.
printf -- "---\nname: demo\nkind: portable\nversion: 1.0.0\ndescription: |\n  Use when the person says \"run the demo\". NOT for real work.\n  ---\nhooks: {}\n---\n\nBody.\n" > skills/demo/SKILL.md

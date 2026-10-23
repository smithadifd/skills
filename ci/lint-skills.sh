#!/usr/bin/env bash
# Lint the skills repository. Usage: ci/lint-skills.sh [root]
#
# Rules (each failure prints "LINT FAIL [rule]: ..."):
#   frontmatter       every skill has name, kind, version and description
#   description-size description plus when_to_use stay under 1,536 characters
#   triggers          the description names a trigger phrase and a NOT-for case
#   skill-size        SKILL.md body warns above 16,384 bytes, fails above 32,768
#   tests-dir         a skill that ships a script has a tests/ directory
#   readme-rows       every skill and agent has a README table row
#   skill-path        no SKILL.md hard-codes the .claude/skills/ install directory
#   prefixed-command  no file spells a command with the plugin prefix
#   no-person-*       no person, host, home path or dated line in a tracked file
#
# NO_PERSON_DENY is a grep extended regular expression (term1|term2|...). When
# it is unset the deny step is skipped with a notice. OWNER and REPO default to
# the origin remote and let a line with the repository's own URL pass.
set -uo pipefail

root=${1:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}
cd "$root" || { printf 'LINT ERROR: cannot enter %s\n' "$root" >&2; exit 2; }

fail=0
lint_fail() {
  printf 'LINT FAIL [%s]: %s\n' "$1" "$2"
  fail=1
}
lint_warn() {
  printf 'LINT WARN [%s]: %s\n' "$1" "$2"
}

# The tracked-file rules need a git work tree; without one they would pass on nothing.
if ! git ls-files -z > /dev/null 2>&1; then
  lint_fail git "$root is not a git work tree; the tracked-file rules cannot run"
  exit 1
fi

# field <file> <key>: the frontmatter value of a top-level key, with folded
# continuation lines joined by single spaces. Empty when the key is absent.
field() {
  awk -v key="$2" '
    NR == 1 { if ($0 != "---") exit; next }
    $0 == "---" { closed = 1; exit }
    {
      if (match($0, /^[A-Za-z_-]+:/)) {
        current = substr($0, 1, RLENGTH - 1)
        if (current == key) {
          rest = substr($0, RLENGTH + 1)
          sub(/^[ \t]+/, "", rest)
          if (rest == ">" || rest == "|" || rest == ">-" || rest == "|-") rest = ""
          out = rest
        }
        next
      }
      if (current == key) {
        line = $0
        sub(/^[ \t]+/, "", line)
        out = (out == "" ? line : out " " line)
      }
    }
    END { if (closed) print out }
  ' "$1"
}

# closed_frontmatter <file>: succeeds when line 1 and a later line are both ---.
closed_frontmatter() {
  awk 'NR == 1 { if ($0 != "---") exit 1; next } $0 == "---" { found = 1; exit 0 } END { exit found ? 0 : 1 }' "$1"
}

# body_bytes <file>: bytes after the closing frontmatter marker.
body_bytes() {
  awk 'BEGIN { n = 0 } n >= 2 { print } $0 == "---" && n < 2 { n++ }' "$1" | wc -c | tr -d ' '
}

plugin_name=sdlc
if [ -f .claude-plugin/plugin.json ]; then
  found=$(sed -n 's/^[[:space:]]*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' .claude-plugin/plugin.json | head -n 1)
  [ -z "$found" ] || plugin_name=$found
fi

shopt -s nullglob
skill_files=(skills/*/SKILL.md)
[ "${#skill_files[@]}" -gt 0 ] || lint_fail frontmatter 'no skills/*/SKILL.md files found'

for file in "${skill_files[@]}"; do
  dir=${file%/SKILL.md}
  skill=${dir##*/}

  closed_frontmatter "$file" ||
    lint_fail frontmatter "$file has no closed frontmatter (a --- line first, and a --- line closing it)"

  missing=
  for key in name kind version description; do
    [ -n "$(field "$file" "$key")" ] || missing="$missing $key"
  done
  [ -z "$missing" ] || lint_fail frontmatter "$file lacks:$missing"

  description=$(field "$file" description)
  when_to_use=$(field "$file" when_to_use)
  size=$(printf '%s%s' "$description" "$when_to_use" | wc -m | tr -d ' ')
  if [ "$size" -ge 1536 ]; then
    lint_fail description-size "$file description plus when_to_use is $size characters; the limit is 1,535"
  fi

  if [ -n "$description" ]; then
    printf '%s' "$description $when_to_use" | grep -qE '"[^"]+"|Use when|Use this|Trigger on|Trigger phrases' ||
      lint_fail triggers "$file description names no trigger phrase (a quoted phrase, or 'Use when ...')"
    printf '%s' "$description $when_to_use" | grep -qiE '\bnot for\b' ||
      lint_fail triggers "$file description names no NOT-for case ('NOT for ...')"
  fi

  bytes=$(body_bytes "$file")
  if [ "$bytes" -gt 32768 ]; then
    lint_fail skill-size "$file body is $bytes bytes; the limit is 32,768. Move longer material to references/"
  elif [ "$bytes" -gt 16384 ]; then
    lint_warn skill-size "$file body is $bytes bytes; above 16,384, consider paging material from references/"
  fi

  scripts=$(find "$dir" -path "$dir/tests" -prune -o -type f \( -name '*.py' -o -name '*.sh' -o -name '*.js' -o -name '*.mjs' -o -name '*.ts' -o -name '*.rb' \) -print | head -n 1)
  if [ -n "$scripts" ] && [ ! -d "$dir/tests" ]; then
    lint_fail tests-dir "$dir ships a script ($scripts) but has no tests/ directory"
  fi

  grep -qE "^\| \`$skill\` \|" README.md 2>/dev/null ||
    lint_fail readme-rows "README.md has no table row for skill $skill"
done

for agent_file in agents/*.md; do
  agent=${agent_file##*/}
  agent=${agent%.md}
  grep -qE "^\| \`$agent\` \|" README.md 2>/dev/null ||
    lint_fail readme-rows "README.md has no table row for agent $agent"
done

# A skill runs its scripts from its own directory so both install paths work.
check_skill_path() {
  if grep -naF '.claude/skills/' "$1"; then
    lint_fail skill-path "$1 hard-codes .claude/skills/; call scripts through \${CLAUDE_SKILL_DIR}"
  fi
}
for skill_doc in skills/*/SKILL.md; do
  [ -f "$skill_doc" ] || continue
  check_skill_path "$skill_doc"
done
for refs_dir in skills/*/references; do
  [ -d "$refs_dir" ] || continue
  while IFS= read -r -d '' ref_file; do
    check_skill_path "$ref_file"
  done < <(find "$refs_dir" -type f -print0)
done

# Unprefixed commands: only the two manifests carry the plugin name.
prefixed="/${plugin_name}:"
if git ls-files -z | xargs -0 grep -naE "${prefixed}[a-z]" 2>/dev/null; then
  lint_fail prefixed-command "a file spells a command with the plugin prefix; write the bare command name"
fi

# No-person gate. Step 1: person-neutral shapes in every tracked file but LICENSE*.
if git ls-files -z -- ':!LICENSE*' | xargs -0 grep -naE \
  '/Users/[a-z][a-z0-9_-]*|-Users-[a-z]|\b[a-z0-9-]+\.home([^A-Za-z0-9_(]|$)|\bstanding (grant|election)\b|\bgranted to\b' |
  grep -vE '/Users/example\b|-Users-someone-app\b'; then
  lint_fail no-person-shapes 'a tracked file carries a home path, a .home host, or a grant phrase (lines above)'
fi

# Step 2: the deny list, case-insensitive; the repository's own URL is allowed.
if [ -z "${OWNER:-}" ] || [ -z "${REPO:-}" ]; then
  slug=$(git remote get-url origin 2>/dev/null | sed -n 's#.*github\.com[:/]\([^/]*\)/\([^/]*\)$#\1 \2#p')
  OWNER=${OWNER:-${slug%% *}}
  REPO=${REPO:-${slug#* }}
  REPO=${REPO%.git}
fi
in_ci=0
if [ "${CI:-}" = true ] || [ "${GITHUB_ACTIONS:-}" = true ]; then in_ci=1; fi
deny_status=0
if [ -n "${NO_PERSON_DENY:-}" ]; then
  printf '' | grep -iE -e "$NO_PERSON_DENY" > /dev/null 2>&1
  deny_status=$?
fi
if [ -z "${NO_PERSON_DENY:-}" ]; then
  if [ "$in_ci" -eq 1 ]; then
    lint_fail no-person-deny-config 'NO_PERSON_DENY is empty or unset in CI; the deny-list step cannot be skipped there'
  else
    printf 'LINT NOTE [no-person-deny]: NO_PERSON_DENY is unset; the deny-list step was skipped\n'
  fi
elif [ "$deny_status" -eq 2 ]; then
  lint_fail no-person-deny-config 'NO_PERSON_DENY is not a valid extended regular expression'
else
  own_url=
  if [ -n "$OWNER" ] && [ -n "$REPO" ]; then
    own_url="github.com/$OWNER/$REPO"
  fi
  # Cut only the repository's own URL text out of each line (a literal,
  # case-insensitive match of one allowed form (https://, http://,
  # ssh://git@, git@host:, or bare host/owner/repo) that starts a token: line
  # start, whitespace, an opening bracket or a quote; and ends at a
  # boundary: end of line, a path or query separator, .git, whitespace, a
  # quote or a closing bracket), then apply the deny expression to what is
  # left, so a denied term elsewhere on the same line is still found. Every
  # stage reads bytes as text (-a, the C locale, NUL turned into a space) so
  # a tracked binary file is scanned too.
  # A file that cannot be read, or a failing stage, fails the step.
  scan_tracked() {
    local f rc=0
    while IFS= read -r -d '' f; do
      [ -d "$f" ] && continue
      LC_ALL=C grep -naH -e '' -- "$f"
      # Status 1 is an empty file; anything above is a read error.
      [ "$?" -le 1 ] || rc=1
    done < <(git ls-files -z -- ':!LICENSE*')
    return "$rc"
  }
  scan_text() {
    scan_tracked | LC_ALL=C tr '\000' ' ' |
      LC_ALL=C awk -v url="$own_url" '
        BEGIN {
          u = tolower(url)
          ure = u
          gsub(/\./, "\\.", ure)
          urc = ure
          sub(/\//, ":", urc)
          # Allowed forms only: https://, http://, ssh://git@ or no scheme
          # before host/owner/repo, or git@host:owner/repo; each a whole token.
          re = "(^|[ \t(<\"\047`])((https?://|ssh://git@)?" ure "|git@" urc ")(\\.git)?([]/#?\"\047)>,;: \t]|\\.($|[ \t])|$)"
        }
        {
          out = ""
          rest = $0
          while (u != "" && match(tolower(rest), re)) {
            start = RSTART + index(substr(tolower(rest), RSTART, RLENGTH), "github.com") - 1
            out = out substr(rest, 1, start - 1)
            rest = substr(rest, start + length(u))
          }
          print out rest
        }'
  }
  scan_out=$(scan_text)
  scan_status=$?
  # Print only path:line; the matched text could be the denied term itself.
  deny_hits=$(printf '%s\n' "$scan_out" | LC_ALL=C grep -aiE -e "$NO_PERSON_DENY" |
    LC_ALL=C cut -d: -f1,2 | sed 's/^/denied term at /')
  if [ -n "$deny_hits" ]; then
    printf '%s\n' "$deny_hits"
    lint_fail no-person-deny 'a tracked file carries a denied term (locations above)'
  fi
  if [ "$scan_status" -ne 0 ]; then
    lint_fail no-person-deny-scan 'the deny-list scan could not read every tracked file'
  fi
fi

# Step 3: dated lines outside tests, except lines carrying a URL.
if git ls-files -- ':!LICENSE*' | grep -vE '(^|/)tests/|(^|/)test[_-][^/]*$' | tr '\n' '\0' |
  xargs -0 grep -naE '\b20[0-9]{2}-[0-9]{2}-[0-9]{2}\b' | grep -vE 'https?://'; then
  lint_fail no-person-dates 'a tracked non-test file carries a dated line (lines above)'
fi

# Identity: report only. History carries a non-noreply address by the owner's choice.
git log --format='%ae%n%ce' 2>/dev/null | sort -u | grep -v '@users\.noreply\.github\.com$' | grep . |
  sed 's/^/LINT NOTE [identity] (report only): /' | sed 's/[^ ]*@[^ ]*$/<address>/'

if [ "$fail" -eq 0 ]; then
  printf 'PASS: skills lint\n'
fi
exit "$fail"

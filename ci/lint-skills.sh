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
  # Hide only the repository's own URL from the deny scan, and only where a
  # whole whitespace-separated word is exactly one allowed form of it.
  # The word may carry one trailing mark (. , ; : ! ?) and one wrapper pair
  # ( ) < > " " ' ' or backticks. Allowed forms: https://, http://,
  # ssh://git@ or no scheme before github.com/owner/repo, and
  # git@github.com:owner/repo, each with an optional .git and an optional
  # tail starting with / ? or #. A one-word Markdown link [text](form) also
  # qualifies. Only the scheme, host, owner and repo characters are removed;
  # the link text, .git, tail, wrappers and punctuation are scanned as usual,
  # and any other word is scanned unchanged. Every stage reads bytes as text
  # (-a, the C locale, NUL turned into a space) so a tracked binary file is
  # scanned too. A file that cannot be read, or a failing stage, fails the step.
  # Each file is read on its own and its name never enters a record: a record
  # is the line number, a SOH byte, then the text (the number is digits, so the
  # first SOH after it is unambiguous). The shell holds the name and builds the
  # report as <name>:<line>. A CR before the line end is part of the line end,
  # not of the last word.
  scan_text() {
    LC_ALL=C awk -v owner="$OWNER" -v repo="$REPO" '
        BEGIN {
          sq = "\047"
          o = tolower(owner)
          r = tolower(repo)
          nf = 0
          if (o != "" && r != "") {
            forms[++nf] = "https://github.com/" o "/" r
            forms[++nf] = "http://github.com/" o "/" r
            forms[++nf] = "ssh://git@github.com/" o "/" r
            forms[++nf] = "git@github.com:" o "/" r
            forms[++nf] = "github.com/" o "/" r
          }
        }
        # formlen(c): length of the allowed form at the start of c when the
        # rest is nothing, or .git and/or a tail starting with / ? or #; else 0.
        function formlen(c,    i, f, rem) {
          for (i = 1; i <= nf; i++) {
            f = forms[i]
            if (substr(c, 1, length(f)) != f) continue
            rem = substr(c, length(f) + 1)
            if (substr(rem, 1, 4) == ".git") rem = substr(rem, 5)
            if (rem == "" || index("/?#", substr(rem, 1, 1)) > 0) return length(f)
          }
          return 0
        }
        # span(c, base): set gs/gl to the 1-based start and length, within the
        # word, of the text to hide when c (starting after base characters of
        # the word) is a form or a one-word Markdown link to one.
        function span(c, base,    fl, i, t, inner) {
          fl = formlen(c)
          if (fl > 0) { gs = base + 1; gl = fl; return 1 }
          if (substr(c, 1, 1) == "[" && substr(c, length(c), 1) == ")") {
            i = index(c, "](")
            if (i > 0) {
              t = substr(c, 2, i - 2)
              if (index(t, "[") == 0 && index(t, "]") == 0) {
                inner = substr(c, i + 2, length(c) - i - 2)
                fl = formlen(inner)
                if (fl > 0) { gs = base + i + 2; gl = fl; return 1 }
              }
            }
          }
          return 0
        }
        function pair(op, cl) {
          return (op == "(" && cl == ")") || (op == "<" && cl == ">") ||
            (op == "\"" && cl == "\"") || (op == sq && cl == sq) || (op == "`" && cl == "`")
        }
        # hide(w): w without the hidden form when w is exempt, else w.
        function hide(w,    len, tr, wr, e, b, ee, c) {
          len = length(w)
          for (tr = 0; tr <= 1; tr++) {
            e = len
            if (tr) {
              if (len < 1 || index(".,;:!?", substr(w, len, 1)) == 0) continue
              e = len - 1
            }
            for (wr = 0; wr <= 1; wr++) {
              b = 0
              ee = e
              if (wr) {
                if (e < 2 || !pair(substr(w, 1, 1), substr(w, e, 1))) continue
                b = 1
                ee = e - 1
              }
              c = tolower(substr(w, b + 1, ee - b))
              if (span(c, b)) return substr(w, 1, gs - 1) substr(w, gs + gl)
            }
          }
          return w
        }
        {
          body = $0
          match(body, /^[0-9]+:/)
          pre = substr(body, 1, RLENGTH - 1)
          body = substr(body, RLENGTH + 1)
          cr = ""
          if (substr(body, length(body), 1) == "\r") {
            cr = "\r"
            body = substr(body, 1, length(body) - 1)
          }
          out = ""
          while (nf > 0 && match(body, /^[ \t]*[^ \t]+/)) {
            seg = substr(body, 1, RLENGTH)
            body = substr(body, RLENGTH + 1)
            match(seg, /[^ \t]+$/)
            out = out substr(seg, 1, RSTART - 1) hide(substr(seg, RSTART))
          }
          print pre "\001" out body cr
        }'
  }
  # read_records <file>: the file's records; fails when any stage fails.
  read_records() {
    local st
    LC_ALL=C grep -na -e '' -- "$1" | LC_ALL=C tr '\000' ' ' | scan_text
    st=("${PIPESTATUS[@]}")
    # grep status 1 is an empty file; anything above is a read error.
    [ "${st[0]}" -le 1 ] && [ "${st[1]}" -eq 0 ] && [ "${st[2]}" -eq 0 ]
  }
  # Print only <path>:<line>; the matched text could be the denied term itself.
  # A denied term in a file name is reported once, at the path alone.
  scan_status=0
  deny_hits=
  soh=$(printf '\001')
  while IFS= read -r -d '' f; do
    [ -d "$f" ] && continue
    if printf '%s\n' "$f" | LC_ALL=C grep -aiqE -e "$NO_PERSON_DENY"; then
      deny_hits+="denied term at $f"$'\n'
    fi
    recs=$(read_records "$f") || scan_status=1
    lines=$(printf '%s\n' "$recs" | LC_ALL=C grep -aiE -e "$NO_PERSON_DENY" | LC_ALL=C cut -d"$soh" -f1)
    for n in $lines; do
      deny_hits+="denied term at $f:$n"$'\n'
    done
  done < <(git ls-files -z -- ':!LICENSE*')
  if [ -n "$deny_hits" ]; then
    printf '%s' "$deny_hits"
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

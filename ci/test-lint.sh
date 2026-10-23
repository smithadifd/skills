#!/usr/bin/env bash
# Test the skills lint. Every directory under ci/fixtures/lint (except base)
# holds a setup.sh that breaks one rule in a copy of the valid base tree, and
# an expect file with the message the lint must print. The lint must fail
# on each fixture with that message, pass on the base tree, and pass on the
# real tree.
set -uo pipefail

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
lint="$repo_dir/ci/lint-skills.sh"
fixtures="$repo_dir/ci/fixtures/lint"
# A synthetic deny term; the fixtures assemble it at run time.
deny="zz""planted-name"
failures=0

note_fail() {
  printf 'ASSERTION FAILED: %s\n' "$1" >&2
  failures=$((failures + 1))
}

# new_dir: print a fresh temporary directory, or stop the whole run.
new_dir() {
  local d
  d=$(mktemp -d "${TMPDIR:-/tmp}/lint-test.XXXXXX") || exit 1
  [ -n "$d" ] && [ -d "$d" ] || exit 1
  printf '%s\n' "$d"
}

# make_tree <dir>: a git repository holding the valid base tree.
make_tree() {
  [ -n "${1:-}" ] && [ -d "$1" ] || exit 1
  cp -R "$fixtures/base/." "$1"
  git -C "$1" init -q
}

# run_lint <tree>: the lint with a synthetic deny list, outside any CI setting.
run_lint() {
  env -u CI -u GITHUB_ACTIONS -u OWNER -u REPO NO_PERSON_DENY="$deny" bash "$lint" "$1" 2>&1
}

base=$(new_dir)
make_tree "$base"
git -C "$base" add -A
if out=$(run_lint "$base"); then
  printf 'PASS: base tree passes the lint\n'
else
  note_fail "base tree should pass the lint: $out"
fi

for dir in "$fixtures"/*/; do
  name=$(basename "$dir")
  [ "$name" != base ] || continue
  tree=$(new_dir)
  make_tree "$tree"
  (cd "$tree" && bash "$dir/setup.sh") || { note_fail "fixture $name: setup failed"; continue; }
  git -C "$tree" add -A
  expect=$(cat "$dir/expect")
  if out=$(run_lint "$tree"); then
    note_fail "fixture $name: the lint passed but should have failed"
  elif printf '%s\n' "$out" | grep -qF -- "$expect"; then
    printf 'PASS: fixture %s fails the lint with its message\n' "$name"
  else
    note_fail "fixture $name: the lint failed without the expected message ($expect): $out"
  fi
done

# Dated lines are allowed under tests/; the same line elsewhere is not.
tree=$(new_dir)
make_tree "$tree"
day=$(printf '%s-%s-%s' 2026 01 02)
printf 'Pinned at %s.\n' "$day" > "$tree/skills/demo/tests/fixture.txt"
git -C "$tree" add -A
if run_lint "$tree" > /dev/null; then
  printf 'PASS: a dated line under tests/ is allowed\n'
else
  note_fail 'a dated line under tests/ should pass'
fi

# The repository's own URL is allowed even when the deny list names its owner.
tree=$(new_dir)
make_tree "$tree"
git -C "$tree" remote add origin https://github.com/zzowner/zzrepo.git
{
  printf 'See https://github.com/zzowner/zzrepo for the source.\n'
  printf 'Clone https://github.com/zzowner/zzrepo.git now.\n'
  printf 'Docs: https://github.com/zzowner/zzrepo/issues, (https://github.com/zzowner/zzrepo)\n'
  printf 'Link "https://github.com/zzowner/zzrepo" and https://github.com/zzowner/zzrepo.\n'
  printf 'Last: https://github.com/zzowner/zzrepo\n'
  printf 'Plain http://github.com/zzowner/zzrepo.git and github.com/zzowner/zzrepo/pulls\n'
  printf 'Remote git@github.com:zzowner/zzrepo.git or ssh://git@github.com/zzowner/zzrepo.git\n'
  printf 'Bare (github.com/zzowner/zzrepo) <git@github.com:zzowner/zzrepo>\n'
}> "$tree/notes.md"
git -C "$tree" add -A
if env -u CI -u GITHUB_ACTIONS -u OWNER -u REPO NO_PERSON_DENY=zzowner bash "$lint" "$tree" > /dev/null 2>&1; then
  printf 'PASS: a line holding only the repository URL passes the deny step\n'
else
  note_fail 'a line holding only the repository URL should pass the deny step'
fi

# The own-URL exemption is word-exact. Every allowed form passes bare, with
# .git, with each tail, wrapped once, with one trailing mark, and as a one-word
# Markdown link; the deny list names the owner and a second owner.
url_deny='zzowner|zzother'
url_lint() {
  env -u CI -u GITHUB_ACTIONS -u OWNER -u REPO NO_PERSON_DENY="$url_deny" bash "$lint" "$1" > /dev/null 2>&1
}
tree=$(new_dir)
make_tree "$tree"
git -C "$tree" remote add origin https://github.com/zzowner/zzrepo.git
: > "$tree/notes.md"
pass_count=0
for form in https://github.com/zzowner/zzrepo http://github.com/zzowner/zzrepo \
  ssh://git@github.com/zzowner/zzrepo git@github.com:zzowner/zzrepo github.com/zzowner/zzrepo; do
  for tail in '' .git /issues/1 '?tab=readme' '#readme' .git/issues; do
    word="$form$tail"
    for wrapped in "$word" "($word)" "<$word>" "\"$word\"" "'$word'" "\`$word\`"; do
      for mark in '' . , ';' : '!' '?'; do
        printf 'x %s%s y\n' "$wrapped" "$mark" >> "$tree/notes.md"
        pass_count=$((pass_count + 1))
      done
    done
    printf '[repo](%s)\n[repo](%s).\n' "$word" "$word" >> "$tree/notes.md"
    pass_count=$((pass_count + 2))
  done
done
git -C "$tree" add -A
if url_lint "$tree"; then
  printf 'PASS: %s own-URL forms pass the deny step\n' "$pass_count"
else
  note_fail 'an allowed own-URL form should pass the deny step'
fi

# None of these may borrow the exemption: the owner (or a second denied owner
# in the tail) is scanned and the lint fails.
url_fail_cases=(
  'https://notgithub.com/zzowner/zzrepo'
  'https://evil.example/@github.com/zzowner/zzrepo'
  'https://evil.example/(github.com/zzowner/zzrepo)'
  'https://x.example/github.com/zzowner/zzrepo'
  'xgit@github.com:zzowner/zzrepo'
  'https://github.com/zzowner/zzrepox'
  'https://github.com/zzowner/zzrepo@evil.example'
  'https://evil.example/[x](https://github.com/zzowner/zzrepo)'
  '((https://github.com/zzowner/zzrepo))'
  'https://github.com/zzowner/zzrepo/../../zzother/x'
  '[my repo](https://github.com/zzowner/zzrepo)'
)
for text in "${url_fail_cases[@]}"; do
  tree=$(new_dir)
  make_tree "$tree"
  git -C "$tree" remote add origin https://github.com/zzowner/zzrepo.git
  printf 'See %s here.\n' "$text" > "$tree/notes.md"
  git -C "$tree" add -A
  if url_lint "$tree"; then
    note_fail "own-URL lookalike should fail the deny step: $text"
  else
    printf 'PASS: own-URL lookalike fails the deny step: %s\n' "$text"
  fi
done

# Record boundaries: a tracked file name with a colon, and a CRLF line end,
# must not change what the own-URL exemption sees. Each file starts a line
# with the repository URL (clean case), then the same file also carries a
# denied word (planted case), which must still fail.
own='https://github.com/zzowner/zzrepo'
for kind in colon-name crlf; do
  for variant in clean planted; do
    tree=$(new_dir)
    make_tree "$tree"
    git -C "$tree" remote add origin https://github.com/zzowner/zzrepo.git
    if [ "$kind" = colon-name ]; then
      target="$tree/notes:2.md"
      eol='\n'
    else
      target="$tree/notes.md"
      eol='\r\n'
    fi
    printf "$own is the source$eol$own$eol" > "$target"
    if [ "$variant" = planted ]; then
      printf "$own zzother wrote this$eol" >> "$target"
    fi
    git -C "$tree" add -A
    out=$(env -u CI -u GITHUB_ACTIONS -u OWNER -u REPO NO_PERSON_DENY="$url_deny" bash "$lint" "$tree" 2>&1)
    status=$?
    if [ "$status" -eq 0 ]; then
      if [ "$variant" = clean ]; then
        printf 'PASS: %s file with a leading own-URL passes the deny step\n' "$kind"
      else
        note_fail "$kind file with a denied word should fail the deny step"
      fi
    else
      if [ "$variant" = planted ]; then
        if printf '%s\n' "$out" | grep -qxF "denied term at ${target#"$tree"/}:3"; then
          printf 'PASS: %s file with a denied word fails and reports path:line\n' "$kind"
        else
          note_fail "$kind file: the report should hold the full path:line (${target#"$tree"/}:3): $out"
        fi
      else
        note_fail "$kind file with only a leading own-URL should pass the deny step"
      fi
    fi
  done
done

# A tree with a planted denied term, for the deny-list configuration checks.
tree=$(new_dir)
make_tree "$tree"
printf 'Written by %s today.\n' "$deny" > "$tree/notes.md"
git -C "$tree" add -A

# Locally, without NO_PERSON_DENY the deny step is skipped with a notice.
out=$(env -u CI -u GITHUB_ACTIONS -u NO_PERSON_DENY bash "$lint" "$tree" 2>&1)
status=$?
if [ "$status" -eq 0 ] && printf '%s\n' "$out" | grep -q 'NO_PERSON_DENY is unset'; then
  printf 'PASS: an unset deny list is skipped with a notice outside CI\n'
else
  note_fail "unset deny list should skip with a notice outside CI: $out"
fi

# In CI, an unset or empty deny list fails.
for ci_var in CI GITHUB_ACTIONS; do
  for value in unset empty; do
    if [ "$value" = unset ]; then
      out=$(env -u CI -u GITHUB_ACTIONS -u NO_PERSON_DENY "$ci_var=true" bash "$lint" "$tree" 2>&1)
    else
      out=$(env -u CI -u GITHUB_ACTIONS "$ci_var=true" NO_PERSON_DENY= bash "$lint" "$tree" 2>&1)
    fi
    status=$?
    if [ "$status" -ne 0 ] && printf '%s\n' "$out" | grep -qF 'LINT FAIL [no-person-deny-config]'; then
      printf 'PASS: %s=true with an %s deny list fails the lint\n' "$ci_var" "$value"
    else
      note_fail "$ci_var=true with an $value deny list should fail: $out"
    fi
  done
done

# An invalid deny expression is a configuration error, in or out of CI.
out=$(env -u CI -u GITHUB_ACTIONS -u OWNER -u REPO NO_PERSON_DENY='good|(unclosed' bash "$lint" "$tree" 2>&1)
status=$?
if [ "$status" -ne 0 ] && printf '%s\n' "$out" | grep -qF 'LINT FAIL [no-person-deny-config]' &&
  ! printf '%s\n' "$out" | grep -q '^PASS'; then
  printf 'PASS: an invalid deny expression fails the lint\n'
else
  note_fail "an invalid deny expression should fail: $out"
fi

# Outside a git work tree the tracked-file rules cannot run, so the lint fails.
plain=$(new_dir)
[ -n "$plain" ] || exit 1
cp -R "$fixtures/base/." "$plain"
out=$(env -u CI -u GITHUB_ACTIONS GIT_CEILING_DIRECTORIES="$(dirname "$plain")" NO_PERSON_DENY="$deny" bash "$lint" "$plain" 2>&1)
status=$?
if [ "$status" -ne 0 ] && printf '%s\n' "$out" | grep -qF 'LINT FAIL [git]'; then
  printf 'PASS: a tree that is not a git work tree fails the lint\n'
else
  note_fail "a non-git tree should fail the lint: $out"
fi

# The real tree passes.
if out=$(bash "$lint" "$repo_dir" 2>&1); then
  printf 'PASS: the repository passes the lint\n'
else
  note_fail "the repository should pass the lint: $out"
fi

if [ "$failures" -ne 0 ]; then
  printf '%s lint test(s) failed\n' "$failures" >&2
  exit 1
fi

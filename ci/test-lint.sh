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

# make_tree <dir>: a git repository holding the valid base tree.
make_tree() {
  cp -R "$fixtures/base/." "$1"
  git -C "$1" init -q
}

run_lint() { # tree [env assignments via the caller]
  NO_PERSON_DENY="$deny" bash "$lint" "$1" 2>&1
}

base=$(mktemp -d)
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
  tree=$(mktemp -d)
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
tree=$(mktemp -d)
make_tree "$tree"
day=$(printf '%s-%s-%s' 2026 01 02)
printf 'Pinned at %s.\n' "$day" > "$tree/skills/demo/tests/fixture.txt"
git -C "$tree" add -A
if run_lint "$tree" > /dev/null; then
  printf 'PASS: a dated line under tests/ is allowed\n'
else
  note_fail 'a dated line under tests/ should pass'
fi

# Without NO_PERSON_DENY the deny step is skipped with a notice, not failed.
tree=$(mktemp -d)
make_tree "$tree"
printf 'Written by %s today.\n' "$deny" > "$tree/notes.md"
git -C "$tree" add -A
out=$(env -u NO_PERSON_DENY bash "$lint" "$tree" 2>&1)
status=$?
if [ "$status" -eq 0 ] && printf '%s\n' "$out" | grep -q 'NO_PERSON_DENY is unset'; then
  printf 'PASS: an unset deny list is skipped with a notice\n'
else
  note_fail "unset deny list should skip with a notice: $out"
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

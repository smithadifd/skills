#!/usr/bin/env bash
# Content test: the reviewer agent text states each review rule.
# Usage: ci/reviewer-axes.sh [path-to-reviewer.md]
set -euo pipefail

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
file=${1:-"$repo_dir/agents/reviewer.md"}

fail() {
  printf 'ASSERTION FAILED: %s\n' "$1" >&2
  exit 1
}

[ -s "$file" ] || fail "reviewer text is missing or empty: $file"

count() {
  grep -F -c -- "$1" "$file" || true
}

first_line() {
  grep -F -n -m 1 -- "$1" "$file" | cut -d: -f1 || true
}

require() {
  [ "$(count "$2")" -gt 0 ] || fail "reviewer text lacks the $1 rule: $2"
}

spec='Spec compliance first'
standards='Standards second'
require 'spec-compliance axis' "$spec"
require 'standards axis' "$standards"
spec_at=$(first_line "$spec")
standards_at=$(first_line "$standards")
[ "$spec_at" -lt "$standards_at" ] || fail 'the standards axis comes before the spec-compliance axis'

require 'scope' 'report only correctness and requirement gaps'
require 'round-1 mutation' 'Round 1 of a code change runs a targeted mutation'
require 'round-1 executable owner' 'whenever you can execute one'
require 'mutation routing' 'route the mutation to a reviewer that can execute it'
require 'mutation fallback' 'this review carries the mutation'
require 'docs exemption' 'Docs-only changes need no mutation'
require 'delta review heading' 'Delta review (round 2 and later)'
require 'operative delta review' 'Review the change since the last reviewed commit'

printf 'PASS: reviewer text states spec-then-standards axes, scope, mutation ownership and routing, docs exemption, and operative delta review\n'

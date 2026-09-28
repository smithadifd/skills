#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"
unset HARNESS_ROOT

fail() {
  printf 'ASSERTION FAILED: %s\n' "$1" >&2
  exit 1
}

skill_paths=(skills/*)
((${#skill_paths[@]} > 0)) || fail 'no skills were discovered'

check_install_all() {
  local home skill_path skill_name
  home=$(mktemp -d)
  HOME="$home" ./install.sh --all
  for skill_path in "${skill_paths[@]}"; do
    [ -d "$skill_path" ] || continue
    skill_name=${skill_path##*/}
    [ -d "$home/.claude/skills/$skill_name" ] || fail "install-all did not install discovered skill: $skill_name"
  done
  printf 'PASS: install-all installed every discovered skill\n'
}

check_install_one() {
  local home selected skill_path
  selected=${skill_paths[0]##*/}
  home=$(mktemp -d)
  HOME="$home" ./install.sh "$selected"

  shopt -s nullglob dotglob
  local installed=("$home/.claude/skills"/*)
  ((${#installed[@]} == 1)) || fail "install-one installed ${#installed[@]} entries instead of exactly one: $selected"
  [ "${installed[0]##*/}" = "$selected" ] || fail "install-one installed ${installed[0]##*/} instead of: $selected"
  for skill_path in "${skill_paths[@]}"; do
    [ -d "$skill_path" ] || continue
    if [ "${skill_path##*/}" != "$selected" ] && [ -e "$home/.claude/skills/${skill_path##*/}" ]; then
      fail "install-one also installed unrequested skill: ${skill_path##*/}"
    fi
  done
  shopt -u nullglob dotglob
  printf 'PASS: install-one installed exactly one discovered skill\n'
}

count_reference_hits() {
  local matches
  if matches=$(grep -oiE 'foundry|harness_root' "$1"); then
    printf '%s\n' "$matches" | wc -l | tr -d ' '
  else
    printf '0\n'
  fi
}

check_references() {
  local home installed_root path relative expected actual
  local allowed_path allowed_count marker reason
  home=$(mktemp -d)
  HOME="$home" ./install.sh --all
  installed_root="$home/.claude/skills"

  while read -r allowed_path allowed_count marker reason; do
    [ "$marker" = "#" ] && [ -n "$reason" ] && [[ "$allowed_count" =~ ^[0-9]+$ ]] ||
      fail "invalid reference allowlist entry: $allowed_path $allowed_count $marker $reason"
    path="$installed_root/$allowed_path"
    actual=0
    [ ! -f "$path" ] || actual=$(count_reference_hits "$path")
    [ "$actual" = "$allowed_count" ] ||
      fail "reference count mismatch for $allowed_path: expected $allowed_count, found $actual"
  done < "$repo_dir/ci/reference-allowlist.txt"

  while IFS= read -r path; do
    relative=${path#"$installed_root/"}
    expected=
    while read -r allowed_path allowed_count marker reason; do
      if [ "$allowed_path" = "$relative" ]; then
        expected=$allowed_count
      fi
    done < "$repo_dir/ci/reference-allowlist.txt"
    actual=$(count_reference_hits "$path")
    [ -n "$expected" ] ||
      fail "reference count mismatch for $relative: expected 0, found $actual"
  done < <(grep -RilE 'foundry|harness_root' "$installed_root")

  printf 'PASS: installed references match the pinned allowlist\n'
}

check_uninstall() {
  local home
  home=$(mktemp -d)
  HOME="$home" ./install.sh --all
  HOME="$home" ./install.sh --uninstall --all

  shopt -s nullglob dotglob
  local leftovers=("$home/.claude/skills"/*)
  ((${#leftovers[@]} == 0)) || fail "uninstall-all left an entry behind: ${leftovers[0]}"
  shopt -u nullglob dotglob
  printf 'PASS: uninstall-all left the skills directory clean\n'
}

case ${1-} in
  install-all) check_install_all ;;
  install-one) check_install_one ;;
  references) check_references ;;
  uninstall) check_uninstall ;;
  '')
    check_install_all
    check_install_one
    check_references
    check_uninstall
    ;;
  *)
    printf 'Unknown check: %s\n' "$1" >&2
    exit 2
    ;;
esac

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

check_references() {
  local home matches
  home=$(mktemp -d)
  HOME="$home" ./install.sh --all
  if matches=$(grep -RilE 'foundry|harness_root' "$home/.claude/skills"); then
    fail "installed tree contains a forbidden reference: $matches"
  fi
  printf 'PASS: installed tree contains no forbidden references\n'
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

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

agent_paths=()
for agent_path in agents/*.md; do
  [ -f "$agent_path" ] || continue
  agent_paths+=("$agent_path")
done
((${#agent_paths[@]} > 0)) || fail 'no agents were discovered'

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
  if matches=$(grep -oiE 'harness_root' "$1"); then
    printf '%s\n' "$matches" | wc -l | tr -d ' '
  else
    printf '0\n'
  fi
}

check_outside_references() {
  local root matches reference
  root=$1
  # Allow ~/.claude, $HOME/.claude, and ${HOME}/.claude: installed skill
  # commands and defaults use the user's own skill and config directories.
  # Allow /Users/<user>/.claude and /home/<user>/.claude for the same
  # directory when a documented path is expanded to an absolute path.
  # Every other home, user, or temporary path into a project is external.
  matches=$(grep -RhoE '(~|\$HOME|\$\{HOME\}|/Users/[^/[:space:]]+|/home/[^/[:space:]]+|/tmp)/[[:alnum:]_.-]+(/[[:alnum:]_.-]+)+' "$root" || true)
  while IFS= read -r reference; do
    [ -n "$reference" ] || continue
    case "$reference" in
      '~/.claude/'*|'$HOME/.claude/'*|'${HOME}/.claude/'*) continue ;;
    esac
    [[ "$reference" =~ ^/(Users|home)/[^/]+/\.claude/ ]] && continue
    fail "installed files contain an outside project reference: $reference"
  done <<< "$matches"
}

check_private_words() {
  local root path
  root=$1
  while IFS= read -r -d '' path; do
    [ -f "$path" ] || fail "installed file contains an unreadable symlink: $path"
    if grep -iqE '(^|[^[:alnum:]_])(foundry|claude-toolkit)([^[:alnum:]_]|$)' "$path"; then
      fail "installed file contains a private project name: $path"
    fi
  done < <(find "$root" -path '*/tests' -prune -o \( -type f -o -type l \) -print0)
}

check_references() {
  local home installed_root path relative expected actual matches
  local allowed_path allowed_count marker reason
  home=$(mktemp -d)
  HOME="$home" ./install.sh --all
  HOME="$home" ./install.sh --all-agents
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
  done < <(grep -RilE 'harness_root' "$installed_root")

  printf 'PASS: installed references match the pinned allowlist\n'
  check_outside_references "$installed_root"
  check_private_words "$installed_root"
  if matches=$(grep -RilE 'harness_root' "$home/.claude/agents"); then
    fail "installed agents contain a forbidden reference: $matches"
  fi
  check_outside_references "$home/.claude/agents"
  check_private_words "$home/.claude/agents"
  printf 'PASS: installed agents contain no forbidden references\n'
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

check_install_all_agents() {
  local home agent_path agent_name
  home=$(mktemp -d)
  HOME="$home" ./install.sh --all-agents
  for agent_path in "${agent_paths[@]}"; do
    [ -f "$agent_path" ] || continue
    agent_name=${agent_path##*/}
    agent_name=${agent_name%.md}
    [ -f "$home/.claude/agents/$agent_name.md" ] || fail "--all-agents did not install discovered agent: $agent_name"
  done
  printf 'PASS: install-all-agents installed every discovered agent\n'
}

check_install_one_agent() {
  local home selected agent_path
  selected=${agent_paths[0]##*/}
  selected=${selected%.md}
  home=$(mktemp -d)
  HOME="$home" ./install.sh --agent "$selected"

  shopt -s nullglob dotglob
  local installed=("$home/.claude/agents"/*)
  ((${#installed[@]} == 1)) || fail "--agent did not install exactly one agent (installed ${#installed[@]} files, wanted only $selected)"
  [ "${installed[0]##*/}" = "$selected.md" ] || fail "--agent installed ${installed[0]##*/} instead of exactly one: $selected.md"
  for agent_path in "${agent_paths[@]}"; do
    [ -f "$agent_path" ] || continue
    if [ "${agent_path##*/}" != "$selected.md" ] && [ -e "$home/.claude/agents/${agent_path##*/}" ]; then
      fail "--agent also installed unrequested agent: ${agent_path##*/}"
    fi
  done
  shopt -u nullglob dotglob
  printf 'PASS: install-one-agent installed exactly one discovered agent\n'
}

check_uninstall_agents() {
  local home agent_path agent_name
  home=$(mktemp -d)
  HOME="$home" ./install.sh --all-agents
  for agent_path in "${agent_paths[@]}"; do
    [ -f "$agent_path" ] || continue
    agent_name=${agent_path##*/}
    agent_name=${agent_name%.md}
    [ -f "$home/.claude/agents/$agent_name.md" ] || fail "uninstall-agents precondition failed; $agent_name was not installed"
  done
  HOME="$home" ./install.sh --uninstall --all-agents

  shopt -s nullglob dotglob
  local leftovers=("$home/.claude/agents"/*)
  ((${#leftovers[@]} == 0)) || fail "uninstall-all-agents left an agent behind: ${leftovers[0]}"
  shopt -u nullglob dotglob
  printf 'PASS: uninstall-all-agents left the agents directory clean\n'
}

check_agent_traversal() {
  local scratch home repo err
  scratch=$(mktemp -d)
  home="$scratch/home"
  repo="$scratch/repo"
  mkdir -p -- "$home" "$repo/agents"
  ln -s -- "$repo_dir/install.sh" "$repo/install.sh"
  printf 'traversal-payload\n' > "$scratch/x.md"
  err="$scratch/err"
  # $0 is the symlink, so agents/../../x.md is the planted file. Slash
  # rejection must refuse that name before the path is read.
  if HOME="$home" "$repo/install.sh" --agent '../../x' >"$scratch/out" 2>"$err"; then
    fail "path traversal via --agent ../../x was accepted"
  fi
  if [ -e "$home/x.md" ] || [ -e "$home/.claude/x.md" ]; then
    fail "path traversal via --agent ../../x was accepted"
  fi
  grep -F -q 'Unknown agent: ../../x' "$err" || fail "path traversal via --agent ../../x was not rejected"
  printf 'PASS: path traversal via --agent ../../x was rejected\n'
}

check_agent_unknown() {
  local home err
  home=$(mktemp -d)
  err="$home/err"
  if HOME="$home" ./install.sh --agent 'not-a-real-agent' >"$home/out" 2>"$err"; then
    fail "unknown agent name was accepted: not-a-real-agent"
  fi
  grep -F -q 'Unknown agent: not-a-real-agent' "$err" || fail "unknown agent name was not rejected: not-a-real-agent"
  printf 'PASS: unknown agent name was rejected\n'
}

case ${1-} in
  install-all) check_install_all ;;
  install-one) check_install_one ;;
  references) check_references ;;
  outside-references) [ "$#" -eq 2 ] || fail 'outside-references needs a directory'; check_outside_references "$2" ;;
  uninstall) check_uninstall ;;
  install-all-agents) check_install_all_agents ;;
  install-one-agent) check_install_one_agent ;;
  uninstall-agents) check_uninstall_agents ;;
  agent-traversal) check_agent_traversal ;;
  agent-unknown) check_agent_unknown ;;
  '')
    check_install_all
    check_install_one
    check_references
    check_uninstall
    check_install_all_agents
    check_install_one_agent
    check_uninstall_agents
    check_agent_traversal
    check_agent_unknown
    ;;
  *)
    printf 'Unknown check: %s\n' "$1" >&2
    exit 2
    ;;
esac

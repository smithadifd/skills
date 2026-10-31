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
  local root path deny_status stripped slug
  root=$1
  # The private words come from NO_PERSON_DENY, a grep extended regular
  # expression (term1|term2|...), as in ci/lint-skills.sh. No name is spelled
  # in this repository. Unset or empty, the word scan is skipped with a notice.
  if [ -z "${NO_PERSON_DENY:-}" ]; then
    printf 'NOTE: NO_PERSON_DENY is unset; the private-word scan of installed files was skipped\n'
  else
    deny_status=0
    printf '' | grep -iE -e "$NO_PERSON_DENY" > /dev/null 2>&1 || deny_status=$?
    [ "$deny_status" -ne 2 ] || fail 'NO_PERSON_DENY is not a valid extended regular expression'
    # The repository's own URL is allowed, as in the lint: OWNER and REPO come
    # from the environment, else from the origin remote.
    if [ -z "${OWNER:-}" ] || [ -z "${REPO:-}" ]; then
      slug=$(git remote get-url origin 2>/dev/null | sed -n 's#.*github\.com[:/]\([^/]*\)/\([^/]*\)$#\1 \2#p' || true)
      OWNER=${OWNER:-${slug%% *}}
      REPO=${REPO:-${slug#* }}
      REPO=${REPO%.git}
    fi
  fi
  while IFS= read -r -d '' path; do
    [ -f "$path" ] || fail "installed file contains an unreadable symlink: $path"
    [ -n "${NO_PERSON_DENY:-}" ] || continue
    stripped=$({ LC_ALL=C grep -na -e '' -- "$path" || [ $? -eq 1 ]; } |
      LC_ALL=C tr '\000' ' ' |
      LC_ALL=C awk -v owner="${OWNER:-}" -v repo="${REPO:-}" -f "$repo_dir/ci/strip-own-url.awk" |
      LC_ALL=C cut -d "$(printf '\001')" -f 2-) || fail "installed file could not be scanned: $path"
    deny_status=0
    LC_ALL=C grep -aiqE -e "$NO_PERSON_DENY" <<< "$stripped" || deny_status=$?
    case "$deny_status" in
      0) fail "installed file contains a private project name: $path" ;;
      1) ;;
      *) fail "installed file could not be scanned: $path" ;;
    esac
  done < <(find "$root" -regex "$root/[^/]*/tests" -type d -prune -o \( -type f -o -type l \) -print0)
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

check_retired_agents() {
  local home fixture="$repo_dir/ci/fixtures/retired-planner.md" out
  # Unmodified copy from the last shipped version: an update removes it.
  home=$(mktemp -d)
  mkdir -p "$home/.claude/agents"
  cp "$fixture" "$home/.claude/agents/planner.md"
  HOME="$home" ./install.sh --all-agents
  [ ! -e "$home/.claude/agents/planner.md" ] || fail "--all-agents left an unmodified retired agent installed"
  [ -f "$home/.claude/agents/reviewer.md" ] || fail "--all-agents did not install the shipped agents beside a retired one"

  # User-modified copy: warned about and kept.
  home=$(mktemp -d)
  mkdir -p "$home/.claude/agents"
  cp "$fixture" "$home/.claude/agents/planner.md"
  printf 'my own change\n' >> "$home/.claude/agents/planner.md"
  out=$(HOME="$home" ./install.sh --all-agents 2>&1)
  [ -f "$home/.claude/agents/planner.md" ] || fail "--all-agents removed a user-modified retired agent"
  case "$out" in *'Warning: retired agent planner'*) ;; *) fail "--all-agents did not warn about a kept retired agent" ;; esac

  # Explicit uninstall of a retired name: removes an unmodified copy, keeps a modified one.
  home=$(mktemp -d)
  mkdir -p "$home/.claude/agents"
  cp "$fixture" "$home/.claude/agents/planner.md"
  HOME="$home" ./install.sh --uninstall --agent planner
  [ ! -e "$home/.claude/agents/planner.md" ] || fail "--uninstall --agent did not remove an unmodified retired agent"
  home=$(mktemp -d)
  mkdir -p "$home/.claude/agents"
  cp "$fixture" "$home/.claude/agents/planner.md"
  printf 'my own change\n' >> "$home/.claude/agents/planner.md"
  HOME="$home" ./install.sh --uninstall --agent planner 2>/dev/null
  [ -f "$home/.claude/agents/planner.md" ] || fail "--uninstall --agent removed a user-modified retired agent"
  printf 'PASS: retired agents are removed only when unmodified\n'
}

check_reviewer_axes() {
  "$repo_dir/ci/reviewer-axes.sh"
}

check_lint() {
  "$repo_dir/ci/lint-skills.sh"
}

check_lint_fixtures() {
  "$repo_dir/ci/test-lint.sh"
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
  retired-agents) check_retired_agents ;;
  reviewer-axes) check_reviewer_axes ;;
  lint) check_lint ;;
  lint-fixtures) check_lint_fixtures ;;
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
    check_retired_agents
    check_reviewer_axes
    check_lint
    check_lint_fixtures
    ;;
  *)
    printf 'Unknown check: %s\n' "$1" >&2
    exit 2
    ;;
esac

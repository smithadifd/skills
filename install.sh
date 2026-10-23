#!/usr/bin/env bash
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
skills_dir="$repo_dir/skills"
install_dir="$HOME/.claude/skills"
agents_dir="$repo_dir/agents"
agents_install_dir="$HOME/.claude/agents"

# Agents that no longer ship, as name:sha256 of the last shipped version. An
# installed copy is removed only if it is byte-identical to that version (or a
# link into this repository); anything else is the user's and is left alone.
retired_agents="planner:b67f0ee09c9678698e24ef7e051f53c997f1d9f35187ff335f02eaec3830731a"

usage() {
  printf 'Usage: %s <name> | --all | --agent <name> | --all-agents | --uninstall <name|--all|--agent <name>|--all-agents>\n' "${0##*/}" >&2
  exit 2
}

require_skill() {
  skill_name=$1
  case "$skill_name" in
    ''|.|..|*/*) ;;
    *)
      if [ -d "$skills_dir/$skill_name" ]; then
        return
      fi
      ;;
  esac
  printf 'Unknown skill: %s\n' "$skill_name" >&2
  exit 1
}

install_one() {
  skill_name=$1
  require_skill "$skill_name"
  mkdir -p -- "$install_dir"
  if [ -e "$install_dir/$skill_name" ]; then
    rm -r -- "$install_dir/$skill_name"
  fi
  mkdir -p -- "$install_dir/$skill_name"
  cp -R -- "$skills_dir/$skill_name/." "$install_dir/$skill_name/"
  find "$install_dir/$skill_name" \( -name __pycache__ -o -name .pytest_cache \) -type d -prune -exec rm -r {} +
  find "$install_dir/$skill_name" -name '*.pyc' -type f -exec rm -f {} +
  printf 'Installed %s\n' "$skill_name"
}

uninstall_one() {
  skill_name=$1
  require_skill "$skill_name"
  if [ -e "$install_dir/$skill_name" ]; then
    rm -r -- "$install_dir/$skill_name"
  fi
  printf 'Uninstalled %s\n' "$skill_name"
}

install_all() {
  for skill_path in "$skills_dir"/*; do
    [ -d "$skill_path" ] || continue
    install_one "${skill_path##*/}"
  done
}

uninstall_all() {
  for skill_path in "$skills_dir"/*; do
    [ -d "$skill_path" ] || continue
    uninstall_one "${skill_path##*/}"
  done
}

retired_sha() {
  for retired_entry in $retired_agents; do
    if [ "${retired_entry%%:*}" = "$1" ]; then
      printf '%s\n' "${retired_entry#*:}"
      return 0
    fi
  done
  return 1
}

file_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -- "$1" | cut -d' ' -f1
  else
    shasum -a 256 -- "$1" | cut -d' ' -f1
  fi
}

# Remove an installed retired agent when this installer put it there.
# Prints one line when it removes or warns; silent when nothing is installed.
retire_one_agent() {
  retired_name=$1
  retired_hash=$(retired_sha "$retired_name") || return 0
  retired_path="$agents_install_dir/$retired_name.md"
  if [ -L "$retired_path" ]; then
    retired_target=$(readlink -- "$retired_path")
    case "$retired_target" in
      "$repo_dir"/*)
        rm -- "$retired_path"
        printf 'Removed retired agent %s\n' "$retired_name"
        ;;
      *)
        printf 'Warning: retired agent %s is a link elsewhere; left in place\n' "$retired_name" >&2
        ;;
    esac
  elif [ -f "$retired_path" ]; then
    if [ "$(file_sha256 "$retired_path")" = "$retired_hash" ]; then
      rm -- "$retired_path"
      printf 'Removed retired agent %s\n' "$retired_name"
    else
      printf 'Warning: retired agent %s has local changes; left in place at %s\n' "$retired_name" "$retired_path" >&2
    fi
  fi
}

retire_all_agents() {
  for retired_entry in $retired_agents; do
    retire_one_agent "${retired_entry%%:*}"
  done
}

require_agent() {
  agent_name=$1
  case "$agent_name" in
    ''|.|..|*/*) ;;
    *)
      if [ -f "$agents_dir/$agent_name.md" ]; then
        return
      fi
      ;;
  esac
  printf 'Unknown agent: %s\n' "$agent_name" >&2
  exit 1
}

install_one_agent() {
  agent_name=$1
  require_agent "$agent_name"
  mkdir -p -- "$agents_install_dir"
  if [ -e "$agents_install_dir/$agent_name.md" ]; then
    rm -r -- "$agents_install_dir/$agent_name.md"
  fi
  cp -f -- "$agents_dir/$agent_name.md" "$agents_install_dir/$agent_name.md"
  printf 'Installed agent %s\n' "$agent_name"
}

uninstall_one_agent() {
  agent_name=$1
  if [ ! -f "$agents_dir/$agent_name.md" ] && retired_sha "$agent_name" >/dev/null; then
    retire_one_agent "$agent_name"
    return
  fi
  require_agent "$agent_name"
  if [ -e "$agents_install_dir/$agent_name.md" ]; then
    rm -r -- "$agents_install_dir/$agent_name.md"
  fi
  printf 'Uninstalled agent %s\n' "$agent_name"
}

install_all_agents() {
  for agent_path in "$agents_dir"/*.md; do
    [ -f "$agent_path" ] || continue
    agent_name=${agent_path##*/}
    agent_name=${agent_name%.md}
    install_one_agent "$agent_name"
  done
  retire_all_agents
}

uninstall_all_agents() {
  for agent_path in "$agents_dir"/*.md; do
    [ -f "$agent_path" ] || continue
    agent_name=${agent_path##*/}
    agent_name=${agent_name%.md}
    uninstall_one_agent "$agent_name"
  done
  retire_all_agents
}

case ${1-} in
  --all)
    [ "$#" -eq 1 ] || usage
    install_all
    ;;
  --all-agents)
    [ "$#" -eq 1 ] || usage
    install_all_agents
    ;;
  --agent)
    [ "$#" -eq 2 ] || usage
    install_one_agent "$2"
    ;;
  --uninstall)
    case ${2-} in
      --all)
        [ "$#" -eq 2 ] || usage
        uninstall_all
        ;;
      --all-agents)
        [ "$#" -eq 2 ] || usage
        uninstall_all_agents
        ;;
      --agent)
        [ "$#" -eq 3 ] || usage
        uninstall_one_agent "$3"
        ;;
      '')
        usage
        ;;
      *)
        [ "$#" -eq 2 ] || usage
        uninstall_one "$2"
        ;;
    esac
    ;;
  '')
    usage
    ;;
  *)
    [ "$#" -eq 1 ] || usage
    install_one "$1"
    ;;
esac

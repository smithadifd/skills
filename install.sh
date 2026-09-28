#!/usr/bin/env bash
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
skills_dir="$repo_dir/skills"
install_dir="$HOME/.claude/skills"
agents_dir="$repo_dir/agents"
agents_install_dir="$HOME/.claude/agents"

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
}

uninstall_all_agents() {
  for agent_path in "$agents_dir"/*.md; do
    [ -f "$agent_path" ] || continue
    agent_name=${agent_path##*/}
    agent_name=${agent_name%.md}
    uninstall_one_agent "$agent_name"
  done
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

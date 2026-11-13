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
# `skills/_lib` is shared code, not a skill: it has no SKILL.md and installs
# beside every skill. Everything below that counts skills leaves it out.
real_skill_paths=()
for skill_path in "${skill_paths[@]}"; do
  [ "${skill_path##*/}" = "_lib" ] || real_skill_paths+=("$skill_path")
done
((${#real_skill_paths[@]} > 0)) || fail 'no skills were discovered'

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
  [ -f "$home/.claude/skills/_lib/ledger_cli.py" ] || fail 'install-all did not install the shared _lib beside the skills'
  printf 'PASS: install-all installed every discovered skill\n'
}

check_install_one() {
  local home selected skill_path
  selected=${real_skill_paths[0]##*/}
  home=$(mktemp -d)
  HOME="$home" ./install.sh "$selected"

  shopt -s nullglob dotglob
  # The selected skill and the shared `_lib` beside it, and nothing else.
  local installed=("$home/.claude/skills"/*)
  ((${#installed[@]} == 2)) || fail "install-one installed ${#installed[@]} entries instead of the skill and _lib: $selected"
  [ -d "$home/.claude/skills/$selected" ] || fail "install-one did not install: $selected"
  [ -f "$home/.claude/skills/_lib/ledger_cli.py" ] || fail "install-one did not install the shared _lib beside: $selected"
  for skill_path in "${real_skill_paths[@]}"; do
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

# The stand-alone check: the ledger repository at one pinned tag plus this
# repository's skills, alone, with no other tooling. It installs the sdlc skills
# into a throwaway home, clones the ledger at its pinned tag (the one use of the
# network), and drives the hooks the tag's own plugin registers with recorded
# hook inputs against a temporary database. No model is called. Every step
# that fails is named in the message, and none is skipped.
LEDGER_E2E_URL=https://github.com/smithadifd/ledger.git
LEDGER_E2E_TAG=v0.2.0
# The commit the tag points at: the clone is checked against it.
LEDGER_E2E_SHA=ba1e6758c276ea1ab8ba8b8fd05dd40a142d9f18

ledger_e2e_fail() {
  fail "ledger-e2e step $1: $2"
}

# The command lines hooks.json registers for one event (and tool), one per line.
ledger_e2e_hook_commands() {
  "$1" -I - "$2" "$3" "$4" <<'PY'
import json, re, sys
path, event, tool = sys.argv[1:4]
config = json.load(open(path, encoding="utf-8"))
for entry in config["hooks"].get(event, []):
    matcher = entry.get("matcher", "")
    if tool and matcher not in ("", "*") and not re.fullmatch(matcher, tool):
        continue
    for hook in entry["hooks"]:
        print(hook["command"])
PY
}

# Run every command hooks.json names for one event with a recorded input on
# stdin, as Claude Code does. Prints their stdout; a non-zero exit fails the step.
ledger_e2e_run_hook() {
  local step=$1 event=$2 tool=$3 input=$4 command out commands
  commands=$(ledger_e2e_hook_commands "$ledger_e2e_python" "$ledger_e2e_hooks" "$event" "$tool") ||
    ledger_e2e_fail "$step" 'hooks.json could not be read'
  while IFS= read -r command; do
    [ -n "$command" ] || continue
    out=$("${ledger_e2e_env[@]}" CLAUDE_PLUGIN_ROOT="$ledger_e2e_plugin" /bin/sh -c "$command" < "$input") ||
      ledger_e2e_fail "$step" "the registered $event hook exited non-zero"
    printf '%s' "$out"
  done <<< "$commands"
}

# One ledger call under the throwaway environment, by explicit path.
ledger_e2e_ledger() {
  "${ledger_e2e_env[@]}" LEDGER_HARNESS=ci "$ledger_e2e_bin/ledger" "$@"
}

# Read a JSON envelope on stdin and print the value of a Python expression on `d`, or the refusal code.
ledger_e2e_json() {
  "$ledger_e2e_python" -I -c 'import json, sys; e = json.load(sys.stdin); d = e["data"]; print(eval(sys.argv[1]) if e["ok"] else "refused: " + str(e["error"]["code"]))' "$1"
}

check_ledger_e2e() {
  local scratch home python out asked name
  local fixtures="$repo_dir/ci/fixtures"
  local f_session="$fixtures/ledger-e2e-session-start.json"
  local f_prompt="$fixtures/ledger-e2e-prompt.json"
  local f_picker="$fixtures/ledger-e2e-picker.json"
  scratch=$(mktemp -d)
  home="$scratch/home"
  mkdir -p -- "$home" "$scratch/bin" "$scratch/data"
  for name in "$f_session" "$f_prompt" "$f_picker"; do
    [ -f "$name" ] || ledger_e2e_fail fixtures "missing recorded hook input: ${name##*/}"
  done

  # The interpreter that runs the pinned ledger, by absolute path.
  python=$(command -v python3) || ledger_e2e_fail python 'no python3 on PATH'
  "$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' ||
    ledger_e2e_fail python "the pinned ledger needs Python 3.11 or newer; $python is older"
  ledger_e2e_python=$python

  # The sdlc plugin from this working tree. It declares no hooks; its skills
  # install into the throwaway home the way a plugin install lays them.
  HOME="$home" ./install.sh --all > /dev/null || ledger_e2e_fail sdlc-install 'install.sh --all failed'
  [ -f "$home/.claude/skills/ticket/ticket.py" ] || ledger_e2e_fail sdlc-install 'the ticket skill was not installed'
  [ -f "$home/.claude/skills/_lib/ledger_cli.py" ] || ledger_e2e_fail sdlc-install 'the shared _lib was not installed'
  # The throwaway install itself must hold no hooks. install.sh writes only
  # ~/.claude/skills/<name>/, so ~/.claude/hooks must not exist at all (a
  # dangling symlink counts; nothing is walked), no settings*.json under
  # ~/.claude may carry a "hooks" key, and no installed skill's frontmatter
  # may declare "hooks:".
  local hooks_path="$home/.claude/hooks"
  if [ -e "$hooks_path" ] || [ -L "$hooks_path" ]; then
    ledger_e2e_fail install "install.sh --all created $hooks_path: $(ls -la -- "$hooks_path" 2>&1 | tr '\n' ' ')"
  fi
  local installed_hooks
  # -E -P rather than -I: PyYAML may live in the user site, which -I drops.
  installed_hooks=$("$python" -E -P - "$home/.claude" <<'PY'
import json, os, re, sys
try:
    import yaml
except ImportError:
    print("yaml-unavailable")
    sys.exit(0)
root = sys.argv[1]
found = []
for base, _dirs, files in os.walk(root):
    for name in files:
        path = os.path.join(base, name)
        if re.fullmatch(r"settings.*\.json", name):
            try:
                data = json.load(open(path, encoding="utf-8"))
            except (OSError, ValueError):
                found.append("settings:" + path + " (unreadable)")
                continue
            if isinstance(data, dict) and "hooks" in data:
                found.append("settings:" + path + " (hooks key)")
        elif name == "SKILL.md":
            lines = open(path, encoding="utf-8").read().split("\n")
            if not lines or lines[0].strip() != "---":
                found.append("frontmatter:" + path + " (no opening ---)")
                continue
            end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
            if end is None:
                found.append("frontmatter:" + path + " (no closing ---)")
                continue
            try:
                meta = yaml.safe_load("\n".join(lines[1:end]))
            except yaml.YAMLError as exc:
                found.append("frontmatter:" + path + " (does not parse: " + str(exc).replace("\n", " ") + ")")
                continue
            if not isinstance(meta, dict):
                found.append("frontmatter:" + path + " (not a mapping)")
            elif "hooks" in meta:
                found.append("skill:" + path)
print("\n".join(found))
PY
  ) || ledger_e2e_fail install 'the installed home could not be scanned for hooks'
  case $installed_hooks in
    '') ;;
    yaml-unavailable) ledger_e2e_fail install 'PyYAML cannot be imported, so skill frontmatter cannot be parsed' ;;
    skill:*) ledger_e2e_fail install "installed skill declares hooks: ${installed_hooks#skill:}" ;;
    frontmatter:*) ledger_e2e_fail install "installed skill frontmatter is unusable: ${installed_hooks#frontmatter:}" ;;
    *) ledger_e2e_fail install "install.sh --all wrote a hooks key: ${installed_hooks#settings:}" ;;
  esac
  "$python" -I - "$repo_dir" <<'PY' || ledger_e2e_fail sdlc-plugin 'the sdlc plugin declares hooks, or its manifest is unreadable'
import json, os, sys
root = sys.argv[1]
for name in ("plugin.json", "marketplace.json"):
    manifest = json.load(open(os.path.join(root, ".claude-plugin", name), encoding="utf-8"))
    assert "hooks" not in manifest, name
assert not os.path.exists(os.path.join(root, "hooks")), "hooks/"
PY

  # The pinned ledger: the tag's own tree, checked against its commit.
  git clone -q --depth 1 --branch "$LEDGER_E2E_TAG" "$LEDGER_E2E_URL" "$scratch/ledger" 2> /dev/null ||
    ledger_e2e_fail ledger-install "git clone of $LEDGER_E2E_URL at $LEDGER_E2E_TAG failed"
  [ "$(git -C "$scratch/ledger" rev-parse HEAD)" = "$LEDGER_E2E_SHA" ] ||
    ledger_e2e_fail ledger-install "$LEDGER_E2E_TAG is not commit $LEDGER_E2E_SHA"
  ledger_e2e_plugin="$scratch/ledger/plugins/ledger-hooks"
  ledger_e2e_hooks="$ledger_e2e_plugin/hooks/hooks.json"
  [ -f "$ledger_e2e_hooks" ] || ledger_e2e_fail hooks-file "the tag has no plugins/ledger-hooks/hooks/hooks.json"

  # The only `ledger` on PATH is the pinned one: PATH holds the scratch bin
  # folder and the system folders, and the CLI is called by explicit path.
  ledger_e2e_bin="$scratch/bin"
  printf '#!/bin/sh\nPYTHONPATH=%s PYTHONDONTWRITEBYTECODE=1 exec %s -m ledger "$@"\n' "$scratch/ledger" "$python" > "$ledger_e2e_bin/ledger"
  chmod +x "$ledger_e2e_bin/ledger"
  ledger_e2e_env=(env -i HOME="$home" PATH="$ledger_e2e_bin:/usr/bin:/bin" LEDGER_DB="$scratch/data/ledger.db" LANG=C.UTF-8 TICKET_LEDGER_CLI="$ledger_e2e_bin/ledger")

  # Step 1: session start prints the brief.
  out=$(ledger_e2e_run_hook session-start SessionStart '' "$f_session")
  case $out in
    'LEDGER AGENT CONTRACT'*) ;;
    *) ledger_e2e_fail session-start 'the SessionStart hook did not print the ledger brief' ;;
  esac
  printf 'PASS: ledger-e2e session start printed the brief\n'

  # The database and the open ask the picker answer is for. The ask is event 2,
  # the event the recorded picker input names.
  ledger_e2e_ledger new --id it-e2e --type gate --title 'e2e fixture' > /dev/null ||
    ledger_e2e_fail setup 'could not create the fixture item'
  asked=$(ledger_e2e_ledger --json append --item it-e2e --kind decision.asked --payload \
    '{"question":"Ship the release today?","options":[{"label":"ship","description":"Ship it today"},{"label":"hold","description":"Hold it until Friday"}],"scope":"this release"}' |
    ledger_e2e_json 'd["event"]["id"]') || ledger_e2e_fail setup 'could not record the fixture ask'
  [ "$asked" = 2 ] || ledger_e2e_fail setup "the fixture ask is event $asked, but the recorded picker input names event 2"

  # Step 2: a submitted prompt is captured.
  ledger_e2e_run_hook prompt-capture UserPromptSubmit '' "$f_prompt" > /dev/null
  out=$(ledger_e2e_ledger --json events --kind utterance.captured | ledger_e2e_json '[e["payload"].get("text") for e in d["events"]]') ||
    ledger_e2e_fail prompt-capture 'could not read the captured utterances'
  [ "$out" = "['Ship the release notes today, and hold the migration until Friday.']" ] ||
    ledger_e2e_fail prompt-capture "the prompt was not captured once as recorded; captures: $out"
  printf 'PASS: ledger-e2e prompt captured\n'

  # Step 3: a picker answer is captured; step 4 binds it. When the plugin
  # registers no working picker hook, nothing was captured and pick has no
  # answer to bind, so that step is the one that goes red.
  ledger_e2e_run_hook pick PostToolUse AskUserQuestion "$f_picker" > /dev/null
  out=$(ledger_e2e_ledger --json events --kind utterance.captured | ledger_e2e_json '[e["payload"]["picker"]["chosen_label"] for e in d["events"] if "picker" in e["payload"]]') ||
    ledger_e2e_fail pick 'could not read the captured utterances'
  [ "$out" = "['hold']" ] ||
    ledger_e2e_fail pick "no picker answer was captured for pick to bind (the plugin's PostToolUse AskUserQuestion hook did not capture it); picker captures: $out"
  printf 'PASS: ledger-e2e picker answer captured\n'
  out=$(ledger_e2e_ledger --json pick it-e2e --ask "$asked" | ledger_e2e_json '(d["election"]["payload"]["chosen"], d["election"]["payload"]["quote_ref"] == d["utterance"]["id"])') ||
    ledger_e2e_fail pick 'ledger pick did not bind the captured picker answer'
  [ "$out" = "('hold', True)" ] || ledger_e2e_fail pick "pick elected the wrong answer or bound another quote: $out"
  printf 'PASS: ledger-e2e pick bound the picker answer\n'

  # Step 5: ticket opens a ledger item, through the installed ticket skill, and
  # stops the pass so the item has a stop to resume.
  printf '%s\n' '{"key":"E2E-1","title":"Stand-alone check ticket","status":"open","description":"Open a ledger item from a ticket.","acceptance_criteria":["The item exists on the ledger"],"url":"https://example.invalid/E2E-1"}' > "$scratch/ticket.json"
  out=$("${ledger_e2e_env[@]}" LEDGER_HARNESS=ci "$python" -I -B - "$home/.claude/skills/ticket" "$scratch/ticket.json" "$scratch" 2>&1 <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import ticket
store = ticket.choose_store(sys.argv[3], "E2E-1", "Stand-alone check ticket")
assert store.selection.startswith("ledger "), store.selection
session = ticket.TicketPass.open("E2E-1", ticket.FileTicketSource(sys.argv[2]), store)
session.more_info("someone_else", "Which release?", role="owner")
PY
  ) || ledger_e2e_fail ticket-open "the ticket skill did not open and stop a pass on the ledger: $out"
  out=$(ledger_e2e_ledger --json query --item E2E-1 | ledger_e2e_json '(d["items"][0]["item_id"], d["items"][0]["type"])') ||
    ledger_e2e_fail ticket-open 'the ledger has no item for the opened ticket'
  [ "$out" = "('E2E-1', 'ticket')" ] || ledger_e2e_fail ticket-open "the opened ticket is not a ledger ticket item: $out"
  printf 'PASS: ledger-e2e ticket opened a ledger item\n'

  # Step 6: resume lists the stop.
  out=$(ledger_e2e_ledger --json resume | ledger_e2e_json '[(i["item_id"], i["stop"]["point"]) for i in d["items"]]') ||
    ledger_e2e_fail resume 'ledger resume failed'
  [ "$out" = "[('E2E-1', 'waiting on owner: Which release?')]" ] ||
    ledger_e2e_fail resume "resume does not list the ticket's stop: $out"
  printf 'PASS: ledger-e2e resume listed the stop\n'

  # Step 7: search finds the decision the pick recorded.
  out=$(ledger_e2e_ledger --json search --type decision hold | ledger_e2e_json '[c["title"] for c in d["cards"]]') ||
    ledger_e2e_fail search 'ledger search failed'
  case $out in
    *"elected: Ship the release today?"*) ;;
    *) ledger_e2e_fail search "search did not find the elected decision; cards: $out" ;;
  esac
  printf 'PASS: ledger-e2e search found the decision\n'
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
  ledger-e2e) check_ledger_e2e ;;
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
    check_ledger_e2e
    ;;
  *)
    printf 'Unknown check: %s\n' "$1" >&2
    exit 2
    ;;
esac

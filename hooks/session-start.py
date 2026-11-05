#!/usr/bin/env python3
"""SessionStart hook: print the ledger's session-start brief, and tag the session for it.

Resolves the ledger CLI the way every skill does (`skills/_lib/ledger_cli.py`,
the topic skill's `ledger_cli` key). With none configured it prints nothing and
exits 0, so the plugin behaves exactly as before.

With one, it runs `<ledger_cli> resume --brief` and prints that command's stdout,
which Claude Code adds to the session's context. If the command fails in any way
(the program is missing, an older ledger has no `resume`, a non-zero exit, a
timeout) it prints nothing. Either way the exit status is 0: a session start must
never be blocked, and the hook adds no noise.

If `CLAUDE_ENV_FILE` is set (Claude Code gives a SessionStart hook the session's
own env file) and a CLI is configured, it appends `export LEDGER_SESSION=<id>`
(the `session_id` from the hook's stdin JSON), and `export LEDGER_AGENT=claude-code`
only when `LEDGER_AGENT` is not already set in the hook's environment. That file
is the only file the hook writes; it never touches settings files.

Python 3 stdlib only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

# The hook runs inside the plugin's install directory: write nothing there.
sys.dont_write_bytecode = True

HOOK_DIR = Path(__file__).resolve().parent
LEDGER_CLI_PATH = HOOK_DIR.parent / "skills" / "_lib" / "ledger_cli.py"
TIMEOUT_SECONDS = 20


def _session_id() -> str | None:
    try:
        data = json.loads(sys.stdin.read())
    except (ValueError, OSError):
        return None
    value = data.get("session_id") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _resolve_cli() -> str | None:
    spec = importlib.util.spec_from_file_location("session_start_ledger_cli", LEDGER_CLI_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.resolve(quiet=True)


def _brief(cli: str) -> str:
    try:
        proc = subprocess.run(
            [cli, "resume", "--brief"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _tag_session(env_file: str, session_id: str | None) -> None:
    lines = []
    if session_id:
        lines.append(f"export LEDGER_SESSION={shlex.quote(session_id)}\n")
    if "LEDGER_AGENT" not in os.environ:
        lines.append("export LEDGER_AGENT=claude-code\n")
    if not lines:
        return
    with open(env_file, "a", encoding="utf-8") as handle:
        handle.write("".join(lines))


def main() -> int:
    session_id = _session_id()
    cli = _resolve_cli()
    if not cli:
        return 0
    output = _brief(cli)
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if env_file:
        try:
            _tag_session(env_file, session_id)
        except OSError:
            pass
    if output:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        code = 0
    raise SystemExit(code)

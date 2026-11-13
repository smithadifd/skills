#!/usr/bin/env python3
"""SessionStart hook: report ledger state and tag the session for ledger writes.

Decides whether the ledger is on by the rule every skill uses
(`skills/_lib/ledger_cli.py`, `decide`): an explicit `ledger_cli` setting in the
topic skill's config wins, `none` turns the ledger off, and otherwise a
compatible `ledger` on `PATH` with `LEDGER_DB` naming an existing file turns it
on. With the ledger off it prints nothing, writes nothing and exits 0.

With it on, it runs `<cli> resume --brief`. The ledger line, the version line
and the brief print only when that call succeeds with non-empty output: first
the ledger line (`ledger on (<rule>: <detail>)`, naming the rule that turned the
ledger on), then `ledger <version>` when `<cli> --version` succeeds, then the
brief. A successful call with empty output prints nothing. Failure prints one
bounded `ledger session-start: ...` line naming a non-zero exit, a program that
could not run, or a timeout. Every path exits 0 so session start never blocks.
The compatibility check and the brief share one deadline.

If `CLAUDE_ENV_FILE` is set and the ledger is on, the hook appends shell-quoted
exports for a non-empty stdin `session_id` and `model` (the latter may also be an
object with a string `id`). `LEDGER_SESSION` is always written from a non-empty
`session_id`, whatever the hook's environment holds. Only `LEDGER_AGENT`,
`LEDGER_HARNESS` and `LEDGER_MODEL` are left alone when already set in that
environment: the hook supplies `LEDGER_AGENT=claude-code` and
`LEDGER_HARNESS=claude-code` when each is absent, and `LEDGER_MODEL` from the input
model id when it is absent. That file is the only file the hook writes; it never
touches settings files.

Python 3 stdlib only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

# The hook runs inside the plugin's install directory: write nothing there.
sys.dont_write_bytecode = True

HOOK_DIR = Path(__file__).resolve().parent
LEDGER_CLI_PATH = HOOK_DIR.parent / "skills" / "_lib" / "ledger_cli.py"
TIMEOUT_SECONDS = 20
MAX_DETAIL_CHARS = 240


def _input_data() -> dict:
    try:
        data = json.loads(sys.stdin.read())
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _nonempty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _model_id(data: dict) -> str | None:
    value = data.get("model")
    if isinstance(value, dict):
        value = value.get("id")
    return _nonempty_string(value)


def _one_line(value: str, limit: int = MAX_DETAIL_CHARS) -> str:
    line = next((line.strip() for line in value.splitlines() if line.strip()), "")
    return line[:limit]


def _decide(deadline: float):
    """The shared rule's decision (`ledger_cli.decide`), or None when the rule cannot be loaded."""
    spec = importlib.util.spec_from_file_location("session_start_ledger_cli", LEDGER_CLI_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    remaining = max(0.0, deadline - time.monotonic())
    return module.decide(quiet=True, probe_timeout=min(module.PROBE_TIMEOUT_SECONDS, remaining))


def _brief(cli: str, deadline: float) -> tuple[str, str]:
    command = f"{_one_line(cli)} resume --brief"
    try:
        proc = subprocess.run(
            [cli, "resume", "--brief"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=max(0, deadline - time.monotonic()),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        seconds = f"{TIMEOUT_SECONDS:g}"
        return "", f"ledger session-start: {command} timed out after {seconds}s\n"
    except OSError as exc:
        reason = _one_line(exc.strerror or str(exc)) or "unknown error"
        return "", f"ledger session-start: {command} could not run: {reason}\n"
    except subprocess.SubprocessError as exc:
        reason = _one_line(str(exc)) or "subprocess error"
        return "", f"ledger session-start: {command} could not run: {reason}\n"
    if proc.returncode != 0:
        detail = _one_line(proc.stderr) or "no stderr"
        return "", f"ledger session-start: {command} exited {proc.returncode}: {detail}\n"
    return proc.stdout, ""


def _version(cli: str, deadline: float) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ""
    try:
        proc = subprocess.run(
            [cli, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=remaining,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    version = _one_line(proc.stdout)
    if not version:
        return ""
    return f"{version if version.startswith('ledger ') else f'ledger {version}'}\n"


def _tag_session(env_file: str, session_id: str | None, model_id: str | None) -> None:
    lines = []
    if session_id:
        lines.append(f"export LEDGER_SESSION={shlex.quote(session_id)}\n")
    if "LEDGER_AGENT" not in os.environ:
        lines.append("export LEDGER_AGENT=claude-code\n")
    if "LEDGER_HARNESS" not in os.environ:
        lines.append("export LEDGER_HARNESS=claude-code\n")
    if model_id and "LEDGER_MODEL" not in os.environ:
        lines.append(f"export LEDGER_MODEL={shlex.quote(model_id)}\n")
    if not lines:
        return
    with open(env_file, "a", encoding="utf-8") as handle:
        handle.write("".join(lines))


def main() -> int:
    data = _input_data()
    session_id = _nonempty_string(data.get("session_id"))
    model_id = _model_id(data)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    decision = _decide(deadline)
    cli = decision.cli if decision is not None else None
    if not cli:
        return 0
    output, diagnostic = _brief(cli, deadline)
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if env_file:
        try:
            _tag_session(env_file, session_id, model_id)
        except OSError:
            pass
    if diagnostic:
        sys.stdout.write(diagnostic)
    elif output:
        sys.stdout.write(" ".join(decision.line.split()) + "\n")
        sys.stdout.write(_version(cli, deadline))
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        code = 0
    raise SystemExit(code)

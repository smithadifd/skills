#!/usr/bin/env python3
"""ledger_cli.py — the one rule that decides whether the ledger is on, for every skill and hook.

    python3 ledger_cli.py [--config <path>]

Prints one JSON object and exits 0:

    {"ledger_cli": <path or null>, "rule": <rule>, "line": <the ledger line>}

`decide` is the rule. Every skill that writes to the ledger (topic, sharpen,
shape, ticket) and the plugin's session-start hook call it; none keeps a lookup
of its own. It applies the first of these that holds:

1. An explicit setting wins. The setting is the `ledger_cli` key of the topic
   skill's config, read the way `topics_dir` is: an explicit `--config` file
   alone, or else the shipped `skills/topic/config.json` with
   `~/.claude/skill-config/topic.json` laid over it. A caller may name one
   environment variable that comes first (the ticket skill names its
   `TICKET_LEDGER_CLI`). A value of `none`, in any case, turns the ledger off
   (rule: off by setting). Any other non-blank string names the ledger CLI
   (rule: explicit setting); a leading `~` is expanded. An explicit CLI is not
   checked here: a CLI that is missing or fails is reported by the call that
   runs it, as before.
2. With no explicit setting (the key absent, null, empty or blank, and the
   variable unset or blank), the ledger is on when `LEDGER_DB` names an
   existing file and a `ledger` found on `PATH` passes the compatibility check
   (rule: ledger on PATH with LEDGER_DB). The check is the one `ticket` has
   always run: `ledger append-many --help` exits 0 within the timeout and names
   `--expect-revision`. `LEDGER_DB` is tested first, so a `ledger` is never run
   without a database. The `ledger` found on `PATH` is never run, not even for
   the check, when its real path lies inside the current working directory:
   that folder is usually a checkout, and its content must not choose a
   program the rule runs.
3. Anything else is off, silently (rule: not configured): no `ledger` on
   `PATH`, a `ledger` on `PATH` inside the working directory, `LEDGER_DB`
   unset, empty or not a file, a `ledger` that fails the check, a `ledger_cli`
   value that is not a string, or a config file that exists but cannot be
   read. The last two never fall through to rule 2: a setting that cannot be
   read might have said `none`. Each config file keeps its own value and read
   status, and they are weighed in precedence order: an override file that
   cannot be read is off even when the shipped config names a CLI, because
   the shipped value is never consulted beneath a broken override.

The ledger line names the rule that chose, in one fixed form:

    ledger on (<rule>: <detail>)
    ledger off (<rule>: <detail>)

where `<rule>` is exactly one of `explicit setting`, `off by setting`,
`ledger on PATH with LEDGER_DB` and `not configured`. `Decision.reason` is the
part in parentheses; the ticket skill prints it as `store: <store> (<reason>)`.

Also holds the small pieces the callers share: the argv of the `link` call and a
`call` that runs the CLI with a timeout and turns a failure into one
`LedgerCallError` line.

Python 3 stdlib only. It lives in the `_lib` directory beside the skills, so a
skill reaches it as `${CLAUDE_SKILL_DIR}/../_lib/ledger_cli.py` in the plugin
and in a standalone `install.sh` copy alike.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

LIB_DIR = Path(__file__).resolve().parent
CONFIG_PATH = LIB_DIR.parent / "topic" / "config.json"
OVERRIDE_PATH = "~/.claude/skill-config/topic.json"
CONFIG_KEY = "ledger_cli"
TIMEOUT_SECONDS = 60

# The four rules. Each is a fixed phrase: the ledger line carries it verbatim.
RULE_EXPLICIT = "explicit setting"
RULE_OFF = "off by setting"
RULE_PATH = "ledger on PATH with LEDGER_DB"
RULE_NOT_CONFIGURED = "not configured"
RULES = (RULE_EXPLICIT, RULE_OFF, RULE_PATH, RULE_NOT_CONFIGURED)

# The value of a setting that turns the ledger off whatever is installed.
OPT_OUT = "none"
# The name looked up on PATH when no setting names a CLI.
LEDGER_COMMAND = "ledger"
# The ledger picks no default database: this variable names it.
LEDGER_DATABASE = "LEDGER_DB"
# Seconds the compatibility check (`append-many --help`) may run before the CLI
# counts as not running.
PROBE_TIMEOUT_SECONDS = 10.0
# How the topic config's key is named in a ledger line.
CONFIG_SOURCE = f"{CONFIG_KEY} in the topic config"


class ConfigError(ValueError):
    """The `ledger_cli` key holds something other than a string."""


class LedgerCallError(Exception):
    """The CLI could not run, timed out, or exited non-zero: one line to report."""


class Decision:
    """What `decide` chose: the CLI (None when the ledger is off), the rule that chose, and why."""

    __slots__ = ("cli", "rule", "detail")

    def __init__(self, cli: Optional[str], rule: str, detail: str):
        if rule not in RULES:
            raise ValueError(f"unknown ledger rule {rule!r}")
        if (cli is not None) != (rule in (RULE_EXPLICIT, RULE_PATH)):
            raise ValueError(f"rule {rule!r} does not match cli {cli!r}")
        self.cli = cli
        self.rule = rule
        self.detail = detail

    @property
    def on(self) -> bool:
        return self.cli is not None

    @property
    def reason(self) -> str:
        """`<rule>: <detail>`, the part of the ledger line in parentheses."""
        return f"{self.rule}: {self.detail}"

    @property
    def line(self) -> str:
        """The ledger line every skill prints first: `ledger on|off (<rule>: <detail>)`."""
        return f"ledger {'on' if self.on else 'off'} ({self.reason})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Decision):
            return NotImplemented
        return (self.cli, self.rule, self.detail) == (other.cli, other.rule, other.detail)

    def __repr__(self) -> str:
        return f"Decision(cli={self.cli!r}, rule={self.rule!r}, detail={self.detail!r})"


def _one_line(text: Any) -> str:
    return " ".join(str(text).split())


def _read_object(
    path: Path, required: bool, who: str, quiet: bool
) -> tuple[dict[str, Any], Optional[str]]:
    """A JSON object from `path` and None; or `{}` and what was wrong, with one stderr line.

    A missing file that is not required is `{}` and None: it sets nothing.
    """

    def unusable(line: str) -> tuple[dict[str, Any], str]:
        if not quiet:
            sys.stderr.write(line)
        return {}, f"{_one_line(path)} could not be read"

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            return unusable(f"{who}: could not read {path} — {CONFIG_KEY} is unset\n")
        return {}, None
    except (OSError, UnicodeDecodeError) as exc:
        return unusable(f"{who}: could not read {path} ({exc}) — ignoring it\n")
    try:
        data = json.loads(text)
    except ValueError as exc:
        return unusable(f"{who}: {path} is not valid JSON ({exc}) — ignoring it\n")
    if not isinstance(data, dict):
        return unusable(f"{who}: {path} is not a JSON object — ignoring it\n")
    return data, None


def _config_value(
    config_path: Optional[Path], who: str, required_shipped: bool, quiet: bool
) -> tuple[Any, Optional[str]]:
    """The raw `ledger_cli` value and the config problem that stands, if any.

    An explicit `config_path` is the only source. Otherwise each source keeps
    its own value and read status, weighed in precedence order: the override
    file, then the shipped config. A source that cannot be read stops the
    walk with its problem and no value, so a value beneath it is never used.
    A source that holds the key (even as null) settles the value; a problem in
    a source beneath it is still returned, so an unset value never falls
    through to the PATH rule past a broken file.
    """
    if config_path is not None:
        config, problem = _read_object(Path(config_path), True, who, quiet)
        return config.get(CONFIG_KEY), problem
    shipped, shipped_problem = _read_object(CONFIG_PATH, required_shipped, who, quiet)
    override, override_problem = _read_object(
        Path(OVERRIDE_PATH).expanduser(), False, who, quiet
    )
    for config, problem in ((override, override_problem), (shipped, shipped_problem)):
        if problem is not None:
            return None, problem
        if CONFIG_KEY in config:
            return config[CONFIG_KEY], shipped_problem
    return None, None


def _explicit(value: str, off_detail: str, on_detail: str) -> Decision:
    """A non-blank explicit setting: `none` is off, anything else names the CLI."""
    if value.lower() == OPT_OUT:
        return Decision(None, RULE_OFF, off_detail)
    return Decision(os.path.expanduser(value), RULE_EXPLICIT, on_detail)


def probe(cli: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> Optional[str]:
    """None when `cli` passes the compatibility check, else what failed, in a few words.

    The check: `<cli> append-many --help` exits 0 within `timeout` seconds and
    its output names `--expect-revision`. Output that is not UTF-8 is decoded
    with replacement, so it fails the check rather than raising.
    """
    try:
        result = subprocess.run(
            [cli, "append-many", "--help"],
            input="",
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"append-many --help did not finish within {timeout:g}s"
    except OSError as exc:
        return str(exc)
    if result.returncode != 0 or "--expect-revision" not in result.stdout:
        return f"append-many --help exited {result.returncode}"
    return None


def _inside_working_directory(path: str) -> bool:
    """True when `path`'s real path lies inside the current working directory, or that cannot be told.

    Folders are compared by identity (`os.path.samestat`), not by spelling, so
    a symlink, a relative `PATH` entry or a case-insensitive file system does
    not hide the containment. A working directory or folder that cannot be
    examined counts as inside: the answer fails closed.
    """
    try:
        here = os.stat(os.getcwd())
        for folder in Path(os.path.realpath(path)).parents:
            if os.path.samestat(os.stat(folder), here):
                return True
    except (OSError, ValueError):
        return True
    return False


def _on_path(probe_timeout: float) -> Decision:
    """Rule 2: a compatible `ledger` on PATH with a usable `LEDGER_DB`, or off with the reason."""
    found = shutil.which(LEDGER_COMMAND)
    if found is None:
        return Decision(
            None, RULE_NOT_CONFIGURED, f"no {LEDGER_COMMAND} CLI on PATH and no setting names one"
        )
    if _inside_working_directory(found):
        return Decision(
            None,
            RULE_NOT_CONFIGURED,
            f"the {LEDGER_COMMAND} CLI on PATH lies inside the working directory, so it is not run",
        )
    database = os.environ.get(LEDGER_DATABASE, "")
    if not database:
        return Decision(
            None,
            RULE_NOT_CONFIGURED,
            f"{found} is on PATH but {LEDGER_DATABASE} is not set, and the ledger picks no default database",
        )
    if not os.path.isfile(database):
        return Decision(
            None,
            RULE_NOT_CONFIGURED,
            f"{found} is on PATH but {LEDGER_DATABASE} names {_one_line(database)}, which is not a file",
        )
    if probe(found, probe_timeout) is not None:
        return Decision(
            None,
            RULE_NOT_CONFIGURED,
            f"{found} on PATH lacks append-many or --expect-revision, or does not run",
        )
    return Decision(
        found, RULE_PATH, f"{found} found on PATH, {LEDGER_DATABASE} names {_one_line(database)}"
    )


def decide(
    config_path: Optional[Path] = None,
    *,
    selector_env: Optional[str] = None,
    who: str = CONFIG_KEY,
    strict: bool = False,
    required_shipped: bool = False,
    quiet: bool = False,
    probe_timeout: float = PROBE_TIMEOUT_SECONDS,
) -> Decision:
    """The one rule (see the module text): whether the ledger is on, through which CLI, and which rule chose.

    `selector_env` names an environment variable that is an explicit setting
    ahead of the config key (the ticket skill's `TICKET_LEDGER_CLI`). `strict`
    raises ConfigError for a key that is not a string; otherwise such a key is
    off. `required_shipped` reports a missing shipped config on stderr (topic's
    long-standing behaviour); otherwise a missing one is silent, because a
    standalone install of one skill may not carry the topic skill. `quiet`
    mutes every stderr line. `probe_timeout` bounds the compatibility check.
    """
    if selector_env:
        value = os.environ.get(selector_env, "").strip()
        if value:
            return _explicit(value, f"{selector_env}={OPT_OUT}", f"{selector_env} names it")
    value, problem = _config_value(config_path, who, required_shipped, quiet)
    if value is not None and not isinstance(value, str):
        if strict:
            raise ConfigError(
                f"{CONFIG_KEY} must be a string naming the ledger executable, "
                f"got {type(value).__name__}"
            )
        return Decision(None, RULE_NOT_CONFIGURED, f"{CONFIG_SOURCE} is not a string")
    value = (value or "").strip()
    if value:
        return _explicit(value, f"{CONFIG_SOURCE} is {OPT_OUT}", f"{CONFIG_SOURCE} names it")
    if problem is not None:
        return Decision(None, RULE_NOT_CONFIGURED, f"{problem}, so no ledger on PATH is used")
    return _on_path(probe_timeout)


def resolve(
    config_path: Optional[Path] = None,
    *,
    who: str = CONFIG_KEY,
    strict: bool = False,
    required_shipped: bool = False,
    quiet: bool = False,
) -> Optional[str]:
    """The ledger CLI `decide` chooses (a path or a name), or None when the ledger is off."""
    return decide(
        config_path, who=who, strict=strict, required_shipped=required_shipped, quiet=quiet
    ).cli


def link_argv(item: str, target: str) -> list[str]:
    """The `link` call that attaches a document to an item."""
    return ["link", item, "--rel", "url", "--target", target]


def call(cli: str, argv: list[str]) -> subprocess.CompletedProcess:
    """Run `<cli> <argv>`; raise LedgerCallError unless it exits 0."""
    verb = argv[0] if argv else "?"
    try:
        proc = subprocess.run(
            [cli, *argv],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise LedgerCallError(
            f"ledger CLI {cli} timed out after {TIMEOUT_SECONDS}s running {verb}"
        )
    except OSError as exc:
        detail = " ".join((exc.strerror or str(exc)).split())
        raise LedgerCallError(f"ledger CLI {cli} could not run ({detail})")
    if proc.returncode != 0:
        detail = " ".join((proc.stderr.strip() or proc.stdout.strip() or "no output").split())
        raise LedgerCallError(
            f"ledger CLI {cli} {verb} failed (exit {proc.returncode}): {detail}"
        )
    return proc


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        decision = decide(args.config)
    except Exception as exc:  # the contract: report, never raise
        sys.stderr.write(f"ledger_cli: {exc}\n")
        decision = Decision(
            None, RULE_NOT_CONFIGURED, f"the setting could not be read ({_one_line(exc)})"
        )
    print(json.dumps({CONFIG_KEY: decision.cli, "rule": decision.rule, "line": decision.line}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""ledger_cli.py — the one rule that decides whether the ledger is on, for every skill.

    python3 ledger_cli.py [--config <path>]
    python3 ledger_cli.py setup

With no subcommand it prints one JSON object and exits 0:

    {"ledger_cli": <path or null>, "rule": <rule>, "line": <the ledger line>}

`decide` is the rule. Every skill that writes to the ledger (topic, sharpen,
shape, ticket) calls it; none keeps a lookup of its own. It applies the first
of these that holds:

1. An explicit setting wins. The setting is the `ledger_cli` key of the topic
   skill's config, read the way `topics_dir` is: an explicit `--config` file
   alone, or else the shipped `skills/topic/config.json` with
   `~/.claude/skill-config/topic.json` laid over it. A caller may name one
   environment variable that comes first (the ticket skill names its
   `TICKET_LEDGER_CLI`). A value of `none`, in any case, turns the ledger off
   (rule: off by setting). Any other non-blank string names the ledger CLI
   (rule: explicit setting); a leading `~` is expanded. An explicit CLI is not
   checked here: a CLI that is missing or fails is reported by the call that
   runs it. While a setting names a CLI nothing looks `ledger` up on `PATH`: a
   skill runs that CLI or refuses, and never falls back. A CLI whose path (after
   `~` expansion) is absolute and is no longer an executable file is refused
   before anything runs, in one line that starts with the fixed token
   `ledger_cli_moved:`, names the path and gives the `setup` command to rerun
   (see Setup below).
2. With no explicit setting (the key absent, null, empty or blank, and the
   variable unset or blank), the ledger is on when `LEDGER_DB` names an
   existing file and a `ledger` found on `PATH` passes the compatibility check
   (rule: ledger on PATH with LEDGER_DB). The check is the one `ticket` has
   always run: `ledger append-many --help` exits 0 within the timeout and names
   `--expect-revision`. `LEDGER_DB` is tested first, so a `ledger` is never run
   without a database. This rule is for the skills a person invokes (`topic`,
   `sharpen`, `shape`, `ticket`). It reads `PATH`, so a writable folder put on
   `PATH` ahead of the real install can plant a `ledger`; `setup` below removes
   that lookup by pinning the real one.
3. Anything else is off, silently (rule: not configured): no `ledger` on
   `PATH`, `LEDGER_DB` unset, empty or not a file, a `ledger` that fails the
   check, a `ledger_cli` value that is not a string, or a config file that
   exists but cannot be read. The last two never fall through to rule 2: a
   setting that cannot be read might have said `none`. Each config file keeps its own value
   and read status, and they are weighed in precedence order: an override file
   that cannot be read is off even when the shipped config names a CLI,
   because the shipped value is never consulted beneath a broken override.

The ledger line names the rule that chose, in one fixed form:

    ledger on (<rule>: <detail>)
    ledger off (<rule>: <detail>)

where `<rule>` is exactly one of `explicit setting`, `off by setting`,
`ledger on PATH with LEDGER_DB` and `not configured`. `Decision.reason` is the
part in parentheses; the ticket skill prints it as `store: <store> (<reason>)`.

Setup. `python3 ledger_cli.py setup` is the one step that pins the ledger CLI:
it resolves `ledger` once (`shutil.which`), runs the compatibility check on it,
and writes its absolute path (made absolute, symlinks kept) as `ledger_cli` in
`~/.claude/skill-config/topic.json`, keeping every other key. From then on rule
1 decides for every skill and nothing reads `PATH`. Setup writes atomically (a
temp file in the same directory, then `os.replace`) and refuses, exit 1 with one
stderr line that starts with its token and nothing written, unless it can write:

- `ledger_cli_setup_config_unreadable`: the override file exists and is not a
  readable UTF-8 JSON object. Checked first, with no program run.
- `ledger_cli_setup_exists`: `ledger_cli` already holds a non-blank value (any
  string, `none` in any case included, or any non-string value other than
  null), unless that value is an absolute path (after `~` expansion) that is no
  longer an executable file, which is the moved case and is replaced. A live
  setting is never changed and the file stays byte-identical. Null, an empty
  or a blank string count as unset. Checked second, with no program run.
- `ledger_cli_setup_not_found`: no `ledger` on `PATH`.
- `ledger_cli_setup_incompatible`: the one found fails the compatibility check;
  the line names what failed.
- `ledger_cli_setup_config_changed`: the override file changed between setup
  reading it and writing it, so a concurrent writer's edit is never lost; rerun.
- `ledger_cli_setup_write_failed`: the directory or file could not be written.

Setup runs the first `ledger` on `PATH` once, for the compatibility check, so
run it from a shell whose `PATH` you trust. A rerun after a stop at any step is
safe: the file is either as it was or holds the pinned path, and a rerun after
success refuses `ledger_cli_setup_exists`. This plugin declares no hooks: the
ledger's brief at session start comes from the ledger's own `ledger-hooks`
plugin, which takes no `ledger_cli` setting and which this file does not govern.

Also holds the small pieces the callers share: the argv of the `link` call and a
`call` that runs the CLI with a timeout and turns a failure into one
`LedgerCallError` line, `ledger_cli_moved:` included; a caller that runs the CLI
itself calls `require_runnable` first to get the same refusal.

Python 3 stdlib only. It lives in the `_lib` directory beside the skills, so a
skill reaches it as `${CLAUDE_SKILL_DIR}/../_lib/ledger_cli.py` in the plugin
and in a standalone `install.sh` copy alike.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
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
# The fixed first word of the refusal for an explicit CLI that is no longer an executable file.
MOVED_TOKEN = "ledger_cli_moved:"
# The fixed first word of every `setup` refusal is one of these, then a colon.
SETUP_NOT_FOUND = "ledger_cli_setup_not_found"
SETUP_INCOMPATIBLE = "ledger_cli_setup_incompatible"
SETUP_CONFIG_UNREADABLE = "ledger_cli_setup_config_unreadable"
SETUP_EXISTS = "ledger_cli_setup_exists"
SETUP_CONFIG_CHANGED = "ledger_cli_setup_config_changed"
SETUP_WRITE_FAILED = "ledger_cli_setup_write_failed"


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


def _on_path(probe_timeout: float) -> Decision:
    """Rule 2: a compatible `ledger` on PATH with a usable `LEDGER_DB`, or off with the reason."""
    found = shutil.which(LEDGER_COMMAND)
    if found is None:
        return Decision(
            None, RULE_NOT_CONFIGURED, f"no {LEDGER_COMMAND} CLI on PATH and no setting names one"
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


def setup_command() -> str:
    """The exact command that pins the ledger CLI, for a refusal to name."""
    return f"python3 {shlex.quote(str(LIB_DIR / 'ledger_cli.py'))} setup"


def _is_executable_file(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _moved(cli: str) -> bool:
    """True when `cli`, after `~` expansion, is an absolute path that is not an executable file."""
    path = os.path.expanduser(cli)
    return os.path.isabs(path) and not _is_executable_file(path)


def require_runnable(cli: str) -> None:
    """Raise LedgerCallError (`ledger_cli_moved: ...`) when an absolute `cli` is not an executable file.

    A bare name is not checked: it is looked up the way the person who wrote it
    meant. No branch of this function reads `PATH`.
    """
    if _moved(cli):
        raise LedgerCallError(
            f"{MOVED_TOKEN} the ledger CLI {_one_line(cli)} is not an executable file, so nothing was run; "
            f"rerun setup with `{setup_command()}`"
        )


def call(cli: str, argv: list[str]) -> subprocess.CompletedProcess:
    """Run `<cli> <argv>`; raise LedgerCallError unless it exits 0."""
    require_runnable(cli)
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


def _refuse_setup(token: str, detail: str) -> int:
    sys.stderr.write(f"{token}: {_one_line(detail)}\n")
    return 1


def _current_bytes(path: Path) -> Optional[bytes]:
    """The file's bytes, or None when it is absent."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


class _ConfigChanged(Exception):
    """The override file is not what setup read."""


def _write_atomic(target: Path, text: str, expected: Optional[bytes]) -> None:
    """Replace `target` with `text` in one step, only if it still holds `expected` (None: absent)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if expected is not None:
            shutil.copymode(target, temp)
        if _current_bytes(target) != expected:
            raise _ConfigChanged()
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def setup() -> int:
    """Pin the ledger CLI: see the module text, Setup. Exit 0 when written, 1 on a named refusal."""
    override = Path(OVERRIDE_PATH).expanduser()
    # Follow a symlinked override so the link itself is kept and its target is rewritten.
    target = Path(os.path.realpath(override))
    try:
        before = _current_bytes(override)
    except OSError as exc:
        return _refuse_setup(SETUP_CONFIG_UNREADABLE, f"could not read {override} ({exc}); nothing was written")
    config: dict[str, Any] = {}
    if before is not None:
        try:
            data = json.loads(before.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            return _refuse_setup(
                SETUP_CONFIG_UNREADABLE, f"{override} is not valid UTF-8 JSON ({exc}); nothing was written"
            )
        if not isinstance(data, dict):
            return _refuse_setup(
                SETUP_CONFIG_UNREADABLE, f"{override} is not a JSON object; nothing was written"
            )
        config = data
    existing = config.get(CONFIG_KEY)
    if existing is not None:
        if not isinstance(existing, str):
            return _refuse_setup(
                SETUP_EXISTS,
                f"{CONFIG_KEY} in {override} is already set to {json.dumps(existing)}; setup does not change it",
            )
        text = existing.strip()
        if text and not _moved(text):
            return _refuse_setup(
                SETUP_EXISTS,
                f"{CONFIG_KEY} in {override} is already set to {text}; setup does not change it",
            )
    found = shutil.which(LEDGER_COMMAND)
    if found is None:
        return _refuse_setup(SETUP_NOT_FOUND, f"no {LEDGER_COMMAND} CLI on PATH; nothing was written")
    pinned = os.path.abspath(found)
    failure = probe(pinned)
    if failure is not None:
        return _refuse_setup(
            SETUP_INCOMPATIBLE, f"{pinned} fails the compatibility check ({failure}); nothing was written"
        )
    config[CONFIG_KEY] = pinned
    try:
        _write_atomic(target, json.dumps(config, indent=2) + "\n", before)
    except _ConfigChanged:
        return _refuse_setup(
            SETUP_CONFIG_CHANGED, f"{override} changed while setup was running; nothing was written, rerun setup"
        )
    except OSError as exc:
        return _refuse_setup(
            SETUP_WRITE_FAILED, f"could not write {target} ({_one_line(exc.strerror or str(exc))}); nothing was written"
        )
    print(f"{CONFIG_KEY} set to {pinned} in {target}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=None)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("setup", help="pin the ledger CLI's absolute path in the topic config override")
    args = parser.parse_args(argv)
    if args.command == "setup":
        if args.config is not None:
            parser.error("setup takes no --config: it writes ~/.claude/skill-config/topic.json")
        return setup()
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

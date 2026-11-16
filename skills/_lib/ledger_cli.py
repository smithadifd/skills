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
1 decides for every skill and nothing reads `PATH`. Setup refuses, exit 1 with
one stderr line that starts with its token, unless it can write; every refusal
writes nothing, except the one `ledger_cli_setup_config_changed` case after a
commit that the residual below describes:

- `ledger_cli_setup_lock_failed`: the lock file beside the override could not
  be opened or locked. Checked first, with no program run.
- `ledger_cli_setup_config_unreadable`: the override file exists and is not a
  readable UTF-8 JSON object. Checked next, with no program run.
- `ledger_cli_setup_exists`: `ledger_cli` already holds a non-blank value (any
  string, `none` in any case included, or any non-string value other than
  null), unless that value is an absolute path (after `~` expansion) that is no
  longer an executable file, which is the moved case and is replaced. A live
  setting is never changed and the file stays byte-identical. Null, an empty
  or a blank string count as unset. Checked after that, with no program run.
- `ledger_cli_setup_not_found`: no `ledger` on `PATH`.
- `ledger_cli_setup_plugin_command`: the resolved `ledger` path (made absolute)
  has a `plugins/cache/` folder segment under a `.claude` folder, so it is
  Claude Code's plugin command. That path changes with every plugin update;
  pinning it would become `ledger_cli_moved` at the next update. The plugin's
  command needs no pin. A path that cannot be made absolute is not treated as
  in the cache. Checked after the path is found, before the compatibility check.
  One recovery: when the existing `ledger_cli` setting is a moved pin whose
  path is in that cache, and the `ledger` now on PATH is also in the cache,
  setup removes that one key, keeps every other key, writes through the same
  commit path, and prints one line that the stale plugin pin was cleared and
  the plugin route needs no pin (exit 0).
- `ledger_cli_setup_incompatible`: the one found fails the compatibility check;
  the line names what failed.
- `ledger_cli_setup_config_changed`: when setup came to write, the override
  file was not the version setup read, because a writer that does not take the
  lock (an editor, say) saved it meanwhile, or a backup from an earlier run
  stands beside it (below). The file that writer left stays at the path; the
  line names the backup when setup kept one; rerun.
- `ledger_cli_setup_no_hardlinks`: the override's folder does not take hard links (the link
  probe below failed), so setup could not put the file back if a step failed. Nothing is
  moved, created or written: the override is untouched and no temp, probe or backup file
  is left.
- `ledger_cli_setup_write_failed`: the directory or file could not be written, or the
  link probe's file could not be removed (the line names it, and nothing was moved), or a
  moved-aside version could not be linked back (the line names where it is kept; move it
  back by hand when a rerun cannot link it).

How setup writes. Setup never removes or overwrites a version of the override
file it did not read. It commits only by (1) creating the file when absent, or
(2) moving the existing file aside, confirming its bytes equal what setup read,
and creating the new file no-clobber. In every other case the file a
non-locking writer left is the one at the path when setup exits, and setup
exits 1. While (2) runs, the path is briefly absent.

In detail: the new text goes to a temp file in the same folder. Before anything is created or
moved, setup links that temp file to a probe name beside it (the temp file's name plus
`.link-probe`; `os.link`, which never replaces) and removes the probe: a failed link, of any
kind, refuses `ledger_cli_setup_no_hardlinks`, and a probe that cannot be removed refuses
`ledger_cli_setup_write_failed` naming it; either way the override is as it was. (1) With no
file at the path, setup links the temp file there (`os.link`, which fails rather
than replace a file that appeared meanwhile; that refuses `config_changed`).
(2) Otherwise setup renames the file to `topic.json.setup-backup` beside it
(the in-flight name), which captures exactly the version at the path at that
instant, and compares the backup's bytes with what it read. Equal: it links the
temp file to the path, again never replacing a file. Different: it links the
backup back to the path and refuses `config_changed`. If at either link a file
has reappeared at the path, setup leaves that file, keeps the backup and
refuses `config_changed` naming it. No step renames or replaces onto the path
except the one below. After a successful commit setup gives the in-flight
file a kept name, `topic.json.setup-previous.<UTC timestamp>-<pid>` (with a
`-<counter>` suffix after a name already taken), by linking the file to it
(`os.link`, which fails rather than replace a file) and then removing the
in-flight name. Every displaced version thus ends at a name setup never uses
again, and no earlier kept file is ever replaced. The success line names the
kept file and says it may be removed once no editor has it open. Setup never
removes the last name of, or replaces, a file that has been at the override
path. It removes the in-flight name only after the kept name is linked, or after
a successful restore, and then only while it is the same file as the one at the
path.

Only an interrupted commit is ever recovered. The in-flight name is fixed so a
later run finds it: a run that stopped while the path was absent left the
file's version there, and the next run, before it reads anything, links it back
to the path. An in-flight file that is the same file as the one at the path (a
run that stopped after its restore) is dropped. Any other in-flight file found
beside a present file is a version setup cannot place, so a run that would move
the file aside refuses `config_changed` naming it rather than move a file over
it: check it, remove it and rerun. The kept files are records that setup never reads,
restores, prunes or rotates: deleting or editing the override later never
brings their keys back. If the link to a kept name fails after a successful
commit, setup still exits 0 and its line says the version it replaced was left
at the in-flight path; that file is then handled as above.

Residual: a writer that edits in place, on a descriptor it opened before the
move, and writes after setup's compare, lands its edit in the displaced file.
When that write lands before setup's last read of it, setup names the
kept file and exits 1 (`config_changed`), though `ledger_cli` is already
written at the path; a write that lands after that last read is in the kept
file too, and setup exits 0 naming it. The edit is in that side file, not in
the override: merge it by hand. A later setup run keeps its own displaced
version under a new name, so the file holding the edit is never replaced.
Editors that save by writing a new file and
renaming it over the path are covered. After a restore, the check that the
backup is the file at the path and its removal are two steps: a save by rename
that lands between them replaces the restored version at the path, as that
save would have without setup, and the backup's link to it goes with it.

Concurrent setup runs serialize. Setup holds an exclusive `fcntl.flock` on
`topic.json.lock`, a file beside the override (created if absent, never deleted,
and the folder is created first when it is missing), from its first read of the
override through its commit or restore, and the second run waits for it. Once it holds
the lock it reads the file afresh and applies the same rules above, so it finds
the first run's pin and refuses `ledger_cli_setup_exists`: two runs never both
write, and neither pin is silently overwritten. The lock is released when setup
ends or dies; the descriptor is not inherited by the program the compatibility
check runs.

Setup runs the first `ledger` on `PATH` once, for the compatibility check, so
run it from a shell whose `PATH` you trust. A rerun after a stop at any step is
safe: the file is either as it was, holds the pinned path, or is absent with its
version in the in-flight backup, which the rerun restores before it reads (by a hard link, which
setup has shown works in that folder before it moved anything; should that link still fail,
the backup holds the version and is moved back by hand); a rerun after
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
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
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
SETUP_NO_HARDLINKS = "ledger_cli_setup_no_hardlinks"
SETUP_WRITE_FAILED = "ledger_cli_setup_write_failed"
SETUP_LOCK_FAILED = "ledger_cli_setup_lock_failed"
SETUP_PLUGIN_COMMAND = "ledger_cli_setup_plugin_command"
# The hard-link probe's name is setup's temp file name plus this suffix, in the override's folder.
PROBE_SUFFIX = ".link-probe"
# The lock file sits beside the override and is named by appending this to its name.
LOCK_SUFFIX = ".lock"
# The in-flight name, beside the override, that setup moves the file it displaces to. Crash recovery
# reads only this name.
BACKUP_SUFFIX = ".setup-backup"
# The kept-name prefix: a successful commit links the in-flight file to
# `<override>.setup-previous.<UTC timestamp>-<pid>` (plus `-<counter>` after a name already taken).
# Setup never replaces, reads, prunes or removes a file under such a name.
PREVIOUS_SUFFIX = ".setup-previous"
# How many counter suffixes setup tries after a kept name is taken before it leaves the file in-flight.
KEEP_ATTEMPTS = 1000


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


class _Refusal(Exception):
    """Setup stops with one named refusal: its token and the rest of its line."""

    def __init__(self, token: str, detail: str):
        super().__init__(f"{token}: {detail}")
        self.token = token
        self.detail = detail


def _backup_path(target: Path) -> Path:
    """The one fixed in-flight place beside `target` that setup moves the displaced file to."""
    return target.parent / (target.name + BACKUP_SUFFIX)


def _utc_stamp() -> str:
    """The UTC timestamp part of a kept name."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _keep_displaced(target: Path, backup: Path) -> Optional[Path]:
    """Give the in-flight file a kept name that no earlier file has: link it there, then unlink `backup`.

    The kept name is `<override>.setup-previous.<UTC timestamp>-<pid>`, with a `-<counter>` suffix
    after a `FileExistsError`. `os.link` never replaces a file, so no earlier kept file is touched,
    and nothing is renamed onto a kept name. Returns the kept path, or None when the file stays at
    the in-flight name: the link failed, every counter was taken, or the in-flight name could not be
    removed after the link. Crash recovery reads only the in-flight name, never a kept one.
    """
    stem = f"{target.name}{PREVIOUS_SUFFIX}.{_utc_stamp()}-{os.getpid()}"
    for counter in range(KEEP_ATTEMPTS):
        kept = target.parent / (stem if counter == 0 else f"{stem}-{counter}")
        try:
            os.link(backup, kept)
        except FileExistsError:
            continue
        except OSError:
            return None
        try:
            os.unlink(backup)
        except OSError:
            return None
        return kept
    return None


def _drop_backup_if_restored(target: Path, backup: Path) -> None:
    """Remove `backup` only while it is the same file as the one at `target`: no version is lost."""
    try:
        if os.path.samefile(backup, target):
            os.unlink(backup)
    except OSError:
        pass


def _restore(target: Path, backup: Path) -> None:
    """Put the moved-aside file back at `target` without replacing anything there.

    Raises _Refusal, keeping the backup, when a file has reappeared at `target` or the link fails.
    """
    try:
        os.link(backup, target)
    except FileExistsError:
        raise _Refusal(
            SETUP_CONFIG_CHANGED,
            f"{target} reappeared while setup was putting it back; it is left as it is, nothing was "
            f"written, and the version setup moved aside is kept at {backup}",
        ) from None
    except OSError as exc:
        raise _Refusal(
            SETUP_WRITE_FAILED,
            f"could not put {target} back ({_one_line(exc.strerror or str(exc))}); its version is "
            f"kept at {backup}; a rerun links it back only if that link can succeed, so if a rerun "
            "fails the same way, move it back by hand",
        ) from None
    _drop_backup_if_restored(target, backup)


def _recover(target: Path) -> None:
    """Undo a stopped run before reading: put a backup back at an absent path; drop a restored one."""
    backup = _backup_path(target)
    if not os.path.lexists(backup):
        return
    if not os.path.lexists(target):
        _restore(target, backup)
        return
    _drop_backup_if_restored(target, backup)


def _require_hard_links(target: Path, temp: str) -> None:
    """Show that a hard link works in the override's folder before anything is created or moved.

    Links setup's own temp file to `<temp>.link-probe` (`os.link`, which never replaces) and
    removes that name. Any OSError from the link (EPERM, ENOTSUP, EXDEV, EMLINK or other) means
    the folder has no usable hard links: refuses SETUP_NO_HARDLINKS. A probe name that cannot
    be removed refuses SETUP_WRITE_FAILED naming it. Both refuse before any rename, so the
    override is present and unchanged.
    """
    probe_name = temp + PROBE_SUFFIX
    try:
        os.link(temp, probe_name)
    except OSError as exc:
        raise _Refusal(
            SETUP_NO_HARDLINKS,
            f"{target.parent} does not take hard links ({_one_line(exc.strerror or str(exc))}), so setup "
            f"cannot move {target.name} aside and put it back; nothing was moved or written",
        ) from None
    try:
        os.unlink(probe_name)
    except OSError as exc:
        raise _Refusal(
            SETUP_WRITE_FAILED,
            f"could not remove the link probe file {probe_name} ({_one_line(exc.strerror or str(exc))}); "
            "nothing was moved or written: remove it and rerun setup",
        ) from None


def _commit(target: Path, text: str, expected: Optional[bytes]) -> tuple[Optional[Path], bool, bool]:
    """Write `text` at `target`, the version setup read being `expected` (None: absent).

    Setup never removes or overwrites a version of the override file it did not read. It commits
    only by (1) creating the file when absent, or (2) moving the existing file aside, confirming its
    bytes equal what setup read, and creating the new file no-clobber. In every other case the file
    a non-locking writer left is the one at the path when setup exits, and setup exits 1. While (2)
    runs, the path is briefly absent.

    Before either path touches the override, `_require_hard_links` shows a hard link works in its
    folder, so a folder without hard links is refused with nothing moved or created.

    The caller holds the setup lock, which serializes setup runs; the move aside and the no-clobber
    links guard against a writer that takes no lock. Nothing renames or replaces onto `target`.
    Raises _Refusal or OSError with nothing committed. After a commit the in-flight backup is
    linked to a kept name (`topic.json.setup-previous.<UTC timestamp>-<pid>`, plus a counter after
    a name already taken) and its in-flight name removed, so crash recovery, which reads only the
    in-flight name, never restores it. Setup never renames or links over, or removes the last name
    of, any file that has been at the override path, and never replaces, reads, prunes or restores
    a kept file; an in-place writer's later write lands in the kept file, to be merged by hand.
    Returns (kept, edited, left): kept is None when the file was created (nothing displaced), else
    the path of the displaced version (the kept file, or the in-flight file when it could not be
    given a kept name, in which case `left` is True); edited is True when a re-read of that file
    shows it no longer holds `expected` (a writer edited it in place after the compare) or could
    not be read. A write that lands after that re-read is in the kept file too.
    """
    backup = _backup_path(target)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _require_hard_links(target, temp)
        if expected is None:
            # (1) Create-if-absent: os.link never replaces a file at its destination.
            try:
                os.link(temp, target)
            except FileExistsError:
                raise _Refusal(
                    SETUP_CONFIG_CHANGED,
                    f"{target} was created while setup was running; it is left as it is and nothing "
                    "was written, rerun setup",
                ) from None
            return None, False, False
        # (2) A backup already here may hold a version setup never read: never move a file over it.
        if os.path.lexists(backup):
            raise _Refusal(
                SETUP_CONFIG_CHANGED,
                f"{backup} is left from an earlier setup run and may hold a version of {target}; "
                "nothing was written: check it, remove it and rerun setup",
            )
        try:
            os.rename(target, backup)
        except FileNotFoundError:
            raise _Refusal(
                SETUP_CONFIG_CHANGED,
                f"{target} was removed while setup was running; nothing was written, rerun setup",
            ) from None
        # The path is absent from here until a link puts a file back.
        try:
            unchanged = backup.read_bytes() == expected
            if unchanged:
                shutil.copymode(backup, temp)
        except OSError:
            _restore(target, backup)
            raise
        if not unchanged:
            _restore(target, backup)
            kept = f"; an earlier version is kept at {backup}" if os.path.lexists(backup) else ""
            raise _Refusal(
                SETUP_CONFIG_CHANGED,
                f"{target} changed while setup was running; it is left as it is and nothing was "
                f"written, rerun setup{kept}",
            )
        try:
            os.link(temp, target)
        except FileExistsError:
            raise _Refusal(
                SETUP_CONFIG_CHANGED,
                f"{target} reappeared while setup was writing it; it is left as it is, nothing was "
                f"written, and the version setup read is kept at {backup}",
            ) from None
        except OSError:
            _restore(target, backup)
            raise
        # Committed. The displaced version stays as a record under a kept name nothing has used
        # before, which crash recovery never reads. If it cannot be linked the file stays at the
        # in-flight name.
        kept = _keep_displaced(target, backup)
        left = kept is None
        if kept is None:
            kept = backup
        # Note whether an in-place writer has already edited it.
        try:
            return kept, kept.read_bytes() != expected, left
        except OSError:
            return kept, True, left
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def _take_lock(target: Path) -> int:
    """Open the lock file beside `target` (created if absent, never deleted) and flock it exclusively.

    Blocks until any other setup run lets go. The caller closes the returned descriptor to release
    the lock. The descriptor is not inheritable, so a program setup runs never holds it.
    """
    descriptor = os.open(target.parent / (target.name + LOCK_SUFFIX), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _absolute_path(path: str) -> Optional[str]:
    """The absolute form of `path`, or None when it cannot be made absolute."""
    try:
        return os.path.abspath(path)
    except (OSError, ValueError):
        return None


def _in_claude_plugin_cache(path: str) -> bool:
    """True when `path` has a `.claude` segment followed later by consecutive `plugins` then `cache`."""
    parts = Path(path).parts
    try:
        claude_at = parts.index(".claude")
    except ValueError:
        return False
    for i in range(claude_at + 1, len(parts) - 1):
        if parts[i] == "plugins" and parts[i + 1] == "cache":
            return True
    return False


def _stale_plugin_cache_pin(value: Any) -> bool:
    """True when `value` is a moved pin whose path is in Claude Code's plugin cache."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or not _moved(text):
        return False
    absolute = _absolute_path(os.path.expanduser(text))
    if absolute is None:
        return False
    return _in_claude_plugin_cache(absolute)


def setup() -> int:
    """Pin the ledger CLI: see the module text, Setup. Exit 0 when written, 1 on a named refusal."""
    override = Path(OVERRIDE_PATH).expanduser()
    # Follow a symlinked override so the link itself is kept and its target is rewritten.
    target = Path(os.path.realpath(override))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _refuse_setup(
            SETUP_WRITE_FAILED, f"could not write {target} ({_one_line(exc.strerror or str(exc))}); nothing was written"
        )
    try:
        lock = _take_lock(target)
    except OSError as exc:
        return _refuse_setup(
            SETUP_LOCK_FAILED,
            f"could not lock {target.name + LOCK_SUFFIX} beside {target} ({_one_line(exc.strerror or str(exc))}); nothing was written",
        )
    try:
        return _setup_locked(override, target)
    finally:
        os.close(lock)


def _setup_locked(override: Path, target: Path) -> int:
    """Setup's read, checks and write, run with the lock held."""
    try:
        _recover(target)
    except _Refusal as refusal:
        return _refuse_setup(refusal.token, refusal.detail)
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
    pinned = _absolute_path(found)
    if pinned is None:
        pinned = found
    elif _in_claude_plugin_cache(pinned):
        if not _stale_plugin_cache_pin(existing):
            return _refuse_setup(
                SETUP_PLUGIN_COMMAND,
                "the plugin's command needs no pin (its path changes with every plugin update); nothing was written",
            )
        config.pop(CONFIG_KEY, None)
        try:
            kept, edited, left = _commit(target, json.dumps(config, indent=2) + "\n", before)
        except _Refusal as refusal:
            return _refuse_setup(refusal.token, refusal.detail)
        except OSError as exc:
            return _refuse_setup(
                SETUP_WRITE_FAILED,
                f"could not write {target} ({_one_line(exc.strerror or str(exc))}); nothing was written",
            )
        if edited:
            return _refuse_setup(
                SETUP_CONFIG_CHANGED,
                f"the stale plugin pin was cleared in {target}, but the version it replaced, kept at {kept}, "
                "was edited after setup compared it or could not be read: merge any edit in it by hand, "
                "then remove it",
            )
        message = f"stale plugin pin cleared from {target}; the plugin route needs no pin"
        if left:
            print(
                f"{message}; the version it replaced was left at {kept} "
                f"(it could not be given a {target.name}{PREVIOUS_SUFFIX}.* name): remove it before the next setup run"
            )
        elif kept is not None:
            print(
                f"{message}; the version it replaced is kept at {kept}; "
                "remove it once no editor has it open"
            )
        else:
            print(message)
        return 0
    failure = probe(pinned)
    if failure is not None:
        return _refuse_setup(
            SETUP_INCOMPATIBLE, f"{pinned} fails the compatibility check ({failure}); nothing was written"
        )
    config[CONFIG_KEY] = pinned
    try:
        kept, edited, left = _commit(target, json.dumps(config, indent=2) + "\n", before)
    except _Refusal as refusal:
        return _refuse_setup(refusal.token, refusal.detail)
    except OSError as exc:
        return _refuse_setup(
            SETUP_WRITE_FAILED, f"could not write {target} ({_one_line(exc.strerror or str(exc))}); nothing was written"
        )
    if edited:
        return _refuse_setup(
            SETUP_CONFIG_CHANGED,
            f"{CONFIG_KEY} is set to {pinned} in {target}, but the version it replaced, kept at {kept}, "
            "was edited after setup compared it or could not be read: merge any edit in it by hand, "
            "then remove it",
        )
    if left:
        print(
            f"{CONFIG_KEY} set to {pinned} in {target}; the version it replaced was left at {kept} "
            f"(it could not be given a {target.name}{PREVIOUS_SUFFIX}.* name): remove it before the next setup run"
        )
    elif kept is not None:
        print(
            f"{CONFIG_KEY} set to {pinned} in {target}; the version it replaced is kept at {kept}; "
            "remove it once no editor has it open"
        )
    else:
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

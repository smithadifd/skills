#!/usr/bin/env python3
"""topics_dir.py — resolved topics directory, and the optional reads beside it.

The topics write directory is `config.json`'s `topics_dir` key. A non-empty
string wins, after `~` expansion. Absent, null, empty, or a non-string falls
through to `DEFAULT_TOPICS_DIR` (`~/.claude/topics`). A missing or malformed
config file degrades to that same default and reports one stderr line — it
does not raise.

This standalone config leaves the key unset, so resolving the shipped file
uses `DEFAULT_TOPICS_DIR`.

Convention, template, and model-routing documents are optional reads derived
from that one directory — not a second path key:

- `<topics_dir>/README.md`
- `<topics_dir>/TEMPLATE.md`
- `<topics_dir>/../standards/model-routing.md`

When one of those files is absent, `read_optional` returns a single-line
notice and does not raise. The caller continues with the built-in default
stated in `SKILL.md`. This module does not copy those documents' contents.

The shared `skills/_lib/config_path.py` helper is used when that file is
present (same contract as the other standalone resolvers). When it is not,
the same contract runs from the fallback below, so this skill still resolves
with no shared library on the path.

CLI
---
    python3 topics_dir.py
    python3 topics_dir.py --config <path>
    python3 topics_dir.py --notices
    python3 topics_dir.py --open <slug> --ask <text>

`--open` writes `<topics_dir>/<slug>.md` only when that file does not already
exist. Notices go to stderr. The resolved path goes to stdout.

Python 3 stdlib only. No network, no subprocess.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Optional, Union

HERE = Path(__file__).resolve().parent
SKILLS_DIR = HERE.parent
CONFIG_PATH = HERE / "config.json"
OVERRIDE_PATH = "~/.claude/skill-config/topic.json"

#: Unset default. A config that names `topics_dir` wins over this.
DEFAULT_TOPICS_DIR = "~/.claude/topics"

ABSENT_NOTICE = (
    "topic: {path} is absent — continuing with the built-in default "
    "stated in SKILL.md"
)

_SLUG_REJECT = frozenset({".", ".."})


class OpenResult(NamedTuple):
    path: Path
    notices: tuple[str, ...]


def _fallback_load_config(config_path: Union[str, Path]) -> Mapping[str, Any]:
    """Same degrade-and-report contract as `skills/_lib/config_path.py`."""
    path = Path(config_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(
            f"config_path: could not read {path} ({exc}) — degrading to "
            "{}; every key falls back to its documented default\n"
        )
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        sys.stderr.write(
            f"config_path: {path} is not valid JSON ({exc}) — degrading to "
            "{}; every key falls back to its documented default\n"
        )
        return {}
    if not isinstance(data, Mapping):
        sys.stderr.write(
            f"config_path: {path} did not parse to a JSON object (got "
            f"{type(data).__name__}) — degrading to {{}}; every key falls "
            "back to its documented default\n"
        )
        return {}
    return data


def _fallback_resolve_configured_path(
    config: Mapping[str, Any], key: str, default: str
) -> Path:
    value = config.get(key) if isinstance(config, Mapping) else None
    if value is not None and not isinstance(value, str):
        sys.stderr.write(
            f"config_path: {key!r} is a {type(value).__name__}, not a "
            f"string ({value!r}) — ignoring it; the default applies "
            "instead\n"
        )
        value = None
    if not value:
        value = default
    return Path(value).expanduser()


def _bind_resolver():
    lib = SKILLS_DIR / "_lib" / "config_path.py"
    if not lib.is_file():
        return _fallback_load_config, _fallback_resolve_configured_path
    spec = importlib.util.spec_from_file_location("topic_config_path", lib)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.load_config, module.resolve_configured_path


_load_config, _resolve_configured_path = _bind_resolver()


def _load_override(path: Path) -> Optional[Mapping[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        sys.stderr.write(
            f"config_path: could not read override {path} ({exc}) — ignoring it\n"
        )
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        sys.stderr.write(
            f"config_path: override {path} is not valid JSON ({exc}) — ignoring it\n"
        )
        return None
    if not isinstance(data, Mapping):
        sys.stderr.write(
            f"config_path: override {path} did not parse to a JSON object — ignoring it\n"
        )
        return None
    value = data.get("topics_dir")
    if value is not None and not isinstance(value, str):
        sys.stderr.write(
            f"config_path: override {path} has invalid 'topics_dir' value "
            f"({type(value).__name__}) — ignoring it\n"
        )
        return None
    return data


def resolve(config_path: Optional[Path] = None) -> Path:
    """Resolve ``topics_dir`` using explicit, override, shipped, then default.

    Missing configuration degrades to the unset default and never raises.
    """
    if config_path is not None:
        config = _load_config(config_path)
    else:
        config = _load_config(CONFIG_PATH)
        override_path = Path(OVERRIDE_PATH).expanduser()
        override = _load_override(override_path)
        if override is not None:
            config = {**config, **override}
    return _resolve_configured_path(config, "topics_dir", DEFAULT_TOPICS_DIR)


def normative_paths(topics: Path) -> dict[str, Path]:
    """The three optional reads, derived from the topics directory alone."""
    return {
        "convention": topics / "README.md",
        "template": topics / "TEMPLATE.md",
        "model_routing": topics.parent / "standards" / "model-routing.md",
    }


def absent_notice(path: Path) -> str:
    return ABSENT_NOTICE.format(path=path)


def read_optional(path: Path) -> tuple[Optional[str], Optional[str]]:
    """Return `(text, None)` when `path` is a file.

    When it is not, return `(None, notice)` and do not raise. The notice is
    one line: the path is absent, continue with the built-in default stated
    in `SKILL.md`.
    """
    if not path.is_file():
        return None, absent_notice(path)
    return path.read_text(encoding="utf-8"), None


def collect_notices(topics: Path) -> tuple[str, ...]:
    notices: list[str] = []
    for path in normative_paths(topics).values():
        _text, notice = read_optional(path)
        if notice:
            notices.append(notice)
    return tuple(notices)


def builtin_topic(slug: str, ask: str, today: str) -> str:
    """Skeleton used when `<topics_dir>/TEMPLATE.md` is absent.

    Section names match the built-in default stated in `SKILL.md`. This is
    not a copy of any external template.
    """
    return (
        f"# {slug}\n"
        "\n"
        "Status: OPEN\n"
        f"Last synced: {today} — created\n"
        "\n"
        "## Ask\n"
        "\n"
        f"{ask}\n"
        "\n"
        "## External refs\n"
        "\n"
        "## Current state\n"
        "\n"
        "## Staged\n"
        "\n"
        "## Log\n"
        "\n"
        f"- {today} — opened\n"
        "\n"
        "## Promote-or-park\n"
        "\n"
        "## Close tier\n"
        "\n"
        "LITE\n"
    )


def _check_slug(slug: str) -> None:
    if (
        not slug
        or slug in _SLUG_REJECT
        or slug != Path(slug).name
        or "/" in slug
        or "\\" in slug
        or re.search(r"[\x00-\x1f]", slug)
    ):
        raise ValueError(f"slug must be a single path segment, got {slug!r}")


def _record_ask(body: str, ask: str) -> str:
    """Place `ask` under an `## Ask` heading.

    A template that already has that heading gets the ask under it.
    Otherwise the heading is appended. The ask text is never searched
    for as a substring of the template.
    """
    lines = body.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip() != "## Ask":
            continue
        if not line.endswith("\n"):
            lines[index] = line + "\n"
        insert_at = index + 1
        if insert_at < len(lines) and lines[insert_at].strip() == "":
            insert_at += 1
        lines.insert(insert_at, f"{ask}\n")
        return "".join(lines)
    return body + f"\n## Ask\n\n{ask}\n"


def open_topic(
    slug: str,
    ask: str,
    config_path: Optional[Path] = None,
    today: Optional[str] = None,
) -> OpenResult:
    """Write `<topics_dir>/<slug>.md` if it does not already exist.

    Prints one stderr line per absent normative read. Does not raise when
    those files are absent. Does not overwrite an existing topic file.
    """
    _check_slug(slug)
    topics = resolve(config_path)
    notices = collect_notices(topics)
    for notice in notices:
        sys.stderr.write(notice + "\n")
    template, _template_notice = read_optional(topics / "TEMPLATE.md")
    stamp = today or date.today().isoformat()
    if template is None:
        body = builtin_topic(slug, ask, stamp)
    else:
        body = template if template.endswith("\n") else template + "\n"
        body = _record_ask(body, ask)
    topics.mkdir(parents=True, exist_ok=True)
    dest = topics / f"{slug}.md"
    if not dest.exists():
        dest.write_text(body, encoding="utf-8")
    return OpenResult(dest, notices)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="This script: %s" % Path(__file__).resolve(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--notices", action="store_true")
    parser.add_argument("--open", dest="slug", default=None)
    parser.add_argument("--ask", default=None)
    args = parser.parse_args(argv)
    if args.slug is not None:
        if args.ask is None:
            parser.error("--open requires --ask")
        result = open_topic(args.slug, args.ask, args.config)
        print(result.path)
        return 0
    topics = resolve(args.config)
    if args.notices:
        for notice in collect_notices(topics):
            print(notice, file=sys.stderr)
    print(topics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

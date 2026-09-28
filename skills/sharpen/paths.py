#!/usr/bin/env python3
"""Resolve sharpen's write directory and optional source paths."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SKILL_DIR / "config.json"

DEFAULT_ROADMAPS = "~/.claude/roadmaps"
DEFAULT_STANDARDS = "~/.claude/standards"
SOURCE_DEFAULTS = {
    "delegation_loop_standard": ("standards", "delegation-loop.md"),
    "calibration_sot": ("roadmaps", "calibration.md"),
    "sot_path": ("roadmaps", "wave-current.md"),
    "model_routing_standard": ("standards", "model-routing.md"),
}


def _load_config(config_path: Path) -> Mapping[str, Any]:
    try:
        text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        sys.stderr.write(
            f"config_path: could not read {config_path} ({exc}) — degrading to "
            "{}; every key falls back to its documented default\n"
        )
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        sys.stderr.write(
            f"config_path: {config_path} is not valid JSON ({exc}) — degrading to "
            "{}; every key falls back to its documented default\n"
        )
        return {}
    if not isinstance(data, Mapping):
        sys.stderr.write(
            f"config_path: {config_path} did not parse to a JSON object (got "
            f"{type(data).__name__}) — degrading to {{}}; every key falls "
            "back to its documented default\n"
        )
        return {}
    return data


def _configured_string(config: Mapping[str, Any], key: str, default: str) -> str:
    value: Any = config
    for part in key.split("."):
        if not isinstance(value, Mapping):
            value = None
            break
        value = value.get(part)
    if value is not None and not isinstance(value, str):
        sys.stderr.write(
            f"config_path: {key!r} is a {type(value).__name__}, not a string "
            f"({value!r}) — ignoring it; the default applies instead\n"
        )
        value = None
    return value or default


def resolve_paths(config_path: Path = CONFIG_PATH) -> dict[str, Path]:
    """Resolve all sharpen paths with the standalone resolver."""
    config = _load_config(config_path)

    resolved = {
        "roadmaps": Path(
            _configured_string(config, "harness_root.roadmaps", DEFAULT_ROADMAPS)
        ).expanduser(),
        "standards": Path(
            _configured_string(config, "harness_root.standards", DEFAULT_STANDARDS)
        ).expanduser(),
    }
    for key, (subkey, default) in SOURCE_DEFAULTS.items():
        value = _configured_string(config, key, default)
        candidate = Path(value)
        if candidate.is_absolute() or value.startswith("~"):
            resolved[key] = candidate.expanduser()
        else:
            assert not candidate.is_absolute()
            resolved[key] = resolved[subkey] / candidate
    return resolved


def report_unavailable_sources(paths: Mapping[str, Path]) -> None:
    for key in SOURCE_DEFAULTS:
        path = paths[key]
        try:
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            sys.stderr.write(
                f"sharpen: optional source {key} unavailable at {path}; "
                "continuing with the built-in default.\n"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args(argv)

    paths = resolve_paths(args.config)
    report_unavailable_sources(paths)
    print(json.dumps({key: str(value) for key, value in paths.items()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Resolve sharpen's write directory and optional source paths."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SKILL_DIR / "config.json"
OVERRIDE_PATH = "~/.claude/skill-config/sharpen.json"

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

_STRING_OVERRIDE_KEYS = frozenset(
    {
        "brief_filename_pattern",
        "delegation_loop_standard",
        "calibration_sot",
        "sot_path",
        "model_routing_standard",
        "_notes",
    }
)


def _load_override(path: Path) -> Mapping[str, Any] | None:
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
    root_paths = data.get("harness_root")
    invalid_root_paths = root_paths is not None and (
        not isinstance(root_paths, Mapping)
        or any(
            value is not None and not isinstance(value, str)
            for value in root_paths.values()
        )
    )
    invalid_string = any(
        key in data and data[key] is not None and not isinstance(data[key], str)
        for key in _STRING_OVERRIDE_KEYS
    )
    invalid_boolean = (
        "brief_commit" in data
        and data["brief_commit"] is not None
        and not isinstance(data["brief_commit"], bool)
    )
    if invalid_root_paths or invalid_string or invalid_boolean:
        sys.stderr.write(
            f"config_path: override {path} has a value of the wrong type — ignoring it\n"
        )
        return None
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


def _resolve_standalone(config: Mapping[str, Any]) -> dict[str, Path]:
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


def _resolve_with_module(
    resolver_path: Path,
    config: Mapping[str, Any],
    cwd: Path | None,
) -> dict[str, Path]:
    module_name = "_sharpen_configured_path_resolver"
    spec = importlib.util.spec_from_file_location(module_name, resolver_path)
    if spec is None or spec.loader is None:
        raise ImportError("could not create an import specification")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)

        resolve = getattr(module, "resolve", None)
        resolve_relative = getattr(module, "resolve_relative", None)
        if not callable(resolve):
            raise TypeError("resolve is missing or not callable")
        if not callable(resolve_relative):
            raise TypeError("resolve_relative is missing or not callable")
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    root_paths = resolve(config, cwd=cwd)
    resolved = {"roadmaps": root_paths.roadmaps, "standards": root_paths.standards}
    for key, (subkey, default) in SOURCE_DEFAULTS.items():
        value = _configured_string(config, key, default)
        resolved[key] = resolve_relative(root_paths, subkey, value)
    return resolved


def resolve_paths(
    config_path: Path | None = None, *, cwd: Path | None = None
) -> dict[str, Path]:
    """Resolve all sharpen paths, optionally using a configured resolver."""
    if config_path is not None:
        config = _load_config(config_path)
    else:
        config = _load_config(CONFIG_PATH)
        override = _load_override(Path(OVERRIDE_PATH).expanduser())
        if override is not None:
            config = {**config, **override}

    resolver = _configured_string(config, "harness_root.resolver", "")
    if resolver:
        resolver_path = Path(resolver).expanduser()
        try:
            if not (Path(resolver).is_absolute() or resolver.startswith("~")):
                raise ValueError("path must be absolute or ~-prefixed")
            return _resolve_with_module(resolver_path, config, cwd)
        except Exception as exc:
            sys.stderr.write(
                f"config_path: harness_root.resolver {resolver_path} "
                f"({type(exc).__name__}: {exc}) — degrading to standalone resolution\n"
            )
    return _resolve_standalone(config)


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
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--cwd", type=Path, default=None)
    args = parser.parse_args(argv)

    paths = resolve_paths(args.config, cwd=args.cwd)
    report_unavailable_sources(paths)
    print(json.dumps({key: str(value) for key, value in paths.items()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

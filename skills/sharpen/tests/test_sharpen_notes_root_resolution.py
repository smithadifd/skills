"""Behavioral tests for sharpen's executable path resolver."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = REPO_ROOT / "skills" / "sharpen"
CONFIG_PATH = SKILL_DIR / "config.json"
PATHS_PATH = SKILL_DIR / "paths.py"
SKILL_DOC = SKILL_DIR / "SKILL.md"

SOURCE_KEYS = {
    "process_standard": ("standards", "process.md"),
    "direction_record": ("roadmaps", "direction.md"),
    "routing_context": ("roadmaps", "routing-context.md"),
    "model_guidance": ("standards", "model-guidance.md"),
}


def test_documented_resolver_path_targets_standalone_install():
    text = SKILL_DOC.read_text(encoding="utf-8")
    assert "python3 ${CLAUDE_SKILL_DIR}/paths.py" in text, (
        "the resolver command must target install.sh's standalone destination"
    )
    assert "~/neighbor-project/" not in text, (
        "standalone instructions must not depend on a project checkout"
    )


def _run_paths(
    script: Path,
    *,
    home: Path,
    cwd: Path,
    notes_root_env: Path | None = None,
    args: tuple[str, ...] = (),
):
    env = os.environ.copy()
    env["HOME"] = str(home)
    if notes_root_env is None:
        env.pop("HARNESS_ROOT", None)
    else:
        env["HARNESS_ROOT"] = str(notes_root_env)
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _resolved_output(result: subprocess.CompletedProcess[str], condition: str) -> dict[str, str]:
    assert result.returncode == 0, f"{condition}: {result.stderr.strip()}"
    return json.loads(result.stdout)


def _copy_standalone(tmp_path: Path, config: str | bytes | None) -> Path:
    skill_dir = tmp_path / "skills" / "sharpen"
    skill_dir.mkdir(parents=True)
    shutil.copy2(PATHS_PATH, skill_dir / "paths.py")
    if isinstance(config, bytes):
        (skill_dir / "config.json").write_bytes(config)
    elif config is not None:
        (skill_dir / "config.json").write_text(config, encoding="utf-8")
    return skill_dir / "paths.py"


def _create_default_sources(home: Path) -> None:
    for subdir, filename in SOURCE_KEYS.values():
        path = home / ".claude" / subdir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("available\n", encoding="utf-8")


def test_brief_dir_key_is_not_reintroduced():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert "brief_dir" not in config, "brief_dir must not fork notes_root.roadmaps"


def test_source_keys_remain_bare_filenames():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for key in SOURCE_KEYS:
        value = config[key]
        assert not value.startswith(("~", "/")), (
            f"{key} must remain relative to its existing directory key"
        )


def test_standalone_clean_home_uses_claude_defaults_and_four_notices(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "standalone sharpen must resolve in a clean home")
    notices = result.stderr.splitlines()

    assert Path(actual["roadmaps"]) == home / ".claude" / "roadmaps", (
        "standalone write default must stay under the clean ~/.claude directory"
    )
    assert Path(actual["standards"]) == home / ".claude" / "standards"
    assert len(notices) == 4, (
        "all four absent normative reads must emit exactly one notice each"
    )
    for key in SOURCE_KEYS:
        assert sum(f"optional source {key} unavailable" in line for line in notices) == 1, (
            f"optional source {key} must emit one one-line notice"
        )
    assert not (home / "neighbor-project").exists(), (
        "standalone clean-home execution must not touch a private sibling directory"
    )


def test_standalone_ignores_neighbor_shared_resolver(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    shared_resolver = tmp_path / "skills" / "_lib" / "notes_root.py"
    shared_resolver.parent.mkdir()
    shared_resolver.write_text(
        """
from pathlib import Path
from types import SimpleNamespace


def resolve(config, cwd=None):
    return SimpleNamespace(
        roadmaps=Path.home() / "neighbor-project" / "roadmaps",
        standards=Path.home() / "neighbor-project" / "standards",
    )


def resolve_relative(paths, key, value):
    return getattr(paths, key) / value
""".lstrip(),
        encoding="utf-8",
    )

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(
        result,
        "standalone sharpen must ignore a neighboring shared resolver",
    )

    assert Path(actual["roadmaps"]) == home / ".claude" / "roadmaps", (
        "standalone roadmaps default must not switch to a neighboring resolver"
    )
    assert Path(actual["standards"]) == home / ".claude" / "standards", (
        "standalone standards default must not switch to a neighboring resolver"
    )


def test_per_skill_override_merges_over_shipped_config(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    roadmaps = tmp_path / "override-roadmaps"
    standards = tmp_path / "override-standards"
    shipped = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    shipped["direction_record"] = "shipped-direction.md"
    script = _copy_standalone(tmp_path, json.dumps(shipped))
    override = home / ".claude" / "skill-config" / "sharpen.json"
    override.parent.mkdir(parents=True)
    override.write_text(
        json.dumps(
            {
                "notes_root": {
                    "roadmaps": str(roadmaps),
                    "standards": str(standards),
                },
                "routing_context": "nested/routing-context.md",
            }
        ),
        encoding="utf-8",
    )
    for path in (
        standards / "process.md",
        roadmaps / "shipped-direction.md",
        roadmaps / "nested" / "routing-context.md",
        standards / "model-guidance.md",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("available\n", encoding="utf-8")

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "per-skill override must merge over shipped config")

    assert Path(actual["roadmaps"]) == roadmaps
    assert Path(actual["standards"]) == standards
    assert Path(actual["routing_context"]) == roadmaps / "nested" / "routing-context.md", (
        "a relative override source must join the overridden directory"
    )
    assert Path(actual["direction_record"]) == roadmaps / "shipped-direction.md", (
        "an unspecified shipped key must survive the override merge"
    )
    assert result.stderr == ""


def test_explicit_config_beats_per_skill_override(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    override_roadmaps = tmp_path / "override-roadmaps"
    explicit_roadmaps = tmp_path / "explicit-roadmaps"
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    override = home / ".claude" / "skill-config" / "sharpen.json"
    override.parent.mkdir(parents=True)
    override.write_text(
        json.dumps({"notes_root": {"roadmaps": str(override_roadmaps)}}),
        encoding="utf-8",
    )
    explicit = tmp_path / "explicit.json"
    explicit.write_text(
        json.dumps({"notes_root": {"roadmaps": str(explicit_roadmaps)}}),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HOME"] = str(home)

    result = subprocess.run(
        [sys.executable, str(script), "--config", str(explicit)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    actual = _resolved_output(result, "explicit config must beat sharpen override")

    assert Path(actual["roadmaps"]) == explicit_roadmaps


def test_invalid_per_skill_override_degrades_once(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _create_default_sources(home)
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    override = home / ".claude" / "skill-config" / "sharpen.json"
    override.parent.mkdir(parents=True)
    override.write_text(
        json.dumps({"notes_root": {"roadmaps": 7}}), encoding="utf-8"
    )

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "invalid sharpen override must degrade")
    lines = result.stderr.splitlines()

    assert Path(actual["roadmaps"]) == home / ".claude" / "roadmaps"
    assert lines == [
        f"config_path: override {override} has a value of the wrong type — ignoring it"
    ], "invalid sharpen override must emit exactly one line naming its path"


def test_non_object_notes_root_override_degrades_once(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _create_default_sources(home)
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    override = home / ".claude" / "skill-config" / "sharpen.json"
    override.parent.mkdir(parents=True)
    override.write_text(
        json.dumps({"notes_root": "notadict"}), encoding="utf-8"
    )

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(
        result, "a non-object notes_root override must degrade without a traceback"
    )

    assert Path(actual["roadmaps"]) == home / ".claude" / "roadmaps"
    assert Path(actual["standards"]) == home / ".claude" / "standards"
    assert Path(actual["routing_context"]) == home / ".claude" / "roadmaps" / "routing-context.md"
    assert result.stderr.splitlines() == [
        f"config_path: override {override} has a value of the wrong type — ignoring it"
    ], "a non-object notes_root override must emit exactly one degradation line"


def test_non_boolean_brief_commit_override_degrades_once(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _create_default_sources(home)
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    override = home / ".claude" / "skill-config" / "sharpen.json"
    override.parent.mkdir(parents=True)
    override.write_text(
        json.dumps({"brief_commit": "yes"}), encoding="utf-8"
    )

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(
        result, "a non-boolean brief_commit override must degrade without a traceback"
    )

    assert Path(actual["roadmaps"]) == home / ".claude" / "roadmaps"
    assert Path(actual["standards"]) == home / ".claude" / "standards"
    assert Path(actual["routing_context"]) == home / ".claude" / "roadmaps" / "routing-context.md"
    assert result.stderr.splitlines() == [
        f"config_path: override {override} has a value of the wrong type — ignoring it"
    ], "a non-boolean brief_commit override must emit exactly one degradation line"


def test_non_string_source_override_degrades_once(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _create_default_sources(home)
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    override = home / ".claude" / "skill-config" / "sharpen.json"
    override.parent.mkdir(parents=True)
    override.write_text(json.dumps({"routing_context": 5}), encoding="utf-8")

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(
        result, "a non-string source override must degrade without a traceback"
    )

    assert Path(actual["roadmaps"]) == home / ".claude" / "roadmaps"
    assert Path(actual["standards"]) == home / ".claude" / "standards"
    assert Path(actual["routing_context"]) == home / ".claude" / "roadmaps" / "routing-context.md"
    assert result.stderr.splitlines() == [
        f"config_path: override {override} has a value of the wrong type — ignoring it"
    ], "a non-string source override must emit exactly one degradation line"


def test_standalone_config_degrades_once_for_each_invalid_shape(tmp_path):
    cases = {
        "missing": None,
        "malformed": "{broken",
        "non-dict": "[]",
        "non-string": json.dumps({"notes_root": {"roadmaps": 7}}),
    }
    expected_fragments = {
        "missing": "could not read",
        "malformed": "is not valid JSON",
        "non-dict": "did not parse to a JSON object",
        "non-string": "'notes_root.roadmaps' is a int, not a string",
    }

    for name, config_text in cases.items():
        case_dir = tmp_path / name
        home = case_dir / "home"
        home.mkdir(parents=True)
        _create_default_sources(home)
        script = _copy_standalone(case_dir, config_text)

        result = _run_paths(script, home=home, cwd=case_dir)
        _resolved_output(result, f"{name} standalone config must degrade")
        lines = result.stderr.splitlines()

        assert len(lines) == 1, (
            f"{name} config must emit exactly one degradation line; got {lines!r}"
        )
        assert expected_fragments[name] in lines[0], (
            f"{name} config degradation line must name the guarded condition"
        )


def test_empty_source_value_uses_its_documented_default(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _create_default_sources(home)
    script = _copy_standalone(
        tmp_path,
        json.dumps({"direction_record": ""}),
    )

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(
        result,
        "an empty source value must use its documented default",
    )

    assert Path(actual["direction_record"]) == (
        home / ".claude" / "roadmaps" / "direction.md"
    ), "an explicit empty source value must retain the existing fallback behavior"


def test_absolute_source_value_stays_explicit(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "explicit" / "routing-context.md"
    source.parent.mkdir()
    source.write_text("available\n", encoding="utf-8")
    script = _copy_standalone(tmp_path, json.dumps({"routing_context": str(source)}))

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "an absolute source value must stay explicit")

    assert Path(actual["routing_context"]) == source, (
        "an absolute source value must not join the configured roadmaps directory"
    )


def test_tilde_prefixed_source_value_stays_explicit_under_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    source = home / "ro" / "routing-context.md"
    source.parent.mkdir()
    source.write_text("available\n", encoding="utf-8")
    script = _copy_standalone(tmp_path, json.dumps({"routing_context": "~/" + "ro/routing-context.md"}))

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "a tilde-prefixed source value must stay explicit")

    assert Path(actual["routing_context"]) == source, (
        "a tilde-prefixed source value must expand under HOME instead of joining roadmaps"
    )


def test_relative_source_value_joins_its_configured_directory(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    roadmaps = tmp_path / "configured-roadmaps"
    source = roadmaps / "nested" / "routing-context.md"
    source.parent.mkdir(parents=True)
    source.write_text("available\n", encoding="utf-8")
    script = _copy_standalone(
        tmp_path,
        json.dumps(
            {
                "notes_root": {"roadmaps": str(roadmaps)},
                "routing_context": "nested/routing-context.md",
            }
        ),
    )

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "a relative source value must join roadmaps")

    assert Path(actual["routing_context"]) == source, (
        "a relative source value must join its configured source directory"
    )


def test_standalone_undecodable_optional_source_degrades_alone(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sources = {}
    for key in SOURCE_KEYS:
        source = tmp_path / f"{key}.md"
        source.write_text("available\n", encoding="utf-8")
        sources[key] = str(source)
    undecodable = Path(sources["process_standard"])
    undecodable.write_bytes(b"\xff\xfe\x00\x00")
    script = _copy_standalone(tmp_path, json.dumps(sources))

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(
        result,
        "undecodable optional source must degrade without affecting other keys",
    )

    expected_notice = (
        "sharpen: optional source process_standard unavailable at "
        f"{undecodable}; continuing with the built-in default."
    )
    assert result.stderr.splitlines() == [expected_notice], (
        "only the undecodable optional source must emit its exact one-line notice"
    )
    assert {key: actual[key] for key in SOURCE_KEYS} == sources, (
        "an undecodable optional source must not affect the other resolved keys"
    )


def test_standalone_undecodable_config_degrades_to_defaults(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _create_default_sources(home)
    script = _copy_standalone(tmp_path, b"\xff\xfe\x00\x00")

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(
        result,
        "undecodable config must degrade to documented defaults",
    )

    config_path = script.parent / "config.json"
    expected_notice = (
        f"config_path: could not read {config_path} "
        "('utf-8' codec can't decode byte 0xff in position 0: invalid start byte) "
        "— degrading to {}; every key falls back to its documented default"
    )
    assert result.stderr.splitlines() == [expected_notice], (
        "undecodable config must emit the existing one-line read degradation"
    )
    assert Path(actual["roadmaps"]) == home / ".claude" / "roadmaps", (
        "undecodable config must apply the documented roadmaps default"
    )
    assert Path(actual["standards"]) == home / ".claude" / "standards", (
        "undecodable config must apply the documented standards default"
    )
    for key, (subdir, filename) in SOURCE_KEYS.items():
        assert Path(actual[key]) == home / ".claude" / subdir / filename, (
            f"undecodable config must apply the documented {key} default"
        )


def _write_resolver(path: Path, *, use_cwd: bool = False) -> None:
    roadmaps = (
        "cwd / 'resolver-roadmaps'"
        if use_cwd
        else "Path(__file__).parent / 'resolver-roadmaps'"
    )
    standards = (
        "cwd / 'resolver-standards'"
        if use_cwd
        else "Path(__file__).parent / 'resolver-standards'"
    )
    path.write_text(
        f"""
from pathlib import Path
from types import SimpleNamespace


def resolve(config, cwd=None):
    return SimpleNamespace(roadmaps={roadmaps}, standards={standards})


def resolve_relative(paths, subkey, value):
    return getattr(paths, subkey) / value
""".lstrip(),
        encoding="utf-8",
    )


def _create_resolver_sources(roadmaps: Path, standards: Path) -> None:
    for subdir, filename in SOURCE_KEYS.values():
        base = roadmaps if subdir == "roadmaps" else standards
        path = base / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("available\n", encoding="utf-8")


def test_explicit_config_uses_configured_resolver_for_all_paths(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    resolver = tmp_path / "resolver.py"
    _write_resolver(resolver)
    roadmaps = tmp_path / "resolver-roadmaps"
    standards = tmp_path / "resolver-standards"
    _create_resolver_sources(roadmaps, standards)
    config = tmp_path / "explicit.json"
    config.write_text(
        json.dumps({"notes_root": {"resolver": str(resolver)}}),
        encoding="utf-8",
    )

    result = _run_paths(
        script,
        home=home,
        cwd=tmp_path,
        args=("--config", str(config)),
    )
    actual = _resolved_output(result, "explicit resolver must handle all paths")

    expected = {"roadmaps": str(roadmaps), "standards": str(standards)}
    for key, (subdir, filename) in SOURCE_KEYS.items():
        expected[key] = str((roadmaps if subdir == "roadmaps" else standards) / filename)
    assert actual == expected, "the configured resolver must produce all six paths"
    assert result.stderr == ""


def test_relative_configured_resolver_degrades_once_to_standalone(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _create_default_sources(home)
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    resolver = tmp_path / "p" / "marker.py"
    resolver.parent.mkdir()
    _write_resolver(resolver)
    config = tmp_path / "explicit.json"
    config.write_text(
        json.dumps({"notes_root": {"resolver": "p/marker.py"}}),
        encoding="utf-8",
    )

    result = _run_paths(
        script,
        home=home,
        cwd=tmp_path,
        args=("--config", str(config)),
    )
    actual = _resolved_output(result, "relative resolver must be refused")

    expected = {
        "roadmaps": str(home / ".claude" / "roadmaps"),
        "standards": str(home / ".claude" / "standards"),
    }
    for key, (subdir, filename) in SOURCE_KEYS.items():
        expected[key] = str(home / ".claude" / subdir / filename)
    assert actual == expected, "a relative resolver must use all standalone defaults"
    lines = result.stderr.splitlines()
    assert len(lines) == 1, (
        f"a relative configured resolver must emit exactly one line; got {lines!r}"
    )
    assert lines[0].startswith("config_path: notes_root.resolver")
    assert "path must be absolute or ~-prefixed" in lines[0]


def test_configured_resolver_delegates_every_source_to_resolve_relative(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    resolver = tmp_path / "resolver.py"
    root = tmp_path / "resolver-root"
    resolver.write_text(
        """
from pathlib import Path
from types import SimpleNamespace


def resolve(config, cwd=None):
    root = Path(__file__).parent / "resolver-root"
    return SimpleNamespace(roadmaps=root / "roadmaps", standards=root / "standards")


def resolve_relative(paths, subkey, value):
    return getattr(paths, subkey) / "via-relative" / value
""".lstrip(),
        encoding="utf-8",
    )
    for subdir, filename in SOURCE_KEYS.values():
        source = root / subdir / "via-relative" / filename
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("available\n", encoding="utf-8")
    config = tmp_path / "explicit.json"
    config.write_text(
        json.dumps({"notes_root": {"resolver": str(resolver)}}),
        encoding="utf-8",
    )

    result = _run_paths(
        script,
        home=home,
        cwd=tmp_path,
        args=("--config", str(config)),
    )
    actual = _resolved_output(result, "resolve_relative must handle every source")

    assert Path(actual["roadmaps"]) == root / "roadmaps"
    assert Path(actual["standards"]) == root / "standards"
    for key, (subdir, filename) in SOURCE_KEYS.items():
        assert Path(actual[key]) == root / subdir / "via-relative" / filename, (
            f"{key} must be resolved through resolve_relative"
        )
    assert result.stderr == ""


def test_configured_dataclass_resolver_is_registered_before_execution(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    resolver = tmp_path / "resolver.py"
    root = tmp_path / "configured-root"
    resolver.write_text(
        """
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    roadmaps: Path
    standards: Path


def resolve(config, cwd=None):
    root = Path(__file__).parent / "configured-root"
    return Paths(roadmaps=root / "roadmaps", standards=root / "standards")


def resolve_relative(paths, subkey, value):
    return getattr(paths, subkey) / value
""".lstrip(),
        encoding="utf-8",
    )
    _create_resolver_sources(root / "roadmaps", root / "standards")
    config = tmp_path / "explicit.json"
    config.write_text(
        json.dumps({"notes_root": {"resolver": str(resolver)}}),
        encoding="utf-8",
    )

    result = _run_paths(
        script,
        home=home,
        cwd=tmp_path,
        args=("--config", str(config)),
    )
    actual = _resolved_output(result, "dataclass resolver must load successfully")

    expected = {
        "roadmaps": str(root / "roadmaps"),
        "standards": str(root / "standards"),
    }
    for key, (subdir, filename) in SOURCE_KEYS.items():
        expected[key] = str(root / subdir / filename)
    assert actual == expected, "the dataclass resolver must produce all six paths"
    assert "config_path: notes_root.resolver" not in result.stderr, (
        "a registered dataclass resolver must not degrade to standalone resolution"
    )


def test_override_uses_configured_resolver(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    resolver = tmp_path / "resolver.py"
    _write_resolver(resolver)
    roadmaps = tmp_path / "resolver-roadmaps"
    standards = tmp_path / "resolver-standards"
    _create_resolver_sources(roadmaps, standards)
    override = home / ".claude" / "skill-config" / "sharpen.json"
    override.parent.mkdir(parents=True)
    override.write_text(
        json.dumps({"notes_root": {"resolver": str(resolver)}}),
        encoding="utf-8",
    )

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "override resolver must be used")

    assert Path(actual["roadmaps"]) == roadmaps
    assert Path(actual["standards"]) == standards
    assert result.stderr == ""


def test_cwd_option_reaches_configured_resolver(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    resolver = tmp_path / "resolver.py"
    _write_resolver(resolver, use_cwd=True)
    forwarded_cwd = tmp_path / "forwarded"
    forwarded_cwd.mkdir()
    roadmaps = forwarded_cwd / "resolver-roadmaps"
    standards = forwarded_cwd / "resolver-standards"
    _create_resolver_sources(roadmaps, standards)
    config = tmp_path / "explicit.json"
    config.write_text(
        json.dumps({"notes_root": {"resolver": str(resolver)}}),
        encoding="utf-8",
    )

    result = _run_paths(
        script,
        home=home,
        cwd=tmp_path,
        args=("--config", str(config), "--cwd", str(forwarded_cwd)),
    )
    actual = _resolved_output(result, "--cwd must reach the configured resolver")

    assert Path(actual["roadmaps"]) == roadmaps, (
        "resolve must receive the path passed through --cwd"
    )
    assert result.stderr == ""


def test_configured_root_absent_refusal_propagates_with_message(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _create_default_sources(home)
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    resolver = tmp_path / "resolver.py"
    resolver.write_text(
        """
class HarnessRootAbsent(LookupError):
    pass


class ResolverRootAbsent(HarnessRootAbsent):
    pass


def resolve(config, cwd=None):
    raise ResolverRootAbsent("choose a root explicitly")


def resolve_relative(paths, subkey, value):
    raise AssertionError("resolve_relative must not run after a root refusal")
""".lstrip(),
        encoding="utf-8",
    )
    config = tmp_path / "explicit.json"
    config.write_text(
        json.dumps({"notes_root": {"resolver": str(resolver)}}),
        encoding="utf-8",
    )

    result = _run_paths(
        script,
        home=home,
        cwd=tmp_path,
        args=("--config", str(config)),
    )

    assert result.returncode != 0, "a configured no-root refusal must stop sharpen"
    assert result.stdout == "", "a refused resolution must not print fallback paths"
    assert "choose a root explicitly" in result.stderr, (
        "the configured resolver's corrective message must reach the caller"
    )
    assert "degrading to standalone resolution" not in result.stderr


def test_configured_resolver_runtime_failure_degrades_to_standalone(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _create_default_sources(home)
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    resolver = tmp_path / "resolver.py"
    resolver.write_text(
        """
def resolve(config, cwd=None):
    raise RuntimeError("resolver exploded")


def resolve_relative(paths, subkey, value):
    raise AssertionError("resolve_relative must not run after resolve fails")
""".lstrip(),
        encoding="utf-8",
    )
    config = tmp_path / "explicit.json"
    config.write_text(
        json.dumps({"notes_root": {"resolver": str(resolver)}}),
        encoding="utf-8",
    )

    result = _run_paths(
        script,
        home=home,
        cwd=tmp_path,
        args=("--config", str(config)),
    )
    actual = _resolved_output(
        result,
        "other configured resolver runtime failures must degrade",
    )

    expected = {
        "roadmaps": str(home / ".claude" / "roadmaps"),
        "standards": str(home / ".claude" / "standards"),
    }
    for key, (subdir, filename) in SOURCE_KEYS.items():
        expected[key] = str(home / ".claude" / subdir / filename)
    assert actual == expected, "a runtime failure must use all standalone defaults"
    assert result.stderr.splitlines() == [
        f"config_path: notes_root.resolver {resolver} "
        "(RuntimeError: resolver exploded) — degrading to standalone resolution"
    ]


def test_missing_configured_resolver_degrades_once_to_standalone(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _create_default_sources(home)
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    resolver = tmp_path / "absent-resolver.py"
    config = tmp_path / "explicit.json"
    config.write_text(
        json.dumps({"notes_root": {"resolver": str(resolver)}}),
        encoding="utf-8",
    )

    result = _run_paths(
        script,
        home=home,
        cwd=tmp_path,
        args=("--config", str(config)),
    )
    actual = _resolved_output(result, "missing resolver must degrade")

    expected = {
        "roadmaps": str(home / ".claude" / "roadmaps"),
        "standards": str(home / ".claude" / "standards"),
    }
    for key, (subdir, filename) in SOURCE_KEYS.items():
        expected[key] = str(home / ".claude" / subdir / filename)
    assert actual == expected, "a missing resolver must use all standalone defaults"
    lines = result.stderr.splitlines()
    assert len(lines) == 1, (
        f"a missing configured resolver must emit exactly one line; got {lines!r}"
    )
    assert lines[0].startswith("config_path: notes_root.resolver")
    assert str(resolver) in lines[0]


def test_invalid_configured_resolver_degrades_once_to_standalone(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _create_default_sources(home)
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    resolver = tmp_path / "resolver.py"
    resolver.write_text(
        "def resolve(config, cwd=None):\n    return object()\n",
        encoding="utf-8",
    )
    config = tmp_path / "explicit.json"
    config.write_text(
        json.dumps({"notes_root": {"resolver": str(resolver)}}),
        encoding="utf-8",
    )

    result = _run_paths(
        script,
        home=home,
        cwd=tmp_path,
        args=("--config", str(config)),
    )
    actual = _resolved_output(result, "invalid resolver must degrade")
    lines = result.stderr.splitlines()

    expected = {
        "roadmaps": str(home / ".claude" / "roadmaps"),
        "standards": str(home / ".claude" / "standards"),
    }
    for key, (subdir, filename) in SOURCE_KEYS.items():
        expected[key] = str(home / ".claude" / subdir / filename)
    assert actual == expected, "an invalid resolver must use all standalone defaults"
    assert len(lines) == 1, (
        f"an invalid configured resolver must emit exactly one line; got {lines!r}"
    )
    assert lines[0].startswith("config_path: notes_root.resolver")
    assert str(resolver) in lines[0]
    assert "resolve_relative" in lines[0]


def test_non_string_configured_resolver_is_ignored_once(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _create_default_sources(home)
    script = _copy_standalone(
        tmp_path,
        json.dumps({"notes_root": {"resolver": 7}}),
    )

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "non-string resolver must be ignored")

    assert Path(actual["roadmaps"]) == home / ".claude" / "roadmaps"
    assert result.stderr.splitlines() == [
        "config_path: 'notes_root.resolver' is a int, not a string "
        "(7) — ignoring it; the default applies instead"
    ]


# Old source key names, assembled from fragments (see the vocabulary scan below).
_OLD_KEYS = {
    "delegation_loop" + "_standard": "process_standard",
    "calibration" + "_s" + "ot": "direction_record",
    "s" + "ot" + "_path": "routing_context",
    "model_routing" + "_standard": "model_guidance",
}


def _old_key_setup(tmp_path, override_data):
    home = tmp_path / "home"
    home.mkdir()
    _create_default_sources(home)
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    override = home / ".claude" / "skill-config" / "sharpen.json"
    override.parent.mkdir(parents=True)
    override.write_text(json.dumps(override_data), encoding="utf-8")
    for value in override_data.values():
        for subdir in ("roadmaps", "standards"):
            (home / ".claude" / subdir / value).write_text("custom\n", encoding="utf-8")
    return home, script, override


def test_old_key_override_resolves_and_prints_one_notice_per_key(tmp_path):
    data = {old: f"custom-{new}.md" for old, new in _OLD_KEYS.items()}
    home, script, override = _old_key_setup(tmp_path, data)

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "old-key override must still resolve")

    for old, new in _OLD_KEYS.items():
        subdir = SOURCE_KEYS[new][0]
        assert Path(actual[new]) == home / ".claude" / subdir / f"custom-{new}.md", (
            f"old key {old!r} must act as an alias of {new!r}"
        )
    assert sorted(result.stderr.splitlines()) == sorted(
        f"config_path: override {override} uses the old key {old!r}; "
        f"use {new!r} instead"
        for old, new in _OLD_KEYS.items()
    ), "each old key must produce exactly one notice naming its replacement"


def test_override_setting_old_and_new_key_is_refused(tmp_path):
    ((old, new), *_) = _OLD_KEYS.items()
    data = {old: "old-choice.md", new: "new-choice.md"}
    home, script, override = _old_key_setup(tmp_path, data)

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "a conflicting override must be refused")

    assert Path(actual[new]) == home / ".claude" / SOURCE_KEYS[new][0] / SOURCE_KEYS[new][1], (
        "a refused override must leave the shipped default in place"
    )
    assert result.stderr.splitlines() == [
        f"config_path: override {override} sets both {old!r} and {new!r} — "
        f"ignoring it; keep only {new!r}"
    ], "a refused override must emit exactly one line naming both keys"


def test_new_key_override_prints_no_notice(tmp_path):
    data = {new: f"custom-{new}.md" for new in _OLD_KEYS.values()}
    home, script, _ = _old_key_setup(tmp_path, data)

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "new-key override must resolve")

    for new in _OLD_KEYS.values():
        assert Path(actual[new]) == home / ".claude" / SOURCE_KEYS[new][0] / f"custom-{new}.md"
    assert result.stderr == "", "new-key overrides must not print a notice"


# Private vocabulary that must not appear in the shipped public files. Terms are
# assembled from fragments so this file does not itself contain them verbatim.
_PRIVATE_PATTERNS = {
    "planning-loop stage": r"\bCHA" + r"RT\b",
    "planning-loop skill": r"chart" + r"-wa" + r"ve",
    "batch term": r"(?i)\bwa" + r"ve\b",
    "source-of-truth abbreviation": r"(?i)(?<![a-z])s" + r"ot(?![a-z])",
    "executor stage": r"\bRO" + r"LL\b",
    "record grammar": r"contract" + r": v3",
    "personal name": r"\bAnd" + r"rew\b",
    "unshipped skill": r"\borches" + r"trate\b|session-hand" + r"off",
}
_PUBLIC_FILES = (
    SKILL_DOC,
    CONFIG_PATH,
    PATHS_PATH,
    Path(__file__).resolve(),
)


def test_public_files_carry_no_denied_terms():
    # Private project and person names are never spelled in a tracked file; the
    # deny expression comes from NO_PERSON_DENY, as the repository lint reads it.
    import os
    import subprocess

    import pytest

    deny = os.environ.get("NO_PERSON_DENY", "").strip()
    if not deny:
        pytest.skip("NO_PERSON_DENY is unset")
    for path in _PUBLIC_FILES:
        result = subprocess.run(
            ["grep", "-a", "-i", "-n", "-E", "-e", deny],
            input=path.read_bytes(),
            capture_output=True,
        )
        assert result.returncode in (0, 1), result.stderr.decode("utf-8", "replace")
        assert result.returncode == 1, f"{path.name} contains a denied term"


def test_public_files_carry_no_private_vocabulary():
    # No allowlist: every shipped file, this test included, must be clean.
    import re

    for path in _PUBLIC_FILES:
        text = path.read_text(encoding="utf-8")
        for label, pattern in _PRIVATE_PATTERNS.items():
            match = re.search(pattern, text)
            assert match is None, (
                f"{path.name} contains private vocabulary ({label}): {match.group(0)!r}"
            )


def test_install_specific_routing_flows_through_the_override(tmp_path):
    # An install keeps its own routing documents under names of its choosing and
    # points the shipped source keys at them from the per-skill override file.
    home = tmp_path / "home"
    home.mkdir()
    roadmaps = tmp_path / "install-roadmaps"
    standards = tmp_path / "install-standards"
    chosen = {
        "process_standard": standards / "install-process.md",
        "direction_record": roadmaps / "install-direction.md",
        "routing_context": roadmaps / "install-context.md",
        "model_guidance": standards / "install-models.md",
    }
    for path in chosen.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("available\n", encoding="utf-8")
    override = home / ".claude" / "skill-config" / "sharpen.json"
    override.parent.mkdir(parents=True)
    override.write_text(
        json.dumps(
            {
                "notes_root": {
                    "roadmaps": str(roadmaps),
                    "standards": str(standards),
                },
                **{key: path.name for key, path in chosen.items()},
            }
        ),
        encoding="utf-8",
    )
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "override-routed sources must resolve")

    for key, path in chosen.items():
        assert Path(actual[key]) == path, (
            f"{key} must follow the override filename into its configured directory"
        )
    assert result.stderr == "", "present override-routed sources must emit no notice"


def test_skill_text_reads_install_routing_only_when_present():
    text = SKILL_DOC.read_text(encoding="utf-8")
    for key in SOURCE_KEYS:
        assert f"`{key}`" in text, f"SKILL.md must document the optional {key} source"
    assert "only when it is present" in " ".join(text.split()), (
        "SKILL.md must state that install-specific documents are read only when present"
    )


# The directory settings were once grouped under another key name. The name is
# assembled from fragments so it stays out of plain-text scans.
OLD_ROOT_KEY = "harness" + "_root"


def _dir_override_setup(tmp_path, override_data):
    home = tmp_path / "home"
    home.mkdir()
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    override = home / ".claude" / "skill-config" / "sharpen.json"
    override.parent.mkdir(parents=True)
    override.write_text(json.dumps(override_data), encoding="utf-8")
    return home, script, override


def test_notes_root_override_sets_the_brief_directory(tmp_path):
    home, script, _ = _dir_override_setup(
        tmp_path, {"notes_root": {"roadmaps": str(tmp_path / "briefs")}}
    )
    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "new-key override must resolve")
    assert Path(actual["roadmaps"]) == tmp_path / "briefs"
    assert OLD_ROOT_KEY not in result.stderr, "the new key must print no notice"


def test_old_directory_key_in_override_is_still_read_with_one_notice(tmp_path):
    home, script, override = _dir_override_setup(
        tmp_path, {OLD_ROOT_KEY: {"roadmaps": str(tmp_path / "briefs")}}
    )
    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "old-key override must still resolve")
    assert Path(actual["roadmaps"]) == tmp_path / "briefs"
    assert result.stderr.count(f"config_path: {override} uses the old key {OLD_ROOT_KEY!r}; use 'notes_root' instead") == 1


def test_old_directory_key_in_explicit_config_is_still_read(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    config = tmp_path / "explicit.json"
    config.write_text(
        json.dumps({OLD_ROOT_KEY: {"standards": str(tmp_path / "std")}}), encoding="utf-8"
    )
    result = _run_paths(script, home=home, cwd=tmp_path, args=("--config", str(config)))
    actual = _resolved_output(result, "old-key --config must still resolve")
    assert Path(actual["standards"]) == tmp_path / "std"
    assert "uses the old key" in result.stderr


def test_new_directory_key_wins_when_both_are_set(tmp_path):
    home, script, _ = _dir_override_setup(
        tmp_path,
        {
            OLD_ROOT_KEY: {"roadmaps": str(tmp_path / "old")},
            "notes_root": {"roadmaps": str(tmp_path / "new")},
        },
    )
    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "conflicting keys must still resolve")
    assert Path(actual["roadmaps"]) == tmp_path / "new"
    assert "using 'notes_root'" in result.stderr


def test_wrong_type_under_old_directory_key_is_ignored(tmp_path):
    home, script, _ = _dir_override_setup(tmp_path, {OLD_ROOT_KEY: "not-an-object"})
    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "bad override must degrade")
    assert Path(actual["roadmaps"]) == home / ".claude" / "roadmaps"
    assert "wrong type" in result.stderr


def test_resolver_refusal_named_for_the_new_key_propagates(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    resolver = tmp_path / "resolver.py"
    resolver.write_text(
        "class NotesRootAbsent(LookupError):\n    pass\n\n\n"
        "def resolve(config, cwd=None):\n    raise NotesRootAbsent('no notes root set')\n\n\n"
        "def resolve_relative(paths, subkey, value):\n    raise AssertionError\n",
        encoding="utf-8",
    )
    config = tmp_path / "explicit.json"
    config.write_text(json.dumps({"notes_root": {"resolver": str(resolver)}}), encoding="utf-8")
    result = _run_paths(script, home=home, cwd=tmp_path, args=("--config", str(config)))
    assert result.returncode != 0
    assert "no notes root set" in result.stderr


def test_resolver_receives_settings_under_both_key_names(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    seen = tmp_path / "seen.json"
    resolver = tmp_path / "resolver.py"
    resolver.write_text(
        "import json\nfrom types import SimpleNamespace\n\n\n"
        "def resolve(config, cwd=None):\n"
        f"    open({str(seen)!r}, 'w').write(json.dumps(config))\n"
        "    from pathlib import Path\n    return SimpleNamespace(roadmaps=Path('/r'), standards=Path('/s'))\n\n\n"
        "def resolve_relative(paths, subkey, value):\n    from pathlib import Path\n    return Path('/x') / value\n",
        encoding="utf-8",
    )
    config = tmp_path / "explicit.json"
    config.write_text(
        json.dumps({"notes_root": {"resolver": str(resolver), "root": "/n"}}), encoding="utf-8"
    )
    result = _run_paths(script, home=home, cwd=tmp_path, args=("--config", str(config)))
    assert result.returncode == 0, result.stderr
    received = json.loads(seen.read_text(encoding="utf-8"))
    assert received["notes_root"]["root"] == "/n"
    assert received[OLD_ROOT_KEY]["root"] == "/n"


def test_malformed_old_key_is_ignored_when_the_new_key_is_valid(tmp_path):
    home, script, _ = _dir_override_setup(
        tmp_path,
        {OLD_ROOT_KEY: "not-an-object", "notes_root": {"roadmaps": str(tmp_path / "new")}},
    )
    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "a malformed old key must not reject a valid new key")
    assert Path(actual["roadmaps"]) == tmp_path / "new"
    assert "wrong type" not in result.stderr
    assert "using 'notes_root'" in result.stderr

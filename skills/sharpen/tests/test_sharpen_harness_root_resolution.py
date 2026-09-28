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
    "delegation_loop_standard": ("standards", "delegation-loop.md"),
    "calibration_sot": ("roadmaps", "calibration.md"),
    "sot_path": ("roadmaps", "wave-current.md"),
    "model_routing_standard": ("standards", "model-routing.md"),
}


def test_documented_resolver_path_targets_standalone_install():
    text = SKILL_DOC.read_text(encoding="utf-8")
    assert "python3 ~/.claude/skills/sharpen/paths.py" in text, (
        "the resolver command must target install.sh's standalone destination"
    )
    assert "~/claude-toolkit/" not in text, (
        "standalone instructions must not depend on a toolkit checkout"
    )


def _run_paths(script: Path, *, home: Path, cwd: Path, harness_root_env: Path | None = None):
    env = os.environ.copy()
    env["HOME"] = str(home)
    if harness_root_env is None:
        env.pop("HARNESS_ROOT", None)
    else:
        env["HARNESS_ROOT"] = str(harness_root_env)
    return subprocess.run(
        [sys.executable, str(script)],
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
    assert "brief_dir" not in config, "brief_dir must not fork harness_root.roadmaps"


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
    assert not (home / "foundry").exists(), (
        "standalone clean-home execution must not touch the foundry directory"
    )


def test_standalone_ignores_neighbor_shared_resolver(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    shared_resolver = tmp_path / "skills" / "_lib" / "harness_root.py"
    shared_resolver.parent.mkdir()
    shared_resolver.write_text(
        """
from pathlib import Path
from types import SimpleNamespace


def resolve(config, cwd=None):
    return SimpleNamespace(
        roadmaps=Path.home() / "foundry" / "roadmaps",
        standards=Path.home() / "foundry" / "standards",
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
    shipped["calibration_sot"] = "shipped-calibration.md"
    script = _copy_standalone(tmp_path, json.dumps(shipped))
    override = home / ".claude" / "skill-config" / "sharpen.json"
    override.parent.mkdir(parents=True)
    override.write_text(
        json.dumps(
            {
                "harness_root": {
                    "roadmaps": str(roadmaps),
                    "standards": str(standards),
                },
                "sot_path": "nested/wave-current.md",
            }
        ),
        encoding="utf-8",
    )
    for path in (
        standards / "delegation-loop.md",
        roadmaps / "shipped-calibration.md",
        roadmaps / "nested" / "wave-current.md",
        standards / "model-routing.md",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("available\n", encoding="utf-8")

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "per-skill override must merge over shipped config")

    assert Path(actual["roadmaps"]) == roadmaps
    assert Path(actual["standards"]) == standards
    assert Path(actual["sot_path"]) == roadmaps / "nested" / "wave-current.md", (
        "a relative override source must join the overridden directory"
    )
    assert Path(actual["calibration_sot"]) == roadmaps / "shipped-calibration.md", (
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
        json.dumps({"harness_root": {"roadmaps": str(override_roadmaps)}}),
        encoding="utf-8",
    )
    explicit = tmp_path / "explicit.json"
    explicit.write_text(
        json.dumps({"harness_root": {"roadmaps": str(explicit_roadmaps)}}),
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
        json.dumps({"harness_root": {"roadmaps": 7}}), encoding="utf-8"
    )

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "invalid sharpen override must degrade")
    lines = result.stderr.splitlines()

    assert Path(actual["roadmaps"]) == home / ".claude" / "roadmaps"
    assert lines == [
        f"config_path: override {override} has a value of the wrong type — ignoring it"
    ], "invalid sharpen override must emit exactly one line naming its path"


def test_non_object_harness_root_override_degrades_once(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _create_default_sources(home)
    script = _copy_standalone(tmp_path, CONFIG_PATH.read_text(encoding="utf-8"))
    override = home / ".claude" / "skill-config" / "sharpen.json"
    override.parent.mkdir(parents=True)
    override.write_text(
        json.dumps({"harness_root": "notadict"}), encoding="utf-8"
    )

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(
        result, "a non-object harness_root override must degrade without a traceback"
    )

    assert Path(actual["roadmaps"]) == home / ".claude" / "roadmaps"
    assert Path(actual["standards"]) == home / ".claude" / "standards"
    assert Path(actual["sot_path"]) == home / ".claude" / "roadmaps" / "wave-current.md"
    assert result.stderr.splitlines() == [
        f"config_path: override {override} has a value of the wrong type — ignoring it"
    ], "a non-object harness_root override must emit exactly one degradation line"


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
    assert Path(actual["sot_path"]) == home / ".claude" / "roadmaps" / "wave-current.md"
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
    override.write_text(json.dumps({"sot_path": 5}), encoding="utf-8")

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(
        result, "a non-string source override must degrade without a traceback"
    )

    assert Path(actual["roadmaps"]) == home / ".claude" / "roadmaps"
    assert Path(actual["standards"]) == home / ".claude" / "standards"
    assert Path(actual["sot_path"]) == home / ".claude" / "roadmaps" / "wave-current.md"
    assert result.stderr.splitlines() == [
        f"config_path: override {override} has a value of the wrong type — ignoring it"
    ], "a non-string source override must emit exactly one degradation line"


def test_standalone_config_degrades_once_for_each_invalid_shape(tmp_path):
    cases = {
        "missing": None,
        "malformed": "{broken",
        "non-dict": "[]",
        "non-string": json.dumps({"harness_root": {"roadmaps": 7}}),
    }
    expected_fragments = {
        "missing": "could not read",
        "malformed": "is not valid JSON",
        "non-dict": "did not parse to a JSON object",
        "non-string": "'harness_root.roadmaps' is a int, not a string",
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
        json.dumps({"calibration_sot": ""}),
    )

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(
        result,
        "an empty source value must use its documented default",
    )

    assert Path(actual["calibration_sot"]) == (
        home / ".claude" / "roadmaps" / "calibration.md"
    ), "an explicit empty source value must retain the existing fallback behavior"


def test_absolute_source_value_stays_explicit(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "explicit" / "wave-current.md"
    source.parent.mkdir()
    source.write_text("available\n", encoding="utf-8")
    script = _copy_standalone(tmp_path, json.dumps({"sot_path": str(source)}))

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "an absolute source value must stay explicit")

    assert Path(actual["sot_path"]) == source, (
        "an absolute source value must not join the configured roadmaps directory"
    )


def test_tilde_prefixed_source_value_stays_explicit_under_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    source = home / "ro" / "wave-current.md"
    source.parent.mkdir()
    source.write_text("available\n", encoding="utf-8")
    script = _copy_standalone(tmp_path, json.dumps({"sot_path": "~/ro/wave-current.md"}))

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "a tilde-prefixed source value must stay explicit")

    assert Path(actual["sot_path"]) == source, (
        "a tilde-prefixed source value must expand under HOME instead of joining roadmaps"
    )


def test_relative_source_value_joins_its_configured_directory(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    roadmaps = tmp_path / "configured-roadmaps"
    source = roadmaps / "nested" / "wave-current.md"
    source.parent.mkdir(parents=True)
    source.write_text("available\n", encoding="utf-8")
    script = _copy_standalone(
        tmp_path,
        json.dumps(
            {
                "harness_root": {"roadmaps": str(roadmaps)},
                "sot_path": "nested/wave-current.md",
            }
        ),
    )

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(result, "a relative source value must join roadmaps")

    assert Path(actual["sot_path"]) == source, (
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
    undecodable = Path(sources["delegation_loop_standard"])
    undecodable.write_bytes(b"\xff\xfe\x00\x00")
    script = _copy_standalone(tmp_path, json.dumps(sources))

    result = _run_paths(script, home=home, cwd=tmp_path)
    actual = _resolved_output(
        result,
        "undecodable optional source must degrade without affecting other keys",
    )

    expected_notice = (
        "sharpen: optional source delegation_loop_standard unavailable at "
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

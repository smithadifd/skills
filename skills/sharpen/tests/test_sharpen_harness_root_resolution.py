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
        [sys.executable, str(script), "--cwd", str(cwd)],
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

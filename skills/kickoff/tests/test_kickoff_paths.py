import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = SKILL_DIR.parent


def _copy_skills(tmp_path: Path, *names: str) -> Path:
    copied_skills = tmp_path / "skills"
    copied_skills.mkdir()
    for name in names:
        shutil.copytree(SKILLS_DIR / name, copied_skills / name)
    return copied_skills


def _run(skill_dir: Path, home: Path, slug: str = "clean-home"):
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("HARNESS_ROOT", None)
    return subprocess.run(
        [sys.executable, str(skill_dir / "paths.py"), "--slug", slug],
        capture_output=True,
        text=True,
        env=env,
    )


def test_topic_config_change_is_followed_without_shared_library(tmp_path):
    copied_skills = _copy_skills(tmp_path, "topic", "kickoff")
    target = tmp_path / "X"
    config_path = copied_skills / "topic" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["topics_dir"] = str(target)
    config_path.write_text(json.dumps(config), encoding="utf-8")

    process = _run(copied_skills / "kickoff", tmp_path / "home", "drift")

    assert process.returncode == 0, (
        "DRIFT: kickoff must load topic's resolver without skills/_lib"
    )
    payload = json.loads(process.stdout)
    assert Path(payload["topics_dir"]) == target, (
        "DRIFT: kickoff must follow topic's configured topics_dir via resolve()"
    )
    assert Path(payload["topic_template"]) == target / "TEMPLATE.md", (
        "DRIFT: kickoff must derive the template through topic's normative_paths()"
    )
    assert Path(payload["model_routing_standard"]) == (
        tmp_path / "standards" / "model-routing.md"
    ), "DRIFT: kickoff must derive model routing through topic's normative_paths()"


def test_topic_override_reaches_kickoff_topic_path(tmp_path):
    copied_skills = _copy_skills(tmp_path, "topic", "kickoff")
    home = tmp_path / "home"
    target = tmp_path / "override-topics"
    override = home / ".claude" / "skill-config" / "topic.json"
    override.parent.mkdir(parents=True)
    override.write_text(json.dumps({"topics_dir": str(target)}), encoding="utf-8")

    process = _run(copied_skills / "kickoff", home, "from-override")

    assert process.returncode == 0
    payload = json.loads(process.stdout)
    assert Path(payload["topics_dir"]) == target
    assert Path(payload["topic_path"]) == target / "from-override.md", (
        "kickoff must obtain its topic path through topic's override-aware resolver"
    )


def test_shipped_standalone_topic_config_uses_clean_home_defaults(tmp_path):
    copied_skills = _copy_skills(tmp_path, "topic", "kickoff")
    home = tmp_path / "home"
    home.mkdir()

    process = _run(copied_skills / "kickoff", home, "standalone")

    assert process.returncode == 0, (
        "SHIPPED-CONFIG: standalone path resolution must use topic's shipped config"
    )
    payload = json.loads(process.stdout)
    topics_dir = home / ".claude" / "topics"
    assert Path(payload["topics_dir"]) == topics_dir, (
        "SHIPPED-CONFIG: unset topics_dir must resolve to ~/.claude/topics"
    )
    assert Path(payload["topic_template"]) == topics_dir / "TEMPLATE.md", (
        "SHIPPED-CONFIG: topic template must be derived from the resolved topics directory"
    )
    assert Path(payload["model_routing_standard"]) == (
        home / ".claude" / "standards" / "model-routing.md"
    ), "SHIPPED-CONFIG: model routing must be derived beside the topics directory"
    assert "topic resolver not found" not in process.stderr, (
        "SHIPPED-CONFIG: kickoff must use the shipped topic resolver"
    )


def test_kickoff_alone_uses_clean_home_fallback_and_three_notices(tmp_path):
    copied_skills = _copy_skills(tmp_path, "kickoff")
    home = tmp_path / "home"
    home.mkdir()

    process = _run(copied_skills / "kickoff", home)

    assert process.returncode == 0, (
        "KICKOFF-ALONE: absent optional template must not stop clean-HOME resolution"
    )
    payload = json.loads(process.stdout)
    target = Path(payload["topic_path"])
    assert target == home / ".claude" / "topics" / "clean-home.md", (
        "KICKOFF-ALONE: topic-absent fallback must write under ~/.claude/topics"
    )
    assert process.stderr.splitlines() == [
        "kickoff: topic resolver not found at "
        f"{copied_skills}/topic/topics_dir.py; using ~/.claude/topics",
        "kickoff: optional topic template not found at "
        f"{home}/.claude/topics/TEMPLATE.md; using the built-in hand-off shape in SKILL.md",
        "kickoff: optional model-routing standard not found at "
        f"{home}/.claude/standards/model-routing.md; using the current session model with no "
        "model-specific consult",
    ], "KICKOFF-ALONE: resolver, template, and model routing must emit exactly 3 notices"

    target.parent.mkdir(parents=True)
    target.write_text("# clean home\n", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "# clean home\n", (
        "KICKOFF-ALONE: resolved clean-HOME topic path must be writable"
    )
    assert {path.name for path in home.iterdir()} == {".claude"}, (
        "KICKOFF-ALONE: clean-HOME writes must touch only the ~/.claude tree"
    )
    assert not (home / "private-sibling-repo").exists(), (
        "KICKOFF-ALONE: clean-HOME resolution must not touch ~/private-sibling-repo"
    )


def test_skill_invocation_uses_installed_path():
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "python3 ~/.claude/skills/kickoff/paths.py --slug <slug>" in skill, (
        "INSTALL-PATH: kickoff instructions must invoke the installed resolver"
    )


def test_kickoff_owns_no_config_file():
    assert not (SKILL_DIR / "config.json").exists(), (
        "CONFIG-OWNERSHIP: kickoff must own no path config keys"
    )

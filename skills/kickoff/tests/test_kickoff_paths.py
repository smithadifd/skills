import json
import os
import re
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
    assert Path(payload["model_notes"]) == (
        tmp_path / "standards" / "model-notes.md"
    ), "DRIFT: kickoff must derive model notes through topic's normative_paths()"


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
    assert Path(payload["model_notes"]) == (
        home / ".claude" / "standards" / "model-notes.md"
    ), "SHIPPED-CONFIG: model notes must be derived beside the topics directory"
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
        "kickoff: optional model notes not found at "
        f"{home}/.claude/standards/model-notes.md; using the current session model with no "
        "model-specific consult",
    ], "KICKOFF-ALONE: resolver, template, and model notes must emit exactly 3 notices"

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
    assert "python3 ${CLAUDE_SKILL_DIR}/paths.py --slug <slug>" in skill, (
        "INSTALL-PATH: kickoff instructions must invoke the installed resolver"
    )
    assert "topic_convention" in skill, (
        "LOCAL-CONVENTIONS: kickoff must tell the reader to consult the optional "
        "topic convention path"
    )


def test_topic_convention_path_follows_topics_dir(tmp_path):
    copied_skills = _copy_skills(tmp_path, "topic", "kickoff")
    home = tmp_path / "home"
    target = tmp_path / "override-topics"
    override = home / ".claude" / "skill-config" / "topic.json"
    override.parent.mkdir(parents=True)
    override.write_text(json.dumps({"topics_dir": str(target)}), encoding="utf-8")

    process = _run(copied_skills / "kickoff", home, "conv")

    assert process.returncode == 0
    payload = json.loads(process.stdout)
    assert Path(payload["topic_convention"]) == target / "README.md", (
        "LOCAL-CONVENTIONS: convention path must derive from topic's resolved topics_dir"
    )


def test_topic_convention_absent_is_silent_in_both_modes(tmp_path):
    for names in (("kickoff",), ("topic", "kickoff")):
        root = tmp_path / "-".join(names)
        root.mkdir()
        copied_skills = _copy_skills(root, *names)
        home = root / "home"
        home.mkdir()

        process = _run(copied_skills / "kickoff", home)

        assert process.returncode == 0
        payload = json.loads(process.stdout)
        assert Path(payload["topic_convention"]) == (
            home / ".claude" / "topics" / "README.md"
        )
        assert "README.md" not in process.stderr, (
            "LOCAL-CONVENTIONS: an absent convention file must not produce a notice"
        )


# Private project and person names are not listed here: they come from the
# NO_PERSON_DENY expression below, so no tracked file spells them.
PRIVATE_VOCABULARY = (
    r"\bCHART\b",
    r"(?i:\bchart-wave\b)",
    r"(?i:\bwaves?\b)",
    r"\bSoT\b",
    r"\bROLL\b",
    r"contract: v3",
    r"\bAstra\b",
    r"(?i:\bnotion\b)",
    r"\borchestrate\b",
)


def _deny_grep(expr, data):
    """Match with the same grep flags the public-vocabulary gate uses."""
    return subprocess.run(
        ["grep", "-a", "-i", "-n", "-E", "-e", expr],
        input=data,
        capture_output=True,
    )


def test_shipped_public_files_name_no_private_vocabulary():
    # The shipped files are the playbook and the resolver. This test file is
    # excluded because it has to spell the vocabulary it rejects.
    # Personal names and hosts come from the shared deny expression, never from
    # this file. The deny expression is matched with grep -E so it means what
    # the public-vocabulary gate means.
    patterns = list(PRIVATE_VOCABULARY)
    deny = os.environ.get("NO_PERSON_DENY", "").strip()
    if deny:
        probe = _deny_grep(deny, b"")
        if probe.returncode == 2:
            detail = probe.stderr.decode("utf-8", "replace").strip()
            raise AssertionError(
                f"NO_PERSON_DENY is not a valid extended regular expression: {detail}"
            )
    hits = []
    for name in ("SKILL.md", "paths.py"):
        text = (SKILL_DIR / name).read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            for pattern in patterns:
                if re.search(pattern, line):
                    hits.append(f"{name}:{number}: {pattern}: {line.strip()}")
        if deny:
            result = _deny_grep(deny, text.encode("utf-8"))
            assert result.returncode in (0, 1), result.stderr.decode("utf-8", "replace")
            for raw_line in result.stdout.decode("utf-8", "replace").splitlines():
                lineno, _, line = raw_line.partition(":")
                hits.append(f"{name}:{lineno}: NO_PERSON_DENY: {line.strip()}")
    assert not hits, "PUBLIC-TEXT: private vocabulary in shipped kickoff files:\n" + "\n".join(hits)


def test_accepted_decision_followups_are_recorded_without_a_convention_file():
    skill = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
    assert (
        "the follow-up work it implies is written down before the session ends, "
        "in the same session, in a durable place"
    ) in skill, "FOLLOW-UPS: the same-session durable-record rule must be in the shipped text"
    assert "This holds for every user, with or without a convention file." in skill, (
        "FOLLOW-UPS: the rule must not depend on the optional convention file"
    )
    assert "but not whether they are recorded" in skill, (
        "FOLLOW-UPS: a convention may change the destination, never waive recording"
    )
    assert "## Follow-ups" in skill, (
        "FOLLOW-UPS: the write-up shape must carry a default Follow-ups section"
    )


def test_missing_reference_prose_exempts_the_silent_convention():
    skill = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
    assert (
        "Each missing optional reference except the topic convention is reported "
        "on stderr in one line; an absent topic convention is silent."
    ) in skill, "NOTICES: the playbook must exempt the topic convention from the notice sentence"


def test_kickoff_owns_no_config_file():
    assert not (SKILL_DIR / "config.json").exists(), (
        "CONFIG-OWNERSHIP: kickoff must own no path config keys"
    )


def _write_older_model_notes(home: Path) -> Path:
    standards = home / ".claude" / "standards"
    standards.mkdir(parents=True)
    legacy = standards / "model-routing.md"
    legacy.write_text("older file name\n", encoding="utf-8")
    return legacy


def test_older_model_notes_file_name_is_still_read_with_topic(tmp_path):
    copied_skills = _copy_skills(tmp_path, "topic", "kickoff")
    home = tmp_path / "home"
    legacy = _write_older_model_notes(home)

    process = _run(copied_skills / "kickoff", home)

    assert process.returncode == 0, process.stderr
    assert Path(json.loads(process.stdout)["model_notes"]) == legacy
    assert "model notes" not in process.stderr


def test_older_model_notes_file_name_is_still_read_without_topic(tmp_path):
    copied_skills = _copy_skills(tmp_path, "kickoff")
    home = tmp_path / "home"
    legacy = _write_older_model_notes(home)

    process = _run(copied_skills / "kickoff", home)

    assert process.returncode == 0, process.stderr
    assert Path(json.loads(process.stdout)["model_notes"]) == legacy
    assert "model notes" not in process.stderr

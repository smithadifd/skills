"""topics_dir suite — config-read boundary, plus the unset-default and
optional-read seams.

Seams under test:

  S1  topics-dir resolution   `resolve(config_path) -> Path`. THE
      observable: the topics directory File-resolution step 2/3 points at.
      Adapter: `topics_dir` present (override wins, `~`-expanded) vs
      absent/null/unreadable (unset default `~/.claude/topics`).
  S2  shipped config          resolving this checkout's `config.json`
      reproduces today's topics directory. Not the unset default.
  S3  optional normative read `read_optional` on a missing file returns a
      one-line notice and does not raise.
  S4  clean HOME              `HARNESS_ROOT` unset, `HOME` a fresh temp
      directory, no topics key: the write lands under `~/.claude/topics`,
      absent reads degrade with the one-line notice, and no path component
      is an unrelated workspace directory name.

Not tautological: expected directories are literal strings in this file,
never `topics_dir.DEFAULT_TOPICS_DIR` compared to itself.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "test_topic_topics_dir", SKILL_DIR / "topics_dir.py"
)
topics_dir = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(topics_dir)

UNSET = Path("~/.claude/topics").expanduser()
NOTICE_BODY = "is absent — continuing with the built-in default stated in SKILL.md"
UNRELATED_DIR = "sample-workspace"


def _notice(path: Path) -> str:
    return f"topic: {path} {NOTICE_BODY}"


def test_unset_default_when_key_absent(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({}), encoding="utf-8")

    assert topics_dir.resolve(config_file) == UNSET


def test_unset_default_when_key_is_null(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"topics_dir": None}), encoding="utf-8")

    assert topics_dir.resolve(config_file) == UNSET


def test_unset_default_when_config_file_is_missing(tmp_path):
    missing = tmp_path / "does-not-exist.json"

    assert topics_dir.resolve(missing) == UNSET


def test_override_is_honoured(tmp_path):
    override = tmp_path / "elsewhere" / "topics"
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"topics_dir": str(override)}), encoding="utf-8")

    resolved = topics_dir.resolve(config_file)

    assert resolved == override
    assert resolved != UNSET


def test_per_skill_override_is_honoured(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target = tmp_path / "override-topics"
    override = home / ".claude" / "skill-config" / "topic.json"
    override.parent.mkdir(parents=True)
    override.write_text(json.dumps({"topics_dir": str(target)}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    assert topics_dir.resolve() == target


def test_explicit_config_beats_per_skill_override(tmp_path, monkeypatch):
    home = tmp_path / "home"
    override_target = tmp_path / "override-topics"
    explicit_target = tmp_path / "explicit-topics"
    override = home / ".claude" / "skill-config" / "topic.json"
    override.parent.mkdir(parents=True)
    override.write_text(
        json.dumps({"topics_dir": str(override_target)}), encoding="utf-8"
    )
    explicit = tmp_path / "explicit.json"
    explicit.write_text(
        json.dumps({"topics_dir": str(explicit_target)}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))

    assert topics_dir.resolve(explicit) == explicit_target


@pytest.mark.parametrize(
    "contents",
    [
        "{broken",
        "[]",
        json.dumps({"topics_dir": 7}),
    ],
)
def test_invalid_per_skill_override_degrades_once(
    tmp_path, monkeypatch, capsys, contents
):
    home = tmp_path / "home"
    override = home / ".claude" / "skill-config" / "topic.json"
    override.parent.mkdir(parents=True)
    override.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    assert topics_dir.resolve() == home / ".claude" / "topics"
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1, (
        f"invalid topic override must emit exactly one degradation line; got {lines!r}"
    )
    assert str(override) in lines[0], "degradation line must name the override path"


def test_unset_default_when_key_is_empty_string(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"topics_dir": ""}), encoding="utf-8")

    assert topics_dir.resolve(config_file) == UNSET


def test_unset_default_when_config_is_malformed_json(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text("{not valid json", encoding="utf-8")

    assert topics_dir.resolve(config_file) == UNSET


def test_unset_default_when_config_file_is_unreadable(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"topics_dir": str(tmp_path / "should-not-be-read")}),
        encoding="utf-8",
    )
    config_file.chmod(0)
    try:
        assert not os.access(config_file, os.R_OK)
        assert topics_dir.resolve(config_file) == UNSET
    finally:
        config_file.chmod(0o644)


def test_unset_default_when_key_is_a_bool(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"topics_dir": True}), encoding="utf-8")

    assert topics_dir.resolve(config_file) == UNSET


def test_unset_default_when_key_is_a_list(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"topics_dir": ["not", "a", "path"]}), encoding="utf-8")

    assert topics_dir.resolve(config_file) == UNSET


def test_unset_default_when_key_is_an_object(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"topics_dir": {"nested": "object"}}), encoding="utf-8")

    assert topics_dir.resolve(config_file) == UNSET


def test_unset_write_default_is_under_claude_not_unrelated_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    resolved = topics_dir.resolve(tmp_path / "missing.json")
    assert resolved == tmp_path / ".claude" / "topics", (
        "unset write default must live under ~/.claude/topics"
    )
    assert UNRELATED_DIR not in resolved.parts, (
        "unset write default must live under ~/.claude/topics"
    )


def test_shipped_config_uses_standalone_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = topics_dir.resolve(SKILL_DIR / "config.json")
    assert resolved == tmp_path / ".claude" / "topics", (
        "shipped config must use the standalone default"
    )


def test_shipped_config_normative_paths_follow_standalone_default(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    topics = topics_dir.resolve(SKILL_DIR / "config.json")
    paths = topics_dir.normative_paths(topics)
    assert paths["convention"] == topics / "README.md"
    assert paths["template"] == topics / "TEMPLATE.md"
    assert paths["model_routing"] == (
        tmp_path / ".claude" / "standards" / "model-routing.md"
    )


def test_standalone_install_paths_are_documented():
    installed_path = "${CLAUDE_SKILL_DIR}/topics_dir.py"
    for filename in ("SKILL.md", "topics_dir.py", "config.json"):
        text = (SKILL_DIR / filename).read_text(encoding="utf-8")
        assert re.findall(r"~/(?!\.claude)\S+", text) == [], (
            "only ~/.claude paths may appear"
        )
        if filename == "topics_dir.py":
            # argparse help does not substitute the variable; the script
            # documents plain invocations and prints its own resolved path.
            assert "${CLAUDE_SKILL_DIR}" not in text
        else:
            assert installed_path in text
    resolver_text = (SKILL_DIR / "topics_dir.py").read_text(encoding="utf-8")
    assert "This standalone config leaves the key unset" in resolver_text
    assert "This checkout's config sets the key" not in resolver_text


def test_cli_help_shows_the_resolved_script_path_not_a_variable():
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, str(SKILL_DIR / "topics_dir.py"), "--help"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "${CLAUDE_SKILL_DIR}" not in out
    assert str((SKILL_DIR / "topics_dir.py").resolve()) in out


def test_absent_normative_read_degrades_with_one_line_notice(tmp_path):
    missing = tmp_path / "README.md"
    try:
        text, notice = topics_dir.read_optional(missing)
    except Exception as exc:
        pytest.fail(
            "absent normative read must degrade with one-line notice, not raise: "
            f"{type(exc).__name__}: {exc}"
        )
    assert text is None, (
        "absent normative read must degrade with one-line notice, not raise"
    )
    assert notice == _notice(missing), (
        "absent normative read must degrade with one-line notice, not raise"
    )
    assert "\n" not in notice


def test_present_normative_read_returns_text(tmp_path):
    present = tmp_path / "README.md"
    present.write_text("convention body\n", encoding="utf-8")
    text, notice = topics_dir.read_optional(present)
    assert text == "convention body\n"
    assert notice is None


def test_open_uses_builtin_skeleton_when_template_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    result = topics_dir.open_topic("demo", "the ask in the asker's words", config, today="2026-09-28")
    assert result.path == tmp_path / ".claude" / "topics" / "demo.md"
    body = result.path.read_text(encoding="utf-8")
    assert "the ask in the asker's words" in body
    assert "Status: OPEN" in body
    assert "## Promote-or-park" in body
    assert len(result.notices) == 3
    for notice in result.notices:
        assert notice.endswith(NOTICE_BODY)
        assert UNRELATED_DIR not in Path(notice.split(" ", 1)[-1]).parts


def test_open_does_not_overwrite_existing_topic(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    dest_dir = tmp_path / ".claude" / "topics"
    dest_dir.mkdir(parents=True)
    existing = dest_dir / "demo.md"
    existing.write_text("keep me\n", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    result = topics_dir.open_topic("demo", "new ask", config)
    assert result.path.read_text(encoding="utf-8") == "keep me\n"


def test_present_template_is_copied(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    topics = tmp_path / ".claude" / "topics"
    topics.mkdir(parents=True)
    (topics / "TEMPLATE.md").write_text("# from-template\n", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    result = topics_dir.open_topic("demo", "the ask", config)
    body = result.path.read_text(encoding="utf-8")
    assert body.startswith("# from-template\n")
    assert "the ask" in body
    assert all("TEMPLATE.md" not in notice for notice in result.notices)
    assert any(notice.endswith(NOTICE_BODY) and "README.md" in notice for notice in result.notices)


def test_skill_states_the_absent_notice_and_builtin_defaults():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "topic: <path> is absent — continuing with the built-in default stated in SKILL.md" in text
    for heading in (
        "## Ask",
        "## External refs",
        "## Current state",
        "## Staged",
        "## Log",
        "## Promote-or-park",
        "## Close tier",
    ):
        assert heading in text
    assert "session's own model" in text
    assert "promote-or-park ledger" in text
    assert "No third state." in text
    assert "marked as waiting on them" in text


def test_resolves_without_shared_lib(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    isolated = tmp_path / "iso" / "topic"
    isolated.mkdir(parents=True)
    shutil.copy(SKILL_DIR / "topics_dir.py", isolated / "topics_dir.py")
    assert not (isolated.parent / "_lib" / "config_path.py").is_file()
    spec = importlib.util.spec_from_file_location(
        "topic_topics_dir_without_lib", isolated / "topics_dir.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resolved = module.resolve(tmp_path / "missing.json")
    assert resolved == tmp_path / ".claude" / "topics", (
        "unset write default must live under ~/.claude/topics"
    )
    text, notice = module.read_optional(tmp_path / "README.md")
    assert text is None
    assert notice == _notice(tmp_path / "README.md"), (
        "absent normative read must degrade with one-line notice, not raise"
    )


def test_clean_home_writes_under_claude_and_degrades_absent_reads():
    home = Path(subprocess.check_output(["mktemp", "-d"], text=True).strip())
    try:
        env = os.environ.copy()
        env.pop("HARNESS_ROOT", None)
        env["HOME"] = str(home)
        missing = home / "no-config.json"
        proc = subprocess.run(
            [
                sys.executable,
                str(SKILL_DIR / "topics_dir.py"),
                "--config",
                str(missing),
                "--open",
                "demo",
                "--ask",
                "the ask",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        written = Path(proc.stdout.strip())
        assert written == home / ".claude" / "topics" / "demo.md", (
            "unset write default must live under ~/.claude/topics"
        )
        assert written.is_file()
        assert "the ask" in written.read_text(encoding="utf-8")
        notices = [line for line in proc.stderr.splitlines() if line.startswith("topic:")]
        assert len(notices) == 3, (
            "absent normative read must degrade with one-line notice, not raise"
        )
        for line in notices:
            assert line.endswith(NOTICE_BODY), (
                "absent normative read must degrade with one-line notice, not raise"
            )
            assert "\n" not in line
        blob = proc.stdout + proc.stderr
        assert UNRELATED_DIR not in blob.split(os.sep)
        assert not (home / UNRELATED_DIR).exists()
        for path in home.rglob("*"):
            assert UNRELATED_DIR not in path.parts, (
                "clean HOME must not touch a path under an unrelated workspace directory"
            )
    finally:
        shutil.rmtree(home)


def _ask_section(body: str):
    lines = body.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "## Ask":
            start = index + 1
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].lstrip().startswith("#"):
            end = index
            break
    return "\n".join(lines[start:end])


def _tree(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


def test_ask_that_is_a_template_substring_is_still_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    topics = tmp_path / ".claude" / "topics"
    topics.mkdir(parents=True)
    (topics / "TEMPLATE.md").write_text(
        "# <topic title>\n\nStatus: OPEN\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    result = topics_dir.open_topic("demo", "OPEN", config)
    section = _ask_section(result.path.read_text(encoding="utf-8"))
    assert section is not None and "OPEN" in section, (
        "an ask that is a substring of the template must still be recorded "
        "under ## Ask"
    )


@pytest.mark.parametrize(
    "slug",
    ["../escaped", "a/b", "..", "../../../../etc/evil"],
)
def test_open_rejects_a_slug_that_leaves_the_topics_dir(slug):
    home = Path(subprocess.check_output(["mktemp", "-d"], text=True).strip())
    try:
        # Nested so a no-op guard still cannot write outside this HOME.
        topics = home / "a" / "b" / "c" / "d" / "e" / "topics"
        assert home in topics.parents
        config = home / "config.json"
        config.write_text(
            json.dumps({"topics_dir": str(topics)}),
            encoding="utf-8",
        )
        before = _tree(home)
        env = os.environ.copy()
        env.pop("HARNESS_ROOT", None)
        env["HOME"] = str(home)
        proc = subprocess.run(
            [
                sys.executable,
                str(SKILL_DIR / "topics_dir.py"),
                "--config",
                str(config),
                "--open",
                slug,
                "--ask",
                "the ask",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        created = sorted(_tree(home) - before)
        assert proc.returncode != 0, (
            f"slug {slug!r} that leaves the topics dir must be rejected; "
            "--open must exit nonzero"
        )
        assert created == [], (
            f"slug {slug!r} that leaves the topics dir must not create a file "
            f"under HOME; created {created}"
        )
    finally:
        shutil.rmtree(home)


def test_accepted_decision_followups_are_promoted_never_parked():
    text = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
    assert (
        "Every follow-up of an accepted decision" in text
        and "is always promoted in the same session to a durable tracked surface, "
        "never parked" in text
    ), "accepted-decision follow-ups must be promoted in the same session, never parked"
    assert "cannot turn this into a park" in text, (
        "the convention document may pick the surface but not allow parking"
    )

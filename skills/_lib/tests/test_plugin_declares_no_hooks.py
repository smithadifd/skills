"""The plugin declares no hooks: the ledger brief at session start comes from the ledger's own plugin.

The ledger repository ships the plugin that prints the brief and records prompts
and question-picker answers. This plugin used to carry a `SessionStart` hook that
printed the same brief, so a person who installed both saw it twice. The hook is
gone, and these checks keep it gone:

- no `hooks` path of any kind at the plugin root: denied by existence, so a
  regular file, a directory (empty or not), a symlink (even a dangling one) and
  a gitlink (which checks out as an empty directory) are each one problem;
- no `hooks` key in the plugin manifest or in a marketplace plugin entry, the
  other place Claude Code reads hook declarations from;
- no text outside the changelog that says this plugin carries a session-start
  hook, a `SessionStart` entry or the `path_rule` option that only the hook used.

Each check is a function of a plugin root, so a test can plant the fault in a
scratch root and assert the named problem. The real repository is then checked
with the same functions.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
THIS_FILE = Path(__file__).resolve()
# These two files drive the LEDGER plugin's SessionStart hook (not this plugin's removed one).
# Only the lines that do so are exempt: each path maps to the one regex a stale
# line must also match, so any other stale line in these files is still a hit.
LEDGER_HOOK_LINES = {
    "ci/check.sh": re.compile(r"ledger_e2e_(run_hook|fail) session-start\b"),
    "ci/fixtures/ledger-e2e-session-start.json": re.compile(r'^\s*"hook_event_name": "SessionStart",?$'),
}

MANIFESTS = (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json")
TEXT_SUFFIXES = {".md", ".py", ".json", ".sh", ".txt", ".yml", ".yaml", ".awk"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}
STALE_TEXT = re.compile(
    r"session[- ]start hook|SessionStart|session-start\.py|\bpath_rule\b", re.IGNORECASE
)


def hook_problems(root: Path) -> list[str]:
    """Every way `root` declares a plugin hook, each as one named problem line."""
    problems = []
    # Deny by existence, whatever the type: lexists so a dangling symlink counts, and no
    # walk over descendants so an empty directory (a checked-out gitlink) counts too.
    if os.path.lexists(root / "hooks"):
        problems.append("hooks exists at the plugin root (a plugin hook path)")
    for rel in MANIFESTS:
        path = root / rel
        if not path.is_file():
            problems.append(f"{rel} is missing")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            problems.append(f"{rel} is not valid JSON")
            continue
        if not isinstance(data, dict):
            problems.append(f"{rel} is not a JSON object")
            continue
        if "hooks" in data:
            problems.append(f"{rel} declares hooks")
        entries = data.get("plugins", [])
        if not isinstance(entries, list):
            problems.append(f"{rel} plugins is not a list")
            continue
        for entry in entries:
            if isinstance(entry, dict) and "hooks" in entry:
                problems.append(f"{rel} plugin {entry.get('name', '?')} declares hooks")
    return problems


def stale_text_hits(root: Path, exempt_lines: dict | None = None) -> list[str]:
    """Lines outside the changelog that name the removed hook, as `path:line`.

    `exempt_lines` maps a root-relative path to a regex: a stale line in that
    file is skipped only when it also matches the regex. This file is exempt whole.
    """
    exempt_lines = exempt_lines or {}
    hits = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or SKIP_DIRS & set(rel.parts):
            continue
        if path.suffix not in TEXT_SUFFIXES or rel.as_posix() == "CHANGELOG.md" or path == THIS_FILE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        exempt = exempt_lines.get(rel.as_posix())
        for number, line in enumerate(text.splitlines(), 1):
            if STALE_TEXT.search(line) and not (exempt and exempt.search(line)):
                hits.append(f"{rel.as_posix()}:{number}")
    return hits


def _plugin_root(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "m", "plugins": [{"name": "x", "source": "./"}]}), encoding="utf-8"
    )
    return root


def test_the_plugin_declares_no_hooks():
    assert hook_problems(REPO_ROOT) == []


def test_the_session_start_files_are_gone():
    assert not (REPO_ROOT / "hooks" / "hooks.json").exists()
    assert not (REPO_ROOT / "hooks" / "session-start.py").exists()


def test_no_text_says_the_plugin_prints_the_brief_at_session_start():
    assert stale_text_hits(REPO_ROOT, LEDGER_HOOK_LINES) == []


def test_a_plugin_with_no_hooks_has_no_problems(tmp_path):
    assert hook_problems(_plugin_root(tmp_path)) == []


def test_a_session_start_entry_in_hooks_json_is_a_problem(tmp_path):
    root = _plugin_root(tmp_path)
    (root / "hooks").mkdir()
    entry = {"hooks": [{"type": "command", "command": "true"}]}
    (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [entry]}}), encoding="utf-8")
    assert hook_problems(root) == ["hooks exists at the plugin root (a plugin hook path)"]


def test_a_dangling_hooks_symlink_is_a_problem(tmp_path):
    root = _plugin_root(tmp_path)
    (root / "hooks").symlink_to(tmp_path / "nowhere")
    assert not (root / "hooks").exists()
    assert hook_problems(root) == ["hooks exists at the plugin root (a plugin hook path)"]


def test_a_hooks_symlink_to_a_directory_is_a_problem(tmp_path):
    root = _plugin_root(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    (root / "hooks").symlink_to(tmp_path / "elsewhere")
    assert hook_problems(root) == ["hooks exists at the plugin root (a plugin hook path)"]


def test_a_hooks_path_that_is_a_plain_file_is_a_problem(tmp_path):
    root = _plugin_root(tmp_path)
    (root / "hooks").write_text("not a directory\n", encoding="utf-8")
    assert hook_problems(root) == ["hooks exists at the plugin root (a plugin hook path)"]


def test_an_empty_hooks_directory_is_a_problem(tmp_path):
    root = _plugin_root(tmp_path)
    (root / "hooks").mkdir()
    assert list((root / "hooks").iterdir()) == []
    assert hook_problems(root) == ["hooks exists at the plugin root (a plugin hook path)"]


def _git(repo: Path, *args: str) -> None:
    # Identity on the command line, never written to any config; the repo is one the test made.
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def test_a_committed_gitlink_named_hooks_is_a_problem(tmp_path):
    source = _plugin_root(tmp_path / "src")
    _git(source, "init", "-q")
    _git(source, "add", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json")
    _git(source, "commit", "-q", "-m", "base")
    _git(source, "update-index", "--add", "--cacheinfo", "160000," + "ab" * 20 + ",hooks")
    _git(source, "commit", "-q", "-m", "gitlink")
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", str(source), str(checkout)], check=True, capture_output=True)
    # A real clone holds the gitlink as an empty directory: no file, no symlink under it.
    assert (checkout / "hooks").is_dir()
    assert list((checkout / "hooks").iterdir()) == []
    assert hook_problems(checkout) == ["hooks exists at the plugin root (a plugin hook path)"]


def test_a_dangling_symlink_inside_hooks_is_a_problem(tmp_path):
    root = _plugin_root(tmp_path)
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").symlink_to(tmp_path / "nowhere")
    assert hook_problems(root) == ["hooks exists at the plugin root (a plugin hook path)"]


def test_a_session_start_script_is_a_problem(tmp_path):
    root = _plugin_root(tmp_path)
    (root / "hooks").mkdir()
    (root / "hooks" / "session-start.py").write_text("print('brief')\n", encoding="utf-8")
    assert hook_problems(root) == ["hooks exists at the plugin root (a plugin hook path)"]


def test_hooks_declared_in_the_plugin_manifest_are_a_problem(tmp_path):
    root = _plugin_root(tmp_path)
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.write_text(json.dumps({"name": "x", "hooks": "./h.json"}), encoding="utf-8")
    assert hook_problems(root) == [".claude-plugin/plugin.json declares hooks"]


def test_hooks_declared_in_a_marketplace_entry_are_a_problem(tmp_path):
    root = _plugin_root(tmp_path)
    market = root / ".claude-plugin" / "marketplace.json"
    market.write_text(
        json.dumps({"name": "m", "plugins": [{"name": "x", "source": "./", "hooks": {}}]}), encoding="utf-8"
    )
    assert hook_problems(root) == [".claude-plugin/marketplace.json plugin x declares hooks"]


@pytest.mark.parametrize("rel", MANIFESTS)
def test_a_missing_manifest_is_a_problem_not_a_pass(tmp_path, rel):
    root = _plugin_root(tmp_path)
    (root / rel).unlink()
    assert hook_problems(root) == [f"{rel} is missing"]


@pytest.mark.parametrize("rel", MANIFESTS)
def test_a_manifest_that_is_not_json_is_a_problem(tmp_path, rel):
    root = _plugin_root(tmp_path)
    (root / rel).write_text("{not json", encoding="utf-8")
    assert hook_problems(root) == [f"{rel} is not valid JSON"]


@pytest.mark.parametrize(
    "line",
    [
        "The plugin's session-start hook prints the brief.",
        "A Session Start hook runs `resume --brief`.",
        "The plugin carries a `SessionStart` hook.",
        "python3 hooks/session-start.py",
        "decide(path_rule=False)",
    ],
)
def test_text_naming_the_removed_hook_is_found_outside_the_changelog(tmp_path, line):
    root = _plugin_root(tmp_path)
    (root / "README.md").write_text(f"intro\n{line}\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text(f"{line}\n", encoding="utf-8")
    assert stale_text_hits(root) == ["README.md:2"]


def _exempt_file_root(tmp_path: Path, rel: str, lines: list[str]) -> Path:
    root = _plugin_root(tmp_path)
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def test_the_ledger_hook_lines_themselves_are_exempt(tmp_path):
    root = _exempt_file_root(
        tmp_path,
        "ci/check.sh",
        [
            "out=$(ledger_e2e_run_hook session-start SessionStart '' \"$f_session\")",
            "ledger_e2e_fail session-start 'the SessionStart hook did not print the ledger brief'",
        ],
    )
    assert stale_text_hits(root, LEDGER_HOOK_LINES) == []
    assert stale_text_hits(root) == ["ci/check.sh:1", "ci/check.sh:2"]


def test_a_stale_line_planted_elsewhere_in_an_exempt_script_is_a_hit(tmp_path):
    root = _exempt_file_root(
        tmp_path,
        "ci/check.sh",
        [
            "ledger_e2e_fail session-start 'the SessionStart hook did not print the ledger brief'",
            "# The plugin's session-start hook prints the brief.",
        ],
    )
    assert stale_text_hits(root, LEDGER_HOOK_LINES) == ["ci/check.sh:2"]


def test_a_stale_line_planted_elsewhere_in_the_exempt_fixture_is_a_hit(tmp_path):
    root = _exempt_file_root(
        tmp_path,
        "ci/fixtures/ledger-e2e-session-start.json",
        [
            "{",
            '  "hook_event_name": "SessionStart",',
            '  "note": "SessionStart"',
            "}",
        ],
    )
    assert stale_text_hits(root, LEDGER_HOOK_LINES) == ["ci/fixtures/ledger-e2e-session-start.json:3"]


def test_an_exempt_regex_applies_only_to_its_own_file(tmp_path):
    root = _exempt_file_root(
        tmp_path,
        "ci/other.sh",
        ["ledger_e2e_fail session-start 'the SessionStart hook did not print the ledger brief'"],
    )
    assert stale_text_hits(root, LEDGER_HOOK_LINES) == ["ci/other.sh:1"]


def test_text_that_does_not_name_the_removed_hook_passes(tmp_path):
    root = _plugin_root(tmp_path)
    (root / "README.md").write_text("The ledger's plugin prints the brief at the start of a session.\n", encoding="utf-8")
    assert stale_text_hits(root) == []

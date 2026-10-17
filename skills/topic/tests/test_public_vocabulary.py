"""The shipped files of the topic skill read as session-note tooling.

They must not carry workflow vocabulary from a private setup, personal names
or fixture directories. Local conventions belong in the optional
`<topics_dir>/README.md`, which the skill reads only when present.

Two checks run. The tracked patterns below are person-neutral and always run.
The deny list (names, hosts, employers) is never committed: it comes from the
`NO_PERSON_DENY` environment variable, comma- or newline-separated, and is
matched case-insensitively against every file in the repository except
`LICENSE*`. When the variable is unset only the tracked patterns run.

Allowlist: this file is the only file not scanned, because it has to spell the
forbidden terms out. Nothing else is exempt.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SKILL_DIR.parent.parent
SELF = Path(__file__).resolve()
DENY_ENV = "NO_PERSON_DENY"

# (label, compiled pattern)
FORBIDDEN = [
    ("CHART", re.compile(r"\bCHART\b")),
    ("chart-wave", re.compile(r"chart-wave", re.I)),
    ("wave", re.compile(r"\bwaves?\b", re.I)),
    ("SoT", re.compile(r"\bSoT\b")),
    ("ROLL", re.compile(r"\bROLL\b")),
    ("contract: v3", re.compile(r"contract:\s*v3", re.I)),
    ("off-queue", re.compile(r"off-queue", re.I)),
    ("seat", re.compile(r"\bseats?\b", re.I)),
]


def _shipped_files():
    return sorted(
        p
        for p in SKILL_DIR.rglob("*")
        if p.is_file()
        and p.resolve() != SELF
        and "__pycache__" not in p.parts
        and ".pytest_cache" not in p.parts
        and p.suffix in {".md", ".py", ".json", ".txt", ".sh"}
    )


def test_shipped_files_are_discovered():
    names = {p.name for p in _shipped_files()}
    assert {"SKILL.md", "topics_dir.py", "config.json", "test_topics_dir.py"} <= names


def test_shipped_files_carry_no_private_vocabulary():
    hits = []
    for path in _shipped_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for label, pattern in FORBIDDEN:
                if pattern.search(line):
                    hits.append(f"{path.relative_to(SKILL_DIR)}:{lineno} [{label}] {line.strip()}")
    assert not hits, "private vocabulary in shipped topic files:\n" + "\n".join(hits)


def deny_patterns(raw):
    """Compile a comma- or newline-separated deny list into (term, pattern) pairs."""
    terms = [t.strip() for t in re.split(r"[,\n]", raw or "") if t.strip()]
    return [(t, re.compile(re.escape(t), re.I)) for t in terms]


def scan_tree(root, patterns):
    hits = []
    for path in sorted(root.rglob("*")):
        if (
            not path.is_file()
            or path.resolve() == SELF
            or ".git" in path.relative_to(root).parts
            or "__pycache__" in path.parts
            or ".pytest_cache" in path.parts
            or path.name.startswith("LICENSE")
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pattern in patterns:
                if pattern.search(line):
                    hits.append(f"{path.relative_to(root)}:{lineno} [{label}]")
    return hits


def test_deny_list_parses_commas_and_newlines():
    labels = [t for t, _ in deny_patterns("alpha, beta\ngamma,,\n  \n")]
    assert labels == ["alpha", "beta", "gamma"]
    assert deny_patterns(None) == [] and deny_patterns("") == []


def test_scan_finds_planted_term_case_insensitively(tmp_path, monkeypatch):
    (tmp_path / "doc.md").write_text("clean\nhas a Zzsynthetic-Term here\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("zzsynthetic-term\n", encoding="utf-8")
    monkeypatch.setenv(DENY_ENV, "other\nzzsynthetic-term")
    hits = scan_tree(tmp_path, deny_patterns(os.environ[DENY_ENV]))
    assert hits == ["doc.md:2 [zzsynthetic-term]"]


def test_shipped_text_has_no_denied_terms():
    hits = scan_tree(REPO_ROOT, deny_patterns(os.environ.get(DENY_ENV)))
    assert not hits, "denied terms in shipped text:\n" + "\n".join(hits)


def _run_deny_test(env_value):
    env = {k: v for k, v in os.environ.items() if k not in (DENY_ENV, "PYTEST_ADDOPTS")}
    if env_value is not None:
        env[DENY_ENV] = env_value
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(SELF), "-q", "-k", "test_shipped_text_has_no_denied_terms"],
        env=env,
        capture_output=True,
        text=True,
    )


def test_deny_test_fails_when_env_names_a_present_term():
    # "memory-gardener" is a directory name present in the shipped tree.
    result = _run_deny_test("zz-absent-term,memory-gardener")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "denied terms in shipped text" in result.stdout


def test_deny_test_runs_and_passes_when_env_unset():
    result = _run_deny_test(None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout

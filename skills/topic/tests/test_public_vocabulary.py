"""The shipped files of the topic skill read as session-note tooling.

They must not carry workflow vocabulary from a private setup, personal names
or fixture directories. Local conventions belong in the optional
`<topics_dir>/README.md`, which the skill reads only when present.

Two checks run. The tracked patterns below are person-neutral and always run.
The deny list (names, hosts, employers) is never committed: it comes from the
`NO_PERSON_DENY` environment variable, a grep-compatible alternation
(`term1|term2|...`) matched case-insensitively against every tracked file
except `LICENSE*`. A line that carries the repository's own `owner/repo` slug
is not a hit. An unset or empty variable denies nothing; an invalid expression
fails the test. This file spells no denied term: the tests plant synthetic
ones at run time.
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
# Synthetic term, built at run time so this file never spells it whole.
PLANT_S = "zz" + "planted"
PLANT = PLANT_S.encode()

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


def deny_pattern(raw):
    """Compile the deny expression; None when nothing is denied."""
    if not raw or not raw.strip():
        return None
    try:
        return re.compile(raw, re.I)
    except re.error as exc:
        raise AssertionError(f"{DENY_ENV} is not a valid regular expression: {exc}") from exc


def own_slug(root):
    """`owner/repo` from the origin remote, or None when there is no remote."""
    result = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    match = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", result.stdout.strip())
    return match.group(1) if match else None


def tracked_files(root):
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    names = [Path(n.decode("utf-8", "surrogateescape")) for n in out.split(b"\0") if n]
    return [n for n in names if not n.name.startswith("LICENSE")]


def decode_for_scan(data):
    """Text for matching. UTF-16 files (BOM) are decoded as such; anything else
    that is not UTF-8 falls back to latin-1, which never fails. No file is skipped."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def scan_tree(root, pattern, slug=None):
    """`path:line` hits over the tracked files of the repository at `root`."""
    if pattern is None:
        return []
    hits = []
    for rel in tracked_files(root):
        path = root / rel
        if not path.is_file():
            continue
        for lineno, line in enumerate(decode_for_scan(path.read_bytes()).splitlines(), 1):
            if slug and slug.lower() in line.lower():
                continue
            if pattern.search(line):
                hits.append(f"{rel}:{lineno}")
    return hits


def _scratch_repo(tmp_path, files):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, data in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def test_unset_and_empty_deny_expression_deny_nothing():
    assert deny_pattern(None) is None and deny_pattern("") is None and deny_pattern("  ") is None


def test_invalid_deny_expression_fails_naming_the_variable():
    try:
        deny_pattern("good|(unclosed")
    except AssertionError as exc:
        assert DENY_ENV in str(exc)
    else:
        raise AssertionError("an invalid expression must fail")


def test_scan_finds_planted_term_case_insensitively(tmp_path):
    _scratch_repo(
        tmp_path,
        {
            "doc.md": b"clean\nhas a Zzsynthetic-Term here\n",
            "LICENSE": b"zzsynthetic-term\n",
            "LICENSE.md": b"zzsynthetic-term\n",
        },
    )
    assert scan_tree(tmp_path, deny_pattern("other|zzsynthetic-term")) == ["doc.md:2"]


def test_scan_reads_the_alternation_form(tmp_path):
    _scratch_repo(tmp_path, {"a.md": b"zzone\n", "b.md": b"zztwo\n", "c.md": b"zzthree\n"})
    assert scan_tree(tmp_path, deny_pattern("zzone|zztwo")) == ["a.md:1", "b.md:1"]


def test_scan_ignores_untracked_files(tmp_path):
    _scratch_repo(tmp_path, {"a.md": b"clean\n"})
    (tmp_path / "untracked.md").write_text(PLANT_S + "\n", encoding="utf-8")
    assert scan_tree(tmp_path, deny_pattern(PLANT_S)) == []


def test_scan_covers_tracked_test_files(tmp_path):
    _scratch_repo(tmp_path, {"skills/x/tests/test_x.py": b"# " + PLANT + b"\n"})
    assert scan_tree(tmp_path, deny_pattern(PLANT_S)) == ["skills/x/tests/test_x.py:1"]


def test_scan_allows_the_own_slug_line(tmp_path):
    _scratch_repo(
        tmp_path,
        {"a.json": b'{"$id": "https://github.com/zzowner/zzrepo/x"}\n', "b.md": b"zzowner alone\n"},
    )
    assert scan_tree(tmp_path, deny_pattern("zzowner"), slug="zzowner/zzrepo") == ["b.md:1"]


def test_scan_does_not_skip_files_that_are_not_utf8(tmp_path):
    _scratch_repo(
        tmp_path,
        {
            "utf16.txt": (PLANT_S + "\n").encode("utf-16"),
            "latin.dat": b"\xfd " + PLANT + b" \xe9\n",
        },
    )
    hits = scan_tree(tmp_path, deny_pattern(PLANT_S))
    assert hits == ["latin.dat:1", "utf16.txt:1"]


def test_own_slug_is_read_from_the_remote(tmp_path):
    _scratch_repo(tmp_path, {"a.md": b"x\n"})
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", "https://github.com/zzowner/zzrepo.git"],
        check=True,
    )
    assert own_slug(tmp_path) == "zzowner/zzrepo"


def test_shipped_text_has_no_denied_terms():
    hits = scan_tree(REPO_ROOT, deny_pattern(os.environ.get(DENY_ENV)), own_slug(REPO_ROOT))
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
    result = _run_deny_test("zz-absent-term|memory-gardener")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "denied terms in shipped text" in result.stdout


def test_deny_test_names_the_variable_on_an_invalid_expression():
    result = _run_deny_test("good|(unclosed")
    assert result.returncode == 1, result.stdout + result.stderr
    assert DENY_ENV in result.stdout


def test_deny_test_runs_and_passes_when_env_unset_or_empty():
    for value in (None, ""):
        result = _run_deny_test(value)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout

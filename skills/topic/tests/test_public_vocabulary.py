"""The shipped files of the topic skill read as session-note tooling.

They must not carry workflow vocabulary from a private setup, personal names
or fixture directories. Local conventions belong in the optional
`<topics_dir>/README.md`, which the skill reads only when present.

Allowlist: this file is the only file not scanned, because it has to spell the
forbidden terms out. Nothing else is exempt.
"""

import re
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

# (label, compiled pattern)
FORBIDDEN = [
    ("CHART", re.compile(r"\bCHART\b")),
    ("chart-wave", re.compile(r"chart-wave", re.I)),
    ("wave", re.compile(r"\bwaves?\b", re.I)),
    ("SoT", re.compile(r"\bSoT\b")),
    ("ROLL", re.compile(r"\bROLL\b")),
    ("contract: v3", re.compile(r"contract:\s*v3", re.I)),
    ("foundry", re.compile(r"foundry", re.I)),
    ("claude-toolkit", re.compile(r"claude-toolkit", re.I)),
    ("off-queue", re.compile(r"off-queue", re.I)),
    ("seat", re.compile(r"\bseats?\b", re.I)),
    ("andrew", re.compile(r"\bandrew\b", re.I)),
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

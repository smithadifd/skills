"""The document skill, its agents and its write script agree with each other.

Set DOCUMENT_REPO_ROOT to run the same checks against another copy of the
repository.
"""

import os
import re
from pathlib import Path

REPO_ROOT = Path(os.environ.get(
    "DOCUMENT_REPO_ROOT", str(Path(__file__).resolve().parent.parent.parent.parent)))
SKILL = (REPO_ROOT / "skills" / "document" / "SKILL.md").read_text()
WRITER = (REPO_ROOT / "agents" / "doc-writer.md").read_text()
AGENTS = ("doc-researcher", "doc-writer", "doc-verifier")
ENVELOPE_HEADING = "## Page text"


def test_every_agent_the_skill_names_exists():
    for name in AGENTS:
        assert f"`{name}`" in SKILL, f"SKILL.md no longer names {name}"
        assert (REPO_ROOT / "agents" / f"{name}.md").is_file(), f"agents/{name}.md is missing"


def test_agent_files_carry_their_own_name():
    for name in AGENTS:
        text = (REPO_ROOT / "agents" / f"{name}.md").read_text()
        assert re.search(rf"^name: {name}$", text, re.MULTILINE)


def test_the_envelope_heading_the_skill_parses_is_in_the_writers_return_only_section():
    assert ENVELOPE_HEADING in SKILL
    assert "**Return-only mode**" in WRITER
    return_only_section = WRITER.split("**Return-only mode**", 1)[1]
    assert ENVELOPE_HEADING in return_only_section


def test_the_skill_names_the_write_script_and_it_ships():
    assert "write_page.py" in SKILL
    assert (REPO_ROOT / "skills" / "document" / "write_page.py").is_file()

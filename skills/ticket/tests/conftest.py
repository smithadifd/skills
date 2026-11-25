"""Make the flat ticket module importable and give each test its own git repository."""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def private_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr("tempfile.tempdir", None)
    # Ledger selection reads more than TICKET_LEDGER_CLI: `ledger_cli` in the topic config
    # (`~/.claude/skill-config/topic.json`) and, with a `ledger` on PATH, LEDGER_DB (rule 2 selects
    # the ledger with no topic config at all). A test and any subprocess it starts must decide the
    # ledger from a HOME and an environment the test owns, so HOME is a fresh directory and both
    # variables are cleared. PATH is left alone; a test that wants a ledger sets these itself.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TICKET_LEDGER_CLI", raising=False)
    monkeypatch.delenv("LEDGER_DB", raising=False)
    # The fake ledger CLI stamps every event with the caller's identity from LEDGER_SESSION and LEDGER_AGENT,
    # so an ambient value would change the events a test reads back (and any digest of them). Both are cleared
    # here; a test that wants an identity sets it itself.
    monkeypatch.delenv("LEDGER_SESSION", raising=False)
    monkeypatch.delenv("LEDGER_AGENT", raising=False)
    # Every legacy close fixture observes an actual offline gh executable, never the host's authenticated CLI.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"state":"MERGED"}\'\n', encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return tmp_path


@pytest.fixture
def repo(tmp_path):
    """A git repository whose ignore rules cover `.ticket/`."""

    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / ".gitignore").write_text(".ticket/\n", encoding="utf-8")
    return path

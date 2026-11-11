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
    monkeypatch.delenv("TICKET_LEDGER_CLI", raising=False)
    return tmp_path


@pytest.fixture
def repo(tmp_path):
    """A git repository whose ignore rules cover `.ticket/`."""

    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / ".gitignore").write_text(".ticket/\n", encoding="utf-8")
    return path

"""write_page.py is the only way the document skill writes a page.

Each guard has its own test, built so that no other guard also stops it:
the in-repository symlink case isolates the symlink walk, the direct calls to
`write_through` isolate O_NOFOLLOW, and the outside-path cases isolate the
containment checks. Set WRITE_PAGE_PATH to run the same tests against another
copy of the script.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

DEFAULT_SCRIPT = Path(__file__).resolve().parent.parent / "write_page.py"
SCRIPT = Path(os.environ.get("WRITE_PAGE_PATH", str(DEFAULT_SCRIPT)))

_spec = importlib.util.spec_from_file_location("write_page_under_test", SCRIPT)
write_page = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(write_page)


@pytest.fixture
def layout(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    return repo, outside


def run(repo, root, dest, mode, text="# Page\n"):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--root", str(root),
         "--dest", str(dest), "--mode", mode],
        input=text.encode(), capture_output=True,
    )


def assert_refused(result, fragment=None):
    assert result.returncode == 1, result.stderr.decode()
    message = result.stderr.decode().strip()
    assert message.startswith("write_page: refused: ")
    assert "\n" not in message
    if fragment:
        assert fragment in message


def test_create_writes_new_page_and_missing_subdirectory(layout):
    repo, _ = layout
    dest = repo / "docs" / "api" / "page.md"
    result = run(repo, repo / "docs", dest, "create", "# API\n")
    assert result.returncode == 0, result.stderr.decode()
    assert dest.read_text() == "# API\n"


def test_update_replaces_existing_page(layout):
    repo, _ = layout
    dest = repo / "docs" / "page.md"
    dest.write_text("old content that is longer than the new content\n")
    result = run(repo, repo / "docs", dest, "update", "new\n")
    assert result.returncode == 0, result.stderr.decode()
    assert dest.read_text() == "new\n"


def test_update_of_a_missing_page_is_refused(layout):
    repo, _ = layout
    dest = repo / "docs" / "page.md"
    assert_refused(run(repo, repo / "docs", dest, "update"), "missing")
    assert not dest.exists()


def test_create_over_an_existing_page_is_refused(layout):
    repo, _ = layout
    dest = repo / "docs" / "page.md"
    dest.write_text("keep me\n")
    assert_refused(run(repo, repo / "docs", dest, "create", "overwrite\n"), "exists")
    assert dest.read_text() == "keep me\n"


def test_empty_page_text_is_refused(layout):
    repo, _ = layout
    dest = repo / "docs" / "page.md"
    assert_refused(run(repo, repo / "docs", dest, "create", "  \n"), "empty")
    assert not dest.exists()


def test_symlinked_docs_root_is_refused(layout):
    repo, outside = layout
    (repo / "docs").rmdir()
    (repo / "docs").symlink_to(outside, target_is_directory=True)
    result = run(repo, repo / "docs", repo / "docs" / "page.md", "create")
    assert_refused(result)
    assert list(outside.iterdir()) == []


def test_symlinked_intermediate_directory_is_refused(layout):
    repo, outside = layout
    (repo / "docs" / "sub").symlink_to(outside, target_is_directory=True)
    result = run(repo, repo / "docs", repo / "docs" / "sub" / "page.md", "create")
    assert_refused(result)
    assert list(outside.iterdir()) == []


def test_symlinked_destination_file_is_refused(layout):
    repo, outside = layout
    target = outside / "page.md"
    target.write_text("untouched\n")
    (repo / "docs" / "page.md").symlink_to(target)
    result = run(repo, repo / "docs", repo / "docs" / "page.md", "update", "pwned\n")
    assert_refused(result)
    assert target.read_text() == "untouched\n"


def test_dotdot_escape_is_refused(layout):
    repo, outside = layout
    dest = repo / "docs" / ".." / ".." / "outside" / "page.md"
    assert_refused(run(repo, repo / "docs", dest, "create"), "'..'")
    assert list(outside.iterdir()) == []


def test_absolute_destination_outside_is_refused(layout):
    repo, outside = layout
    result = run(repo, repo / "docs", outside / "page.md", "create")
    assert_refused(result)
    assert list(outside.iterdir()) == []


def test_readme_is_allowed_only_in_the_readme_form(layout):
    repo, _ = layout
    readme = repo / "README.md"
    assert run(repo, repo, readme, "create", "# Hi\n").returncode == 0
    assert readme.read_text() == "# Hi\n"
    other = repo / "NOTES.md"
    assert_refused(run(repo, repo, other, "create"), "README.md")
    assert not other.exists()
    nested = repo / "docs" / "page.md"
    assert_refused(run(repo, repo, nested, "create"), "README.md")
    assert not nested.exists()


# Direct calls: each guard below is exercised with the others out of the way.


def test_check_paths_refuses_a_symlink_that_stays_inside_the_repository(layout):
    repo, _ = layout
    (repo / "docs" / "real").mkdir()
    (repo / "docs" / "alias").symlink_to(repo / "docs" / "real", target_is_directory=True)
    with pytest.raises(write_page.Refusal, match="symlink"):
        write_page.check_paths(
            str(repo), str(repo / "docs"), str(repo / "docs" / "alias" / "page.md"))


def test_check_paths_refuses_a_root_outside_the_repository(layout):
    repo, outside = layout
    with pytest.raises(write_page.Refusal, match="root"):
        write_page.check_paths(str(repo), str(outside), str(outside / "page.md"))


def test_check_paths_refuses_a_destination_outside_the_root(layout):
    repo, _ = layout
    with pytest.raises(write_page.Refusal, match="destination"):
        write_page.check_paths(str(repo), str(repo / "docs"), str(repo / "page.md"))


def test_write_through_does_not_follow_a_symlinked_directory(layout):
    repo, outside = layout
    (repo / "docs" / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(write_page.Refusal):
        write_page.write_through(
            os.path.realpath(repo), ["docs", "link", "page.md"], 1, b"x", "create")
    assert list(outside.iterdir()) == []


def test_write_through_does_not_follow_a_symlinked_final_component(layout):
    repo, outside = layout
    target = outside / "page.md"
    target.write_text("untouched\n")
    (repo / "docs" / "page.md").symlink_to(target)
    with pytest.raises(write_page.Refusal):
        write_page.write_through(
            os.path.realpath(repo), ["docs", "page.md"], 1, b"pwned", "update")
    assert target.read_text() == "untouched\n"

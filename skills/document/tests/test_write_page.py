"""write_page.py is the only way the document skill writes a page.

Each guard has its own test, built so that no other guard also stops it:
the in-repository symlink case isolates the symlink walk, the direct calls to
`write_through` isolate O_NOFOLLOW, and the outside-path cases isolate the
containment checks. Set WRITE_PAGE_PATH to run the same tests against another
copy of the script.
"""

import importlib.util
import io
import os
import stat
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


# The page is published by directory entry, never written through its inode.


def test_update_of_a_hard_link_leaves_the_other_file_untouched(layout):
    repo, outside = layout
    other = repo / "src.txt"
    other.write_text("not documentation\n")
    page = repo / "docs" / "page.md"
    os.link(other, page)
    result = run(repo, repo / "docs", page, "update", "new page\n")
    assert result.returncode == 0, result.stderr.decode()
    assert other.read_bytes() == b"not documentation\n"
    assert page.read_bytes() == b"new page\n"
    assert [p.name for p in (repo / "docs").iterdir()] == ["page.md"]


def test_a_failed_write_leaves_the_page_and_no_temporary_file(layout, monkeypatch):
    repo, _ = layout
    page = repo / "docs" / "page.md"
    page.write_bytes(b"original page\n")

    def failing_write(fd, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(write_page.os, "write", failing_write)
    with pytest.raises(OSError):
        write_page.write_through(
            os.path.realpath(repo), ["docs", "page.md"], 1, b"replacement", "update")
    monkeypatch.undo()
    assert page.read_bytes() == b"original page\n"
    assert [p.name for p in (repo / "docs").iterdir()] == ["page.md"]


def test_create_refuses_when_the_page_appears_before_the_link(layout, monkeypatch):
    repo, _ = layout
    page = repo / "docs" / "page.md"
    real_link = os.link

    def racing_link(src, dst, **kwargs):
        page.write_bytes(b"someone else got here first\n")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(write_page.os, "link", racing_link)
    with pytest.raises(write_page.Refusal, match="exists"):
        write_page.write_through(
            os.path.realpath(repo), ["docs", "page.md"], 1, b"mine", "create")
    monkeypatch.undo()
    assert page.read_bytes() == b"someone else got here first\n"
    assert [p.name for p in (repo / "docs").iterdir()] == ["page.md"]


def _main_with_stdin(monkeypatch, repo, dest, mode, text):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(text)))
    return write_page.main(["--repo", str(repo), "--root", str(repo / "docs"),
                            "--dest", str(dest), "--mode", mode])


def test_a_directory_sync_failure_after_publication_is_not_reported_as_unchanged(
        layout, monkeypatch, capsys):
    repo, _ = layout
    page = repo / "docs" / "page.md"
    page.write_bytes(b"old\n")
    real_fsync = os.fsync

    def fsync_failing_on_directories(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "Input/output error")
        return real_fsync(fd)

    monkeypatch.setattr(write_page.os, "fsync", fsync_failing_on_directories)
    code = _main_with_stdin(monkeypatch, repo, page, "update", b"new\n")
    monkeypatch.undo()
    err = capsys.readouterr().err
    assert code == 1
    assert "written, but syncing the directory failed" in err
    assert "unchanged" not in err
    assert page.read_bytes() == b"new\n"
    assert [p.name for p in (repo / "docs").iterdir()] == ["page.md"]


def _dir_close_failing(monkeypatch):
    """Make closing a directory descriptor fail, after really closing it."""
    real_close = os.close
    failures = []

    def close_failing_on_directories(fd):
        is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        real_close(fd)
        if is_dir:
            failures.append(fd)
            raise OSError(5, f"close failure {len(failures)}")

    monkeypatch.setattr(write_page.os, "close", close_failing_on_directories)


@pytest.mark.parametrize("mode", ["update", "create"])
def test_a_directory_close_failure_after_publication_is_not_reported_as_unchanged(
        layout, monkeypatch, capsys, mode):
    repo, _ = layout
    page = repo / "docs" / "page.md"
    if mode == "update":
        page.write_bytes(b"old\n")
    opened = []
    real_open = os.open

    def recording_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        if kwargs.get("dir_fd") is not None or args[1] & os.O_DIRECTORY:
            opened.append(fd)
        return fd

    monkeypatch.setattr(write_page.os, "open", recording_open)
    _dir_close_failing(monkeypatch)
    failing_close = write_page.os.close
    closed = []

    def counting_close(fd):
        closed.append(fd)
        return failing_close(fd)

    monkeypatch.setattr(write_page.os, "close", counting_close)
    code = _main_with_stdin(monkeypatch, repo, page, mode, b"new\n")
    monkeypatch.undo()
    err = capsys.readouterr().err
    # Every directory descriptor opened (repository and docs) had a close
    # attempted although the first close failed.
    assert len(opened) >= 3
    assert set(opened) <= set(closed)
    assert code == 1
    # The first failure is the one reported.
    assert "written, but closing a directory failed: close failure 1" in err
    assert "unchanged" not in err
    assert page.read_bytes() == b"new\n"
    assert [p.name for p in (repo / "docs").iterdir()] == ["page.md"]


def test_a_temporary_file_cleanup_failure_after_publication_is_not_reported_as_unchanged(
        layout, monkeypatch, capsys):
    repo, _ = layout
    page = repo / "docs" / "page.md"
    real_unlink = os.unlink

    def unlink_failing(path, **kwargs):
        raise OSError(13, "Permission denied")

    real_fsync = os.fsync
    directory_syncs = []

    def counting_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_syncs.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(write_page.os, "unlink", unlink_failing)
    monkeypatch.setattr(write_page.os, "fsync", counting_fsync)
    code = _main_with_stdin(monkeypatch, repo, page, "create", b"new\n")
    monkeypatch.undo()
    err = capsys.readouterr().err
    leftovers = [p.name for p in (repo / "docs").iterdir() if p.name != "page.md"]
    assert code == 1
    assert len(leftovers) == 1
    # The message names the file a person has to delete, relative to the repository.
    assert f"written, but removing the temporary file docs/{leftovers[0]} failed" in err
    assert str(repo) not in err
    assert "unchanged" not in err
    assert page.read_bytes() == b"new\n"
    # The directory is still synced after the cleanup failure.
    assert len(directory_syncs) == 1
    for name in leftovers:
        real_unlink(repo / "docs" / name)


def test_a_sync_failure_after_a_cleanup_failure_still_names_the_leftover(
        layout, monkeypatch, capsys):
    repo, _ = layout
    page = repo / "docs" / "page.md"
    real_unlink = os.unlink
    real_fsync = os.fsync

    def unlink_failing(path, **kwargs):
        raise OSError(13, "Permission denied")

    def fsync_failing_on_directories(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "Input/output error")
        return real_fsync(fd)

    monkeypatch.setattr(write_page.os, "unlink", unlink_failing)
    monkeypatch.setattr(write_page.os, "fsync", fsync_failing_on_directories)
    code = _main_with_stdin(monkeypatch, repo, page, "create", b"new\n")
    monkeypatch.undo()
    err = capsys.readouterr().err
    leftovers = [p.name for p in (repo / "docs").iterdir() if p.name != "page.md"]
    assert code == 1
    assert len(leftovers) == 1
    assert f"written, but removing the temporary file docs/{leftovers[0]} failed" in err
    assert "syncing the directory" not in err
    for name in leftovers:
        real_unlink(repo / "docs" / name)


def test_a_failure_before_publication_still_reports_the_page_unchanged(
        layout, monkeypatch, capsys):
    repo, _ = layout
    page = repo / "docs" / "page.md"
    page.write_bytes(b"old\n")

    def failing_write(fd, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(write_page.os, "write", failing_write)
    code = _main_with_stdin(monkeypatch, repo, page, "update", b"new\n")
    monkeypatch.undo()
    err = capsys.readouterr().err
    assert code == 1
    assert "destination unchanged" in err
    assert "written, but" not in err
    assert page.read_bytes() == b"old\n"


def test_a_directory_close_failure_before_publication_does_not_hide_the_refusal(
        layout, monkeypatch, capsys):
    repo, _ = layout
    page = repo / "docs" / "missing.md"
    _dir_close_failing(monkeypatch)
    code = _main_with_stdin(monkeypatch, repo, page, "update", b"new\n")
    monkeypatch.undo()
    err = capsys.readouterr().err
    assert code == 1
    assert "destination is missing" in err
    assert "written, but" not in err


def test_the_temporary_file_is_never_wider_than_0600_while_it_holds_content(
        layout, monkeypatch):
    repo, _ = layout
    page = repo / "docs" / "page.md"
    page.write_bytes(b"old\n")
    page.chmod(0o644)
    seen = []
    real_write = os.write

    def watching_write(fd, data):
        seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_write(fd, data)

    monkeypatch.setattr(write_page.os, "write", watching_write)
    write_page.write_through(
        os.path.realpath(repo), ["docs", "page.md"], 1, b"secret draft\n", "update")
    monkeypatch.undo()
    assert seen and all(mode & 0o077 == 0 for mode in seen), oct(seen[0])


def test_update_keeps_the_pages_permission_bits(layout):
    repo, _ = layout
    for bits in (0o600, 0o640, 0o664):
        page = repo / "docs" / f"page-{bits:o}.md"
        page.write_bytes(b"old\n")
        page.chmod(bits)
        result = run(repo, repo / "docs", page, "update", "new\n")
        assert result.returncode == 0, result.stderr.decode()
        assert stat.S_IMODE(page.stat().st_mode) == bits
        assert page.read_bytes() == b"new\n"


def test_create_gives_the_page_mode_0644(layout):
    repo, _ = layout
    page = repo / "docs" / "page.md"
    assert run(repo, repo / "docs", page, "create").returncode == 0
    assert stat.S_IMODE(page.stat().st_mode) == 0o644


def test_a_250_byte_page_name_can_be_created_and_updated(layout):
    repo, _ = layout
    page = repo / "docs" / ("a" * 247 + ".md")
    assert len(page.name.encode()) == 250
    result = run(repo, repo / "docs", page, "create", "long\n")
    assert result.returncode == 0, result.stderr.decode()
    result = run(repo, repo / "docs", page, "update", "longer\n")
    assert result.returncode == 0, result.stderr.decode()
    assert page.read_bytes() == b"longer\n"
    assert [p.name for p in (repo / "docs").iterdir()] == [page.name]


# A repository with no docs folder yet: create mode makes the output root
# itself, below the same no-follow rules as every other component.

def test_create_makes_a_missing_docs_root(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    dest = repo / "docs" / "page.md"
    result = run(repo, repo / "docs", dest, "create", "# New\n")
    assert result.returncode == 0, result.stderr.decode()
    assert dest.read_text() == "# New\n"


def test_update_still_needs_an_existing_docs_root(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    result = run(repo, repo / "docs", repo / "docs" / "page.md", "update")
    assert_refused(result)
    assert not (repo / "docs").exists()


def test_a_symlink_where_the_docs_root_would_go_is_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "docs").symlink_to(outside, target_is_directory=True)
    result = run(repo, repo / "docs", repo / "docs" / "page.md", "create")
    assert_refused(result)
    assert list(outside.iterdir()) == []


# Swap a symlink in at each step of the walk, between the check and that
# step's open, and prove nothing lands outside the repository. The swap runs
# from a wrapper around the real os call, installed by the test only.

def _snapshot(path):
    return sorted((str(p.relative_to(path)), p.read_bytes() if p.is_file() else None)
                  for p in path.rglob("*"))


def _hook(monkeypatch, name, when, action, kind="before"):
    real = getattr(os, name)
    state = {"done": False}

    def wrapper(*args, **kwargs):
        hit = not state["done"] and when(args, kwargs)
        if hit:
            state["done"] = True
            if kind == "before":
                action()
        result = real(*args, **kwargs)
        if hit and kind == "after":
            action()
        return result

    monkeypatch.setattr(os, name, wrapper)
    return state


def _swap_dir(path, target):
    """Replace the directory at path with a symlink to target."""
    def action():
        path.rename(path.with_name(path.name + "-moved"))
        path.symlink_to(target, target_is_directory=True)
    return action


def _plant(path, target):
    def action():
        if path.exists() or path.is_symlink():
            path.unlink()
        path.symlink_to(target)
    return action


STEPS = ["repo", "docs_component", "nested_component", "created_root",
         "temp_file", "create_link", "update_replace"]


@pytest.mark.parametrize("step", STEPS)
def test_a_symlink_swapped_in_at_each_step_never_writes_outside(tmp_path, monkeypatch, step):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    (outside / "docs" / "api").mkdir(parents=True)
    (outside / "file.md").write_text("untouched\n")
    mode = "create"
    if step == "created_root":
        repo.mkdir()
    else:
        (repo / "docs" / "api").mkdir(parents=True)
    dest = repo / "docs" / "api" / "page.md"
    if step == "update_replace":
        mode = "update"
        dest.write_text("old\n")
    before = _snapshot(outside)
    real_repo = os.path.realpath(repo)

    if step == "repo":
        _hook(monkeypatch, "open", lambda a, k: a[0] == real_repo,
              _swap_dir(repo, outside))
    elif step == "docs_component":
        _hook(monkeypatch, "open", lambda a, k: a[0] == "docs" and "dir_fd" in k,
              _swap_dir(repo / "docs", outside / "docs"))
    elif step == "nested_component":
        _hook(monkeypatch, "open", lambda a, k: a[0] == "api" and "dir_fd" in k,
              _swap_dir(repo / "docs" / "api", outside / "docs" / "api"))
    elif step == "created_root":
        _hook(monkeypatch, "mkdir", lambda a, k: a[0] == "docs",
              _swap_dir(repo / "docs", outside / "docs"), kind="after")
    elif step == "temp_file":
        monkeypatch.setattr(write_page.secrets, "token_hex", lambda n: "fixed")
        _plant(repo / "docs" / "api" / ".wp-fixed.tmp", outside / "file.md")()
    elif step == "create_link":
        _hook(monkeypatch, "link", lambda a, k: a[1] == "page.md",
              _plant(dest, outside / "file.md"))
    elif step == "update_replace":
        _hook(monkeypatch, "replace", lambda a, k: a[1] == "page.md",
              _plant(dest, outside / "file.md"))

    paths = write_page.check_paths(str(repo), str(repo / "docs"), str(dest), mode)
    try:
        write_page.write_through(*paths, b"escaped\n", mode)
    except write_page.Refusal:
        pass
    except OSError:
        pass
    assert _snapshot(outside) == before
    assert (outside / "file.md").read_text() == "untouched\n"


def test_a_different_directory_at_the_repository_path_is_refused(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    other = tmp_path / "other"
    (other / "docs").mkdir(parents=True)
    dest = repo / "docs" / "page.md"
    paths = write_page.check_paths(str(repo), str(repo / "docs"), str(dest), "create")
    repo.rename(tmp_path / "repo-moved")
    other.rename(repo)
    with pytest.raises(write_page.Refusal, match="changed after"):
        write_page.write_through(*paths, b"x", "create")
    assert list((repo / "docs").iterdir()) == []


# Only the output root itself may be created; its parents must already exist.

def test_nested_root_with_a_missing_parent_is_refused_and_creates_nothing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    root = repo / "site" / "docs"
    result = run(repo, root, root / "page.md", "create")
    assert_refused(result, "directory is missing: site")
    assert not (repo / "site").exists()


def test_nested_root_with_an_existing_parent_is_created(tmp_path):
    repo = tmp_path / "repo"
    (repo / "site").mkdir(parents=True)
    root = repo / "site" / "docs"
    result = run(repo, root, root / "api" / "page.md", "create", "# A\n")
    assert result.returncode == 0, result.stderr.decode()
    assert (root / "api" / "page.md").read_text() == "# A\n"


def test_a_root_directly_under_the_repository_is_created(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    result = run(repo, repo / "docs", repo / "docs" / "page.md", "create")
    assert result.returncode == 0, result.stderr.decode()


def test_a_repository_swapped_while_it_is_being_checked_writes_nothing(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    replacement = tmp_path / "replacement"
    (replacement / "docs").mkdir(parents=True)
    real_repo = os.path.realpath(repo)
    calls = []

    def swap():
        calls.append(1)
        if len(calls) == 2:
            repo.rename(tmp_path / "original")
            replacement.rename(repo)

    _hook_stat = os.stat

    def racing_stat(path, *args, **kwargs):
        if os.fspath(path) == real_repo:
            swap()
        return _hook_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", racing_stat)
    # check_paths' own second identity read must refuse; write_through's check
    # is pinned separately by test_a_different_directory_at_the_repository_path_is_refused.
    with pytest.raises(write_page.Refusal, match="changed while it was being checked"):
        write_page.check_paths(str(repo), str(repo / "docs"), str(repo / "docs" / "page.md"), "create")
    monkeypatch.undo()
    assert list((repo / "docs").iterdir()) == []

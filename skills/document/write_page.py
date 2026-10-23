#!/usr/bin/env python3
"""Write one documentation page, refusing any path that could escape the docs folder.

Usage:
    write_page.py --repo <repo root> --root <docs root> --dest <page path> \
        --mode create|update < page.md

The page text is read from standard input. The check and the write are one
call: the destination is opened relative to a directory handle, one component
at a time, with O_NOFOLLOW, so a path that changes after the check cannot
redirect the write. The text goes to a temporary file in that directory and
is published by directory entry (a rename for update, an exclusive link for
create), never written through the destination's own file, so a hard link to
another file is untouched and a failed write leaves the page as it was.

Refusals exit 1, print one line on stderr and write nothing:
  - --root or --dest contains a ".." component;
  - --root is not inside --repo, or --dest is not inside --root;
  - any existing component below --repo on the way to --dest is a symlink;
  - the canonical --root is outside the canonical --repo, or the canonical
    --dest is outside the canonical --root;
  - --mode create and the destination exists;
  - --mode update and the destination is missing or is not a regular file.
  - the repository path, or any directory on the way, is a symlink when it is
    opened, or the opened repository is not the directory that was checked.

In create mode a missing output root is created, one component at a time,
with the same no-follow opening.

The repository README is allowed only in the form
    --root <repo> --dest <repo>/README.md
Any other destination directly under the repository root is refused.
"""

import argparse
import os
import secrets
import stat
import sys

NOFOLLOW = os.O_NOFOLLOW
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY
README_NAME = "README.md"


class Refusal(Exception):
    """A guard failed; nothing was written."""


class RepoPath(str):
    """The canonical repository path, plus the identity of the directory seen
    when it was validated, so the later open can prove it is the same one."""

    identity = None


class Published(Exception):
    """The page is in place, but syncing the directory afterwards failed."""


def _inside(child, parent):
    """True when child equals parent or lies below it (both absolute, normalised)."""
    return os.path.commonpath([child, parent]) == parent


def _parts(path):
    return [p for p in path.split(os.sep) if p]


def check_paths(repo, root, dest, mode="update"):
    """Validate the three paths without touching the destination.

    Returns (real_repo, rel_parts, root_depth): the canonical repository root
    (carrying the identity of the directory that was validated), the
    destination as components below it, and how many of those components lead
    to the output root. In create mode the output root may not exist yet;
    write_through then creates it.
    """
    for label, value in (("repo", repo), ("root", root), ("dest", dest)):
        if not value:
            raise Refusal(f"--{label} is empty")
        if ".." in _parts(value):
            raise Refusal(f"--{label} contains a '..' component")
    repo_abs = os.path.abspath(repo)
    root_abs = os.path.abspath(root)
    dest_abs = os.path.abspath(dest)

    if not _inside(root_abs, repo_abs):
        raise Refusal("output root is not inside the repository")
    if not _inside(dest_abs, root_abs) or dest_abs == root_abs:
        raise Refusal("destination is not inside the output root")
    if root_abs == repo_abs and dest_abs != os.path.join(repo_abs, README_NAME):
        raise Refusal("the repository root is allowed only for its README.md")

    # Symlink walk: every existing component below the repository root.
    current = repo_abs
    for part in _parts(os.path.relpath(dest_abs, repo_abs)):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise Refusal(f"symlink in path: {current}")

    real_repo = os.path.realpath(repo_abs)
    real_root = os.path.realpath(root_abs)
    real_dest = os.path.realpath(dest_abs)
    if not os.path.isdir(real_repo):
        raise Refusal("repository root is not a directory")
    if not _inside(real_root, real_repo):
        raise Refusal("canonical output root is outside the canonical repository")
    if mode != "create" and not os.path.isdir(real_root):
        raise Refusal("output root is not an existing directory")
    if not _inside(real_dest, real_root) or real_dest == real_root:
        raise Refusal("canonical destination is outside the canonical output root")

    rel_parts = _parts(os.path.relpath(dest_abs, repo_abs))
    root_depth = len(_parts(os.path.relpath(root_abs, repo_abs)))
    try:
        seen = os.stat(real_repo)
    except OSError as exc:
        raise Refusal(f"cannot inspect the repository root: {exc.strerror}")
    real_repo = RepoPath(real_repo)
    real_repo.identity = (seen.st_dev, seen.st_ino)
    return real_repo, rel_parts, root_depth


def _unlink_quietly(name, dir_fd):
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def _write_temp(dir_fd, text, perm):
    """Write text to a new temporary file in the directory and return its name.

    The name is short and independent of the destination, so a long page name
    cannot make it too long. The file is created exclusively with mode 0600,
    filled completely, synced, and only then given its final permission bits,
    so its content is never readable more widely than the page it replaces.
    On any failure it is removed before the error propagates.
    """
    tmp = f".wp-{secrets.token_hex(8)}.tmp"
    out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        try:
            view = memoryview(text)
            while view:
                view = view[os.write(out, view):]
            os.fsync(out)
            os.fchmod(out, perm)
        finally:
            os.close(out)
    except BaseException:
        _unlink_quietly(tmp, dir_fd)
        raise
    return tmp


def write_through(real_repo, rel_parts, root_depth, text, mode):
    """Open the destination relative to directory handles and write text.

    Every open on the way is relative to a directory handle that is already
    held and refuses a symlink (O_NOFOLLOW): the repository, each component,
    the temporary file. In create mode any missing directory, including the
    output root itself, is made the same way; update mode needs them all.
    The repository handle must be the directory check_paths validated.
    """
    fds = []
    try:
        try:
            fd = os.open(real_repo, DIR_FLAGS | NOFOLLOW)
        except OSError as exc:
            raise Refusal(f"cannot open the repository without following links: {exc.strerror}")
        fds.append(fd)
        expected = getattr(real_repo, "identity", None)
        if expected is not None:
            opened_id = os.fstat(fd)
            if (opened_id.st_dev, opened_id.st_ino) != expected:
                raise Refusal("the repository changed after it was checked")
        for part in rel_parts[:-1]:
            try:
                try:
                    fd = os.open(part, DIR_FLAGS | NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if mode != "create":
                        raise Refusal(f"directory is missing: {part}")
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                    fd = os.open(part, DIR_FLAGS | NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                raise Refusal(f"cannot open directory {part} without following links: {exc.strerror}")
            fds.append(fd)

        name = rel_parts[-1]
        perm = 0o644
        if mode == "update":
            # lstat does not follow a symlink, so a link reads as not regular.
            try:
                kind = os.lstat(name, dir_fd=fd).st_mode
            except FileNotFoundError:
                raise Refusal(f"destination is missing (update mode): {name}")
            except OSError as exc:
                raise Refusal(f"cannot inspect {name}: {exc.strerror}")
            if not stat.S_ISREG(kind):
                raise Refusal(f"destination is not a regular file: {name}")
            perm = stat.S_IMODE(kind) & 0o777

        # Never write through the destination's inode: it may be hard-linked
        # to a file elsewhere, and an in-place write could be left half done.
        # Write a temporary file next to it, then publish it by directory entry.
        tmp = _write_temp(fd, text, perm)
        published = False
        try:
            if mode == "create":
                try:
                    os.link(tmp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                except FileExistsError:
                    raise Refusal(f"destination exists (create mode): {name}")
                except OSError as exc:
                    raise Refusal(f"cannot create {name}: {exc.strerror}")
            else:
                os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
            published = True
        finally:
            # Before publication the temporary file is removed. After it, the
            # page is already in place and a failure must not claim otherwise.
            if not published:
                _unlink_quietly(tmp, fd)
        try:
            if mode == "create":
                _unlink_quietly(tmp, fd)
            os.fsync(fd)
        except OSError as exc:
            raise Published(exc.strerror)
    finally:
        for opened in fds:
            os.close(opened)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--mode", required=True, choices=("create", "update"))
    args = parser.parse_args(argv)
    try:
        text = sys.stdin.buffer.read()
        if not text.strip():
            raise Refusal("page text on stdin is empty")
        real_repo, rel_parts, root_depth = check_paths(args.repo, args.root, args.dest, args.mode)
        write_through(real_repo, rel_parts, root_depth, text, args.mode)
    except Refusal as exc:
        print(f"write_page: refused: {exc}", file=sys.stderr)
        return 1
    except Published as exc:
        print(f"write_page: written, but syncing the directory failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"write_page: refused: write failed, destination unchanged: {exc.strerror}", file=sys.stderr)
        return 1
    print(f"wrote {args.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

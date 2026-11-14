"""The shape of the source tree the ledger e2e check installs from (stdlib only).

install.sh reads the source through two roots, skills/ and agents/, with globs and
`[ -d ]` / `[ -f ]` tests that follow symlinks, so a symlinked root, entry or nested
file would copy outside data into the throwaway home while every later assertion
stayed green. `violations(src)` refuses, by lstat type, anything under those two roots
(the roots included) that is not a regular file or a directory; `names(src)` returns
the skill and agent names install.sh would install. A skill name follows install.sh's
is_skill_name: not empty, not a dot-name, not `__pycache__`, then either `_lib` as a
directory (the shared helper directory, which has no SKILL.md and is installed beside
every skill) or a directory that holds a regular file named SKILL.md. Any other
directory under skills/ (a cache, a notes folder) is not a skill. An agent is a regular
file ending in .md whose name is not a dot-name. `names` raises on any `violations`
finding before it reads a name, so no symlink under either root is ever followed by the
SKILL.md check.

Run as a script with the tree as its one argument: it prints one violation per line and
exits 1, or prints one line and exits 2 on an OSError; exit 0 prints nothing.
"""

import os
import stat
import sys

ROOTS = ("skills", "agents")


def _raise(error):
    raise error


def violations(src):
    found = []
    for root in ROOTS:
        top = os.path.join(src, root)
        try:
            kind = stat.S_IFMT(os.lstat(top).st_mode)
        except FileNotFoundError:
            found.append(root + " (missing)")
            continue
        if kind != stat.S_IFDIR:
            found.append(root + " (not a real directory)")
            continue
        for base, dirs, files in os.walk(top, followlinks=False, onerror=_raise):
            for name in sorted(dirs + files):
                path = os.path.join(base, name)
                kind = stat.S_IFMT(os.lstat(path).st_mode)
                if kind not in (stat.S_IFREG, stat.S_IFDIR):
                    found.append(os.path.relpath(path, src))
    return found


def _lstat_kind(path):
    try:
        return stat.S_IFMT(os.lstat(path).st_mode)
    except FileNotFoundError:
        return None


def _is_skill_name(skills_dir, name):
    # The rule install.sh's is_skill_name applies (and ci/check.sh copies): refuse the
    # empty name, a dot-name, a name with a slash and __pycache__; `_lib` is a skill when it
    # is a directory; any other name is a skill when it is a directory holding a SKILL.md.
    # Only called after violations() found no symlink, so lstat equals the test install.sh's
    # [ -d ] and [ -f ] make.
    if name == "" or name.startswith(".") or "/" in name or name == "__pycache__":
        return False
    path = os.path.join(skills_dir, name)
    if _lstat_kind(path) != stat.S_IFDIR:
        return False
    if name == "_lib":
        return True
    return _lstat_kind(os.path.join(path, "SKILL.md")) == stat.S_IFREG


def names(src):
    found = violations(src)
    if found:
        raise ValueError("source tree shape: " + "; ".join(found))
    skills_dir = os.path.join(src, "skills")
    agents_dir = os.path.join(src, "agents")
    skills = {n for n in os.listdir(skills_dir) if _is_skill_name(skills_dir, n)}
    agents = {
        n
        for n in os.listdir(agents_dir)
        if not n.startswith(".") and n.endswith(".md") and stat.S_ISREG(os.lstat(os.path.join(agents_dir, n)).st_mode)
    }
    return skills, agents


def main(argv):
    if len(argv) != 2:
        print("usage: source_shape.py <tree>")
        return 64
    try:
        found = violations(argv[1])
    except OSError as error:
        print("source shape walk error: %s" % type(error).__name__)
        return 2
    if found:
        print("\n".join(found))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

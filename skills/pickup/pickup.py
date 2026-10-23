#!/usr/bin/env python3
"""Read a handoff note and show what to trust. Runs nothing unless asked.

    python3 pickup.py [NOTE_PATH] [--repo NAME] [--cwd DIR]
    python3 pickup.py NOTE_PATH --rerun 1,3 --note-digest DIGEST [--cwd DIR]

Without NOTE_PATH, uses the newest note in the per-user notes directory under
the temporary directory (only files owned by you, not group- or world-writable,
not symlinks). The default run parses the note and lists its Verified commands
numbered; it executes none of them. --rerun runs only the listed commands, and
requires an explicit NOTE_PATH and the note digest the listing printed; if
the note changed since the listing, nothing runs. A rerun command that exits nonzero or cannot
run is demoted to Claimed; so is every command not rerun.
A Verified line looks like:  - `command` => result
"""
import argparse
import glob
import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

VERIFIED_LINE = re.compile(r"^- `(?P<cmd>[^`]+)`\s*(?:=>\s*(?P<result>.*))?$")
RERUN_TIMEOUT = 120


def notes_dir():
    """The private per-user notes directory, or None when absent or unsafe."""
    path = os.path.join(tempfile.gettempdir(), "handoff-notes-%d" % os.getuid())
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if (stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode)
            or st.st_uid != os.getuid() or st.st_mode & 0o077):
        return None
    return path


def find_note(repo=None):
    """Newest of your own handoff notes in the private directory, or None."""
    base = notes_dir()
    if base is None:
        return None
    pattern = "handoff-%s-*.md" % (repo if repo else "*")
    best = None
    for path in glob.glob(os.path.join(base, pattern)):
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or st.st_mode & 0o022):
            continue
        if best is None or st.st_mtime > best[0]:
            best = (st.st_mtime, path)
    return best[1] if best else None


def parse_sections(text):
    sections, name = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            name = line[3:].strip()
            sections[name] = []
        elif name is not None:
            sections[name].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def written_at(text):
    m = re.search(r"^Written: (\S+)", text, re.MULTILINE)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def latest_commit_time(cwd):
    """Committer time of HEAD in cwd, or None when it is not a repository."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI"],
            cwd=cwd, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return datetime.fromisoformat(out.stdout.strip())
    except ValueError:
        return None


def rerun(cmd, cwd):
    """Run one Verified command. Returns (ok, detail)."""
    try:
        done = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=RERUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, "timed out after %ds" % RERUN_TIMEOUT
    except OSError as exc:
        return False, "could not run: %s" % exc
    return done.returncode == 0, "exit %d" % done.returncode


def _bullets(body):
    return [l.strip() for l in body.splitlines() if l.strip() and l.strip() != "none"]


def digest(text):
    """Short digest (first 12 hex of sha256) of exact text, for display."""
    return note_digest_of(text)[:12]


def note_digest_of(text):
    """Full sha256 hex digest of exact text; authorizes a rerun."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pickup(text, cwd=None, rerun_indices=(), note_digest=None):
    """Parse a note. Runs only the Verified commands whose 1-based number is in
    rerun_indices (none by default). Returns a dict: stale, commands, verified,
    demoted, not_rerun, claimed, open_questions, skills, decisions."""
    if rerun_indices and note_digest != note_digest_of(text):
        raise ValueError("note digest does not match the listing; the note changed "
                         "or no digest was given. Nothing was run. List it again.")
    sections = parse_sections(text)
    commands = []
    for line in sections.get("Verified", "").splitlines():
        m = VERIFIED_LINE.match(line.strip())
        if m:
            commands.append({"command": m.group("cmd"), "result": m.group("result") or "",
                             "digest": digest(m.group("cmd"))})
    verified, demoted, not_rerun = [], [], []
    for number, item in enumerate(commands, 1):
        if number not in rerun_indices:
            not_rerun.append(item)
            continue
        ok, detail = rerun(item["command"], cwd)
        (verified if ok else demoted).append(dict(item, detail=detail))
    claimed = _bullets(sections.get("Claimed but not verified", ""))
    claimed += ["- `%s` (was verified; failed on rerun: %s)" % (d["command"], d["detail"])
                for d in demoted]
    claimed += ["- `%s` (was verified; not rerun)" % d["command"] for d in not_rerun]
    wrote, head = written_at(text), latest_commit_time(cwd)
    return {
        "stale": bool(wrote and head and head > wrote),
        "digest": note_digest_of(text),
        "written": wrote,
        "head_commit": head,
        "commands": commands,
        "verified": verified,
        "demoted": demoted,
        "not_rerun": not_rerun,
        "claimed": claimed,
        "open_questions": _bullets(sections.get("Open questions", "")),
        "skills": _bullets(sections.get("Skills to invoke next", "")),
        "decisions": _bullets(sections.get("Decisions and why", "")),
    }


def report(result):
    out = []
    if result["written"] is None:
        out.append("WARNING: the note has no readable Written date; cannot compare with commits.")
    elif result["stale"]:
        out.append("WARNING: the repository has a commit newer than this note (%s > %s)."
                   % (result["head_commit"].isoformat(), result["written"].isoformat()))
    out.append("Note digest: %s" % result["digest"])
    out.append("Verified commands in the note (%d):" % len(result["commands"]))
    out += ["  %d. [%s] `%s`" % (i, c["digest"], c["command"])
            for i, c in enumerate(result["commands"], 1)]
    out.append("Rerun and passed (%d):" % len(result["verified"]))
    out += ["  - `%s`" % v["command"] for v in result["verified"]]
    out.append("Rerun and failed, demoted to claimed (%d):" % len(result["demoted"]))
    out += ["  - `%s` (%s)" % (d["command"], d["detail"]) for d in result["demoted"]]
    out.append("Not rerun, treated as claimed (%d)" % len(result["not_rerun"]))
    out.append("Claimed, not verified:")
    out += ["  " + c for c in result["claimed"]] or ["  none"]
    out.append("Open questions:")
    out += ["  " + q for q in result["open_questions"]] or ["  none"]
    out.append("Skills to load next:")
    out += ["  " + s for s in result["skills"]] or ["  none"]
    return "\n".join(out)


def parse_indices(spec):
    try:
        return {int(p) for p in spec.split(",") if p.strip()}
    except ValueError:
        raise ValueError("--rerun takes comma-separated command numbers, got %r" % spec)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("note", nargs="?")
    ap.add_argument("--repo")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--rerun", help="comma-separated Verified command numbers to run")
    ap.add_argument("--note-digest", help="the note digest printed by the listing")
    args = ap.parse_args(argv)
    if args.rerun is not None and not args.note:
        print("pickup: --rerun needs an explicit note path", file=sys.stderr)
        return 2
    try:
        indices = parse_indices(args.rerun) if args.rerun is not None else set()
        path = args.note or find_note(args.repo)
        if not path or not os.path.isfile(path):
            print("pickup: no handoff note found", file=sys.stderr)
            return 1
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        result = pickup(text, cwd=args.cwd, rerun_indices=indices,
                        note_digest=args.note_digest)
    except (OSError, ValueError) as exc:
        print("pickup: %s" % exc, file=sys.stderr)
        return 1
    print("Note: " + path)
    print(report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

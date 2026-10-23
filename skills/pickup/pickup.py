#!/usr/bin/env python3
"""Read a handoff note, recheck it, and print what to trust.

    python3 pickup.py [NOTE_PATH] [--repo NAME] [--cwd DIR]

Without NOTE_PATH, uses the newest handoff-*.md in the temporary directory
(narrowed by --repo when given). Every command in the note's Verified section
is rerun; one that exits nonzero, or cannot run, is demoted to Claimed.
A Verified line looks like:  - `command` => result
"""
import argparse
import glob
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

VERIFIED_LINE = re.compile(r"^- `(?P<cmd>[^`]+)`\s*(?:=>\s*(?P<result>.*))?$")
RERUN_TIMEOUT = 120


def find_note(repo=None):
    """Newest handoff note in the temporary directory, or None."""
    pattern = "handoff-%s-*.md" % (repo if repo else "*")
    hits = glob.glob(os.path.join(tempfile.gettempdir(), pattern))
    return max(hits, key=os.path.getmtime) if hits else None


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
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


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
    return datetime.fromisoformat(out.stdout.strip())


def rerun(cmd, cwd):
    """Run a Verified command. Returns (ok, detail)."""
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


def pickup(text, cwd=None):
    """Recheck a note. Returns a dict: stale, verified, demoted, claimed,
    open_questions, skills, decisions."""
    sections = parse_sections(text)
    verified, demoted = [], []
    for line in sections.get("Verified", "").splitlines():
        m = VERIFIED_LINE.match(line.strip())
        if not m:
            continue
        ok, detail = rerun(m.group("cmd"), cwd)
        (verified if ok else demoted).append(
            {"command": m.group("cmd"), "result": m.group("result") or "", "detail": detail}
        )
    claimed = _bullets(sections.get("Claimed but not verified", ""))
    claimed += ["- `%s` (was verified; failed on rerun: %s)" % (d["command"], d["detail"])
                for d in demoted]
    wrote, head = written_at(text), latest_commit_time(cwd)
    return {
        "stale": bool(wrote and head and head > wrote),
        "written": wrote,
        "head_commit": head,
        "verified": verified,
        "demoted": demoted,
        "claimed": claimed,
        "open_questions": _bullets(sections.get("Open questions", "")),
        "skills": _bullets(sections.get("Skills to invoke next", "")),
        "decisions": _bullets(sections.get("Decisions and why", "")),
    }


def report(result):
    out = []
    if result["stale"]:
        out.append("WARNING: the repository has a commit newer than this note (%s > %s)."
                   % (result["head_commit"].isoformat(), result["written"].isoformat()))
    out.append("Still verified (%d):" % len(result["verified"]))
    out += ["  - `%s`" % v["command"] for v in result["verified"]]
    out.append("Demoted to claimed (%d):" % len(result["demoted"]))
    out += ["  - `%s` (%s)" % (d["command"], d["detail"]) for d in result["demoted"]]
    out.append("Claimed, not verified:")
    out += ["  " + c for c in result["claimed"]] or ["  none"]
    out.append("Open questions:")
    out += ["  " + q for q in result["open_questions"]] or ["  none"]
    out.append("Skills to load next:")
    out += ["  " + s for s in result["skills"]] or ["  none"]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("note", nargs="?")
    ap.add_argument("--repo")
    ap.add_argument("--cwd", default=os.getcwd())
    args = ap.parse_args(argv)
    path = args.note or find_note(args.repo)
    if not path or not os.path.isfile(path):
        print("no handoff note found", file=sys.stderr)
        return 1
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    print("Note: " + path)
    print(report(pickup(text, cwd=args.cwd)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

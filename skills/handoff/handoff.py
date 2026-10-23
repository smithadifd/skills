#!/usr/bin/env python3
"""Write a session resume note: fixed sections, redacted, capped.

    python3 handoff.py --repo NAME --slug SLUG --input content.json [--out PATH]

content.json maps section names to text (a string, or a list of strings that
become bullet lines). Missing sections are written as "none". The Secrets
section is always "none, redacted", whatever the input says.

Default output: handoff-<repo>-<slug>.md in a private per-user directory
(mode 0700) under the temporary directory, file mode 0600. The note is never
written inside a repository unless --out names a path there.
"""
import argparse
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone

CAP_BYTES = 20 * 1024
REDACTED = "[REDACTED]"
SECRETS_TEXT = "none, redacted"
TRIM_MARK = " [...]"
NOTICE_RESERVE = 120

SECTIONS = [
    "Goal",
    "Where we stopped",
    "Verified",
    "Claimed but not verified",
    "Decisions and why",
    "Failed approaches",
    "Open questions",
    "References",
    "Skills to invoke next",
    "Secrets",
]

_NAME = r"[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Za-z0-9_]*"
# Quoted values are escape-aware: a backslash-escaped quote does not end them.
_DQ = r'"(?:[^"\\\n]|\\.)*"'
_SQ = r"'(?:[^'\\\n]|\\.)*'"
_VALUE = r"(?:" + _DQ + "|" + _SQ + r"|[^\s\"']+)"

# Order matters: whole PEM blocks first, then specific token shapes, then
# generic assignments.
_PATTERNS = [
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
        re.DOTALL,
    ),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
]
# An Authorization key: one of these names, case-insensitive, not run on from
# or into a longer word (a letter, digit or underscore on either side makes
# it part of another word; a dash does not, so X-Authorization and
# Proxy-Authorization are keys through the plain name). A
# letter or digit that ends a written escape (\n, \r, \t, \xHH, \uHHHH, all
# lowercase) or a percent-encoded byte (%HH, %25HH) is a boundary. Only the
# name is case-insensitive, so \N or \X0a is not an escape. No
# separator is required, so prose mentions count as keys too. A section that
# holds a key is redacted whole.
# See references/authorization-redaction.md.
_AUTH_KEY = re.compile(
    r"(?:(?<![A-Za-z0-9_])"
    r"|(?<=\\[nrt])|(?<=\\x[0-9A-Fa-f]{2})|(?<=\\u[0-9A-Fa-f]{4})"
    r"|(?<=%[0-9A-Fa-f]{2})|(?<=%25[0-9A-Fa-f]{2}))"
    r"(?i:(?:REDIRECT_HTTP_|HTTP_PROXY_|HTTP_)?AUTHORIZATION)"
    r"(?![A-Za-z0-9_])",
)
_FLAG = re.compile(
    r"(--[A-Za-z0-9-]*(?:key|token|secret|password|passwd)[A-Za-z0-9-]*)(\s+|=)(?!-)(" + _VALUE + ")",
    re.IGNORECASE,
)
_ASSIGN = re.compile(r"\b(" + _NAME + r")(\s*=\s*)(" + _VALUE + ")", re.IGNORECASE)
_COLON = re.compile(r"\b(" + _NAME + r")(\s*:\s*)(" + _VALUE + ")", re.IGNORECASE)
_JSON_PAIR = re.compile(r"(\"" + _NAME + r"\"\s*:\s*)" + _DQ, re.IGNORECASE)


class NoteError(Exception):
    pass


def redact(text):
    """Replace secret-shaped strings with [REDACTED].

    text is one section (or the header). When it holds an Authorization key,
    the whole of it is replaced by one marker.
    """
    if _AUTH_KEY.search(text):
        return REDACTED
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _FLAG.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
    text = _ASSIGN.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
    text = _COLON.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
    text = _JSON_PAIR.sub(lambda m: m.group(1) + '"' + REDACTED + '"', text)
    return text


def _trim(body, limit):
    """Cut body to at most limit bytes without splitting a character."""
    raw = body.encode("utf-8")
    if len(raw) <= limit:
        return body
    return raw[:limit].decode("utf-8", errors="ignore").rstrip() + TRIM_MARK


def cap_bodies(bodies, fixed_bytes, limit=CAP_BYTES):
    """Fit section bodies (name -> text) into limit bytes of document.

    fixed_bytes is the size of everything that is not a body (title, date,
    headings). Every heading is kept; the largest bodies are cut first, down to
    a common length. Returns (bodies, truncated, dropped_bytes).
    """
    size = {n: len(b.encode("utf-8")) for n, b in bodies.items()}
    if fixed_bytes + sum(size.values()) <= limit:
        return bodies, False, 0
    fixed_secrets = size.get("Secrets", 0)
    trimmable = [n for n in bodies if n != "Secrets"]
    budget = (limit - fixed_bytes - NOTICE_RESERVE - fixed_secrets
              - len(TRIM_MARK.encode("utf-8")) * len(trimmable))
    lo, hi = 0, max(size.values())
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if sum(min(size[n], mid) for n in trimmable) <= budget:
            lo = mid
        else:
            hi = mid - 1
    out = {n: (_trim(b, lo) if n in trimmable else b) for n, b in bodies.items()}
    dropped = sum(size.values()) - sum(len(b.encode("utf-8")) for b in out.values())
    return out, True, max(dropped, 0)


def _section_text(value):
    if value is None or value == "" or value == []:
        return "none"
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v) if str(v).startswith("- ") else "- " + str(v) for v in value)
    return str(value).strip()


HEADER_FIELD_MAX = 80


def _bound(value):
    """One line, at most HEADER_FIELD_MAX characters, with a marker when cut."""
    value = " ".join(str(value).split())
    if len(value) > HEADER_FIELD_MAX:
        value = value[: HEADER_FIELD_MAX - 3] + "..."
    return value


def build_note(content, repo, slug, now=None):
    """Render the note. Returns (text, truncated). Every section is always
    present; when the note is too large, section bodies are cut, never headings."""
    now = now or datetime.now(timezone.utc)
    repo, slug = _bound(repo), _bound(slug)
    head = "# Handoff: %s / %s\n\nWritten: %s\n\n" % (
        repo, slug, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    bodies = {}
    for name in SECTIONS:
        body = SECRETS_TEXT if name == "Secrets" else _section_text(content.get(name))
        bodies[name] = redact(body)
    # The blank line that ends the header is layout, not content.
    head = redact(head.rstrip("\n")) + "\n\n"
    fixed = len(head.encode("utf-8")) + sum(
        len(("## %s\n\n\n\n" % n).encode("utf-8")) for n in SECTIONS)
    bodies, truncated, dropped = cap_bodies(bodies, fixed)
    text = head + "".join("## %s\n\n%s\n\n" % (n, bodies[n]) for n in SECTIONS)
    if truncated:
        text += "[truncated: note exceeded %d KB; %d bytes dropped]\n" % (CAP_BYTES // 1024, dropped)
    return text, truncated


def _safe(part):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", part).strip("-") or "x"


def notes_dir():
    """Private per-user directory for notes under the temporary directory.

    Created 0700. Refused when it exists but is a symlink, is not a directory,
    is not owned by the current user, or is not mode 0700.
    """
    path = os.path.join(tempfile.gettempdir(), "handoff-notes-%d" % os.getuid())
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise NoteError("%s is not a plain directory" % path)
    if st.st_uid != os.getuid():
        raise NoteError("%s is not owned by you" % path)
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise NoteError("%s must be mode 0700" % path)
    return path


def default_path(repo, slug):
    return os.path.join(notes_dir(), "handoff-%s-%s.md" % (_safe(repo), _safe(slug)))


def write_note(content, repo, slug, out=None, now=None):
    """Write the note. Returns (path, truncated)."""
    path = out or default_path(repo, slug)
    note, truncated = build_note(content, repo, slug, now=now)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(note)
    return path, truncated


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--input", required=True, help="JSON file of section name to text")
    ap.add_argument("--out", help="explicit output path (default: private notes directory)")
    args = ap.parse_args(argv)
    try:
        with open(args.input, encoding="utf-8") as fh:
            content = json.load(fh)
        if not isinstance(content, dict):
            raise ValueError("input must be a JSON object of section name to text")
        path, truncated = write_note(content, args.repo, args.slug, out=args.out)
    except (OSError, ValueError, NoteError) as exc:
        print("handoff: %s" % exc, file=sys.stderr)
        return 1
    print(path)
    print("truncated: %s" % ("yes" if truncated else "no"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

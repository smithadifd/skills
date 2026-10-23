#!/usr/bin/env python3
"""Write a session resume note: fixed sections, redacted, capped.

    python3 handoff.py --repo NAME --slug SLUG --input content.json [--out PATH]

content.json maps section names to text (a string, or a list of strings that
become bullet lines). Missing sections are written as "none". The Secrets
section is always "none, redacted", whatever the input says.

Default output: <temporary directory>/handoff-<repo>-<slug>.md. The note is
never written inside a repository unless --out names a path there.
"""
import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

CAP_BYTES = 20 * 1024
REDACTED = "[REDACTED]"
SECRETS_TEXT = "none, redacted"

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
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
]
_ASSIGN = re.compile(
    r"\b(" + _NAME + r")(\s*=\s*)(\"[^\"\n]*\"|'[^'\n]*'|[^\s\"']+)",
    re.IGNORECASE,
)
_JSON_PAIR = re.compile(
    r"(\"" + _NAME + r"\"\s*:\s*)\"[^\"\n]*\"",
    re.IGNORECASE,
)


def redact(text):
    """Replace secret-shaped strings with [REDACTED]."""
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _ASSIGN.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
    text = _JSON_PAIR.sub(lambda m: m.group(1) + '"' + REDACTED + '"', text)
    return text


def cap(text, limit=CAP_BYTES):
    """Return (text, truncated). The result, notice included, fits in limit bytes."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    notice = "\n\n[truncated: note exceeded %d KB; %d bytes dropped]\n"
    # Reserve room for the notice, whose numbers are at most a few digits.
    reserve = len((notice % (limit // 1024, len(raw))).encode("utf-8"))
    kept = raw[: limit - reserve].decode("utf-8", errors="ignore")
    dropped = len(raw) - len(kept.encode("utf-8"))
    return kept.rstrip() + notice % (limit // 1024, dropped), True


def _section_text(value):
    if value is None or value == "" or value == []:
        return "none"
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v) if str(v).startswith("- ") else "- " + str(v) for v in value)
    return str(value).strip()


def build_note(content, repo, slug, now=None):
    """Render the note: header, then every section in fixed order."""
    now = now or datetime.now(timezone.utc)
    parts = [
        "# Handoff: %s / %s" % (repo, slug),
        "",
        "Written: %s" % now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "",
    ]
    for name in SECTIONS:
        body = SECRETS_TEXT if name == "Secrets" else _section_text(content.get(name))
        parts += ["## " + name, "", body, ""]
    note = redact("\n".join(parts))
    note, _ = cap(note)
    return note


def _safe(part):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", part).strip("-") or "x"


def default_path(repo, slug):
    return os.path.join(tempfile.gettempdir(), "handoff-%s-%s.md" % (_safe(repo), _safe(slug)))


def write_note(content, repo, slug, out=None, now=None):
    path = out or default_path(repo, slug)
    note = build_note(content, repo, slug, now=now)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(note)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--input", required=True, help="JSON file of section name to text")
    ap.add_argument("--out", help="explicit output path (default: temporary directory)")
    args = ap.parse_args(argv)
    with open(args.input, encoding="utf-8") as fh:
        content = json.load(fh)
    print(write_note(content, args.repo, args.slug, out=args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

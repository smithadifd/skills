#!/usr/bin/env python3
"""Memory gardener — deterministic mechanics for reconciling a Claude memory dir.

REPORT-FIRST. This helper never decides anything on its own: the *model* reads
`scan` output, classifies each fact file (KEEP / UPDATE / SUPERSEDED / DEAD /
UNVERIFIED>90d), and writes the plan. The helper then renders that plan into a
review report (`report`) and — only after the human has read it — executes
exactly that plan (`apply`). The report IS the plan: `report` and `apply` build
the proposed file content through the SAME function, so nothing is re-derived
between what you reviewed and what gets written.

Subcommands
  scan    Read the memory dir and emit a JSON inventory (frontmatter, index
          line, wikilinks, age vs last_verified, and heuristic pathology
          signals). Grounds the model's classification; changes nothing.
  report  Render a plan JSON into a markdown review report: per-file verdict,
          rationale, a unified diff of every proposed change, and a summary
          table. Writes only its own report under ~/.claude/logs/memory-gardener/.
  apply   Execute a plan JSON against the memory dir: additive frontmatter
          stamps, body rewrites, MEMORY.md index rebuild, archive moves with a
          tombstone, and wikilink repair. This is the gated, mutating step.
  budget  Read-only index-load-cost check; when over the ceiling it PROPOSES a
          topic-sharding split (never writes). Schedulable.
  classify  Read-only: tag every note personal / harness-fact / decision /
          project-gotcha / stale, with evidence. Writes only an explicit --out.
  shard   EXECUTE the topic-split budget proposes. Dry-run by DEFAULT (prints
          exactly what would be written, reusing propose_shards); `--apply`
          performs it behind a sha256 drift guard, an on-disk backup, a no-loss
          invariant, and a no-clobber guard. The writing sibling of `budget`.

Hard invariants (unit-tested):
  * Frontmatter edits are ADDITIVE-ONLY. The only keys this tool will ever write
    are `status`, `last_verified`, `supersedes`, `description`. Existing keys
    (name, metadata.*, and description itself when the plan doesn't touch it)
    are byte-preserved because the frontmatter block is spliced as text, never
    reparsed-and-reserialized. `description` is writable because it is the
    field the harness reads for recall (issue #177) — a `SUPERSEDED`/
    `UPDATE` rewrite of a note's body previously had no mechanical way to
    correct a `description` that now contradicted it.
  * Nothing is ever deleted. DEAD facts are MOVED into archive/ with a tombstone
    frontmatter line; git detects the rename on the human's commit.
  * [[wikilinks]] are preserved; archive/rename repairs are applied per the plan,
    and any link left dangling is reported loudly.

stdlib-only, offline. See SKILL.md for the full contract.
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import date, datetime

# Additive-only frontmatter whitelist — the ONLY keys this tool will write.
# `description` (issue #177): the harness's recall surface — the one
# field a SUPERSEDED/UPDATE body rewrite previously had no mechanical way to
# correct, leaving a note retrievable (or not) under a summary its own body no
# longer supports. Same guardrails as the other three: report-first,
# --apply-gated, additive-splice-only — see apply_frontmatter/_serialize_value
# for the description-specific quoting and block-scalar handling this needed.
WRITABLE_KEYS = ("status", "last_verified", "supersedes", "description")
VALID_VERDICTS = ("KEEP", "UPDATE", "SUPERSEDED", "DEAD", "UNVERIFIED>90d")
UNVERIFIED_DAYS = 90
REPORT_DIR = os.path.expanduser("~/.claude/logs/memory-gardener")
ARCHIVE_DIRNAME = "archive"
DOCS_DIRNAME = "docs"

# Heuristic pathology markers (case-insensitive) — a correction/saga chain.
SAGA_MARKERS = [
    "WRONG", "CORRECTION", "CORRECTED", "SUPERSEDED", "red herring",
    "I got this wrong", "dead-end", "dead end", "fooled me", "NOT a red herring",
    "reverted", "was WRONG",
]
# An index line longer than this (chars, after the "— ") reads as a state dump,
# not a scannable one-liner. Real MEMORY.md one-liners sit well under this.
INDEX_BALLOON_CHARS = 320

# --- Index-load-cost budget ------------------------------------------------- #
# MEMORY.md is loaded WHOLESALE into every session's context, so its size is a
# standing per-session token tax. `budget` (read-only) measures that cost and,
# when it exceeds the ceiling, proposes topic-sharding: a small always-loaded
# core index + JIT-loaded topic sub-indexes (MEMORY.<topic>.md).
#
# Token estimate: the chars/4 heuristic — deliberately DEPENDENCY-FREE (no
# tokenizer). It over/under-counts by a little but is stable and defensible for
# a budget tripwire; the ceiling is generous enough to absorb the imprecision.
CHARS_PER_TOKEN = 4
# Defaults: the ceiling that only trips on real bloat — index-sickness
# (ballooned lines) OR sheer note-count growth.
#
# Retuned (2500 tok / 40 lines -> 4500 / 100) against a real sharded store,
# which had outgrown the original calibration. The old numbers assumed a ~40-note
# index landing at 800-1200 tok. A sharded store's CORE holds the always-loaded
# working set, and at 80 notes that is arithmetically unreachable: the bullet +
# `[title](file.md)` link overhead alone is ~86 chars/line, so the links WITHOUT
# any summary text already cost ~1730 tok — 69% of the old 2500 ceiling. Hitting
# 2500 at 80 notes needs ~38-char summaries, which are not useful lines; even
# cutting every summary to an aggressive 70 chars still lands at ~3130.
#
# So a red `budget` was no longer evidence of bloat — it was evidence the store
# is 4x the size the ceiling was written for, and the only lever that could clear
# it was sharding the working set into JIT files where it would not be loaded by
# default. That trades a real cost (facts you need going unseen) for a number.
# 4500 / 100 keeps the tripwire live — it still fires if ~10 lines balloon back,
# or on roughly a dozen notes of growth — without demanding a fix that makes the
# store worse. Re-derive these if the core's note count changes materially.
DEFAULT_MAX_TOKENS = 4500
DEFAULT_MAX_LINES = 100
# Over-budget exit code for the schedulable check — distinct from a config/plan
# error (2) so an unattended wrapper alerts ONLY on genuine over-budget.
OVER_BUDGET_EXIT = 3

# Default topic taxonomy for the shard proposal. Ordered: first keyword hit on a
# note's filename stem wins; an unmatched note stays in the always-loaded core.
# `budget` and `shard` prefer an explicit --taxonomy, then the store's own
# shard-taxonomy.json; these defaults apply only when neither file is present.
# Brand-specific keywords belong in a user's own --taxonomy file; these
# defaults are generic category words that substring-match real note stems.
DEFAULT_SHARD_TAXONOMY = [
    ("fleet", "Fleet, orchestration & agentic maturity",
     ["fleet", "growth", "agentic", "harness", "model-pinning",
      "delegate", "orchestrate", "session-handoff", "last-stretch",
      "portfolio-case", "roadmap"]),
    ("infra", "Home-lab infrastructure, networking & hosting",
     ["infra", "router", "syslog", "docker", "vpn",
      "media", "nas", "backup", "server"]),
    ("automation", "Scheduled automations, pipelines & digests",
     ["ai-coding", "digest", "briefing", "podcast", "streaming",
      "feed", "reader", "broker", "scheduled", "watchlist",
      "condenser", "transcript"]),
    ("secrets", "Secrets, password manager, dotfiles & sync",
     ["op-", "oplock", "password", "secret", "sync", "dotfiles",
      "-tree"]),
]

# --- Per-file harness truncation budget (`filebudget`) ---------------------- #
# `budget`/`shard` above police MEMORY.md's own wholesale-load token cost (a
# JIT-sharding concern, tuned defaults). This is a DIFFERENT, harder ceiling:
# the Claude Code runtime itself silently truncates ANY memory file — not just
# MEMORY.md — behind a single WARNING line once it exceeds 200 lines OR 25,000
# bytes (the Claude Code runtime's per-file memory load limit). Content past the cap is unreachable, with no error. `filebudget`
# is the read-only tripwire for THAT ceiling, across every store.
FILE_BUDGET_MAX_LINES = 200
FILE_BUDGET_MAX_BYTES = 25000
# Warn before the file actually truncates, not after.
FILE_BUDGET_WARN_LINES = 180
# A distinct exit code for "approaching the ceiling but not over it yet" — kept
# apart from OVER_BUDGET_EXIT (3) so an unattended wrapper can tell "must fix"
# from "worth a look" apart, same way OVER_BUDGET_EXIT is kept apart from the
# config/plan-error code (2).
FILEBUDGET_WARN_EXIT = 4


# --------------------------------------------------------------------------- #
# Frontmatter: read with a minimal parser; WRITE by text splice (additive-only)
# --------------------------------------------------------------------------- #

def split_frontmatter(text):
    """Return (fm_lines, body) where fm_lines are the raw lines BETWEEN the two
    `---` fences (fences excluded), body is everything after the closing fence.
    If there is no frontmatter, fm_lines is None and body is the whole text."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\n") != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].rstrip("\n") == "---":
            return lines[1:i], "".join(lines[i + 1:])
    return None, text  # unterminated fence -> treat as no frontmatter


def read_fm_field(fm_lines, key):
    """Read a TOP-LEVEL scalar frontmatter field (unindented `key: value`).
    Minimal, read-only; strips surrounding quotes. Returns None if absent.

    A DOUBLE-quoted value is decoded with `json.loads`, not just quote-
    stripped: `description` (issue #177/#186) and each `supersedes`
    item are WRITTEN via `json.dumps`, so a value containing an escaped
    quote, backslash, or literal-newline escape must come back as the real
    string on read, or a write->scan round trip silently returns the raw
    escape sequences instead of the original text. Falls back to a bare
    quote-strip if the value merely LOOKS double-quoted but isn't valid JSON
    (e.g. a hand-edited file) — this is a read-only path, so a malformed
    value degrades gracefully instead of raising."""
    if fm_lines is None:
        return None
    pat = re.compile(r"^%s:\s*(.*)$" % re.escape(key))
    for ln in fm_lines:
        if ln and not ln[0].isspace():
            m = pat.match(ln.rstrip("\n"))
            if m:
                v = m.group(1).strip()
                if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                    try:
                        return json.loads(v)
                    except ValueError:
                        pass  # not actually valid JSON — fall through
                if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
                    v = v[1:-1]
                return v
    return None


def read_metadata_field(fm_lines, key):
    """Read `metadata:\\n  <key>: value` (one level of nesting). Read-only."""
    if fm_lines is None:
        return None
    in_meta = False
    pat = re.compile(r"^\s+%s:\s*(.*)$" % re.escape(key))
    for ln in fm_lines:
        stripped = ln.rstrip("\n")
        if stripped.startswith("metadata:"):
            in_meta = True
            continue
        if in_meta:
            if stripped and not stripped[0].isspace():
                break  # left the metadata block
            m = pat.match(stripped)
            if m:
                v = m.group(1).strip()
                if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
                    v = v[1:-1]
                return v
    return None


def _serialize_value(key, value):
    if key == "supersedes":
        if isinstance(value, str):
            value = [value]
        inner = ", ".join(json.dumps(v) for v in value)
        return "[%s]" % inner
    if key == "description":
        # Unlike status's fixed enum or last_verified's ISO date, description
        # is free-text model-authored prose (issue #177) that can
        # contain YAML-significant characters — ": ", leading "- "/"# ",
        # embedded quotes — that would corrupt a plain unquoted scalar or
        # silently change what a real YAML parser reads. JSON-quote it: a
        # JSON string is a valid YAML double-quoted scalar, the same quoting
        # `supersedes` already applies per-item above. This also guarantees a
        # SINGLE physical output line even if the input string itself
        # contains a literal newline (json.dumps escapes it to the two
        # characters backslash-n rather than a real line break), which is
        # what keeps this a byte-precise single-field splice on the WRITE
        # side regardless of what the plan supplies. ensure_ascii=False: the
        # real memory corpus writes em-dashes and other non-ASCII characters
        # literally (never `\uXXXX`), and escaping them would round-trip
        # wrong through this module's own minimal `read_fm_field` (which
        # strips surrounding quotes but does not decode JSON escapes).
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def apply_frontmatter(text, additions):
    """Return `text` with `additions` (dict, keys must be in WRITABLE_KEYS)
    spliced into the frontmatter additively. A key already present at top level
    is updated in place; a new key is appended before the closing fence. Any
    key outside WRITABLE_KEYS raises ValueError. Existing lines are otherwise
    byte-preserved (no reparse of name/metadata, or of description itself when
    a plan doesn't touch it).

    A key already present as a YAML BLOCK SCALAR (`key: >` or `key: |`,
    optionally with chomping/indent modifiers, e.g. SKILL.md's own
    `description: >`) is replaced whole, not just its first line: every
    indented-or-blank continuation line immediately following it (up to the
    next top-level key or the closing fence) is consumed along with it. Every
    key this tool has EVER written (status/last_verified/supersedes) is always
    a single-line scalar by construction, so this path was previously
    unreachable; `description` (issue #177) is free-text and a plan
    could in principle be replacing a value some other tool or a hand-edit
    left as a block scalar.

    Any OTHER multi-line shape — a quoted flow scalar folded across lines
    (`key: "part one\\n  part two"`), or a plain scalar continuing by
    indentation with no delimiter at all — raises ValueError instead of being
    guessed at (issue #186 review): a text splice cannot bound those
    unambiguously (escaped quotes/backslashes make even counting quote
    characters unreliable), so silently overwriting just the first line would
    orphan the rest as corrupt YAML. A plain single-line scalar (the only
    shape the other three keys ever take, and the ordinary case for
    `description` too) matches neither pattern and is replaced in place as
    before, with nothing extra consumed."""
    bad = [k for k in additions if k not in WRITABLE_KEYS]
    if bad:
        raise ValueError(
            "refusing to write non-additive frontmatter key(s): %s "
            "(only %s may be written)" % (bad, list(WRITABLE_KEYS)))
    if not additions:
        return text

    lines = text.splitlines(keepends=True)
    has_fm = bool(lines) and lines[0].rstrip("\n") == "---"
    close_idx = None
    if has_fm:
        for i in range(1, len(lines)):
            if lines[i].rstrip("\n") == "---":
                close_idx = i
                break
        if close_idx is None:
            has_fm = False

    if not has_fm:
        # No frontmatter at all — create a minimal block with the additions.
        block = ["---\n"]
        for k in WRITABLE_KEYS:
            if k in additions:
                block.append("%s: %s\n" % (k, _serialize_value(k, additions[k])))
        block.append("---\n")
        return "".join(block) + text

    fm_body = lines[1:close_idx]
    for k in WRITABLE_KEYS:
        if k not in additions:
            continue
        new_line = "%s: %s\n" % (k, _serialize_value(k, additions[k]))
        replaced = False
        for j, ln in enumerate(fm_body):
            if ln and not ln[0].isspace() and re.match(r"^%s:" % re.escape(k), ln):
                end = j + 1
                # Does the old value actually stop at this line, or does it
                # keep going (blank/indented lines immediately following,
                # up to the next top-level key or the fence)? Most values
                # this tool has ever written (status/last_verified/
                # supersedes, and the ordinary single-line description case)
                # have NOTHING following — end stays j+1 and the simple
                # single-line replace below is exactly as before.
                while end < len(fm_body) and (
                        fm_body[end] == "\n"
                        or (fm_body[end] and fm_body[end][0].isspace())):
                    end += 1
                if end > j + 1:
                    # The old value spans multiple lines. Only ONE shape is
                    # safe for a text splice to consume unambiguously: an
                    # explicit YAML BLOCK scalar (`key: >`/`key: |`, optional
                    # chomping "+"/"-" or an explicit indent digit) — by YAML
                    # rules every indented-or-blank line up to the next
                    # top-level key belongs to it, full stop. Anything else
                    # that merely LOOKS multi-line — a quoted FLOW scalar
                    # folded across lines (`key: "part one\n  part two"`,
                    # issue #186 review), or a plain scalar
                    # continuing by indentation with no delimiter at all —
                    # has no such unambiguous boundary without a real YAML
                    # parser: escaped quotes/backslashes make even counting
                    # quote characters unreliable. FAIL LOUD rather than
                    # guess — silently replacing only the first line would
                    # orphan the rest as corrupt YAML, on a tool whose whole
                    # design is refuse-over-corrupt (see e.g. the pointerize
                    # no-loss/drift guards elsewhere in this file).
                    if not re.match(r"^%s:\s*[|>][+\-0-9]*\s*$" % re.escape(k),
                                    ln.rstrip("\n")):
                        raise ValueError(
                            "REFUSING to rewrite %r: its existing value "
                            "spans multiple lines in a shape this "
                            "splice-based tool does not safely recognize "
                            "(not a YAML block scalar `>`/`|`) — only a "
                            "block scalar's continuation lines can be "
                            "consumed unambiguously by a text splice. "
                            "Silently replacing just the first line would "
                            "orphan the rest as corrupt YAML. Fix this "
                            "field by hand (or rewrite it as a `>` block "
                            "scalar) before re-running." % k)
                fm_body[j:end] = [new_line]
                replaced = True
                break
        if not replaced:
            fm_body.append(new_line)
    return "".join(lines[:1] + fm_body + lines[close_idx:])


# --------------------------------------------------------------------------- #
# Wikilinks & index
# --------------------------------------------------------------------------- #

WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")
# An index bullet: `- [title](target.md) — summary`. The title may itself
# contain a bracketed run — a real live line is
# ``- [`/model` rewrites the settings pin to `[1m]`](model-command-….md) — …``
# — so a flat `[^\]]*` title stops at the INNER `]`, then fails to find the
# `(` and matches nothing at all. That miss is silent and it fails OPEN, not
# closed: `rebuild_index` treats the unmatched line as "this note has no
# index line yet" and APPENDS a second bullet for the same note, leaving the
# stale one in place while reporting "1 line(s) updated" (observed on a
# real store). The same regex gates `parse_index`,
# `propose_shards` and `_is_shard_pointer_line`, so an unmatched line is
# also invisible to the sharder.
#
# `(?:[^\[\]]|\[[^\[\]]*\])*` accepts one level of nesting inside the title,
# which is as deep as markdown link titles go in practice. It deliberately
# CANNOT cross a `](` boundary, so a line whose SUMMARY carries a second
# markdown link (`… Studio-only dark → [studio-wifi-wedge](studio-wifi-wedge.md)`)
# still resolves to the FIRST link's target, not the last — a greedy
# match-to-the-final-`](` would silently re-key that line to the wrong note.
# Deeper nesting still fails to match; `_assert_no_duplicate_index_targets`
# is the backstop that turns any such miss into a loud refusal instead of a
# duplicated entry.
INDEX_LINE_RE = re.compile(r"^-\s*\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(([^)]+)\)")


def extract_wikilinks(text):
    return [m.group(1).strip() for m in WIKILINK_RE.finditer(text)]


def repair_wikilinks(text, repairs):
    """repairs: dict old_target -> new_target. Rewrites [[old]] and [[old|alias]]."""
    def sub(m):
        target = m.group(1).strip()
        if target in repairs:
            rest = m.group(0)[2:-2]
            if "|" in rest:
                _, alias = rest.split("|", 1)
                return "[[%s|%s]]" % (repairs[target], alias)
            return "[[%s]]" % repairs[target]
        return m.group(0)
    return WIKILINK_RE.sub(sub, text)


def _link_norm_key(s):
    """Canonical key for hyphen/underscore-insensitive wikilink matching:
    lowercase, underscores folded to hyphens. Used ONLY to SUGGEST a repair —
    never to auto-apply one, and never as a stand-in for a genuinely different
    name (a missing prefix, a different word); a look-alike that only shares a
    normalized form by coincidence still needs a human's eyes, not a silent fix."""
    return s.strip().lower().replace("_", "-")


def find_hyphen_underscore_mismatches(memory_dir):
    """Read-only. Suggest repairs for `[[wikilinks]]` that are dangling by
    EXACT match but would resolve to a live fact file IN THE SAME memory dir
    if hyphens/underscores were normalized — e.g. `[[feedback-self-review]]`
    when the file on disk is `feedback_self_review.md`. Scoped to one memory
    dir on purpose: wikilinks are expected to resolve within their own
    project's store, same as `dangling_links` above.

    This is a SUGGESTION generator, not a rewriter — it never touches disk and
    the model decides whether to fold a suggestion into a plan's
    `wikilink_repairs` (the existing apply-side mechanism; no new one needed).
    A target that merely shares a normalized key by coincidence (e.g. missing
    an entire prefix like `feedback_`/`project_`, or naming an entirely
    different, cross-store note) is NOT proposed here unless the normalized
    forms match exactly — see the module's `find_hyphen_underscore_mismatches`
    test coverage for real examples of both a genuine mismatch and a
    look-alike that must NOT be suggested."""
    files = list_fact_files(memory_dir)
    live_stems = {os.path.splitext(f)[0] for f in files}
    by_norm = {}
    for s in live_stems:
        by_norm.setdefault(_link_norm_key(s), []).append(s)

    suggestions = []
    seen = set()
    for name in files:
        text = _read(os.path.join(memory_dir, name))
        for tgt in extract_wikilinks(text):
            if tgt in live_stems:
                continue  # already resolves — not a mismatch
            candidates = sorted(s for s in by_norm.get(_link_norm_key(tgt), ())
                                if s != tgt)
            if not candidates:
                continue
            key = (name, tgt)
            if key in seen:
                continue
            seen.add(key)
            suggestions.append({
                "file": name,
                "target": tgt,
                "suggested": candidates,
                "ambiguous": len(candidates) > 1,
            })
    return suggestions


def parse_index(memory_md_text):
    """Return dict basename -> full index line (including leading '- ')."""
    out = {}
    for ln in memory_md_text.splitlines():
        m = INDEX_LINE_RE.match(ln)
        if m:
            target = os.path.basename(m.group(1))
            out[target] = ln
        # tolerate wrapped/continued lines by only keying the first match
    return out


def index_summary(index_line):
    """The prose after the ' — ' separator, or '' if none."""
    if " — " in index_line:
        return index_line.split(" — ", 1)[1].strip()
    return ""


def _assert_no_duplicate_index_targets(text, label):
    """Raise if `text` lists any note twice. The load-bearing backstop for the
    whole index-rebuild path: every way this module can go wrong on an index
    (a title shape `INDEX_LINE_RE` cannot parse, a plan entry whose home index
    was resolved wrongly, a shard-resident note handed to the core rebuild)
    converges on the SAME observable defect — `rebuild_index` appends a second
    bullet for a note that already has one, and reports success. Checking the
    produced text for duplicate targets catches all of those at once, and
    turns a silent duplication into a refusal before anything is written."""
    seen, dupes = set(), []
    for ln in text.splitlines():
        m = INDEX_LINE_RE.match(ln)
        if not m:
            continue
        target = os.path.basename(m.group(1))
        if target in seen and target not in dupes:
            dupes.append(target)
        seen.add(target)
    if dupes:
        raise ValueError(
            "REFUSING to write %s: it would list %s twice (%s). An index must "
            "carry at most one bullet per note — a duplicate here means a line "
            "was appended instead of replaced (an unparsable title shape, or a "
            "note whose index line lives in a different index file)."
            % (label, "note" if len(dupes) == 1 else "notes", ", ".join(dupes)))


def rebuild_index(memory_md_text, new_lines, drop):
    """new_lines: dict basename -> replacement full line (with leading '- ').
    drop: set of basenames whose index line should be removed (archived).
    Returns the rebuilt MEMORY.md text, preserving all non-index lines/spacing.
    A new_lines entry with NO existing index line is APPENDED after the last
    index line (or at EOF if the index has none) — never silently dropped."""
    out = []
    consumed = set()
    last_index_pos = None
    for ln in memory_md_text.splitlines(keepends=True):
        m = INDEX_LINE_RE.match(ln)
        if not m:
            out.append(ln)
            continue
        base = os.path.basename(m.group(1))
        if base in drop:
            continue
        if base in new_lines:
            nl = new_lines[base]
            if not nl.endswith("\n"):
                nl += "\n"
            out.append(nl)
            consumed.add(base)
        else:
            out.append(ln)
        last_index_pos = len(out)
    leftovers = [b for b in new_lines if b not in consumed]
    if leftovers:
        add = []
        for b in sorted(leftovers):
            nl = new_lines[b]
            if not nl.endswith("\n"):
                nl += "\n"
            add.append(nl)
        if last_index_pos is None:
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
            out.extend(add)
        else:
            out[last_index_pos:last_index_pos] = add
    return "".join(out)


# --------------------------------------------------------------------------- #
# Age / verification
# --------------------------------------------------------------------------- #

def _parse_date(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s.strip()[:10], fmt).date()
        except ValueError:
            continue
    return None


def age_info(path, fm_lines, today):
    """Return (age_days, source) where source is 'last_verified' or 'mtime'.
    age_days is None if neither is resolvable."""
    lv = _parse_date(read_fm_field(fm_lines, "last_verified"))
    if lv:
        return (today - lv).days, "last_verified"
    try:
        mtime = date.fromtimestamp(os.path.getmtime(path))
        return (today - mtime).days, "mtime"
    except OSError:
        return None, None


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #

def list_fact_files(memory_dir):
    """Individual fact-note `.md` files in memory_dir — excludes MEMORY.md (the
    core index) AND any `MEMORY.<topic>.md` topic-shard file (a shard is a
    sub-index, not a note itself; see SHARD_TARGET_RE)."""
    out = []
    for name in sorted(os.listdir(memory_dir)):
        if not name.endswith(".md") or name == "MEMORY.md":
            continue
        if SHARD_TARGET_RE.match(name):
            continue
        if os.path.isdir(os.path.join(memory_dir, name)):
            continue
        out.append(name)
    return out


def list_shard_files(memory_dir):
    """Topic-shard files (`MEMORY.<name>.md`) physically present in
    memory_dir, sorted. Existence-only — this is a plain inventory for
    budget's size reporting, NOT a correctness check of whether a shard is
    actually pointed to by MEMORY.md's core index (that check lives in
    _shard_indexed_notes, used by scan())."""
    out = []
    for name in sorted(os.listdir(memory_dir)):
        if not SHARD_TARGET_RE.match(name):
            continue
        if os.path.isdir(os.path.join(memory_dir, name)):
            continue
        out.append(name)
    return out


def _shard_indexed_homes(memory_dir, index):
    """basename -> the `MEMORY.<topic>.md` shard file whose index lists it.

    Same eligibility rule as `_shard_indexed_notes` (which now delegates
    here), but it keeps the shard file it found the note in instead of
    discarding it. That home is what any WRITE to a note's index line has to
    target: `apply_plan` used to rebuild `MEMORY.md` unconditionally, so
    handing it a shard-resident note appended a duplicate bullet to the core
    rather than updating the note's real line (reproduced on a real
    store). A note listed in more than one shard keeps the first by
    sorted shard order, so the mapping is deterministic; the duplicate itself
    is a pre-existing defect this function does not try to repair."""
    out = {}
    for basename in sorted(index):
        if not _is_shard_pointer_line(index[basename]):
            continue
        shard_path = os.path.join(memory_dir, basename)
        if not os.path.exists(shard_path):
            continue
        with open(shard_path, encoding="utf-8") as f:
            for note in parse_index(f.read()):
                out.setdefault(note, basename)
    return out


def _shard_indexed_notes(memory_dir, index):
    """Basenames of notes that count as indexed via a topic shard: the note's
    only listing lives inside a MEMORY.<topic>.md shard file, and that shard
    file is ITSELF pointed to by a JIT pointer bullet in `index` (MEMORY.md's
    own parsed index). A shard file that exists on disk but has no pointer
    line in the core index is an orphan — nothing would ever load it
    just-in-time, so notes inside it do NOT count as indexed. Read-only."""
    return set(_shard_indexed_homes(memory_dir, index))


def _index_home_lists(memory_dir, core):
    """Return every reachable index home for each note.

    Unlike `index_homes`, this intentionally preserves multiple homes so scan
    can report a note that is locally valid in each index but globally
    ambiguous. Core comes first; shards follow their sorted pointer order.
    """
    homes = {}
    for note in sorted(core):
        if not _is_shard_pointer_line(core[note]):
            homes.setdefault(note, []).append("MEMORY.md")
    for shard in sorted(core):
        if not _is_shard_pointer_line(core[shard]):
            continue
        shard_path = os.path.join(memory_dir, shard)
        if not os.path.exists(shard_path):
            continue
        with open(shard_path, encoding="utf-8") as f:
            shard_index = parse_index(f.read())
        for note in sorted(shard_index):
            homes.setdefault(note, []).append(shard)
    return homes


def index_homes(memory_dir):
    """basename -> the index file that owns that note's index line: either
    "MEMORY.md" or a `MEMORY.<topic>.md` shard. Notes listed in no index at
    all are absent from the mapping (callers treat those as core-bound, which
    is `rebuild_index`'s documented append-to-core behaviour for a brand-new
    entry). The core wins if a note is somehow listed in both. Read-only."""
    core = {}
    memory_md = os.path.join(memory_dir, "MEMORY.md")
    if os.path.exists(memory_md):
        with open(memory_md, encoding="utf-8") as f:
            core = parse_index(f.read())
    homes = {}
    for note, line in core.items():
        if not _is_shard_pointer_line(line):
            homes[note] = "MEMORY.md"
    for note, shard in _shard_indexed_homes(memory_dir, core).items():
        homes.setdefault(note, shard)
    return homes


def read_all_index_lines(memory_dir):
    """basename -> that note's full index line, wherever it lives (the core or
    a JIT-pointed shard). `scan` reports `index_summary_len` from this, so the
    `balloon_index` heuristic can see shard-resident notes at all: keying only
    off MEMORY.md reported a summary length of 0 for every one of them, which
    silently made every note outside the core immune to the index-sickness
    signal — and that covered every shard-resident note, including the ones
    with the WORST index lines. Read-only."""
    lines = {}
    core = {}
    memory_md = os.path.join(memory_dir, "MEMORY.md")
    if os.path.exists(memory_md):
        with open(memory_md, encoding="utf-8") as f:
            core = parse_index(f.read())
    lines.update(core)
    for note, shard in _shard_indexed_homes(memory_dir, core).items():
        if note in lines:
            continue
        with open(os.path.join(memory_dir, shard), encoding="utf-8") as f:
            shard_lines = parse_index(f.read())
        if note in shard_lines:
            lines[note] = shard_lines[note]
    return lines


def scan(memory_dir, today):
    memory_md = os.path.join(memory_dir, "MEMORY.md")
    index = {}
    if os.path.exists(memory_md):
        with open(memory_md, encoding="utf-8") as f:
            index = parse_index(f.read())
    home_lists = _index_home_lists(memory_dir, index)
    cross_index_duplicates = [
        {"file": note, "index_files": note_homes}
        for note, note_homes in sorted(home_lists.items())
        if len(note_homes) > 1
    ]
    shard_indexed = _shard_indexed_notes(memory_dir, index)
    # Index lines keyed wherever they actually live, plus each note's home
    # index file — so `index_summary_len` (and therefore `balloon_index`) sees
    # shard-resident notes, and a plan author can tell which file a proposed
    # `index_line` will be written to.
    all_index_lines = read_all_index_lines(memory_dir)
    homes = index_homes(memory_dir)

    files = list_fact_files(memory_dir)
    live_stems = {os.path.splitext(f)[0] for f in files}

    entries = []
    inbound = {}  # stem -> [referrers]
    for name in files:
        path = os.path.join(memory_dir, name)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        fm_lines, body = split_frontmatter(text)
        stem = os.path.splitext(name)[0]
        links = extract_wikilinks(text)
        for tgt in links:
            inbound.setdefault(tgt, [])
            if stem not in inbound[tgt]:
                inbound[tgt].append(stem)

        idx_line = all_index_lines.get(name, "")
        summ = index_summary(idx_line)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        age_days, age_src = age_info(path, fm_lines, today)
        low = text.lower()
        matched_markers = sorted({m for m in SAGA_MARKERS if m.lower() in low})

        entries.append({
            "file": name,
            "stem": stem,
            "name": read_fm_field(fm_lines, "name"),
            "description": read_fm_field(fm_lines, "description"),
            "type": read_metadata_field(fm_lines, "type"),
            "status": read_fm_field(fm_lines, "status"),
            "last_verified": read_fm_field(fm_lines, "last_verified"),
            "supersedes": read_fm_field(fm_lines, "supersedes"),
            "has_frontmatter": fm_lines is not None,
            "sha256": sha,
            "in_index": name in index or name in shard_indexed,
            "index_file": homes.get(name),
            "index_summary_len": len(summ),
            "wikilinks": links,
            "age_days": age_days,
            "age_source": age_src,
            "signals": {
                "balloon_index": len(summ) > INDEX_BALLOON_CHARS,
                "correction_chain": len(matched_markers) >= 2,
                "saga_markers": matched_markers,
                "unverified_over_90d": bool(age_days is not None
                                            and age_days > UNVERIFIED_DAYS),
                "missing_frontmatter_stamp": read_fm_field(fm_lines, "last_verified") is None,
            },
        })

    for e in entries:
        e["inbound_links"] = sorted(inbound.get(e["stem"], []))

    dangling = sorted({
        tgt for tgt, refs in inbound.items()
        if tgt not in live_stems
    })

    return {
        "memory_dir": memory_dir,
        "today": today.isoformat(),
        "file_count": len(files),
        "dangling_links": dangling,
        # Suggestion-only subset of dangling_links: a target that would
        # resolve if hyphens/underscores were normalized. Never auto-applied.
        "hyphen_underscore_repairs": find_hyphen_underscore_mismatches(memory_dir),
        "integrity": {
            "clean": not cross_index_duplicates,
            "cross_index_duplicates": cross_index_duplicates,
        },
        "files": entries,
    }


# --------------------------------------------------------------------------- #
# Plan -> proposed content (SHARED by report and apply — no re-derivation)
# --------------------------------------------------------------------------- #

def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def compute_new_content(entry, existing_text, today):
    """Given one plan entry and the file's current text, return the proposed new
    text (frontmatter + body). Deterministic and pure. Used identically by
    `report` (to diff) and `apply` (to write)."""
    fm_lines, body = split_frontmatter(existing_text)

    # Body: rewrite if provided; optionally append a changelog block.
    new_body = entry.get("body") if entry.get("body") is not None else body
    changelog = entry.get("changelog") or []
    if changelog:
        block = ["\n\n---\n\n### Changelog\n\n"]
        for line in changelog:
            block.append("- %s\n" % line)
        # Avoid double-appending if a Changelog heading is already present.
        if "### Changelog" not in new_body:
            new_body = new_body.rstrip("\n") + "".join(block)

    rebuilt = (("---\n" + "".join(fm_lines) + "---\n") if fm_lines is not None else "") + new_body

    # Frontmatter additive stamps.
    additions = dict(entry.get("frontmatter") or {})
    if entry.get("verdict") in ("UPDATE", "SUPERSEDED") and "last_verified" not in additions:
        additions["last_verified"] = today.isoformat()
    if additions:
        rebuilt = apply_frontmatter(rebuilt, additions)
    return rebuilt


# --------------------------------------------------------------------------- #
# Pointer-memory refactor (`pointerize` plan field)
# --------------------------------------------------------------------------- #
# Automates a manual sweep of the largest notes: move an oversized
# note's body to memory/docs/<stem>-full.md, leave a pointer at the file's
# ORIGINAL basename (so inbound [[wikilinks]] and index links need zero
# repair). The pointer TEXT itself is model-authored, same as any other `body`
# — this only mechanizes the file move + the hard destination guard.

def docs_target_path(memory_dir, stem):
    """Resolve a pointerize extraction target: memory_dir/docs/<stem>-full.md.

    HARD INVARIANT (unit-tested, not just commented): the resolved path must
    land strictly inside a REAL memory_dir/docs/ directory. Syncthing's
    .stignore negates only `!/projects/*/memory` for sync, so ANY destination
    outside the memory/ subtree reads as a DELETE on the peer machine. A stem
    containing a path separator or `..` component — crafted or accidental —
    must never be able to escape docs/; this is the single choke point every
    pointerize call goes through.

    What this guard actually guarantees (this docstring previously overclaimed
    "raises on any escape" — it did not cover every escape vector, see below):
    it raises ValueError if (a) the stem contains a path separator, (b) the
    docs/ directory COMPONENT ITSELF is a symlink, or (c) the fully-resolved
    target still falls outside the fully-resolved docs/ directory. (b) exists
    because (c) alone is not sufficient: if memory_dir/docs is a symlink to
    somewhere outside the memory tree, realpath() resolves BOTH target_real
    and docs_dir_real THROUGH that same symlink, so the commonpath check in
    (c) is vacuously satisfied — target_real and docs_dir_real agree on the
    external directory — even though the actual write lands entirely outside
    memory/. That is exactly the write-outside-memory/ case this guard exists
    to prevent, so checking os.path.islink() on the raw (pre-resolution) docs
    path is required, not optional.

    Deliberately NOT extended to memory_dir itself (or any ancestor above it):
    memory_dir is the caller-supplied root — already validated as a real
    directory by the caller before this function is ever invoked (e.g.
    discover_memory_stores()'s os.path.isdir gate, or an explicit --dir from
    the owner). This function's job is to guard only the ONE path segment it
    adds beyond that trusted root (the literal `docs` component, plus the
    caller-supplied stem, already checked above). If memory_dir itself were
    untrustworthy, no check inside this function could help — every other
    read/write in this module already trusts it completely, so re-checking it
    here would be a false sense of security, not real defense-in-depth.

    Note this check and the eventual write are not atomic (TOCTOU): a symlink
    swapped in between this call and the write it guards would not be caught.
    That race is out of scope here — it applies equally to nearly every
    path-then-open pattern in this file and is not the vacuous-check finding
    this guard closes."""
    if os.sep in stem or (os.altsep and os.altsep in stem):
        raise ValueError(
            "REFUSING pointerize stem with a path separator: %r — a memory "
            "file stem must not describe a subdirectory" % stem)
    docs_dir = os.path.join(memory_dir, DOCS_DIRNAME)
    if os.path.islink(docs_dir):
        raise ValueError(
            "REFUSING pointerize: %r is a symlink, not a real directory — "
            "resolving it and the target through the same symlink would let "
            "the outside-memory/docs/ escape check below pass vacuously "
            "while the write actually lands outside memory/ (which reads as "
            "a DELETE on the Syncthing peer). Replace it with a real "
            "directory before pointerizing." % docs_dir)
    docs_dir_real = os.path.realpath(docs_dir)
    target = os.path.join(docs_dir, "%s-full.md" % stem)
    target_real = os.path.realpath(target)
    if os.path.commonpath([target_real, docs_dir_real]) != docs_dir_real:
        raise ValueError(
            "REFUSING pointerize target outside memory/docs/: stem %r would "
            "resolve to %r, which escapes %r — Syncthing only un-ignores "
            "memory/, so any other destination reads as a DELETE on the peer"
            % (stem, target_real, docs_dir_real))
    return target


def compute_pointerize_output(entry, memory_dir):
    """Given a plan entry, return (docs_relpath, docs_content) for its
    `pointerize` block, or None if the entry has none. PURE — used identically
    by `report` (to preview "WOULD WRITE") and `apply` (to write), the same
    report/apply parity guarantee as `compute_new_content`. Goes through
    `docs_target_path` (raises on an escaping stem) before returning.

    This function does NOT check that `full_body` actually preserves the
    source's content — that is `check_pointerize_no_loss`, and the two are
    bound together in `compute_pointerize_output_checked`, which is what
    `report` and `apply` both call. Prefer the checked variant."""
    pz = entry.get("pointerize")
    if not pz:
        return None
    stem = os.path.splitext(entry["file"])[0]
    target = docs_target_path(memory_dir, stem)  # guarded; raises on escape
    content = pz["full_body"]
    if not content.endswith("\n"):
        content += "\n"
    return os.path.relpath(target, memory_dir), content


def _content_lines(text):
    """The comparable content units of a memory body: non-blank lines with
    their LEADING whitespace preserved exactly and only trailing whitespace
    normalized away. The pointerize analogue of `_index_bullets` — a multiset
    of THESE is what the pointerize no-loss invariant compares.

    Blank lines are dropped (re-flowing blank lines around a moved block is not
    content loss); everything else — prose, bullets, headings, code, table rows
    — counts, because in a fact note any of them can be the fact.

    LEADING whitespace is content (D6). It was previously stripped, and that
    let a whole class of loss through the invariant undetected: markdown
    indentation is meaning-bearing, so re-flowing

        - prod
            - password: hunter2
        - staging
            - password: swordfish

    into four top-level bullets kept every line's text while destroying which
    password belongs to which server, and de-indenting a line out of a fenced
    code block (`    return 1` -> `return 1`) silently broke the code it
    recorded. Both APPLIED cleanly under the stripped comparison. A pointerize
    MOVES a body verbatim, so demanding the indentation survive costs a correct
    plan nothing.

    TRAILING whitespace and the newline convention (CRLF vs LF, normalized by
    `splitlines`) are NOT content: they carry no fact, editors rewrite them
    silently, and refusing on them would be a false alarm on a correct move."""
    return [ln.rstrip() for ln in text.splitlines() if ln.strip()]


def check_pointerize_no_loss(original_text, pointer_text, full_body):
    """Assert that pointerizing loses NO memory content: every content line of
    the ORIGINAL body must still be present in {the new pointer stub} ∪ {the
    extracted full body}. Raises ValueError with a diagnostic on any loss —
    the pointerize twin of `check_no_loss`, and the reason `full_body` can no
    longer be trusted as self-evidently correct.

    Why this is needed at all: `full_body` is MODEL-AUTHORED prose written
    verbatim to docs/<stem>-full.md while the source is simultaneously
    overwritten with a short pointer stub. Nothing previously compared the two
    against the source, so a summary — or a hallucinated body — silently
    replaced the real note and the original was gone. A pointerize is a MOVE,
    not a rewrite: the body has to come out the other side.

    CONTAINMENT, not equality (this is the deliberate difference from
    `check_no_loss`, which demands an exact multiset match): the pointer stub
    is new prose by design, and the extracted body may gain a heading, so
    EXTRA lines are expected and fine. Only MISSING lines are a defect.

    Frontmatter is excluded from the comparison on both sides — it stays on
    the pointer file and is maintained additively by `apply_frontmatter`, not
    moved. PURE: reads nothing, writes nothing."""
    _fm, original_body = split_frontmatter(original_text)
    _pfm, pointer_body = split_frontmatter(pointer_text)

    original = _content_lines(original_body)
    survived = {}
    for ln in _content_lines(pointer_body) + _content_lines(full_body):
        survived[ln] = survived.get(ln, 0) + 1

    missing = []
    for ln in original:
        if survived.get(ln, 0) > 0:
            survived[ln] -= 1
        else:
            missing.append(ln)

    if missing:
        raise ValueError(
            "POINTERIZE NO-LOSS INVARIANT VIOLATED: %d of %d content line(s) "
            "from the original body appear in NEITHER the pointer stub nor "
            "the extracted full_body — writing NOTHING. A pointerize MOVES a "
            "body, it does not summarize one; `full_body` must carry the "
            "original content verbatim. Original body %d bytes -> pointer %d "
            "+ full_body %d. First missing line(s): %r"
            % (len(missing), len(original), len(original_body),
               len(pointer_body), len(full_body), missing[:5]))


def compute_pointerize_output_checked(entry, memory_dir, original_text,
                                      pointer_text):
    """`compute_pointerize_output` + the no-loss invariant, bound together so
    `report` and `apply` cannot drift apart on it. BOTH call this one — the
    same report/apply parity guarantee the destination guard and
    `compute_new_content` already have. Raises (writing nothing) on an
    escaping stem or on any lost content."""
    pz_out = compute_pointerize_output(entry, memory_dir)
    if pz_out is not None:
        check_pointerize_no_loss(original_text, pointer_text, pz_out[1])
    return pz_out


# Verdicts whose apply MUTATES the file (and therefore must carry a drift hash).
MUTATING_VERDICTS = ("UPDATE", "SUPERSEDED", "DEAD")


def file_sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _check_docs_dest_drift(pz, dest, memory_dir, f, require_hashes):
    """Drift guard on the pointerize DESTINATION, `docs/<stem>-full.md`.

    The plan's `sha256` guards the SOURCE against changing between report and
    apply. The destination had no such guard: `report` only noted whether the
    file existed, and `apply` then overwrote whatever was there — so a
    docs/<stem>-full.md that appeared, or changed, during the (potentially
    hours-long) report-review-apply gap was clobbered silently.

    Symmetric with the source guard, via `pointerize.docs_sha256`:
      * a provided hash must match the destination on disk (ALWAYS checked, in
        `report` as well as `apply` — same as the source guard);
      * the destination having VANISHED while the plan carries a hash is drift
        too, and refuses;
      * with require_hashes (i.e. `apply`), an EXISTING destination MUST carry
        one — that is the "it appeared after the report was reviewed" case,
        which is exactly the silent clobber, and it is unrepresentable without
        making presence itself require an acknowledgement.

    A destination that does not exist and has no hash is the ordinary
    first-time extraction: nothing to drift against, nothing to prove."""
    rel = os.path.relpath(dest, memory_dir)
    declared = pz.get("docs_sha256") if isinstance(pz, dict) else None
    exists = os.path.exists(dest)

    if declared is not None:
        if not exists:
            raise ValueError(
                "STALE PLAN: pointerize destination %s for %s carries a "
                "docs_sha256 but no longer exists on disk (it was deleted or "
                "moved since the plan was written) — re-scan, re-report"
                % (rel, f))
        actual = file_sha256(dest)
        if actual != declared:
            raise ValueError(
                "STALE PLAN: pointerize destination %s changed on disk since "
                "the plan was written (sha256 %s != expected %s) — applying "
                "would overwrite content nobody reviewed; re-report and "
                "re-copy docs_sha256" % (rel, actual, declared))
    elif exists and require_hashes:
        raise ValueError(
            "pointerize entry %s would OVERWRITE an existing %s but carries "
            "no docs_sha256 — apply refuses to clobber a destination the plan "
            "never acknowledged (it may have appeared after the report was "
            "reviewed). Copy the destination hash the report prints into "
            "pointerize.docs_sha256 to confirm the overwrite." % (f, rel))


def validate_plan(plan, memory_dir, require_hashes=False):
    """Raise ValueError on any structural problem. Returns the plan files list.

    Drift guard: a plan entry may carry `sha256` — the file's content hash as
    emitted by `scan` (copy it verbatim). Any provided hash that no longer
    matches the file on disk fails validation (the file changed between plan
    and now — re-scan, re-plan, re-report). With `require_hashes=True` (used by
    `apply`), every MUTATING verdict (UPDATE/SUPERSEDED/DEAD) MUST carry one:
    the report-review-apply gap can be hours, and apply must never silently
    overwrite content that changed after the report was reviewed."""
    if "files" not in plan or not isinstance(plan["files"], list):
        raise ValueError("plan must have a 'files' list")
    seen = set()
    for e in plan["files"]:
        f = e.get("file")
        if not f:
            raise ValueError("plan entry missing 'file'")
        if f in seen:
            raise ValueError("duplicate plan entry for %s" % f)
        seen.add(f)
        v = e.get("verdict")
        if v not in VALID_VERDICTS:
            raise ValueError("bad verdict %r for %s (allowed: %s)"
                             % (v, f, list(VALID_VERDICTS)))
        for k in (e.get("frontmatter") or {}):
            if k not in WRITABLE_KEYS:
                raise ValueError(
                    "plan for %s wants to write non-additive key %r "
                    "(only %s allowed)" % (f, k, list(WRITABLE_KEYS)))
        if v == "DEAD" and not (e.get("archive") or {}).get("reason"):
            raise ValueError("DEAD entry %s needs archive.reason (tombstone)" % f)
        pz = e.get("pointerize")
        if pz is not None:
            if v not in ("UPDATE", "SUPERSEDED"):
                raise ValueError(
                    "pointerize entry %s needs verdict UPDATE or SUPERSEDED "
                    "(got %r) — KEEP writes nothing and DEAD already has its "
                    "own archive move" % (f, v))
            full_body = pz.get("full_body") if isinstance(pz, dict) else None
            if not full_body or not isinstance(full_body, str):
                raise ValueError(
                    "pointerize entry %s needs a non-empty 'full_body' string" % f)
            # Fail fast on an escaping stem BEFORE any mutation (report and
            # apply both validate before writing anything).
            dest = docs_target_path(memory_dir, os.path.splitext(f)[0])
            _check_docs_dest_drift(pz, dest, memory_dir, f, require_hashes)
        path = os.path.join(memory_dir, f)
        if not os.path.exists(path):
            raise ValueError("plan references missing file %s" % path)
        sha = e.get("sha256")
        if sha is None and require_hashes and v in MUTATING_VERDICTS:
            raise ValueError(
                "plan entry %s (verdict %s) has no sha256 — apply refuses to "
                "mutate without the drift guard; copy sha256 from scan output"
                % (f, v))
        if sha is not None and sha != file_sha256(path):
            raise ValueError(
                "STALE PLAN: %s changed on disk since the plan was written "
                "(sha256 mismatch) — re-scan, update the plan, re-report" % f)
    return plan["files"]


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

VERDICT_ORDER = {v: i for i, v in enumerate(
    ["DEAD", "SUPERSEDED", "UPDATE", "UNVERIFIED>90d", "KEEP"])}


def _diff(old, new, path):
    if old == new:
        return None
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile="a/%s" % path, tofile="b/%s" % path))


def render_report(plan, memory_dir, today):
    entries = validate_plan(plan, memory_dir)
    memory_md = os.path.join(memory_dir, "MEMORY.md")
    # Read index lines from wherever they live and resolve each note's home
    # index file — the same two calls `apply_plan` makes, so the previewed
    # "before" line and the named destination file are the ones apply acts on.
    index = read_all_index_lines(memory_dir)
    homes = index_homes(memory_dir)

    rows = []
    sections = []
    index_changes = []
    for e in sorted(entries, key=lambda x: (VERDICT_ORDER.get(x["verdict"], 9), x["file"])):
        f = e["file"]
        path = os.path.join(memory_dir, f)
        old = _read(path)
        verdict = e["verdict"]

        if verdict == "DEAD":
            reason = e["archive"]["reason"]
            # SAME call as apply_plan — parity by construction, not coincidence.
            new = _inject_tombstone(old, reason, today)
            dest = "%s/%s" % (ARCHIVE_DIRNAME, f)
            diff = _diff(old, new, f)
            sections.append(_section(f, verdict, e.get("rationale", ""),
                                     diff, extra="Move: `%s` → `%s`" % (f, dest)))
            rows.append((verdict, f, "→ archive/ (%s)" % reason))
            index_changes.append((f, None))  # dropped
        else:
            new = compute_new_content(e, old, today)
            diff = _diff(old, new, f)
            note = "no file change" if diff is None else "frontmatter/body updated"
            # Same checked call `apply` makes — raises pre-write on an escaping
            # stem OR on lost content, so the report can never preview green
            # something apply would refuse (parity in the SAFE direction).
            pz_out = compute_pointerize_output_checked(e, memory_dir, old, new)
            if pz_out:
                note += " + pointerize"
            sections.append(_section(f, verdict, e.get("rationale", ""), diff))
            if pz_out:
                rel, content = pz_out
                dest_path = os.path.join(memory_dir, rel)
                exists = os.path.exists(dest_path)
                sections[-1] += (
                    "\n\n**Pointerize — extract full body to `%s`%s:**\n"
                    "```markdown\n%s\n```"
                    % (rel, " (OVERWRITES an existing file)" if exists else "",
                       content.rstrip("\n")))
                if exists:
                    # apply refuses to clobber a destination the plan never
                    # acknowledged — hand over the hash to acknowledge it with.
                    sections[-1] += (
                        "\n\nExisting `%s` sha256: `%s`\n"
                        "  (copy this into the entry's "
                        "`pointerize.docs_sha256` to confirm the overwrite; "
                        "`apply` refuses without it)"
                        % (rel, file_sha256(dest_path)))
            rows.append((verdict, f, note))
            if e.get("index_line"):
                old_idx = index.get(f, "(none)")
                new_idx = e["index_line"]
                if not new_idx.startswith("- "):
                    new_idx = "- " + new_idx
                index_changes.append((f, new_idx))
                home = homes.get(f, "MEMORY.md")
                sections[-1] += (
                    "\n\n**Index line** (in `%s`%s)**:**\n```diff\n- %s\n+ %s\n```"
                    % (home,
                       "" if f in homes else " — new entry, appended",
                       old_idx.lstrip("- ").rstrip(), new_idx.lstrip("- ").rstrip()))

    # Wikilink repair preview
    repairs = collect_repairs(entries)
    dangle_note = ""
    if repairs:
        dangle_note = "\n## Wikilink repairs\n\n" + "\n".join(
            "- `[[%s]]` → `[[%s]]`" % (o, n) for o, n in sorted(repairs.items()))

    out = []
    out.append("# Memory gardener — reconciliation report — %s\n" % today.isoformat())
    out.append("Memory dir: `%s`\n" % memory_dir)
    out.append("**This report IS the plan.** `apply` executes exactly these "
               "changes from the same plan JSON — nothing is re-derived.\n")
    out.append("## Summary\n")
    out.append("| Verdict | File | Change |")
    out.append("|---|---|---|")
    for verdict, f, note in rows:
        out.append("| %s | `%s` | %s |" % (verdict, f, note))
    counts = {}
    for verdict, _, _ in rows:
        counts[verdict] = counts.get(verdict, 0) + 1
    out.append("\n%s\n" % "  ·  ".join("%s: %d" % (k, counts[k])
                                       for k in sorted(counts)))
    if dangle_note:
        out.append(dangle_note)
    out.append("\n## Per-file detail\n")
    out.extend(sections)
    return "\n".join(out) + "\n"


def _section(f, verdict, rationale, diff, extra=None):
    s = ["### `%s` — **%s**\n" % (f, verdict)]
    if rationale:
        s.append(rationale + "\n")
    if extra:
        s.append(extra + "\n")
    if diff:
        s.append("```diff\n%s```" % diff)
    else:
        s.append("_No content change (flag only)._")
    return "\n".join(s)


def _inject_tombstone(text, reason, today):
    """Add status: archived + a tombstone line to frontmatter (additive)."""
    text = apply_frontmatter(text, {"status": "archived",
                                    "last_verified": today.isoformat()})
    # tombstone as an extra frontmatter comment line just below status.
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if re.match(r"^status:", ln):
            tomb = "# archived %s: %s\n" % (today.isoformat(), reason)
            lines.insert(i + 1, tomb)
            break
    return "".join(lines)


def collect_repairs(entries):
    repairs = {}
    for e in entries:
        for r in (e.get("wikilink_repairs") or []):
            repairs[r["from"]] = r["to"]
    return repairs


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #

POINTERIZE_BACKUP_INFIX = ".pre-pointerize-"


TMP_POINTERIZE_SUFFIX = ".tmp-pointerize"


def _reserve_pointerize_backup_path(archive_dir, stem, today):
    """RESERVE `archive/<stem>.pre-pointerize-<date>.md`, then `-2`, `-3`, …
    and return the reserved path. Same sacrosanct-backup rule as
    `_next_backup_path`: an existing backup is NEVER overwritten (that would
    destroy the only undo path); a repeat run claims the next free name.

    Reservation, not just selection (D7): the name is claimed with
    O_CREAT|O_EXCL, so it is ours from the moment we pick it. Merely testing
    `os.path.exists` and returning the name left a TOCTOU window — a same-day
    backup that appeared between the check and the `os.replace` was silently
    overwritten by it, breaking the never-overwrite invariant precisely when a
    second gardener was racing us and the undo path mattered most.

    The reservation is a 0-byte placeholder; the caller replaces it with the
    real content, or removes it if the commit never gets that far."""
    base = "%s%s%s" % (stem, POINTERIZE_BACKUP_INFIX, today.isoformat())
    i = 1
    while True:
        cand = os.path.join(
            archive_dir, ("%s.md" % base) if i == 1 else ("%s-%d.md" % (base, i)))
        try:
            fd = os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            i += 1
            continue
        os.close(fd)
        return cand


def _stage_sibling(final_path, text):
    """Stage `text` in a fresh, exclusively-created sibling of `final_path`, and
    return the temp path.

    `mkstemp` opens with O_CREAT|O_EXCL|O_WRONLY under a random name (D8). The
    previous fixed `final_path + ".tmp-pointerize"` name, opened with a
    truncating `open(tmp, "w")`, had three holes: it silently destroyed a stale
    temp left by a crashed run, two concurrent gardeners staged into the SAME
    file and interleaved each other's bytes, and — worst — the write FOLLOWED a
    symlink sitting at that predictable path, landing the extracted body
    OUTSIDE memory/ (reproduced: the target file outside the memory dir
    received the note's body). That is the very escape the `docs/` symlink
    guard above exists to prevent; a predictable temp name walked around it.
    O_EXCL closes all three: it never truncates, never collides, and never
    follows a symlink.

    The mode is matched to the file being replaced (mkstemp's 0600 would
    otherwise silently tighten a memory note's permissions on every commit)."""
    d = os.path.dirname(final_path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=TMP_POINTERIZE_SUFFIX,
                               prefix=os.path.basename(final_path) + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            mode = stat.S_IMODE(os.lstat(final_path).st_mode)
        except OSError:
            mode = 0o644
        os.chmod(tmp, mode)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return tmp


def _commit_pointerize(memory_dir, f, old, new, pz, pz_out, today):
    """Commit a pointerize ATOMICALLY and return its action lines.

    The defect this closes: the source was overwritten with the short pointer
    stub FIRST and docs/<stem>-full.md written SECOND, with no staging, no
    backup and no try/except. Any failure on the second write — docs/ present
    as a regular file (FileExistsError), a non-writable docs/ (PermissionError),
    a full disk — left the body destroyed and unrecoverable, having already
    reported the extraction as fine. The two writes are one logical move, so
    they now commit as one:

      1. the extraction directory is created FIRST, so a docs/ that cannot be
         created or written fails while the source is still fully intact;
      2. the pre-pointerize backup, the pointer stub and the extracted body are
         ALL staged to exclusively-created `.tmp-pointerize` siblings
         (`_stage_sibling`);
      3. only once every stage succeeded are they os.replace'd into place
         (atomic per-file on POSIX), SOURCE LAST;
      4. any failure removes the temps and re-raises.

    Commit ORDER is the safety property, because a multi-file commit cannot be
    made atomic on POSIX — `os.replace` is atomic per file, and nothing makes
    three of them one transaction. So the order is chosen to make every
    reachable partial state safe (D10), replacing the source LAST:

        backup  ->  destination  ->  source

    Any failure therefore leaves the source at its ORIGINAL content. The worst
    partial state is "an extra `archive/` backup and/or an extracted
    `docs/<stem>-full.md` exist, and the source was never touched" — additive,
    harmless, and idempotently retryable by re-running the same plan (the
    destination hash then guards the re-run). The previous order committed the
    source SECOND and the destination THIRD, so a failure on the third replace
    left the source pointerized at a destination that did not exist — the exact
    data-loss shape this function was written to prevent, reachable by a crash
    or SIGKILL between two replaces, and NOT recoverable by the cleanup path
    (it only removes temps; it cannot un-replace).

    The destination's drift hash is re-verified immediately before its replace
    (D9) — `validate_plan` checked it at PLAN time, which leaves a window in
    which a concurrent writer creates or edits the destination while we stage.
    Reproduced: content written during staging was clobbered silently.

    Never deletes anything except its own temps and its own unused reservation."""
    rel, content = pz_out
    path = os.path.join(memory_dir, f)
    dest_path = os.path.join(memory_dir, rel)

    # (1) Fail here, not after the source is gone: a docs/ that is a regular
    # file raises FileExistsError and a read-only parent raises PermissionError
    # while nothing at all has been written yet.
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    actions = []
    backup_path = backup_rel = None
    if new != old:
        # Only the destructive case needs an undo path: if the source text is
        # unchanged, nothing was moved out of it and there is nothing to
        # recover. The backup holds the TRUE pre-pointerize original.
        archive_dir = os.path.join(memory_dir, ARCHIVE_DIRNAME)
        os.makedirs(archive_dir, exist_ok=True)
        backup_path = _reserve_pointerize_backup_path(
            archive_dir, os.path.splitext(f)[0], today)
        backup_rel = os.path.relpath(backup_path, memory_dir)

    # (2) stage every output before committing any of it.
    staged = []          # [(final_path, tmp)] in COMMIT order
    committed = []
    try:
        if backup_path is not None:
            staged.append((backup_path, _stage_sibling(backup_path, old)))
        staged.append((dest_path, _stage_sibling(dest_path, content)))
        if backup_path is not None:
            staged.append((path, _stage_sibling(path, new)))

        # (3) commit, source last.
        for final_path, tmp in staged:
            if final_path == dest_path:
                # Close the stage-time window: the plan-time check cannot see a
                # destination that appeared while we were staging.
                _check_docs_dest_drift(pz, dest_path, memory_dir, f,
                                       require_hashes=True)
            os.replace(tmp, final_path)
            committed.append(final_path)
    except BaseException:
        for final_path, tmp in staged:
            if final_path in committed:
                continue
            try:
                os.remove(tmp)
            except OSError:
                pass
        # Release an unused reservation — a 0-byte placeholder we created and
        # never filled. Anything already committed is left alone (the source is
        # still original, so a committed backup is a redundant copy, not the
        # only one).
        if backup_path is not None and backup_path not in committed:
            try:
                if os.path.getsize(backup_path) == 0:
                    os.remove(backup_path)
            except OSError:
                pass
        raise

    if backup_rel:
        actions.append("backed up %s -> %s (pre-pointerize)" % (f, backup_rel))
        actions.append("updated %s" % f)
    actions.append("pointerized %s -> %s (full body extracted)" % (f, rel))
    return actions


def apply_plan(plan, memory_dir, today):
    entries = validate_plan(plan, memory_dir, require_hashes=True)
    archive_dir = os.path.join(memory_dir, ARCHIVE_DIRNAME)
    memory_md = os.path.join(memory_dir, "MEMORY.md")
    actions = []

    index_new_lines = {}
    index_drop = set()

    for e in entries:
        f = e["file"]
        path = os.path.join(memory_dir, f)
        old = _read(path)
        verdict = e["verdict"]

        if verdict == "DEAD":
            reason = e["archive"]["reason"]
            new = _inject_tombstone(old, reason, today)
            os.makedirs(archive_dir, exist_ok=True)
            dest = os.path.join(archive_dir, f)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)
            os.replace(path, dest)  # git detects the rename on commit
            actions.append("archived %s -> %s/%s (%s)"
                           % (f, ARCHIVE_DIRNAME, f, reason))
            index_drop.add(f)
        else:
            new = compute_new_content(e, old, today)
            # Escape guard + destination drift already fired in validate_plan;
            # the no-loss invariant fires HERE, still before any write.
            pz_out = compute_pointerize_output_checked(e, memory_dir, old, new)
            if pz_out:
                # The source rewrite and the docs extraction are ONE commit —
                # see _commit_pointerize. Never write the pointer stub first.
                actions.extend(
                    _commit_pointerize(memory_dir, f, old, new,
                                       e.get("pointerize"), pz_out, today))
            elif new != old:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(new)
                actions.append("updated %s" % f)
            if e.get("index_line"):
                nl = e["index_line"]
                if not nl.startswith("- "):
                    nl = "- " + nl
                index_new_lines[f] = nl

    # Rebuild each index file that OWNS one of the changed lines. This used to
    # rebuild MEMORY.md unconditionally, which was wrong for any note whose
    # line lives in a JIT shard: `rebuild_index` found no matching line in the
    # core, so it APPENDED one — leaving the shard's stale line untouched and
    # the note listed twice, while reporting "1 line(s) updated". A note with
    # no index line anywhere still lands in the core (rebuild_index's
    # documented never-silently-dropped behaviour for a new entry).
    rebuilt_shards = {}   # shard basename -> the bytes just written
    if index_new_lines or index_drop:
        homes = index_homes(memory_dir)
        by_file = {}
        for base, line in index_new_lines.items():
            by_file.setdefault(homes.get(base, "MEMORY.md"), {})[base] = line
        drop_by_file = {}
        for base in index_drop:
            drop_by_file.setdefault(homes.get(base, "MEMORY.md"), set()).add(base)
        for index_name in sorted(set(by_file) | set(drop_by_file)):
            index_path = os.path.join(memory_dir, index_name)
            if not os.path.exists(index_path):
                continue
            md = _read(index_path)
            updates = by_file.get(index_name, {})
            drops = drop_by_file.get(index_name, set())
            rebuilt = rebuild_index(md, updates, drops)
            # Refuse rather than write a duplicated listing (see the helper).
            _assert_no_duplicate_index_targets(rebuilt, index_name)
            if rebuilt != md:
                with open(index_path, "w", encoding="utf-8") as fh:
                    fh.write(rebuilt)
                actions.append("rebuilt %s index (%d line(s) updated, %d dropped)"
                               % (index_name, len(updates), len(drops)))
                if index_name != "MEMORY.md":
                    rebuilt_shards[index_name] = rebuilt

    # Rewriting a SHARD's index lines changes that shard's load cost, so the
    # core's JIT pointer for it is now stale. Nothing else refreshes it:
    # `propose_extend` only refreshes shards it moves bullets into. Left
    # unfixed, the always-loaded core advertises a cost the shards no longer
    # have (one trim overstated by 24%). Refresh here, from the bytes
    # actually written, so the two can never drift apart in this path again.
    if rebuilt_shards and os.path.exists(memory_md):
        core = _read(memory_md)
        updated = core
        for shard_name in sorted(rebuilt_shards):
            updated = refresh_shard_pointer(updated, shard_name,
                                            rebuilt_shards[shard_name])
        if updated != core:
            with open(memory_md, "w", encoding="utf-8") as fh:
                fh.write(updated)
            actions.append("refreshed %d JIT pointer line(s) in MEMORY.md"
                           % len(rebuilt_shards))

    # Wikilink repairs across ALL live files (+ MEMORY.md).
    repairs = collect_repairs(entries)
    if repairs:
        targets = [os.path.join(memory_dir, n) for n in list_fact_files(memory_dir)]
        if os.path.exists(memory_md):
            targets.append(memory_md)
        for tp in targets:
            txt = _read(tp)
            fixed = repair_wikilinks(txt, repairs)
            if fixed != txt:
                with open(tp, "w", encoding="utf-8") as fh:
                    fh.write(fixed)
                actions.append("repaired wikilinks in %s" % os.path.basename(tp))

    # Dangling-link warning after the fact.
    live = {os.path.splitext(n)[0] for n in list_fact_files(memory_dir)}
    warnings = []
    for n in list_fact_files(memory_dir):
        for tgt in extract_wikilinks(_read(os.path.join(memory_dir, n))):
            if tgt not in live and not tgt.startswith(ARCHIVE_DIRNAME + "/"):
                warnings.append("dangling [[%s]] in %s" % (tgt, n))

    return {"actions": actions, "warnings": sorted(set(warnings))}


# --------------------------------------------------------------------------- #
# budget — read-only index-load-cost guard + shard proposal (SCHEDULABLE)
# --------------------------------------------------------------------------- #
# Everything here is PURE / read-only over MEMORY.md. It measures the wholesale
# load cost, decides over/under a configurable ceiling, and — when over —
# proposes a topic-sharding split. It NEVER writes to the memory dir and has no
# --apply path: the restructure is executed by the `shard` sibling below (its
# writing counterpart), not by this check. That read-only-ness is what makes
# `budget` schedulable.

def estimate_tokens(text):
    """Approximate token count via the documented chars/4 heuristic. No tokenizer
    dependency by design. Rounds up so any non-empty text costs at least 1."""
    n = len(text)
    return (n + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def measure_text(text):
    """Size an index blob four ways (chars / bytes / lines / estimated tokens).

    `bytes` is what the harness's own truncation ceiling counts, and it is NOT
    interchangeable with `chars` here: these indexes are dense with multi-byte
    characters (em dashes, `·`), so char length undercounts a real index file
    by hundreds of bytes — enough to read as "inside the ceiling" while the
    file on disk is over it."""
    return {
        "chars": len(text),
        "bytes": len(text.encode("utf-8")),
        "lines": len(text.splitlines()),
        "est_tokens": estimate_tokens(text),
    }


def load_taxonomy(path):
    """Load a shard taxonomy override from JSON. Accepts either a bare list of
    shard objects or {"shards": [...]}, each {name, label?, keywords:[...]}.
    Returns the internal (name, label, [keyword,...]) tuple list.

    Validates defensively — every malformed-input shape is a clean ValueError
    (caller turns it into exit 2), never an uncaught KeyError/AttributeError:
    a missing "shards" key, a non-object shard entry, a duplicate shard name
    (which would otherwise silently collapse/duplicate shards downstream), and
    an empty-string keyword (which would substring-match every filename)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        shards = data.get("shards")
        if shards is None:
            raise ValueError("taxonomy object must have a 'shards' key")
    else:
        shards = data
    if not isinstance(shards, list) or not shards:
        raise ValueError("taxonomy must be a non-empty list of shard objects")
    out = []
    seen_names = set()
    for s in shards:
        if not isinstance(s, dict):
            raise ValueError("each shard must be a JSON object, got %r" % (s,))
        name = s.get("name")
        keys = s.get("keywords")
        if not name or not isinstance(name, str):
            raise ValueError("each shard needs a non-empty string 'name'")
        if not isinstance(keys, list) or not keys:
            raise ValueError("each shard needs a non-empty 'keywords' list")
        norm_keys = [str(k).strip().lower() for k in keys]
        if any(not k for k in norm_keys):
            raise ValueError(
                "shard %r has an empty keyword (would match every filename)" % name)
        if name in seen_names:
            raise ValueError("duplicate shard name %r in taxonomy" % name)
        seen_names.add(name)
        out.append((name, s.get("label", name), norm_keys))
    return out


def select_taxonomy(memory_dir, explicit_path=None):
    """Select the taxonomy for one memory store and identify its source.

    Precedence is deliberate: an explicit CLI override wins, otherwise the
    taxonomy shipped beside the store's MEMORY.md wins, and only a store with
    neither gets the built-in defaults. A present local file is loaded rather
    than probed for validity here, so malformed JSON/configuration raises and
    refuses the command instead of silently changing classification rules.
    """
    if explicit_path:
        path = os.path.expanduser(explicit_path)
        return load_taxonomy(path), "explicit --taxonomy: %s" % path

    local_path = os.path.join(memory_dir, "shard-taxonomy.json")
    if os.path.isfile(local_path):
        return load_taxonomy(local_path), "store-local: %s" % local_path

    return DEFAULT_SHARD_TAXONOMY, "built-in defaults"


def classify_shard(stem, taxonomy):
    """Return the shard name for a note's filename stem, or None for the core.
    The stem is split into tokens on `-`, `_` and `.`; the first keyword whose
    tokens appear as a contiguous run of the stem's tokens wins. A keyword
    never matches inside a longer word (`feed` is not in `breastfeed`)."""
    tokens = _shard_tokens(stem)
    for name, _label, keys in taxonomy:
        for k in keys:
            want = _shard_tokens(k)
            if not want:
                continue
            for i in range(len(tokens) - len(want) + 1):
                if tokens[i:i + len(want)] == want:
                    return name
    return None


def _shard_tokens(text):
    return [t for t in re.split(r"[-_.]", text.lower()) if t]


def _shard_file(name):
    return "MEMORY.%s.md" % name


def shard_file_body(label, lines):
    """Deterministic content for one `MEMORY.<name>.md` shard file: a one-line
    `# Memory — <label>` title then the shard's index bullet lines in original
    relative order. This is the SINGLE source of shard-file content — both the
    read-only `propose_shards` preview and the writing `execute_shard` build it
    here, so the dry-run is byte-identical to what gets written."""
    return "# Memory — %s\n\n" % label + "\n".join(lines) + "\n"


# Suffix stamped on every generated JIT pointer — human-readable prose only.
# NOT used for detection (see JIT_POINTER_SENTINEL below, issue #183
# finding 2) — a real note's own summary can legitimately contain this exact
# phrase, so it must never be what decides whether a line IS a pointer.
JIT_POINTER_SUFFIX = "load JIT when the topic comes up"
# issue #183 finding 2: _is_shard_pointer_line's second signal used to
# match ANY line containing JIT_POINTER_SUFFIX's literal text — so a REAL
# note whose own summary happened to contain that exact human-readable phrase
# was misclassified as a pointer, and silently excluded from the no-loss
# comparison (see check_extend_no_loss, which compares non-pointer bullets on
# both sides — a note excluded from BOTH sides can be silently dropped by an
# unrelated bug without the invariant ever firing). U+2063 INVISIBLE SEPARATOR
# is appended ONLY by `_shard_pointer` below. Renders as nothing (zero visual
# width) wherever the line is viewed.
#
# IMPORTANT (post-merge review follow-up, same finding): the sentinel alone
# does NOT make bare substring containment ("SENTINEL in ln") safe — a stray
# U+2063 is plausible in real note prose (PDF-extraction copy/paste, some
# ICU/office-doc pipelines emit it), so "contains this character anywhere"
# is the exact same class of bug as the original text-substring match, just
# against a different string. `_is_shard_pointer_line` therefore anchors on
# POSITION too: the sentinel must immediately follow JIT_POINTER_SUFFIX at
# the very END of the line — the exact, and only, shape `_shard_pointer`
# actually emits — never mere containment.
JIT_POINTER_SENTINEL = chr(0x2063)
# A shard FILE target is `MEMORY.<name>.md`. A bullet linking to one is a
# generated pointer, never a real note — used to keep pointers out of the note
# set and to detect a core that was already sharded. Structural (keys off the
# markdown LINK TARGET, not any human-readable text) — the first of
# _is_shard_pointer_line's two signals, unaffected by the finding above.
SHARD_TARGET_RE = re.compile(r"^MEMORY\.[^/]+\.md$")


def _shard_pointer(name, label, count, est_tokens):
    """One-line pointer added to the core index for a topic shard (JIT-loaded).
    Carries JIT_POINTER_SENTINEL (invisible) at the end — the machine-only
    signature _is_shard_pointer_line's second signal keys off."""
    return ("- [%s](%s) — %s · %d note%s · ~%d tok · %s%s"
            % (_shard_file(name), _shard_file(name), label, count,
               "" if count == 1 else "s", est_tokens, JIT_POINTER_SUFFIX,
               JIT_POINTER_SENTINEL))


# The `· N notes · ~T tok ·` stats segment inside a JIT pointer line. Matched
# on its own so a pointer's stats can be refreshed WITHOUT regenerating the
# whole line through `_shard_pointer` — see `refresh_shard_pointer`.
POINTER_STATS_RE = re.compile(r"· \d+ notes? · ~\d+ tok ·")


def shard_pointer_stats(shard_text):
    """(note_count, est_tokens) for a shard file's CURRENT on-disk content.
    Note count excludes JIT pointer bullets, exactly as `propose_shards` counts."""
    n = sum(1 for ln in shard_text.split("\n")
            if INDEX_LINE_RE.match(ln) and not _is_shard_pointer_line(ln))
    return n, estimate_tokens(shard_text)


def refresh_shard_pointer(core_text, shard_basename, shard_text):
    """Update `shard_basename`'s JIT pointer stats in the core index to match
    `shard_text`, returning the new core text (unchanged if there is nothing to do).

    Surgical by design: only the `· N notes · ~T tok ·` segment is rewritten, and
    the rest of the pointer line — including any hand-added trailing prose — stays
    byte-identical. Live pointers DO get augmented by hand (the fleet pointer
    carries a standing ADR/calibration reminder after the generated suffix), so
    regenerating the line through `_shard_pointer` would silently delete it.

    Why this exists: `propose_extend` refreshes a pointer only for a shard it MOVES
    bullets into, and its docstring is explicit that "an untouched shard's pointer
    line (and file) is left byte-identical". That is correct for extend, but it
    means NOTHING refreshed a pointer when a shard's size changed by any other
    path. `apply_plan` rebuilding a shard-resident note's index line is exactly
    such a path: one run trimmed 21 ballooned lines across three shards
    and left all three pointers advertising the pre-trim cost — the core claimed
    6912 tok of JIT shards against a real 5578, overstating by 24%, with the
    largest error (1155 tok) on a shard that extend would never touch.

    A pointer whose stats segment is missing (edited past recognition) is left
    alone rather than guessed at — a human owns that line.
    """
    if not core_text:
        return core_text
    out = []
    changed = False
    for ln in core_text.split("\n"):
        m = INDEX_LINE_RE.match(ln)
        if m and m.group(1) == shard_basename and _is_shard_pointer_line(ln):
            count, est = shard_pointer_stats(shard_text)
            new_ln = POINTER_STATS_RE.sub(
                "· %d note%s · ~%d tok ·" % (count, "" if count == 1 else "s", est),
                ln, count=1)
            if new_ln != ln:
                changed = True
            out.append(new_ln)
            continue
        out.append(ln)
    return "\n".join(out) if changed else core_text


def _is_shard_pointer_line(ln):
    """True if `ln` is a generated JIT shard pointer (a bullet linking to a
    `MEMORY.<name>.md` shard file, or ending in the generator's own trailing
    JIT_POINTER_SUFFIX+JIT_POINTER_SENTINEL shape) — NOT a real note. Two
    independent signals so a hand-tweaked pointer (edited enough to no
    longer link at a shard file, but with the trailing suffix+sentinel
    still intact) is still caught.

    The second signal checks POSITION, not mere containment
    (issue #183 finding 2 follow-up, post-merge review of the first
    attempt at this fix): matching on `JIT_POINTER_SENTINEL in ln` — ANYWHERE
    in the line — is still a bare text-content match, just against a
    different string than the original human-readable-suffix bug. A stray
    U+2063 is plausible in real note prose (PDF-extraction copy/paste, some
    ICU/office-doc pipelines emit it), and a note that happened to contain
    one anywhere would be misclassified exactly the same way the original
    bug misclassified a note containing JIT_POINTER_SUFFIX's text anywhere.
    Anchoring to `ln.endswith(JIT_POINTER_SUFFIX + JIT_POINTER_SENTINEL)` —
    the EXACT trailing shape `_shard_pointer` actually emits, verified
    against its real output, not assumed — requires the sentinel to
    immediately follow the suffix at the very end of the line; a sentinel
    anywhere else (mid-prose, adjacent to unrelated text) does not match."""
    m = INDEX_LINE_RE.match(ln)
    if not m:
        return False
    if SHARD_TARGET_RE.match(os.path.basename(m.group(1))):
        return True
    return ln.endswith(JIT_POINTER_SUFFIX + JIT_POINTER_SENTINEL)


def propose_shards(memory_md_text, taxonomy):
    """Group the MEMORY.md index bullet lines into topic shards. PURE + read-only:
    this produces a PROPOSAL (what the sharded layout WOULD look like), executing
    nothing.

    Preservation contract: every original line ends up somewhere in the
    proposal — in the header, in a shard's `lines`, or in the proposed core
    body (`core_body_lines`, which folds in BOTH core-kept index bullets AND
    any non-index content — headings, prose, comments, malformed bullets —
    that appears after the index starts, in original relative order). Nothing
    after the header is silently dropped; `non_index_lines_retained` reports
    the count of that non-bullet content so callers can assert no-loss."""
    header = []
    seen_index = False
    shard_lines = {name: [] for name, _, _ in taxonomy}
    core_body_lines = []
    core_kept_notes = 0
    for ln in memory_md_text.splitlines():
        m = INDEX_LINE_RE.match(ln)
        if not m:
            if not seen_index:
                header.append(ln)
            else:
                # Non-index content interleaved after the index starts (a
                # heading, prose, a comment, a malformed bullet) — retained
                # in the core body, in place, never dropped.
                core_body_lines.append(ln)
            continue
        seen_index = True
        if _is_shard_pointer_line(ln):
            # Belt-and-suspenders: a generated JIT pointer is NOT a note. Never
            # classify it into a shard (its stem `MEMORY.<topic>` would otherwise
            # keyword-match its own topic and get "sharded", discarding real
            # notes). Retain it in the core body, unclassified, uncounted.
            core_body_lines.append(ln)
            continue
        stem = os.path.splitext(os.path.basename(m.group(1)))[0]
        shard = classify_shard(stem, taxonomy)
        if shard is None:
            core_body_lines.append(ln)
            core_kept_notes += 1
        else:
            shard_lines[shard].append(ln)

    shards = []
    pointers = []
    for name, label, _ in taxonomy:
        lines = shard_lines[name]
        if not lines:
            continue
        # Each shard file: a small heading + its bullet lines.
        body = shard_file_body(label, lines)
        est = estimate_tokens(body)
        shards.append({
            "name": name,
            "label": label,
            "file": _shard_file(name),
            "count": len(lines),
            "est_tokens": est,
            "lines": lines,
        })
        pointers.append(_shard_pointer(name, label, len(lines), est))

    non_index_retained = sum(1 for ln in core_body_lines if not INDEX_LINE_RE.match(ln))

    # Proposed core index = original header + shard pointers + the retained
    # core body (core-kept index bullets AND any interleaved non-index content,
    # in original relative order — see the preservation contract above).
    core_parts = list(header)
    if core_parts and core_parts[-1].strip():
        core_parts.append("")
    core_parts.extend(pointers)
    if core_body_lines:
        core_parts.append("")
        core_parts.extend(core_body_lines)
    core_preview = "\n".join(core_parts).rstrip("\n") + "\n"

    return {
        "core_index_preview": core_preview,
        "core_measure": measure_text(core_preview),
        "core_kept_notes": core_kept_notes,
        "non_index_lines_retained": non_index_retained,
        "pointers": pointers,
        "shards": shards,
    }


def compute_budget(memory_dir, max_tokens, max_lines, taxonomy, today):
    """Read-only. Measure MEMORY.md's wholesale load cost and decide over/under
    the ceiling; when over, attach a shard proposal. Writes NOTHING.

    Two DISJOINT proposal shapes, chosen by whether the core is already
    sharded (issue #175): a NOT-yet-sharded core gets `proposal`
    (`propose_shards`'s fresh-split grouping, unchanged from before this
    fix) with `reshard_proposal` staying None. An ALREADY-sharded core gets
    `reshard_proposal` (`propose_extend`'s move-into-existing-shards
    proposal — the SAME one `shard --extend` itself would execute) with
    `proposal` staying None.

    `propose_shards` cannot be trusted on an already-sharded core: it has no
    notion that a shard file already exists on disk, so it would MINT a
    second, colliding pointer line for each touched shard carrying only the
    overflow note's count (not the shard's true total) — the "8 pointer
    lines, 4 duplicates" defect issue #175 reports, since JIT pointer lines
    ARE excluded from note-classification (`_is_shard_pointer_line`) but are
    NOT reconciled against a same-named freshly-proposed shard. Plain
    `shard --apply` (no `--extend`) also still refuses outright on an
    already-sharded core — see `_core_is_already_sharded` / `execute_shard`
    — so a `proposal` this check hands out in that case would dead-end at
    `--apply` time regardless; `reshard_proposal` names the mutating path
    that actually works (`shard --extend --apply`)."""
    if max_tokens <= 0 or max_lines <= 0:
        # A non-positive ceiling makes even an empty file "over budget" for no
        # meaningful reason — a config error, not a real over-budget signal.
        raise ValueError(
            "max_tokens/max_lines must be positive (got %r/%r)"
            % (max_tokens, max_lines))
    memory_md = os.path.join(memory_dir, "MEMORY.md")
    if not os.path.exists(memory_md):
        raise ValueError("no MEMORY.md in %s" % memory_dir)
    text = _read(memory_md)
    measure = measure_text(text)
    over_tokens = measure["est_tokens"] > max_tokens
    over_lines = measure["lines"] > max_lines
    over = over_tokens or over_lines
    already_sharded = _core_is_already_sharded(text)
    proposal = None
    reshard_proposal = None
    if over:
        if already_sharded:
            reshard_proposal = propose_extend(memory_dir, taxonomy, text=text)
        else:
            proposal = propose_shards(text, taxonomy)
    # Report-only inventory of shard files that ALREADY exist on disk, beside
    # the core's own measure. No new gating: the ceiling above still applies
    # only to MEMORY.md — a shard file can never make this "over budget".
    live_shards = [
        {"file": name, "measure": measure_text(_read(os.path.join(memory_dir, name)))}
        for name in list_shard_files(memory_dir)
    ]
    return {
        "memory_dir": memory_dir,
        "today": today.isoformat(),
        "chars_per_token": CHARS_PER_TOKEN,
        "measure": measure,
        "index_entries": len(parse_index(text)),
        "max_tokens": max_tokens,
        "max_lines": max_lines,
        "over_tokens": over_tokens,
        "over_lines": over_lines,
        "over_budget": over,
        "already_sharded": already_sharded,
        "proposal": proposal,
        "reshard_proposal": reshard_proposal,
        "live_shards": live_shards,
    }


def budget_alert_line(result):
    """The one-line, grep-friendly headline for a scheduled/alerting run."""
    m = result["measure"]
    if result["over_budget"]:
        why = []
        if result["over_tokens"]:
            why.append("%d tok > %d" % (m["est_tokens"], result["max_tokens"]))
        if result["over_lines"]:
            why.append("%d lines > %d" % (m["lines"], result["max_lines"]))
        # issue #175: an already-sharded core's sanctioned remedy is
        # `shard --extend` (moves overflow into an EXISTING shard); plain
        # `shard` (a fresh split) is the remedy only when it isn't sharded yet.
        remedy = ("run `shard --extend` to move overflow lines into an "
                  "existing shard" if result.get("already_sharded")
                  else "run the memory gardener to shard the index")
        return "MEMORY.md OVER BUDGET (%s) — %s." % ("; ".join(why), remedy)
    return ("MEMORY.md within budget (%d tok <= %d, %d lines <= %d)."
            % (m["est_tokens"], result["max_tokens"], m["lines"], result["max_lines"]))


def _render_reshard_proposal_section(result):
    """Render budget's guidance for an ALREADY-SHARDED, over-budget core
    (issue #175). The plain fresh-split proposal (`propose_shards`)
    is never shown here: it has no notion that the shard files already
    exist, so it would mint a SECOND, colliding pointer line per touched
    shard carrying the wrong (overflow-only) note count — the "8 pointer
    lines, 4 duplicates" defect the issue reports. Renders the
    `propose_extend` proposal instead (`result["reshard_proposal"]`) — the
    SAME one `shard --extend` itself executes, so this can never drift from
    what `--apply` actually does. Plain `shard --apply` (no `--extend`)
    still refuses unconditionally on an already-sharded core — `--force`
    does not override it — so the "next step" here names `--extend`, never
    plain `shard --apply`."""
    rp = result["reshard_proposal"]
    out = ["## Already sharded — re-shard via `shard --extend`\n"]
    out.append(
        "`MEMORY.md` already carries JIT shard pointers, so the plain "
        "fresh-split proposal does not apply here — it would mint a SECOND, "
        "colliding pointer for each shard that already exists, with the "
        "wrong (overflow-only) note count (issue #175). Plain "
        "`shard --apply` (no `--extend`) refuses outright on an "
        "already-sharded core; `--force` does not override that refusal. "
        "The sanctioned path back under the ceiling is `shard --extend`: it "
        "reads each EXISTING `MEMORY.<topic>.md` shard file back in and "
        "moves overflow core bullets into it, refreshing that shard's "
        "pointer note-count/token estimate from disk on every run.\n")
    if not rp["shards"]:
        out.append(
            "No overflow core bullet classifies to an EXISTING shard under "
            "the current taxonomy — there is nothing sanctioned to move "
            "either. (`shard --extend` never creates a new shard; growing "
            "the taxonomy, or building a new shard by hand, is outside what "
            "either check can propose.)\n")
        return "\n".join(out)

    out.append("| Shard | File | Moving in | Before | After | Post-move vs harness cap |")
    out.append("|---|---|---|---|---|---|")
    for s in rp["shards"]:
        out.append("| %s | `%s` | %d | %d tok | %d tok | %s |"
                   % (s["label"], s["file"], s["moved_count"],
                      s["old_measure"]["est_tokens"], s["new_measure"]["est_tokens"],
                      _format_ceiling(s)))
    out.append("")
    out.append(_ceiling_footnote(rp["shards"]))
    out.append("Total moved: **%d** line(s).\n" % rp["moved_total"])

    out.append("### Proposed core index (`MEMORY.md`)\n")
    out.append("```markdown")
    out.append(rp["new_core_text"].rstrip("\n"))
    out.append("```\n")
    for s in rp["shards"]:
        out.append("### `%s` — %s (moving in %d)\n" % (s["file"], s["label"], s["moved_count"]))
        out.append("```markdown")
        out.append(rp["new_shard_texts"][s["file"]].rstrip("\n"))
        out.append("```\n")

    out.append("_Next step: `shard --extend` (dry-run) to preview the exact "
               "files, then `shard --extend --apply --expect-sha256 <hash>` "
               "to execute the move. `--apply` stays supervised, "
               "interactive-only._\n")
    return "\n".join(out)


def render_budget_report(result):
    """Render the budget result as a markdown report. Read-only output."""
    m = result["measure"]
    out = []
    out.append("# Memory index — load-cost budget check — %s\n" % result["today"])
    out.append("Memory dir: `%s`\n" % result["memory_dir"])
    if result.get("taxonomy_source"):
        out.append("Taxonomy source: **%s**\n" % result["taxonomy_source"])
    out.append("> %s\n" % budget_alert_line(result))
    out.append("`MEMORY.md` is loaded **wholesale into every session**, so its size is a "
               "standing per-session token tax. Token cost is estimated with the "
               "chars/%d heuristic (no tokenizer dependency).\n" % result["chars_per_token"])
    out.append("## Current cost\n")
    out.append("| Metric | Value | Budget | Status |")
    out.append("|---|---|---|---|")
    out.append("| Est. tokens | %d | %d | %s |"
               % (m["est_tokens"], result["max_tokens"],
                  "OVER" if result["over_tokens"] else "ok"))
    out.append("| Lines | %d | %d | %s |"
               % (m["lines"], result["max_lines"],
                  "OVER" if result["over_lines"] else "ok"))
    out.append("| Chars | %d | — | — |" % m["chars"])
    out.append("| Index entries | %d | — | — |\n" % result["index_entries"])

    live_shards = result.get("live_shards") or []
    if live_shards:
        out.append("## Existing topic shards\n")
        out.append("Topic-shard files already present in `%s` — loaded JIT "
                   "(not wholesale), so their size is reported here for "
                   "visibility only; the ceiling above still applies to "
                   "`MEMORY.md` alone, never to these.\n" % result["memory_dir"])
        out.append("| Shard | Tokens | Lines |")
        out.append("|---|---|---|")
        for s in live_shards:
            out.append("| `%s` | %d | %d |"
                       % (s["file"], s["measure"]["est_tokens"], s["measure"]["lines"]))
        out.append("")

    if not result["over_budget"]:
        out.append("Index is within budget — no sharding proposed. "
                   "(This check is read-only; it never edits memory.)\n")
        return "\n".join(out) + "\n"

    if result.get("already_sharded"):
        # issue #175: never fall through to the fresh-split
        # rendering below on an already-sharded core — see
        # _render_reshard_proposal_section for why that proposal is unsafe
        # to show here (colliding, wrongly-counted pointer lines).
        out.append(_render_reshard_proposal_section(result))
        return "\n".join(out) + "\n"

    prop = result.get("proposal")
    if not prop:
        out.append("Index is within budget — no sharding proposed. "
                   "(This check is read-only; it never edits memory.)\n")
        return "\n".join(out) + "\n"

    cm = prop["core_measure"]
    out.append("## Proposed topic-sharding\n")
    out.append("Split `MEMORY.md` into a small always-loaded **core index** plus "
               "topic sub-indexes pulled **just-in-time**. Markdown only (grep/Explore "
               "displaces an embedding index). This is a PROPOSAL — this check writes "
               "nothing; the restructure is executed by the **supervised** "
               "`shard --apply` sibling (dry-run default, sha256-guarded).\n")
    out.append("| Shard | File | Notes | ~Tokens |")
    out.append("|---|---|---|---|")
    for s in prop["shards"]:
        out.append("| %s | `%s` | %d | %d |"
                   % (s["label"], s["file"], s["count"], s["est_tokens"]))
    out.append("| **Core index** | `MEMORY.md` | %d kept + %d pointers | %d |"
               % (prop["core_kept_notes"], len(prop["pointers"]), cm["est_tokens"]))
    out.append("")
    out.append("Always-loaded cost drops from **~%d tok** to **~%d tok** "
               "(topic shards load only when their subject comes up).\n"
               % (m["est_tokens"], cm["est_tokens"]))

    out.append("### Proposed core index (`MEMORY.md`)\n")
    out.append("Each shard gets a one-line JIT pointer:\n")
    out.append("```markdown")
    out.append(prop["core_index_preview"].rstrip("\n"))
    out.append("```\n")

    for s in prop["shards"]:
        out.append("### `%s` — %s (%d notes, ~%d tok, JIT)\n"
                   % (s["file"], s["label"], s["count"], s["est_tokens"]))
        out.append("```markdown")
        out.extend(s["lines"])
        out.append("```\n")

    out.append("_Next step: `shard` (dry-run) to preview the exact files, then "
               "`shard --apply --expect-sha256 <hash>` to execute this split. "
               "`--apply` stays supervised, interactive-only._\n")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# shard — EXECUTE the topic-split proposed (read-only) by `budget` (MUTATING)
# --------------------------------------------------------------------------- #
# `budget` PROPOSES a topic-sharding split but has no write path. `shard` is its
# writing sibling: dry-run by DEFAULT (prints exactly what WOULD be written,
# reusing propose_shards so the plan is identical to budget's), and `--apply`
# performs the split behind a sha256 drift guard, an on-disk backup (the memory
# dir is not git-tracked), a no-loss invariant, and a no-silent-clobber guard.

SHARD_BACKUP_PREFIX = "MEMORY.pre-shard-"


def _index_bullets(text):
    """The index bullet lines (`- [Title](file.md) — ...`) in a blob, in order.
    A multiset of these is what the no-loss invariant compares."""
    return [ln for ln in text.splitlines() if INDEX_LINE_RE.match(ln)]


def _shard_backup_name(today):
    return "%s%s.md" % (SHARD_BACKUP_PREFIX, today.isoformat())


def build_shard_layout(memory_dir, taxonomy, text=None):
    """Return (original_text, proposal, files) where `files` is an ordered list
    of (relpath, content) pairs that a split WOULD write: the new core
    `MEMORY.md` first, then each `MEMORY.<name>.md` shard. PURE. Reuses
    propose_shards so this is identical to budget's proposal.

    `text` lets a caller pass MEMORY.md's already-read bytes (as str) so the
    layout is built from the SAME bytes that were hashed — no re-read (the
    executor's TOCTOU-free path). When None, MEMORY.md is read from disk."""
    memory_md = os.path.join(memory_dir, "MEMORY.md")
    if text is None:
        if not os.path.exists(memory_md):
            raise ValueError("no MEMORY.md in %s" % memory_dir)
        text = _read(memory_md)
    prop = propose_shards(text, taxonomy)
    files = [("MEMORY.md", prop["core_index_preview"])]
    for s in prop["shards"]:
        files.append((s["file"], shard_file_body(s["label"], s["lines"])))
    return text, prop, files


def check_no_loss(original_text, prop, files):
    """Assert the multiset of index bullet lines across {new core + all shard
    files} exactly equals the multiset in the original MEMORY.md. The core's
    JIT pointer lines are NOT original bullets (they point at MEMORY.<name>.md,
    which never existed as notes), so they are excluded. Raises ValueError with
    a diagnostic on any mismatch — the load-bearing safety check that guarantees
    the split never drops, duplicates, or mangles a memory."""
    pointer_set = set(prop["pointers"])
    original = _index_bullets(original_text)
    produced = []
    for rel, content in files:
        for b in _index_bullets(content):
            if rel == "MEMORY.md" and b in pointer_set:
                continue  # a JIT pointer, not a carried-over note bullet
            produced.append(b)
    if sorted(original) != sorted(produced):
        orig_c = {}
        for b in original:
            orig_c[b] = orig_c.get(b, 0) + 1
        prod_c = {}
        for b in produced:
            prod_c[b] = prod_c.get(b, 0) + 1
        missing = [b for b in orig_c if orig_c[b] > prod_c.get(b, 0)]
        extra = [b for b in prod_c if prod_c[b] > orig_c.get(b, 0)]
        raise ValueError(
            "NO-LOSS INVARIANT VIOLATED: the sharded layout does not reproduce "
            "the original index bullets exactly — writing NOTHING. "
            "missing from shards+core: %r ; unexpected extra: %r"
            % (missing[:5], extra[:5]))


def render_shard_dryrun(memory_dir, taxonomy):
    """Read-only preview: the proposed core MEMORY.md and each shard file's full
    content plus a summary (per-shard counts, before/after token estimate, and
    the current MEMORY.md sha256 to copy into --expect-sha256). Writes nothing.

    Branches on `_core_is_already_sharded` exactly as `compute_budget` does
    (BQ9, BP8's D3 follow-up): `propose_shards`/`build_shard_layout` have no
    notion that the core is already sharded, so on an already-sharded store
    they would mint a fresh-split preview carrying colliding, wrongly-counted
    pointer lines for shards that already exist on disk (issue #175's
    own defect shape) — one `--apply` (no `--extend`) refuses outright
    anyway. Preview the `shard --extend` path instead, via the SAME
    `propose_extend` proposal `--extend --apply` itself would execute
    (`_render_extend_proposal_dryrun`, shared with `render_extend_dryrun` so
    the two can never drift apart)."""
    memory_md = os.path.join(memory_dir, "MEMORY.md")
    if not os.path.exists(memory_md):
        raise ValueError("no MEMORY.md in %s" % memory_dir)
    text = _read(memory_md)
    if _core_is_already_sharded(text):
        # codex round-2 (BQ9 follow-up): the classify-read above is ONLY used
        # to pick this branch — reused it here (paired with a SEPARATE
        # file_sha256() call) and a writer landing between the two calls
        # could make the returned sha describe different bytes than the
        # proposal shown. Fresh atomic read here instead, mirroring
        # render_extend_dryrun's own single-read discipline: both `text`
        # (fed to propose_extend) and `sha` (displayed) come from the SAME
        # `raw`, read once, before propose_extend is even called — so
        # nothing that happens during/after propose_extend can desync them.
        with open(memory_md, "rb") as fh:
            raw = fh.read()
        sha = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8")
        return _render_extend_proposal_dryrun(
            memory_dir, propose_extend(memory_dir, taxonomy, text=text), sha)

    text, prop, files = build_shard_layout(memory_dir, taxonomy, text=text)
    before_tok = estimate_tokens(text)
    after_tok = prop["core_measure"]["est_tokens"]

    out = []
    out.append("# Memory index — topic-shard DRY RUN (writes nothing)\n")
    out.append("Memory dir: `%s`" % memory_dir)
    out.append("Current `MEMORY.md` sha256: `%s`" % file_sha256(memory_md))
    out.append("  (copy this into `--expect-sha256` when you run `--apply`)\n")

    if not prop["shards"]:
        out.append("No shard matched any note under the current taxonomy — "
                   "nothing to split. (Every index bullet stays in the core.)")
        return "\n".join(out) + "\n"

    out.append("## Summary\n")
    out.append("| Shard | File | Notes | ~Tokens |")
    out.append("|---|---|---|---|")
    for s in prop["shards"]:
        out.append("| %s | `%s` | %d | %d |"
                   % (s["label"], s["file"], s["count"], s["est_tokens"]))
    out.append("| **Core index** | `MEMORY.md` | %d kept + %d pointers | %d |"
               % (prop["core_kept_notes"], len(prop["pointers"]), after_tok))
    out.append("")
    out.append("Always-loaded cost: **~%d tok → ~%d tok** "
               "(topic shards load JIT). `non_index_lines_retained` = %d.\n"
               % (before_tok, after_tok, prop["non_index_lines_retained"]))

    for rel, content in files:
        out.append("### WOULD WRITE `%s`\n" % rel)
        out.append("```markdown")
        out.append(content.rstrip("\n"))
        out.append("```\n")

    out.append("_Dry run only — nothing was written. Re-run with `--apply "
               "--expect-sha256 <hash above>` (supervised) to execute the split._")
    return "\n".join(out) + "\n"


def _claim_dated_path(directory, base, ext):
    """`<directory>/<base><ext>`, then `<base>-2<ext>`, `-3<ext>`, … — the first
    name not already taken. Same never-overwrite discipline as
    `_next_backup_path` / `_reserve_pointerize_backup_path`, generalized for any
    date-named output (the report). Returns a path; creates nothing."""
    cand = os.path.join(directory, "%s%s" % (base, ext))
    if not os.path.exists(cand):
        return cand
    i = 2
    while True:
        cand = os.path.join(directory, "%s-%d%s" % (base, i, ext))
        if not os.path.exists(cand):
            return cand
        i += 1


def _next_backup_path(archive_dir, today):
    """A pre-shard backup path that never collides: `MEMORY.pre-shard-<date>.md`,
    then `-2`, `-3`, … The backup is SACROSANCT — we never overwrite an existing
    one (that would destroy the only undo path), under any flag; a repeat run
    just claims the next free name."""
    base = _shard_backup_name(today)                 # MEMORY.pre-shard-<date>.md
    cand = os.path.join(archive_dir, base)
    if not os.path.exists(cand):
        return cand
    stem = base[:-len(".md")]
    i = 2
    while True:
        cand = os.path.join(archive_dir, "%s-%d.md" % (stem, i))
        if not os.path.exists(cand):
            return cand
        i += 1


def _core_is_already_sharded(text):
    """True if MEMORY.md already holds a generated JIT pointer line — i.e. it was
    sharded before. Re-sharding is OUT OF SCOPE (correct re-sharding must read
    the existing shard files back as part of 'the original'); `shard` refuses
    instead of silently discarding those shard files' notes."""
    return any(_is_shard_pointer_line(ln) for ln in text.splitlines())


def execute_shard(memory_dir, taxonomy, today, expect_sha256, force):
    """MUTATING. Execute the topic-split. Guards run in order, all refusing
    BEFORE any write; the backup, shards and core rewrite then commit, EVERY
    output staged to a temp sibling first, then os.replace'd into place in a
    fixed order — backup, then every shard, then the core LAST (issue
    #183 finding 1, BM10; see the commit-order comment beside `outputs` below
    for why core-last is the safety property, since a multi-file commit
    cannot be made atomic on POSIX):

      1. single read + drift — MEMORY.md read ONCE; its sha256 over those exact
         bytes must match --expect-sha256 (required; no blind-apply; no TOCTOU
         re-read between hashing and sharding);
      2. no re-shard — refuse an already-sharded core; NOT overridable by
         --force (restore from the pre-shard backup to redo);
      3. no-loss — {new core + shards} must reproduce every original bullet;
      4. no clobber — refuse pre-existing shard OUTPUT files unless --force;
         the backup is written to a fresh name and is NEVER overwritten.

    On any failure during staging the temp files are removed and the real dir is
    left byte-identical to the start. Never deletes anything."""
    memory_md = os.path.join(memory_dir, "MEMORY.md")
    if not os.path.exists(memory_md):
        raise ValueError("no MEMORY.md in %s" % memory_dir)

    # 1. Read MEMORY.md exactly ONCE; hash + build the layout from the SAME
    #    bytes (no re-read → no TOCTOU window between hashing and sharding).
    with open(memory_md, "rb") as fh:
        raw = fh.read()
    actual = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")

    if not expect_sha256:
        raise ValueError(
            "--apply requires --expect-sha256 <hex> (the MEMORY.md hash you "
            "reviewed in the dry run) — refusing to blind-apply the split")
    if actual != expect_sha256:
        raise ValueError(
            "STALE PLAN: MEMORY.md changed on disk since the dry run "
            "(sha256 %s != expected %s) — re-run the dry run, review, re-apply"
            % (actual, expect_sha256))

    # 2. Refuse re-sharding an already-sharded core. --force does NOT override.
    if _core_is_already_sharded(text):
        raise ValueError(
            "ALREADY SHARDED: MEMORY.md already contains JIT shard pointers — "
            "re-sharding is not supported (it would discard the existing shard "
            "files' notes). To re-shard, first restore MEMORY.md from its "
            "archive/MEMORY.pre-shard-*.md backup. --force cannot override this.")

    text, prop, files = build_shard_layout(memory_dir, taxonomy, text=text)
    if not prop["shards"]:
        raise ValueError(
            "no shard matched any note under this taxonomy — nothing to split")

    # 3. No-loss invariant (raises, writing nothing, on any mismatch).
    check_no_loss(text, prop, files)

    # 4. No silent clobber of shard OUTPUT files. The core rewrite of MEMORY.md
    #    is intentional; the backup is separate and never overwritten.
    clashes = [rel for rel, _c in files
               if rel != "MEMORY.md"
               and os.path.exists(os.path.join(memory_dir, rel))]
    if clashes and not force:
        raise ValueError(
            "refusing to overwrite existing shard file(s) %r — sharding is "
            "normally a one-time op; pass --force to overwrite" % clashes)

    before_tok = estimate_tokens(text)
    after_tok = prop["core_measure"]["est_tokens"]
    archive_dir = os.path.join(memory_dir, ARCHIVE_DIRNAME)
    os.makedirs(archive_dir, exist_ok=True)
    backup_path = _next_backup_path(archive_dir, today)   # never collides
    backup_rel = os.path.relpath(backup_path, memory_dir)

    # Atomic commit: stage EVERY output (backup + shards + new core) to a sibling
    # temp file; only once all temps are written do we os.replace them into place
    # (atomic per-file on POSIX). Any failure removes the temps, so the real dir
    # is left byte-identical to the start — no partial/duplicated state.
    #
    # COMMIT ORDER (issue #183 finding 1, BM10): the core MEMORY.md is
    # replaced LAST, after every shard file it points to. A multi-file commit
    # cannot be made atomic on POSIX — os.replace is atomic per file, and
    # nothing makes N of them one transaction — so, as with
    # _commit_pointerize above, the order is chosen to make every reachable
    # partial state safe. Replacing the core first (the prior order) meant a
    # crash between the core's replace and a shard's replace left the core
    # already claiming a note lives in a shard that did not yet hold (or
    # exist with) that note's bullet — the note unreachable through EITHER
    # file. Replacing the core last means any crash before it completes
    # leaves the OLD core in place, still holding every note's bullet
    # directly (sharding has not visibly happened yet) — the store is always
    # fully old or fully new, never a state with an orphaned note.
    #
    # Residual (codex second-review of BM10, PR #192, finding 2 — noted,
    # not fixed): a crash after some shard(s) commit but before the core
    # does leaves those shard files sitting on disk unreferenced by the
    # (still-old) core. This is NOT silent — the "no silent clobber" guard
    # (`clashes`, a few lines above this function's start) already refuses
    # a plain retry, naming those leftover files, and forces a human to
    # `--force` or clean up by hand before continuing. `execute_extend` has
    # no file-existence equivalent (its target shards always pre-exist by
    # definition) and so needed its own dedicated already-applied guard
    # instead — see that function's own commit-order comment and its
    # guard 4.
    core_output = None
    outputs = [(backup_path, text)]                       # backup = the original
    for rel, content in files:
        final_path = os.path.join(memory_dir, rel)
        if rel == "MEMORY.md":
            core_output = (final_path, content)
            continue
        outputs.append((final_path, content))
    outputs.append(core_output)                            # core LAST

    staged = []
    try:
        for final_path, content in outputs:
            tmp = final_path + ".tmp-shard"
            # issue #183 finding 4: secure create — O_EXCL|O_NOFOLLOW
            # refuses (raises) if anything already sits at `tmp` (a leftover
            # file or a planted symlink) rather than silently
            # following/truncating it, the same opener execute_extend
            # already uses for its own staging writes. Only append to
            # `staged` AFTER a successful create, so cleanup below never
            # removes a pre-existing path that wasn't ours to begin with.
            with open(tmp, "w", encoding="utf-8",
                      opener=_excl_nofollow_opener) as fh:
                fh.write(content)
            staged.append((final_path, tmp))
        for final_path, tmp in staged:
            os.replace(tmp, final_path)
    except BaseException:
        for _final, tmp in staged:
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise

    actions = ["backed up MEMORY.md -> %s" % backup_rel]
    for s in prop["shards"]:
        actions.append("wrote %s (%d note(s))" % (s["file"], s["count"]))
    actions.append("rewrote MEMORY.md as core index (%d kept + %d JIT pointers)"
                   % (prop["core_kept_notes"], len(prop["pointers"])))
    actions.append("est. always-loaded cost: ~%d tok -> ~%d tok"
                   % (before_tok, after_tok))
    return {"actions": actions, "before_tokens": before_tok,
            "after_tokens": after_tok, "backup": backup_rel}


# --------------------------------------------------------------------------- #
# shard --extend — sanctioned "move overflow lines into an EXISTING shard"
# mode (issue #175). A real incident needed this done by hand: 15
# overflow index lines had built back up in MEMORY.md's core after its topic
# shards were already created, and there was no sanctioned way to sweep them
# into those EXISTING shards short of a bespoke one-off script. `extend` is
# that sanctioned mechanism, built into the tool.
#
# Distinct precondition from `shard` above: `shard` REFUSES when the core is
# ALREADY sharded; `extend` REFUSES when it is NOT (run `shard --apply`
# first). Same dry-run-default / --apply-is-sha256-guarded shape as `shard`,
# reusing `_next_backup_path` for the pre-change core backup. `extend` never
# creates a NEW shard file — a taxonomy match against a shard with no
# existing MEMORY.<name>.md on disk is left untouched in the core; growing
# the taxonomy into a brand-new topic is `shard`'s job, not extend's.
# --------------------------------------------------------------------------- #

def _non_pointer_bullets(text):
    """Index bullet lines in `text` that are NOT a generated JIT shard
    pointer — i.e. real note bullets, in order. `_index_bullets` alone
    includes pointer lines; extend's no-loss check must exclude them (a
    touched shard's pointer TEXT is expected to change when its count is
    refreshed — that is not a note gain or loss)."""
    return [ln for ln in _index_bullets(text) if not _is_shard_pointer_line(ln)]


def _extend_ceiling(new_measure):
    """Judge a proposed post-move shard against the harness truncation ceiling
    `filebudget` polices (FILE_BUDGET_MAX_LINES / FILE_BUDGET_MAX_BYTES).

    Until this existed, FILE_BUDGET_MAX_BYTES was consulted by `filebudget`
    alone: `shard`/`--extend` would happily propose a move that put a
    destination shard over the cap, converting a cured core into a truncating
    shard — the same data loss, relocated into a JIT-loaded file where it is
    less likely to be noticed. Measured on a real store, an
    extend put MEMORY.fleet.md at 19,840 B / 72 notes: inside the
    cap, but with ~15 notes of headroom against ~1.25 fleet notes/day.

    Returns {status, headroom_bytes, headroom_lines, notes_headroom}, where
    status is "ok", "warn" (inside the cap but with little room left, using
    filebudget's own 180-line warn band plus a proportional byte band), or
    "over". `execute_extend` refuses on "over"; "warn" is surfaced, not
    blocked — a tight-but-legal shard is still a legitimate thing to write."""
    over = (new_measure["lines"] > FILE_BUDGET_MAX_LINES
            or new_measure["bytes"] > FILE_BUDGET_MAX_BYTES)
    headroom_bytes = FILE_BUDGET_MAX_BYTES - new_measure["bytes"]
    headroom_lines = FILE_BUDGET_MAX_LINES - new_measure["lines"]
    bullets = max(1, new_measure["lines"])
    mean_line_bytes = max(1.0, new_measure["bytes"] / float(bullets))
    warn = (not over) and (
        new_measure["lines"] >= FILE_BUDGET_WARN_LINES
        or new_measure["bytes"] >= FILE_BUDGET_MAX_BYTES
        * (FILE_BUDGET_WARN_LINES / float(FILE_BUDGET_MAX_LINES)))
    return {
        "status": "over" if over else ("warn" if warn else "ok"),
        "headroom_bytes": headroom_bytes,
        "headroom_lines": headroom_lines,
        # How many more index lines, at this shard's own current density, fit
        # before the ceiling — the number that actually tells you whether the
        # move buys real runway or just defers the truncation.
        "notes_headroom": int(max(0, headroom_bytes) // mean_line_bytes),
    }


def _format_ceiling(shard):
    """One table cell describing a proposed destination shard against the
    harness truncation ceiling — bytes used, and how many more index lines fit
    at that shard's own line density. The notes-headroom figure is the point:
    "19,840 B" reads as comfortable, "15 more notes" against a shard that
    gains ~1 note a day does not."""
    c = shard["ceiling"]
    mark = {"ok": "", "warn": "WARN ", "over": "OVER "}[c["status"]]
    return ("%s%d/%d B · room for ~%d more note%s"
            % (mark, shard["new_measure"]["bytes"], FILE_BUDGET_MAX_BYTES,
               c["notes_headroom"], "" if c["notes_headroom"] == 1 else "s"))


def _ceiling_footnote(shards):
    """Explain the ceiling column, and say plainly when a proposal is refused.
    A dry run that prints a clean-looking table for a move `--apply` will
    reject is the report/apply parity failure this whole module is built to
    avoid, so the refusal is stated here, in the preview."""
    over = [s for s in shards if s["ceiling"]["status"] == "over"]
    warn = [s for s in shards if s["ceiling"]["status"] == "warn"]
    note = ("The harness truncates ANY memory file past %d lines / %d bytes, "
            "silently — a shard is not exempt, so a move that fixes MEMORY.md "
            "by overflowing a shard has only relocated the loss into a "
            "JIT-loaded file.\n" % (FILE_BUDGET_MAX_LINES, FILE_BUDGET_MAX_BYTES))
    if over:
        note += ("\n**`--apply` WILL REFUSE this proposal**: %s would land over "
                 "the ceiling. Trim that shard's index lines, or split the "
                 "taxonomy so the overflow spreads across more shards, then "
                 "re-run.\n" % ", ".join("`%s`" % s["file"] for s in over))
    elif warn:
        note += ("\nHeads-up: %s would land inside the ceiling but with little "
                 "room left. The move is allowed; plan a trim before the next "
                 "extend.\n" % ", ".join("`%s`" % s["file"] for s in warn))
    return note


def propose_extend(memory_dir, taxonomy, text=None, shard_texts=None):
    """PURE + read-only. Given an ALREADY-SHARDED core, find overflow index
    bullets sitting directly in MEMORY.md that classify (via the same
    classify_shard taxonomy matcher propose_shards already uses) to a shard
    that ALREADY EXISTS on disk, and propose moving each into that existing
    shard file. Never proposes creating a new shard — a match against a
    taxonomy shard with no corresponding MEMORY.<name>.md file on disk is
    left in the core, untouched.

    Each touched shard's JIT pointer line in the core is refreshed (new note
    count + token estimate) so the core index stays accurate after the move;
    an untouched shard's pointer line (and file) is left byte-identical.

    `text` lets a caller pass MEMORY.md's already-read bytes (as str) so the
    proposal is built from the SAME bytes that were hashed — no re-read.
    `shard_texts` similarly lets a caller pass already-read shard file
    contents (basename -> text) so a shard already read once (e.g. for the
    no-loss check) is never re-read; any shard file not present in this dict
    is read from disk as usual.

    Returns a dict:
      already_sharded  — False if MEMORY.md has no JIT pointers yet (nothing
                          for extend to do; `shard --apply` runs first).
      core_text        — the original core text, as read.
      new_core_text    — the full proposed new MEMORY.md content (identical
                          to core_text when there is nothing to move).
      shards           — one entry per shard that gains lines: name, label,
                          file, moved_count, lines (the moved bullets, in
                          original order), already_applied (see below),
                          old_measure, new_measure.
      (per-shard) already_applied — the subset of `lines` already present
                          verbatim among the shard's on-disk bullets before
                          this proposal (see the `already_applied`
                          assignment below for why: a crash between a prior
                          run's shard-commit and its core-commit). Non-empty
                          ONLY on a partial-crash retry; `execute_extend`
                          refuses to write when any shard has one rather
                          than silently duplicating the bullet.
      new_shard_texts  — dict shard-file -> its full proposed new content
                          (only for shards that actually gain lines).
      moved_total      — total bullets proposed to move, across all shards.
    Reads memory_dir's files but writes nothing."""
    memory_md = os.path.join(memory_dir, "MEMORY.md")
    if text is None:
        if not os.path.exists(memory_md):
            raise ValueError("no MEMORY.md in %s" % memory_dir)
        text = _read(memory_md)

    already_sharded = _core_is_already_sharded(text)
    disk_shards = set(list_shard_files(memory_dir))
    existing = {name: label for name, label, _ in taxonomy
                if _shard_file(name) in disk_shards}

    moves = {}              # shard name -> [bullet line, ...] in core order
    move_basenames = set()  # note basenames whose core bullet is moving out
    if already_sharded:
        for ln in text.splitlines():
            m = INDEX_LINE_RE.match(ln)
            if not m or _is_shard_pointer_line(ln):
                continue
            stem = os.path.splitext(os.path.basename(m.group(1)))[0]
            shard = classify_shard(stem, taxonomy)
            if shard is not None and shard in existing:
                moves.setdefault(shard, []).append(ln)
                move_basenames.add(os.path.basename(m.group(1)))

    shards_out = []
    new_pointer_lines = {}   # shard file (basename) -> refreshed pointer line
    new_shard_texts = {}
    for name, lines in moves.items():
        label = existing[name]
        shard_file = _shard_file(name)
        if shard_texts is not None and shard_file in shard_texts:
            old_text = shard_texts[shard_file]
        else:
            old_text = _read(os.path.join(memory_dir, shard_file))
        # already_applied (issue #183 finding 1 follow-up, codex
        # second-review of BM10): bullets in `lines` that are ALREADY present
        # verbatim among the shard's own on-disk bullets. A crash landing
        # between a prior run's shard commit and its core commit leaves
        # exactly this shape (the shard already holds the moved bullet, but
        # MEMORY.md was never updated to drop it, so the next read still
        # proposes moving it again) — appending unconditionally here would
        # silently duplicate the note; execute_extend refuses instead of
        # writing when this is non-empty (see its guard). Empty in the
        # ordinary, non-retry case.
        old_bullets = _non_pointer_bullets(old_text)
        already_applied = [ln for ln in lines if ln in old_bullets]
        new_text = old_text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
        new_count = len(_index_bullets(old_text)) + len(lines)
        new_tokens = estimate_tokens(new_text)
        new_pointer_lines[shard_file] = _shard_pointer(name, label, new_count, new_tokens)
        new_shard_texts[shard_file] = new_text
        new_measure = measure_text(new_text)
        shards_out.append({
            "name": name,
            "label": label,
            "file": shard_file,
            "moved_count": len(lines),
            "lines": lines,
            "already_applied": already_applied,
            "old_measure": measure_text(old_text),
            "new_measure": new_measure,
            # Would this move push the DESTINATION over the harness's own
            # truncation ceiling? A shard is a memory file like any other, so
            # the 200-line/25,000-byte cap applies to it exactly as it does to
            # MEMORY.md — and sharding is worthless if it cures the core by
            # silently truncating the shard instead. See _extend_ceiling.
            "ceiling": _extend_ceiling(new_measure),
        })
    shards_out.sort(key=lambda s: s["name"])

    # Core rewrite reuses rebuild_index: drop the moved bullets (keyed by
    # their note's basename) and swap in the refreshed pointer line for each
    # touched shard, preserving every other line's exact text/spacing.
    new_core_text = (rebuild_index(text, new_pointer_lines, move_basenames)
                     if moves else text)

    return {
        "memory_dir": memory_dir,
        "already_sharded": already_sharded,
        "core_text": text,
        "new_core_text": new_core_text,
        "shards": shards_out,
        "new_shard_texts": new_shard_texts,
        "moved_total": sum(s["moved_count"] for s in shards_out),
    }


def check_extend_no_loss(original_pairs, produced_pairs):
    """Assert every NON-POINTER note bullet across {core + every existing
    shard file on disk}, BEFORE the extend, still exists somewhere across
    {new core + every shard file}, AFTER — the adapted no-loss invariant for
    'move a line from the core into an EXISTING shard' (check_no_loss's
    sibling, which instead covers 'split the core into brand-new shards').

    `original_pairs`/`produced_pairs`: ordered (label, text) pairs — the core
    plus EVERY shard file physically present on disk, before and after.
    Pointer lines are excluded from both sides (a touched shard's pointer
    TEXT is an expected byproduct of refreshing its count, never a note
    gain/loss). An untouched shard's pair is byte-identical text on both
    sides, which trivially satisfies the invariant AND proves nothing else
    was touched. Raises ValueError with a diagnostic on any mismatch,
    writing nothing."""
    original = []
    for _label, blob in original_pairs:
        original.extend(_non_pointer_bullets(blob))
    produced = []
    for _label, blob in produced_pairs:
        produced.extend(_non_pointer_bullets(blob))
    if sorted(original) != sorted(produced):
        orig_c = {}
        for b in original:
            orig_c[b] = orig_c.get(b, 0) + 1
        prod_c = {}
        for b in produced:
            prod_c[b] = prod_c.get(b, 0) + 1
        missing = [b for b in orig_c if orig_c[b] > prod_c.get(b, 0)]
        extra = [b for b in prod_c if prod_c[b] > orig_c.get(b, 0)]
        raise ValueError(
            "NO-LOSS INVARIANT VIOLATED: the extend does not preserve every "
            "original note bullet across the core + existing shards — "
            "writing NOTHING. missing: %r ; unexpected extra: %r"
            % (missing[:5], extra[:5]))


def _render_extend_proposal_dryrun(memory_dir, prop, sha):
    """Shared rendering body for the `shard --extend` proposal preview.
    Takes an already-built `prop` (propose_extend's output) and the caller's
    own `sha` so this never reads MEMORY.md itself — used both by
    `render_extend_dryrun`'s own direct entry point and by
    `render_shard_dryrun`'s already-sharded branch (BQ9, BP8's D3
    follow-up), so the plain `shard` dry-run and the `shard --extend` dry-run
    can never print two different previews for the same already-sharded
    store."""
    out = []
    out.append("# Memory index — shard EXTEND dry run (writes nothing)\n")
    out.append("Memory dir: `%s`" % memory_dir)

    if not prop["already_sharded"]:
        out.append("\nMEMORY.md is not sharded yet — `extend` only operates on "
                   "an ALREADY-sharded core. Run `shard --apply` first if you "
                   "want to split it; nothing to do here.")
        return "\n".join(out) + "\n"

    out.append("Current `MEMORY.md` sha256: `%s`" % sha)
    out.append("  (copy this into `--expect-sha256` when you run `--apply`)\n")

    if not prop["shards"]:
        out.append("No overflow core bullet classifies to an EXISTING shard "
                   "under the current taxonomy — nothing to move. (`extend` "
                   "never creates a new shard; that's `shard`'s job.)")
        return "\n".join(out) + "\n"

    out.append("## Summary\n")
    out.append("| Shard | File | Moving in | Before | After | Post-move vs harness cap |")
    out.append("|---|---|---|---|---|---|")
    for s in prop["shards"]:
        out.append("| %s | `%s` | %d | %d tok | %d tok | %s |"
                   % (s["label"], s["file"], s["moved_count"],
                      s["old_measure"]["est_tokens"], s["new_measure"]["est_tokens"],
                      _format_ceiling(s)))
    out.append("")
    out.append(_ceiling_footnote(prop["shards"]))
    out.append("Total moved: **%d** line(s). The core is never re-split — only "
               "the moved bullets leave it; every other core line (including "
               "unmatched notes and untouched shard pointers) is untouched.\n"
               % prop["moved_total"])

    out.append("### WOULD WRITE `MEMORY.md` (core)\n")
    out.append("```markdown")
    out.append(prop["new_core_text"].rstrip("\n"))
    out.append("```\n")
    for s in prop["shards"]:
        out.append("### WOULD WRITE `%s`\n" % s["file"])
        out.append("```markdown")
        out.append(prop["new_shard_texts"][s["file"]].rstrip("\n"))
        out.append("```\n")

    out.append("_Dry run only — nothing was written. Re-run with `shard --extend "
               "--apply --expect-sha256 <hash above>` (supervised) to execute._")
    return "\n".join(out) + "\n"


def render_extend_dryrun(memory_dir, taxonomy):
    """Read-only preview: which overflow core bullets WOULD move into which
    existing shard, the resulting core + shard file contents, and the
    current MEMORY.md sha256 to copy into --expect-sha256. Writes nothing.

    MEMORY.md is read ONCE as bytes; both the displayed sha256 AND the
    rendered proposal are derived from that SAME read — never two separate
    reads that could observe different content if the file changed between
    them (mirrors execute_extend's own single-read discipline)."""
    memory_md = os.path.join(memory_dir, "MEMORY.md")
    if not os.path.exists(memory_md):
        raise ValueError("no MEMORY.md in %s" % memory_dir)
    with open(memory_md, "rb") as fh:
        raw = fh.read()
    sha = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")

    prop = propose_extend(memory_dir, taxonomy, text=text)
    return _render_extend_proposal_dryrun(memory_dir, prop, sha)


def _excl_nofollow_opener(path, _flags):
    """Custom `opener` for `open(path, "w", ...)` used by execute_shard's AND
    execute_extend's staging writes (issue #183 finding 4 — ported to
    execute_shard's own, older staging loop for parity; ORIGINALLY
    execute_extend-only): O_EXCL refuses to open if ANYTHING already sits at
    `path` — a leftover file or a planted symlink — instead of silently
    following and truncating it; O_NOFOLLOW is belt-and-suspenders against
    following a symlink specifically. Ignores the `flags` open() would
    otherwise compute from mode="w" — a staging temp file always wants
    exactly these secure-create semantics, never a silent
    overwrite-through-symlink."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)


def _check_extend_source_drift(final_path, expected_text, label):
    """Re-verify that `final_path` still holds EXACTLY `expected_text` —
    called immediately before that file's own os.replace in execute_extend's
    commit loop, mirroring `_commit_pointerize`'s destination re-check
    (D9/`_check_docs_dest_drift`) for the same reason: the snapshot guards at
    the top of execute_extend (the MEMORY.md sha256, each touched shard's
    `shard_texts_before` text) only prove what the store looked like at ONE
    instant, read long before this loop runs — classification
    (propose_extend), no-loss checking and the backup-path scan all happen in
    between. A store mutated by something else during that window must fail
    CLOSED here rather than have its change silently clobbered by output
    built from the now-stale snapshot. Treats a vanished/unreadable file the
    same as changed content — both are drift, not a pass."""
    try:
        with open(final_path, "rb") as fh:
            current = fh.read().decode("utf-8")
    except OSError:
        current = None
    if current != expected_text:
        raise ValueError(
            "STORE CHANGED MID-APPLY: %s was modified on disk after this "
            "run's snapshot was read (between validation and commit) — "
            "refusing to overwrite a concurrent change with output built "
            "from stale content. Nothing was written for this file or "
            "anything after it. Re-run the dry run, review, re-apply."
            % label)


def execute_extend(memory_dir, taxonomy, today, expect_sha256):
    """MUTATING. Execute the proposed extend. Guards run in order, all
    refusing BEFORE any write; the backup, shard rewrites and core rewrite
    then commit, EVERY output staged to a temp sibling first, then
    os.replace'd into place in a fixed order — backup, then every touched
    shard, then the core LAST (issue #183 finding 1, BM10; see the
    commit-order comment beside `outputs` below, identical reasoning to
    execute_shard's):

      1. single read + drift — MEMORY.md read ONCE; its sha256 over those
         exact bytes must match --expect-sha256 (required; no blind-apply;
         no TOCTOU re-read between hashing and building the plan);
      2. sharded precondition — refuse a core with NO JIT pointers yet (the
         inverse of `shard`'s own already-sharded refusal): run `shard
         --apply` first;
      3. something to move — refuse when no overflow bullet matches an
         EXISTING shard under this taxonomy;
      4. no double-apply (issue #183 finding 1 follow-up, codex
         second-review of BM10) — refuse when any shard already holds a
         bullet this run would move into it again (propose_extend's
         `already_applied`): the shape a crash between a prior run's shard
         commit and its (never-reached) core commit leaves behind. A plain
         retry must never silently duplicate a note — see the guard below
         for the by-hand recovery it points the operator at;
      5. no-loss — {new core + every shard file on disk} must reproduce
         every original non-pointer bullet from {core + every shard file on
         disk}, exactly (check_extend_no_loss);
      6. apply-time revalidation (issue #208's own PR body, codex
         HIGH finding on this function, confirmed unmodified by that diff) —
         guards 1-5 all validate the ONE snapshot read at the very top of
         this function, but everything between that read and the actual
         commit (classification, no-loss checking, the backup-path scan) is
         a real window in which MEMORY.md or a touched shard could be
         mutated on disk by something else. `_check_extend_source_drift` is
         called immediately before EACH such file's os.replace in the commit
         loop below (see `drift_checks`), so a mutation landing anywhere in
         that window fails CLOSED — typed error, no partial write — instead
         of being silently overwritten by output built from stale content.

    On any failure during staging the temp files are removed and the real
    dir is left byte-identical to the start. Never deletes anything; never
    creates a new shard file."""
    memory_md = os.path.join(memory_dir, "MEMORY.md")
    if not os.path.exists(memory_md):
        raise ValueError("no MEMORY.md in %s" % memory_dir)

    # 1. Read MEMORY.md exactly ONCE; hash + build the plan from the SAME
    #    bytes (no re-read → no TOCTOU window between hashing and extending).
    with open(memory_md, "rb") as fh:
        raw = fh.read()
    actual = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")

    if not expect_sha256:
        raise ValueError(
            "--apply requires --expect-sha256 <hex> (the MEMORY.md hash you "
            "reviewed in the dry run) — refusing to blind-apply the extend")
    if actual != expect_sha256:
        raise ValueError(
            "STALE PLAN: MEMORY.md changed on disk since the dry run "
            "(sha256 %s != expected %s) — re-run the dry run, review, re-apply"
            % (actual, expect_sha256))

    # 2. extend's precondition is the INVERSE of shard's: refuse when the
    #    core has no JIT pointers yet — there is no existing shard to extend.
    if not _core_is_already_sharded(text):
        raise ValueError(
            "NOT SHARDED: MEMORY.md has no JIT shard pointers yet — `extend` "
            "only operates on an already-sharded core. Run `shard --apply` "
            "first if you want to split it.")

    disk_shards_before = list_shard_files(memory_dir)
    shard_texts_before = {f: _read(os.path.join(memory_dir, f)) for f in disk_shards_before}

    prop = propose_extend(memory_dir, taxonomy, text=text, shard_texts=shard_texts_before)
    if not prop["shards"]:
        raise ValueError(
            "no overflow core bullet matched an EXISTING shard under this "
            "taxonomy — nothing to extend")

    # 4. No double-apply (issue #183 finding 1 follow-up, codex
    #    second-review of BM10): a crash landing between a prior run's shard
    #    commit and its core commit leaves the shard already holding the
    #    moved bullet while the (unchanged) core still lists it directly too
    #    — so a plain retry, hashing the same unchanged core, proposes
    #    moving it again. check_extend_no_loss below is a MULTISET check and
    #    cannot catch this: the duplicate already exists on BOTH sides of the
    #    comparison (once pre-existing in the shard, once still in the core)
    #    before this run, so appending it again keeps the multiset balanced
    #    while permanently duplicating it in the shard. Refuse instead of
    #    guessing which side is stale.
    dupes = [(s["file"], s["already_applied"]) for s in prop["shards"]
             if s["already_applied"]]
    if dupes:
        detail = "; ".join(
            "%s already has %d bullet(s) this run would move into it again "
            "%r" % (f, len(lns), lns[:3]) for f, lns in dupes)
        raise ValueError(
            "POSSIBLE PARTIAL RETRY: %s — MEMORY.md was never rewritten to "
            "drop them (a crash likely landed between a prior run's shard "
            "commit and its core commit). Refusing to duplicate. Inspect by "
            "hand: if the shard's copy is correct, delete the matching "
            "line(s) directly from MEMORY.md (or restore the pre-change "
            "backup in archive/ and start over) before re-running --apply."
            % detail)

    # 4b. Harness truncation ceiling on the DESTINATIONS. A shard is a memory
    #     file, so the same 200-line/25,000-byte cap `filebudget` polices
    #     applies to it. Curing MEMORY.md by pushing a shard over that cap
    #     just relocates the silent truncation into a JIT-loaded file, so this
    #     refuses rather than writes. Non-overridable: there is no flag, and
    #     the fix is to trim index lines (or add a shard) first, never to
    #     force an over-cap destination.
    over_cap = [s for s in prop["shards"] if s["ceiling"]["status"] == "over"]
    if over_cap:
        detail = "; ".join(
            "%s would reach %d bytes / %d lines (ceiling %d/%d)"
            % (s["file"], s["new_measure"]["bytes"], s["new_measure"]["lines"],
               FILE_BUDGET_MAX_BYTES, FILE_BUDGET_MAX_LINES) for s in over_cap)
        raise ValueError(
            "REFUSING the extend: %s. The harness truncates ANY memory file "
            "past that ceiling, silently — moving lines out of MEMORY.md into "
            "a shard that then truncates does not fix the problem, it hides "
            "it in a file loaded just-in-time. Trim the destination shard's "
            "index lines (or split the taxonomy so the overflow lands in more "
            "than one shard) and re-run." % detail)

    # 5. No-loss invariant against every shard file physically on disk (not
    #    just the touched ones) — an untouched shard's pair is identical
    #    text on both sides, which both satisfies the invariant trivially
    #    AND proves the extend didn't silently touch it.
    original_pairs = [("MEMORY.md", text)] + list(shard_texts_before.items())
    produced_pairs = [("MEMORY.md", prop["new_core_text"])] + [
        (f, prop["new_shard_texts"].get(f, shard_texts_before[f]))
        for f in disk_shards_before
    ]
    check_extend_no_loss(original_pairs, produced_pairs)

    # Backup the pre-change core — same never-collide naming `shard` uses.
    archive_dir = os.path.join(memory_dir, ARCHIVE_DIRNAME)
    os.makedirs(archive_dir, exist_ok=True)
    backup_path = _next_backup_path(archive_dir, today)
    backup_rel = os.path.relpath(backup_path, memory_dir)

    # COMMIT ORDER (issue #183 finding 1, BM10): as in execute_shard,
    # the core MEMORY.md commits LAST, after every touched shard — a crash
    # before it completes leaves the OLD core in place (still holding the
    # about-to-move bullets directly), so the store is always fully old or
    # fully new, never a state where a moved note is gone from the core but
    # not yet landed in its shard. See execute_shard's commit-order comment
    # for the full reasoning (identical here).
    outputs = [(backup_path, text)]
    for f, new_text in prop["new_shard_texts"].items():
        outputs.append((os.path.join(memory_dir, f), new_text))
    outputs.append((memory_md, prop["new_core_text"]))     # core LAST

    # 6. Apply-time revalidation targets (see guard 6 in the docstring above):
    # every SOURCE file this run's snapshot vouches for — MEMORY.md itself,
    # plus each shard actually being touched. The backup path is excluded
    # on purpose: it is a brand-new file this run creates, not a snapshot of
    # something pre-existing, so there is nothing on disk for it to drift
    # from.
    drift_checks = {memory_md: ("MEMORY.md", text)}
    for f in prop["new_shard_texts"]:
        drift_checks[os.path.join(memory_dir, f)] = (f, shard_texts_before[f])

    staged = []
    try:
        for final_path, content in outputs:
            tmp = final_path + ".tmp-extend"
            # Secure create: refuses (raises) if anything — a leftover file
            # or a planted symlink — already sits at `tmp`, rather than
            # silently following/truncating it. Only append to `staged`
            # AFTER a successful create, so cleanup below never removes a
            # pre-existing path that wasn't ours to begin with.
            with open(tmp, "w", encoding="utf-8", opener=_excl_nofollow_opener) as fh:
                fh.write(content)
            staged.append((final_path, tmp))
        for final_path, tmp in staged:
            if final_path in drift_checks:
                label, expected = drift_checks[final_path]
                _check_extend_source_drift(final_path, expected, label)
            os.replace(tmp, final_path)
    except BaseException:
        for _final, tmp in staged:
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise

    actions = ["backed up MEMORY.md -> %s" % backup_rel]
    for s in prop["shards"]:
        actions.append("moved %d note(s) into %s (%d tok -> %d tok)"
                       % (s["moved_count"], s["file"],
                          s["old_measure"]["est_tokens"], s["new_measure"]["est_tokens"]))
    actions.append("rewrote MEMORY.md core (%d line(s) removed)" % prop["moved_total"])
    return {"actions": actions, "backup": backup_rel, "moved_total": prop["moved_total"]}


# --------------------------------------------------------------------------- #
# filebudget — read-only per-file harness truncation check, ALL stores
# (SCHEDULABLE; distinct from `budget`/`shard` above)
# --------------------------------------------------------------------------- #
# `budget` measures MEMORY.md's own wholesale-load token cost for the
# topic-sharding decision. `filebudget` checks a DIFFERENT, harder ceiling
# that applies to EVERY memory file (fact notes, MEMORY.md, MEMORY.<topic>.md
# shards): the harness truncates on load past 200 lines OR 25,000 bytes,
# silently, behind one WARNING line (the Claude Code runtime's per-file
# memory load limit). The OR is load-bearing — a file can blow past the byte cap on
# dense prose well under 200 lines, or blow past the line cap with short
# lines well under 25KB. Both must be checked independently.
#
# Why memory/docs/ and memory/archive/ are EXCLUDED from OVER/WARN:
# A naive scan across every store hits real files today — but every one of
# them is a FALSE POSITIVE: the docs/*-full.md bodies are the pointer-refactor
# OUTPUT (deliberately oversized — that's the whole point of moving them out
# of the wholesale-loaded set), and archive/MEMORY.pre-shard-*.md backups are
# frozen historical snapshots, never re-loaded by the harness. Flagging either
# class would tell the owner to "fix" files that are already correctly parked
# outside the load path — pure noise on day one. They are EXCLUDED from
# OVER/WARN classification (not silently dropped — they're still walked and
# reported, tagged `excluded`, so the count is auditable).

def _is_excluded_subpath(rel_parts):
    """True if a file's path, relative to its memory dir, sits under a
    top-level docs/ or archive/ subdirectory — the two non-actionable classes
    above. Only the FIRST path component counts (a nested docs/ deeper down,
    if one ever existed, is not this exclusion's concern)."""
    return bool(rel_parts) and rel_parts[0] in (DOCS_DIRNAME, ARCHIVE_DIRNAME)


def _walk_through_symlinked_dir(link_dir, rel_prefix):
    """Follow EXACTLY ONE symlink hop into `link_dir` (an already-named,
    already-known top-level docs/ or archive/ symlink) and yield its *.md
    files as (abs_path, rel_parts). Beneath this single manual hop the walk
    reverts to os.walk's own followlinks=False default, so nothing nested
    inside `link_dir` can ever be recursed into via a further symlink — this
    cannot loop, because the only symlink ever manually followed is the ONE
    the caller already named, never one discovered while walking."""
    for root, dirs, files in os.walk(link_dir, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        rel_from_link = os.path.relpath(root, link_dir)
        extra = () if rel_from_link == "." else tuple(rel_from_link.split(os.sep))
        for name in sorted(files):
            if name.endswith(".md"):
                yield os.path.join(root, name), rel_prefix + extra + (name,)


def iter_memory_files(memory_dir):
    """Walk memory_dir recursively for *.md files, skipping dotdirs (Syncthing
    internals: .stfolder, .stversions — never real memory content). Yields
    (abs_path, rel_parts) where rel_parts is the path relative to memory_dir
    split into components, in deterministic sorted order.

    Symlink policy: subdirectories are NOT followed in general (os.walk's
    default followlinks=False), to avoid symlink loops anywhere in an
    arbitrary memory tree. The one deliberate exception: if the TOP-LEVEL
    docs/ or archive/ entry itself is a symlink, its *.md contents are still
    walked — one manual hop via `_walk_through_symlinked_dir`, itself using
    followlinks=False beneath that hop, so nothing nested can ever loop — and
    yielded with the normal docs/... or archive/... rel_parts prefix, same as
    a real (non-symlink) docs/archive directory would be.

    Without this exception, a symlinked docs/ or archive/ goes completely
    dark to check_file_budgets: not reported as `excluded` (counted,
    auditable, per the module notes above) but silently invisible — worse
    than exclusion, because it contradicts the "excluded paths are always
    walked and reported" design. This is a READ-ONLY scan (filebudget) —
    unlike docs_target_path's WRITE guard, there is no data-loss risk in
    reading through the symlink here, only in silently under-reporting it, so
    following this one specific, already-named hop is the right tradeoff (as
    opposed to a blanket `followlinks=True`, which would invite loops on any
    symlink anywhere in the tree, not just this one known, expected-flat
    pair). `_display_path` naturally renders a target that resolves outside
    the memory tree as a raw absolute path in the report (its relpath against
    known roots falls through), so an unusual docs/archive symlink still
    reads as unusual in the output, not as an ordinary in-tree file."""
    first = True
    for root, dirs, files in os.walk(memory_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            abs_path = os.path.join(root, name)
            rel = os.path.relpath(abs_path, memory_dir)
            yield abs_path, tuple(rel.split(os.sep))
        if first:
            first = False
            for d in dirs:
                if d in (DOCS_DIRNAME, ARCHIVE_DIRNAME):
                    link_dir = os.path.join(root, d)
                    if os.path.islink(link_dir):
                        yield from _walk_through_symlinked_dir(link_dir, (d,))


def measure_file(path):
    """Measure one memory file the way the harness's own truncation ceiling
    does: BYTE length (not char length — these notes are full of multi-byte
    characters like em dashes, so char count silently undercounts) plus line
    count via the same splitlines() convention used elsewhere in this file."""
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8", errors="replace")
    return {"lines": len(text.splitlines()), "bytes": len(raw)}


def discover_memory_stores(projects_root):
    """Every existing <project>/memory dir under projects_root, sorted."""
    root = os.path.expanduser(projects_root)
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        cand = os.path.join(root, name, "memory")
        if os.path.isdir(cand):
            out.append(cand)
    return out


def _display_path(abs_path, memory_dir, display_root):
    """Best-effort relative path for report display: prefer relative to
    display_root (e.g. ~/.claude/projects, giving `-Users-someone-app/memory/
    x.md`); fall back to relative to the store's own parent dir (useful for
    a single --dir outside display_root, e.g. a test fixture); else absolute."""
    for base in (display_root, os.path.dirname(memory_dir)):
        try:
            rel = os.path.relpath(abs_path, base)
        except ValueError:
            continue
        if not rel.startswith(".."):
            return rel
    return abs_path


def check_file_budgets(memory_dirs, display_root,
                        max_lines=FILE_BUDGET_MAX_LINES,
                        max_bytes=FILE_BUDGET_MAX_BYTES,
                        warn_lines=FILE_BUDGET_WARN_LINES):
    """Read-only. For every *.md file under each memory dir in memory_dirs
    (recursively, minus Syncthing dotdirs), measure it against the harness's
    hard truncation ceiling and classify OVER / WARN / ok / excluded (see
    module notes above for the docs/archive exclusion). Writes nothing; has no
    --apply. Raises ValueError on a non-positive or inconsistent ceiling
    (config error, not a bogus alert) — same posture as `compute_budget`."""
    if max_lines <= 0 or max_bytes <= 0 or warn_lines <= 0:
        raise ValueError(
            "max_lines/max_bytes/warn_lines must be positive (got %r/%r/%r)"
            % (max_lines, max_bytes, warn_lines))
    if warn_lines > max_lines:
        raise ValueError(
            "warn_lines (%r) must be <= max_lines (%r)" % (warn_lines, max_lines))

    entries = []
    for memory_dir in memory_dirs:
        for abs_path, rel_parts in iter_memory_files(memory_dir):
            m = measure_file(abs_path)
            excluded = _is_excluded_subpath(rel_parts)
            over = (m["lines"] > max_lines) or (m["bytes"] > max_bytes)
            warn = (not over) and (m["lines"] >= warn_lines)
            if excluded:
                status = "excluded"
            elif over:
                status = "OVER"
            elif warn:
                status = "WARN"
            else:
                status = "ok"
            entries.append({
                "path": _display_path(abs_path, memory_dir, display_root),
                "memory_dir": memory_dir,
                "lines": m["lines"],
                "bytes": m["bytes"],
                "status": status,
            })
    entries.sort(key=lambda e: e["path"])

    counts = {"over": 0, "warn": 0, "ok": 0, "excluded": 0}
    for e in entries:
        counts[e["status"].lower()] += 1

    return {
        "max_lines": max_lines,
        "max_bytes": max_bytes,
        "warn_lines": warn_lines,
        "store_count": len(memory_dirs),
        "entries": entries,
        "counts": counts,
        "over_budget": counts["over"] > 0,
        "warn_only": counts["over"] == 0 and counts["warn"] > 0,
    }


def file_budget_alert_line(result):
    """The one-line, grep-friendly headline for a scheduled/alerting run."""
    c = result["counts"]
    if c["over"] > 0:
        return ("MEMORY FILE BUDGET: %d file(s) OVER the %d-line/%d-byte "
                "harness truncation ceiling (%d warn, %d excluded as "
                "docs/archive, %d scanned across %d store(s))."
                % (c["over"], result["max_lines"], result["max_bytes"],
                   c["warn"], c["excluded"], len(result["entries"]),
                   result["store_count"]))
    if c["warn"] > 0:
        return ("MEMORY FILE BUDGET: %d file(s) approaching the %d-line warn "
                "band (none over yet; %d excluded as docs/archive, %d "
                "scanned across %d store(s))."
                % (c["warn"], result["warn_lines"], c["excluded"],
                   len(result["entries"]), result["store_count"]))
    return ("MEMORY FILE BUDGET: all %d scanned file(s) within the %d-line/"
            "%d-byte ceiling across %d store(s) (%d excluded as docs/archive)."
            % (len(result["entries"]), result["max_lines"], result["max_bytes"],
               result["store_count"], c["excluded"]))


def render_file_budget_report(result):
    """Render the filebudget result as a plain-text report. Read-only output."""
    out = []
    out.append("# Memory file budget — harness truncation check\n")
    out.append("> %s\n" % file_budget_alert_line(result))
    out.append("Ceiling: %d lines OR %d bytes (OR is load-bearing — either "
               "axis alone is enough to truncate); warn band starts at %d "
               "lines.\n" % (result["max_lines"], result["max_bytes"],
                             result["warn_lines"]))
    out.append("`memory/docs/` (pointer-refactor output) and `memory/archive/` "
               "(frozen backups) are excluded from OVER/WARN — see SKILL.md "
               "for why — but still counted below.\n")

    flagged = [e for e in result["entries"] if e["status"] in ("OVER", "WARN")]
    if flagged:
        out.append("## Flagged\n")
        for e in sorted(flagged, key=lambda x: (x["status"] != "OVER", x["path"])):
            out.append("%s lines=%d bytes=%d  %s"
                       % (e["status"], e["lines"], e["bytes"], e["path"]))
        out.append("")
    else:
        out.append("## Flagged\n\nNone.\n")

    excluded = [e for e in result["entries"] if e["status"] == "excluded"]
    if excluded:
        out.append("## Excluded (docs/ and archive/ — non-actionable)\n")
        for e in excluded:
            out.append("lines=%d bytes=%d  %s" % (e["lines"], e["bytes"], e["path"]))
        out.append("")

    c = result["counts"]
    out.append("Scanned %d file(s) across %d store(s): %d over, %d warn, "
               "%d ok, %d excluded.\n"
               % (len(result["entries"]), result["store_count"],
                  c["over"], c["warn"], c["ok"], c["excluded"]))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# classify (report-only migration classification)
# --------------------------------------------------------------------------- #

CLASSIFY_CLASSES = ("personal", "harness-fact", "decision", "project-gotcha", "stale")
LOW_CONFIDENCE = "low-confidence"
# A rules line counts as the duplicate of a note only when the note's
# description and the line share at least DUP_MIN_SHARED distinctive words AND
# the Jaccard score of the two word sets (shared / union) is at least
# DUP_MIN_JACCARD. The union makes the test symmetric: a long line that merely
# contains the description's words, or a short line inside a long description,
# scores low. Deliberately strict: a missed duplicate costs a human glance, a
# false one would tell the owner a note is safe to drop. The values sit in the
# gap between the loose matches seen in a first review (at most 4 shared words,
# score 0.31 or less) and true restatements of a rule (5 or more shared words,
# score 0.45 or more); a verbatim copy scores 1.0.
DUP_MIN_SHARED = 5
DUP_MIN_JACCARD = 0.4
CLASSIFY_STOPWORDS = frozenset((
    "this that with from have been were will would should could when then than "
    "them they their there what which while about after before into over under "
    "only also each every does done just like more most much must never always "
    "note notes used uses using need needs want wants").split())
# Words common to nearly every rules line; they never count toward a duplicate.
CLASSIFY_GENERIC = frozenset((
    "harness session sessions model models file files memory rule rules "
    "standard standards claude").split())
HARNESS_VOCAB = frozenset((
    "harness seat seats orchestrate orchestrator director builder reviewer "
    "worktree launchd cmux omp codex subagent subagents packet handback "
    "rail rails wave queue").split())
DECISION_CUES = ("decided", "decision", "ratified", "election", "elected", "adr",
                 "chose", "approved", "agreed", "going forward", "greenlight",
                 "outcome", "outcomes")
GOTCHA_CUES = ("gotcha", "workaround", "breaks", "fails", "failing", "bug",
               "crash", "error", "do not", "don't", "never", "must", "quirk",
               "silently", "mechanics")
STALE_LINE_RE = re.compile(
    r"^[\s>*_#-]*(status:\s*)?(resolved|superseded|obsolete|deprecated|replaced[- ]by)\b",
    re.IGNORECASE)
STALE_STATUS_VALUES = ("resolved", "superseded", "obsolete", "deprecated",
                       "dead", "archived")
# A marker in a title or description counts only when it leads the text: a
# mid-sentence "superseded by" or "3 of 4 now RESOLVED" can sit in a note that
# still states current truth.
STALE_TEXT_RE = re.compile(
    r"^\W*(resolved|superseded|obsolete|replaced[- ]by)\b", re.IGNORECASE)
# A body line is a status line when it opens the body (its first few lines) or
# is written as `Status: ...`; a RESOLVED line deep in a long note is history.
STALE_BODY_LINES = 3
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]{3,}")
REPO_PATH_RE = re.compile(r"(?:^|[\s`(/~])(?:code|work)/([A-Za-z0-9_.-]+)")
# A quoted path is only worth checking when it can name one real file: no
# placeholder, glob or template syntax, not a URL, not a scratch location.
PATH_TOKEN_RE = re.compile(r"^(~|/)[^\s]*/[^\s]+$")
PATH_PLACEHOLDER_RE = re.compile(r"[<>*?{}\[\]$]|\.\.\.|://")
PATH_SCRATCH_RE = re.compile(
    r"^(?:/private)?/(?:tmp|var)/|/worktrees?/|/wt/|/scratch|/\.claude/worktrees/")


def _sig_words(text):
    return {w for w in WORD_RE.findall(text.lower())
            if w not in CLASSIFY_STOPWORDS and w not in CLASSIFY_GENERIC
            and w not in HARNESS_VOCAB}


def _collect_md_lines(roots):
    """[(label, lineno, sig_words, lowered_text)] over every *.md under each
    root, sorted by (label, line) so the search is deterministic. The label is
    `<root basename>/<relative path>`, never an absolute path."""
    out = []
    for root in roots:
        root = os.path.expanduser(root)
        base = os.path.basename(os.path.normpath(root))
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                if not name.endswith(".md"):
                    continue
                path = os.path.join(dirpath, name)
                label = "%s/%s" % (base, os.path.relpath(path, root))
                try:
                    with open(path, encoding="utf-8") as f:
                        lines = f.read().splitlines()
                except (OSError, UnicodeDecodeError):
                    continue
                for i, ln in enumerate(lines, 1):
                    out.append((label, i, _sig_words(ln), ln.lower()))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def _find_duplicate(description, md_lines):
    """`file:line` of the best-matching line in md_lines, or None. A line
    matches only on a symmetric overlap with the description (see DUP_*), best
    Jaccard score first, then most shared words, then the earliest line."""
    desc = _sig_words(description)
    best = None  # (jaccard, shared, label, line)
    for label, lineno, words, _lowered in md_lines:
        shared = len(desc & words)
        if shared < DUP_MIN_SHARED:
            continue
        jaccard = shared / len(desc | words)
        if jaccard < DUP_MIN_JACCARD:
            continue
        if best is None or (jaccard, shared) > best[:2]:
            best = (jaccard, shared, label, lineno)
    return None if best is None else "%s:%d" % (best[2], best[3])


def _stale_reason(fm_lines, body):
    """Why a note is stale, or None. Only an explicit marker counts; whether a
    quoted path still exists never decides the class (see _missing_path)."""
    status = (read_fm_field(fm_lines, "status") or "").strip().lower()
    if status in STALE_STATUS_VALUES:
        return "frontmatter status: %s" % status
    if read_fm_field(fm_lines, "superseded_by"):
        return "frontmatter superseded_by is set"
    title = next((ln.lstrip("# ") for ln in body.splitlines()
                  if ln.startswith("#")), "")
    for label, text in (("title", title), ("description",
                        read_fm_field(fm_lines, "description") or "")):
        m = STALE_TEXT_RE.search(text)
        if m:
            return "%s starts with %s" % (label, m.group(1).lower())
    seen = 0
    for ln in body.splitlines():
        if not ln.strip():
            continue
        seen += 1
        m = STALE_LINE_RE.match(ln)
        if m and (m.group(1) or seen <= STALE_BODY_LINES):
            return "marked %s" % m.group(2).upper()
    return None


def _missing_path(body):
    """First quoted absolute or `~/` path that looks like one real file and is
    missing, or None. Low-confidence evidence only, never a class."""
    for t in sorted(set(BACKTICK_RE.findall(body))):
        if not PATH_TOKEN_RE.match(t) or PATH_PLACEHOLDER_RE.search(t) \
                or PATH_SCRATCH_RE.search(t):
            continue
        path = os.path.expanduser(t)
        # `/api/v1/x` style routes: the first directory is not on this machine.
        if not os.path.isdir(os.path.join("/", path.split("/")[1])):
            continue
        if not os.path.exists(path):
            return t
    return None


def _note_repo(fm_lines, text, store_slug):
    """(repo, confident). Order: frontmatter `repo`, a code/<name> or
    work/<name> path in the note, else the store's own project slug."""
    repo = read_fm_field(fm_lines, "repo")
    if repo:
        return repo, True
    for m in REPO_PATH_RE.finditer(text):
        name = m.group(1).rstrip(".")
        if re.search(r"[A-Za-z0-9]", name):
            return name, True
    return store_slug, False


def classify_note(name, text, md_lines, ledger_lines, store_slug):
    """-> (class, evidence, low_confidence_reason_or_None). Rules, in order:
    1 explicit stale marker -> stale; 2 type user -> personal; 3 duplicate
    found in a rules root -> harness-fact (never without a rules root);
    4 by metadata.type: feedback -> personal, project -> decision or
    project-gotcha by cue count, reference and mechanics -> project-gotcha;
    anything else decided by cues, flagged. A quoted path that no longer
    exists only adds a low-confidence reason."""
    fm_lines, body = split_frontmatter(text)
    ntype = (read_metadata_field(fm_lines, "type") or "").strip().lower()
    description = read_fm_field(fm_lines, "description") or ""
    haystack = ("%s %s %s" % (name, description, body)).lower()
    low = []

    reason = _stale_reason(fm_lines, body)
    if reason:
        return "stale", "reason: %s" % reason, None

    missing = _missing_path(body)
    if missing:
        low.append("names a path that no longer exists: %s" % missing)

    if ntype == "user":
        return "personal", "type=user", "; ".join(low) or None

    harness_words = sorted(w for w in HARNESS_VOCAB
                           if re.search(r"\b%s\b" % w, haystack))
    harness_like = len(harness_words) >= 2 and ntype != "project"
    suffix = ""
    if md_lines is not None:
        dup = _find_duplicate(description, md_lines)
        if dup:
            return ("harness-fact", "duplicates: %s" % dup,
                    "; ".join(low) or None)
        if harness_like:
            low.append("harness vocabulary but no duplicate found in the rules roots")
    elif harness_like:
        suffix = "; duplicates: not searched"
        low.append("harness vocabulary (%s) but no --rules-root given, so "
                   "duplicates were not searched" % ", ".join(harness_words[:3]))

    decide = sum(1 for c in DECISION_CUES if c in haystack)
    gotcha = sum(1 for c in GOTCHA_CUES if c in haystack)
    if ntype == "feedback":
        return ("personal", "type=feedback" + suffix, "; ".join(low) or None)
    if ntype == "project":
        cls = "decision" if decide > gotcha else "project-gotcha"
        if decide == gotcha:
            low.append("project note with no clear decision/gotcha cue")
    elif ntype in ("reference", "mechanics"):
        cls = "project-gotcha"
    else:
        cls = "decision" if decide > gotcha else "project-gotcha"
        low.append("no usable metadata.type (%s)" % (ntype or "absent"))

    if cls == "decision":
        if ledger_lines is None:
            evidence = "ledger: none yet"
        else:
            hit = _find_duplicate(description, ledger_lines)
            evidence = "ledger: %s" % (hit or "no match")
            if not hit:
                low.append("no matching ledger item")
        return cls, evidence + suffix, "; ".join(low) or None
    repo, confident = _note_repo(fm_lines, text, store_slug)
    if not confident:
        low.append("repo taken from the store name, not the note")
    return cls, "repo: %s%s" % (repo, suffix), "; ".join(low) or None


def _require_dir(path, what):
    if not os.path.isdir(os.path.expanduser(path)):
        raise ValueError("%s %r does not exist or is not a directory"
                         % (what, path))


def classify_store(memory_dir, rules_roots, ledger_roots):
    """Classify every fact note in memory_dir. Reads only; returns a dict with
    sorted `entries` and per-class `counts`. Every input root must exist."""
    _require_dir(memory_dir, "memory dir")
    for r in rules_roots:
        _require_dir(r, "--rules-root")
    for r in ledger_roots:
        _require_dir(r, "--ledger-root")
    md_lines = _collect_md_lines(rules_roots) if rules_roots else None
    ledger_lines = _collect_md_lines(ledger_roots) if ledger_roots else None
    store_slug = os.path.basename(os.path.dirname(
        os.path.realpath(memory_dir))) or "unknown"
    entries = []
    for name in list_fact_files(memory_dir):
        with open(os.path.join(memory_dir, name), encoding="utf-8") as f:
            text = f.read()
        cls, evidence, low = classify_note(
            name, text, md_lines, ledger_lines, store_slug)
        entries.append({"note": name, "class": cls, "evidence": evidence,
                        "low_confidence": low})
    counts = {c: 0 for c in CLASSIFY_CLASSES}
    for e in entries:
        counts[e["class"]] += 1
    return {"entries": entries, "counts": counts,
            "rules_searched": md_lines is not None}


def classify_count_line(result):
    return "counts: %s total=%d" % (
        " ".join("%s=%d" % (c, result["counts"][c]) for c in CLASSIFY_CLASSES),
        len(result["entries"]))


def render_classify_report(result, stamp=None):
    out = ["# Memory classification (report-only; nothing was moved)"]
    if stamp:
        out.append("generated: %s" % stamp)
    out.append(classify_count_line(result))
    out.append("")
    for e in result["entries"]:
        line = "%s | %s | %s" % (e["class"], e["note"], e["evidence"])
        if e["low_confidence"]:
            line += " | %s: %s" % (LOW_CONFIDENCE, e["low_confidence"])
        out.append(line)
    return "\n".join(out) + "\n"


def _inside(parent, child):
    return child == parent or os.path.commonpath([parent, child]) == parent


def check_classify_out(memory_dir, out):
    """Refuse a report path inside (or equal to) the store being classified,
    by its spelled path and by where it resolves, and refuse a symlink target,
    so the report can never become a note of the store it describes or
    overwrite a file through a link."""
    if (_inside(os.path.abspath(memory_dir), os.path.abspath(out))
            or _inside(os.path.realpath(memory_dir), os.path.realpath(out))):
        raise ValueError("--out %r is inside the memory dir being classified; "
                         "classify is report-only" % out)
    if os.path.islink(out):
        raise ValueError("--out %r is a symlink; refusing to write through it"
                         % out)


def write_classify_report(out, text):
    """Write the report to a new file beside `out`, then rename it onto `out`,
    so an existing inode (a hard link to a store note, say) is never opened
    or truncated.

    Keep mkstemp's private mode when `out` does not exist. When it does,
    copy the existing target's mode (without setuid/setgid/sticky) onto the
    new file so a rewrite never widens a private report and never tightens
    a group-readable one. One no-follow stat decides: missing keeps the
    private mode; a non-regular target or any other stat failure is refused
    and the report is not written."""
    out_dir = os.path.dirname(os.path.abspath(out))
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".classify-", suffix=".tmp",
                                   dir=out_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            st = os.lstat(out)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(
                    "--out %r is not a regular file; refusing to write" % out)
            os.chmod(tmp, stat.S_IMODE(st.st_mode) & 0o777)
        os.replace(tmp, out)
        tmp = None
    except OSError as e:
        raise ValueError("cannot write --out %r: %s" % (out, e.strerror))
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _load_plan(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _today(args):
    if getattr(args, "today", None):
        d = _parse_date(args.today)
        if d is None:
            raise ValueError("bad --today %r (expected YYYY-MM-DD)" % args.today)
        return d
    return date.today()


def _default_memory_dir():
    """Default `--dir`: $MEMORY_GARDENER_DIR if set, else the memory dir of the
    project rooted at the user's home directory (Claude Code names a project
    dir after its path with every `/` replaced by `-`)."""
    override = os.environ.get("MEMORY_GARDENER_DIR")
    if override:
        return os.path.expanduser(override)
    home = os.path.expanduser("~")
    slug = home.replace(os.sep, "-")
    return os.path.join(home, ".claude", "projects", slug, "memory")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Memory gardener helper (report-first).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    default_dir = _default_memory_dir()

    sp = sub.add_parser("scan", help="emit a JSON inventory of the memory dir")
    sp.add_argument("--dir", default=default_dir)
    sp.add_argument("--today")

    rp = sub.add_parser("report", help="render a plan JSON into a review report")
    rp.add_argument("--dir", default=default_dir)
    rp.add_argument("--plan", required=True)
    rp.add_argument("--out", help="report path (default: ~/.claude/logs/memory-gardener/<date>.md)")
    rp.add_argument("--today")

    apc = sub.add_parser("apply", help="execute a reviewed plan JSON (mutating)")
    apc.add_argument("--dir", default=default_dir)
    apc.add_argument("--plan", required=True)
    apc.add_argument("--today")

    bp = sub.add_parser(
        "budget",
        help="read-only index load-cost check + shard proposal (schedulable; never mutates)")
    bp.add_argument("--dir", default=default_dir)
    bp.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                    help="token ceiling for the wholesale-loaded index (default %d)"
                         % DEFAULT_MAX_TOKENS)
    bp.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES,
                    help="line ceiling for MEMORY.md (default %d)" % DEFAULT_MAX_LINES)
    bp.add_argument("--taxonomy",
                    help="JSON taxonomy override (default: store-local "
                         "shard-taxonomy.json, then built-in defaults)")
    bp.add_argument("--json", action="store_true",
                    help="emit the raw budget result as JSON instead of a report")
    bp.add_argument("--today")

    shp = sub.add_parser(
        "shard",
        help="EXECUTE the topic-split budget proposes (dry-run default; "
             "--apply is sha256-guarded + backs up first)")
    shp.add_argument("--dir", default=default_dir)
    shp.add_argument("--taxonomy",
                     help="JSON taxonomy override (default: store-local "
                          "shard-taxonomy.json, then built-in defaults)")
    shp.add_argument("--apply", action="store_true",
                     help="perform the split (default is a read-only dry run)")
    shp.add_argument("--expect-sha256",
                     help="the MEMORY.md sha256 you reviewed (REQUIRED with "
                          "--apply; refuses on drift — never blind-applies)")
    shp.add_argument("--force", action="store_true",
                     help="overwrite pre-existing shard/backup files (sharding "
                          "is normally a one-time op)")
    shp.add_argument("--extend", action="store_true",
                     help="EXTEND mode: move overflow core bullets into an "
                          "EXISTING shard instead of splitting (inverse "
                          "precondition — the core must ALREADY be sharded; "
                          "never creates a new shard). Same dry-run-default / "
                          "--apply sha256-guard shape as plain `shard`.")
    shp.add_argument("--today")

    fbp = sub.add_parser(
        "filebudget",
        help="read-only per-file harness truncation check across ALL memory "
             "stores (200 lines OR 25000 bytes; 180-200 line warn band; "
             "schedulable; never mutates)")
    fbp.add_argument("--dir",
                     help="scan a single memory dir instead of every store "
                          "(default: every store under --projects-root)")
    fbp.add_argument("--projects-root", default="~/.claude/projects",
                     help="root to glob */memory stores under when --dir is "
                          "omitted (default: ~/.claude/projects)")
    fbp.add_argument("--max-lines", type=int, default=FILE_BUDGET_MAX_LINES,
                     help="line ceiling per file (default %d)" % FILE_BUDGET_MAX_LINES)
    fbp.add_argument("--max-bytes", type=int, default=FILE_BUDGET_MAX_BYTES,
                     help="byte ceiling per file (default %d)" % FILE_BUDGET_MAX_BYTES)
    fbp.add_argument("--warn-lines", type=int, default=FILE_BUDGET_WARN_LINES,
                     help="warn-band start, in lines (default %d)" % FILE_BUDGET_WARN_LINES)
    fbp.add_argument("--json", action="store_true",
                     help="emit the raw result as JSON instead of a report")
    fbp.add_argument("--today")

    clp = sub.add_parser(
        "classify",
        help="read-only migration classification of every note into one of "
             "personal / harness-fact / decision / project-gotcha / stale "
             "(report only; moves nothing)")
    clp.add_argument("--dir", default=default_dir)
    clp.add_argument("--rules-root", action="append", default=[],
                     help="directory of *.md rules/standards to search for the "
                          "line a harness-fact note duplicates (repeatable; "
                          "no default)")
    clp.add_argument("--ledger-root", action="append", default=[],
                     help="directory of *.md ledger items a decision note maps "
                          "to (repeatable; none means 'ledger: none yet')")
    clp.add_argument("--out", help="write the report here (default: stdout); "
                                   "refused inside the memory dir")
    clp.add_argument("--stamp", action="store_true",
                     help="add one 'generated: <date>' header line")
    clp.add_argument("--today")

    args = ap.parse_args(argv)
    memory_dir = os.path.expanduser(args.dir) if getattr(args, "dir", None) else None
    try:
        today = _today(args)

        if args.cmd == "scan":
            print(json.dumps(scan(memory_dir, today), indent=2))
            return 0

        if args.cmd == "report":
            plan = _load_plan(args.plan)
            text = render_report(plan, memory_dir, today)
            out = args.out
            if not out:
                os.makedirs(REPORT_DIR, exist_ok=True)
                # A report is named for the day it ran, so a SECOND run on the
                # same day used to overwrite the first with no backup — the one
                # write path here that destroyed data (a re-run
                # clobbered that morning's report, the only record of 79
                # pre-trim index lines; unrecoverable, the sync peer was off).
                # Every other backup in this module is sacrosanct and claims
                # -2/-3/... rather than overwrite; the report now matches.
                out = _claim_dated_path(REPORT_DIR, today.isoformat(), ".md")
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
            sys.stderr.write("report written: %s\n" % out)
            print(text)
            return 0

        if args.cmd == "apply":
            plan = _load_plan(args.plan)
            result = apply_plan(plan, memory_dir, today)
            for a in result["actions"]:
                print("APPLIED: %s" % a)
            for w in result["warnings"]:
                sys.stderr.write("WARNING: %s\n" % w)
            return 1 if result["warnings"] else 0

        if args.cmd == "budget":
            taxonomy, taxonomy_source = select_taxonomy(memory_dir, args.taxonomy)
            result = compute_budget(
                memory_dir, args.max_tokens, args.max_lines, taxonomy, today)
            result["taxonomy_source"] = taxonomy_source
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(render_budget_report(result))
            # Alert headline on stderr so a scheduled wrapper can surface it cheaply.
            sys.stderr.write(budget_alert_line(result) + "\n")
            # Over-budget is a distinct exit code (not an error) for unattended alerting.
            return OVER_BUDGET_EXIT if result["over_budget"] else 0

        if args.cmd == "shard":
            taxonomy, taxonomy_source = select_taxonomy(memory_dir, args.taxonomy)
            print("Taxonomy source: %s" % taxonomy_source)
            if args.extend:
                if not args.apply:
                    print(render_extend_dryrun(memory_dir, taxonomy))
                    return 0
                result = execute_extend(memory_dir, taxonomy, today, args.expect_sha256)
                for a in result["actions"]:
                    print("APPLIED: %s" % a)
                return 0
            if not args.apply:
                print(render_shard_dryrun(memory_dir, taxonomy))
                return 0
            result = execute_shard(memory_dir, taxonomy, today,
                                   args.expect_sha256, args.force)
            for a in result["actions"]:
                print("APPLIED: %s" % a)
            return 0

        if args.cmd == "classify":
            from_env = bool(os.environ.get("MEMORY_GARDENER_DIR")) \
                and memory_dir == _default_memory_dir()
            _require_dir(memory_dir, "memory dir (from MEMORY_GARDENER_DIR)"
                         if from_env else "memory dir")
            if args.out:
                check_classify_out(memory_dir, os.path.expanduser(args.out))
            result = classify_store(memory_dir, args.rules_root, args.ledger_root)
            text = render_classify_report(
                result, today.isoformat() if args.stamp else None)
            if args.out:
                write_classify_report(os.path.expanduser(args.out), text)
                sys.stderr.write("report written: %s\n" % args.out)
                print(classify_count_line(result))
            else:
                sys.stdout.write(text)
            return 0

        if args.cmd == "filebudget":
            projects_root = os.path.expanduser(args.projects_root)
            dirs = [memory_dir] if memory_dir else discover_memory_stores(projects_root)
            result = check_file_budgets(dirs, projects_root, args.max_lines,
                                        args.max_bytes, args.warn_lines)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(render_file_budget_report(result))
            sys.stderr.write(file_budget_alert_line(result) + "\n")
            if result["counts"]["over"] > 0:
                return OVER_BUDGET_EXIT
            if result["counts"]["warn"] > 0:
                return FILEBUDGET_WARN_EXIT
            return 0
    except ValueError as e:
        # Plan/drift validation problems are refusals, not tracebacks.
        sys.stderr.write("error: %s\n" % e)
        return 2

    return 2


if __name__ == "__main__":
    sys.exit(main())

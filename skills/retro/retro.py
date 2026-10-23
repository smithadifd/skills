#!/usr/bin/env python3
"""Deterministic contract for the retro skill (standard library only).

Subcommands:
  triage <transcript>            exit 0 to proceed, exit 3 for a trivial session
  scan <transcript>              print repeated tool-failure classes and user corrections
  build <draft.json> --out F     type, sort and write the findings file F
  check <findings.json|.md>      validate a findings file (exit 0 valid, 1 invalid)
  approve <findings> <id>        record the person's approval of one edit finding

The only file any subcommand writes is the findings file named on the command
line. Nothing here applies a diff or touches any other path.
"""

import argparse
import json
import re
import sys
from pathlib import Path

SEVERITIES = ("high", "medium", "low")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}
TYPES = ("encode-in-structure", "skill-or-rule-edit", "repo-learning", "nothing-met-the-bar")
STRUCTURES = ("hook", "lint", "test", "ci")
MIN_INSTANCES = 2

# A session below both limits is too small to learn from.
TRIVIAL_MAX_TOOL_CALLS = 10
TRIVIAL_MAX_EDITS = 3
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

CHECK_COMMAND = re.compile(
    r"\b(lint|ruff|eslint|flake8|mypy|tsc|pytest|prettier|black|shellcheck|clippy)\b", re.I
)
FAILURE_TEXT = re.compile(r"\b(error|errors|failed|failure)\b", re.I)
_WORD = r"(?:errors?|failed|failures?)"
ZERO_COUNT = re.compile(
    rf"\b(?:0|no)\s+{_WORD}\b|\b{_WORD}\s*[:=]?\s*0\b(?!\.?\d)", re.I
)
RULE_CODE = re.compile(r"\b[A-Z]{1,4}\d{2,4}\b")
CORRECTION_CUE = re.compile(
    r"^\s*(no\b|don'?t\b|do not\b|stop\b|again\b|i said\b|that'?s not\b|wrong\b)", re.I
)


class RetroError(Exception):
    pass


# ---------------------------------------------------------------- transcripts


def _blocks(record):
    message = record.get("message")
    if isinstance(message, dict):
        role = message.get("role") or record.get("type")
        content = message.get("content")
    else:
        role = record.get("role") or record.get("type")
        content = record.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        content = []
    return role, content


def events(path):
    """Yield flat events: user/assistant text, tool_use and tool_result."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RetroError(f"cannot read transcript: {exc}")
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            raise RetroError(f"transcript line {number} is not JSON")
        if not isinstance(record, dict):
            continue
        role, content = _blocks(record)
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                yield {"kind": role, "line": number, "text": str(block.get("text", ""))}
            elif kind == "tool_use":
                yield {
                    "kind": "tool_use",
                    "line": number,
                    "id": block.get("id"),
                    "name": block.get("name", ""),
                    "input": block.get("input") or {},
                }
            elif kind == "tool_result":
                body = block.get("content", "")
                if isinstance(body, list):
                    body = "\n".join(str(b.get("text", "")) for b in body if isinstance(b, dict))
                yield {
                    "kind": "tool_result",
                    "line": number,
                    "id": block.get("tool_use_id"),
                    "error": bool(block.get("is_error")),
                    "text": str(body),
                }


def triage(path):
    tool_calls = 0
    edits = 0
    for event in events(path):
        if event["kind"] == "tool_use":
            tool_calls += 1
            if event["name"] in EDIT_TOOLS:
                edits += 1
    trivial = tool_calls < TRIVIAL_MAX_TOOL_CALLS and edits < TRIVIAL_MAX_EDITS
    return {"trivial": trivial, "tool_calls": tool_calls, "edits": edits}


def _failure_lines(text):
    """Lines that report a failure; zero-count results such as '0 errors' do not."""
    return [ln for ln in text.splitlines() if FAILURE_TEXT.search(ZERO_COUNT.sub("", ln))]


def _signature(text):
    match = RULE_CODE.search(text)
    if match:
        return match.group(0)
    first = next(iter(_failure_lines(text)), text.strip()[:80])
    first = re.sub(r"\S+\.\w+(:\d+)*:?", "", first)
    first = re.sub(r"(?<![A-Za-z])\d+", "N", first.lower())
    return " ".join(first.split())[:80]


def scan(path):
    """Group failed check-tool runs by class; list the person's correction turns."""
    commands = {}
    classes = {}
    corrections = []
    for event in events(path):
        if event["kind"] == "tool_use":
            commands[event["id"]] = str(event["input"].get("command", ""))
        elif event["kind"] == "tool_result":
            command = commands.get(event["id"], "")
            failed = event["error"] or _failure_lines(event["text"])
            if failed and CHECK_COMMAND.search(command):
                sig = _signature(event["text"])
                excerpt = next(
                    (ln for ln in event["text"].splitlines() if ln.strip()), ""
                )[:120]
                classes.setdefault(sig, []).append(
                    {"ref": f"line {event['line']}", "excerpt": excerpt}
                )
        elif event["kind"] == "user" and CORRECTION_CUE.search(event["text"]):
            corrections.append({"ref": f"line {event['line']}", "excerpt": event["text"][:160]})
    return {
        "mechanical_candidates": [
            {"class": sig, "count": len(inst), "instances": inst}
            for sig, inst in sorted(classes.items())
        ],
        "corrections": corrections,
    }


# ------------------------------------------------------------------- findings


def _valid_instance(i):
    return isinstance(i, dict) and isinstance(i.get("ref"), str) and bool(i["ref"].strip())


def _instances(item):
    """Cited instances; raises RetroError when any is malformed."""
    raw = item.get("instances") or []
    if not isinstance(raw, list) or not all(_valid_instance(i) for i in raw):
        raise RetroError(
            f"finding {item.get('id', '?')}: each instance needs a non-empty string ref"
        )
    return raw


def classify(item):
    """Type one drafted finding. Mechanical mistake classes become structure."""
    seen = len({i["ref"] for i in _instances(item)})
    if item.get("mechanical") and seen >= MIN_INSTANCES:
        return "encode-in-structure"
    if not item.get("mechanical") and (item.get("obsolete_rule") or seen >= MIN_INSTANCES):
        return "skill-or-rule-edit"
    if item.get("learning") and not item.get("already_documented"):
        return "repo-learning"
    return None


def build(draft):
    findings = []
    dropped = []
    for item in draft.get("findings", []):
        kind = classify(item)
        if kind is None:
            dropped.append(item.get("id", "?"))
            continue
        out = {
            "id": item["id"],
            "title": item.get("title", ""),
            "severity": item.get("severity", "low"),
            "type": kind,
            "evidence": item.get("evidence", []),
            "instances": _instances(item),
        }
        if kind == "encode-in-structure":
            out["structure"] = item.get("structure")
            out["proposal"] = item.get("proposal", "")
        elif kind == "skill-or-rule-edit":
            out["diff"] = item.get("diff", "")
            out["approved"] = False
            if item.get("obsolete_rule"):
                out["removal"] = True
        else:
            out["path"] = item.get("path", "")
            out["note"] = item["learning"]
        findings.append(out)
    if not findings:
        findings = [
            {
                "id": "none",
                "title": "Nothing met the bar",
                "severity": "low",
                "type": "nothing-met-the-bar",
                "evidence": ["no mistake class was seen twice and no rule is obsolete"],
            }
        ]
    findings.sort(key=lambda f: SEVERITY_RANK.get(f["severity"], len(SEVERITIES)))
    return {"findings": findings, "dropped": dropped}


def _load_findings(path):
    text = Path(path).read_text(encoding="utf-8")
    if str(path).endswith(".md"):
        match = re.search(r"```json\n(.*?)\n```", text, re.S)
        if not match:
            raise RetroError("markdown findings file has no json block")
        text = match.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RetroError(f"findings are not valid JSON: {exc}")


def _has_text(value):
    if isinstance(value, list):
        return any(isinstance(v, str) and v.strip() for v in value)
    return isinstance(value, str) and bool(value.strip())


def validate(data):
    """Return a list of problems; empty means the file is valid."""
    problems = []
    findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(findings, list) or not findings:
        return ["findings must be a non-empty list"]
    ranks = []
    for index, item in enumerate(findings):
        label = f"finding {item.get('id', index) if isinstance(item, dict) else index}"
        if not isinstance(item, dict):
            problems.append(f"{label}: not an object")
            continue
        try:
            _instances(item)
        except RetroError:
            problems.append(f"{label}: each instance needs a non-empty string ref")
            continue
        if item.get("severity") not in SEVERITY_RANK:
            problems.append(f"{label}: severity must be one of {', '.join(SEVERITIES)}")
        else:
            ranks.append(SEVERITY_RANK[item["severity"]])
        kind = item.get("type")
        if kind not in TYPES:
            problems.append(f"{label}: type must be one of {', '.join(TYPES)}")
            continue
        if not _has_text(item.get("evidence")):
            problems.append(f"{label}: evidence is required")
        if kind == "encode-in-structure" and item.get("structure") not in STRUCTURES:
            problems.append(f"{label}: structure must be one of {', '.join(STRUCTURES)}")
        if kind == "encode-in-structure":
            if not _has_text(item.get("proposal")):
                problems.append(f"{label}: a structure finding needs a proposal")
            if len({i["ref"] for i in _instances(item)}) < MIN_INSTANCES:
                problems.append(
                    f"{label}: a structure finding needs at least {MIN_INSTANCES} cited instances"
                )
        if kind == "skill-or-rule-edit":
            if not _has_text(item.get("diff")):
                problems.append(f"{label}: an edit finding needs a diff")
            if not isinstance(item.get("approved"), bool):
                problems.append(f"{label}: an edit finding needs an approved state")
            seen = {i["ref"] for i in _instances(item)}
            if not item.get("removal") and len(seen) < MIN_INSTANCES:
                problems.append(
                    f"{label}: a rule proposal needs at least {MIN_INSTANCES} cited instances"
                )
        if kind == "repo-learning":
            target = str(item.get("path", ""))
            if not target.startswith("docs/") or ".." in target.split("/"):
                problems.append(f"{label}: a learning belongs in a note under docs/")
            if not _has_text(item.get("note")):
                problems.append(f"{label}: a learning needs note text")
        if kind == "nothing-met-the-bar" and len(findings) != 1:
            problems.append(f"{label}: nothing-met-the-bar must be the only result")
    if ranks != sorted(ranks):
        problems.append("findings are not sorted by severity (high first)")
    return problems


def render(data):
    lines = ["# Retro findings", "", "```json", json.dumps(data, indent=2), "```", ""]
    for item in data["findings"]:
        lines.append(f"- [{item['severity']}] {item['type']}: {item.get('title', '')}")
    return "\n".join(lines) + "\n"


def write_findings(data, out):
    out = Path(out)
    text = render(data) if out.suffix == ".md" else json.dumps(data, indent=2) + "\n"
    out.write_text(text, encoding="utf-8")


def approve(path, finding_id):
    data = _load_findings(path)
    problems = validate(data)
    if problems:
        raise RetroError("refusing to approve an invalid findings file: " + "; ".join(problems))
    for item in data.get("findings", []):
        if item.get("id") == finding_id:
            if item.get("type") != "skill-or-rule-edit":
                raise RetroError(f"{finding_id} is not an edit finding")
            item["approved"] = True
            write_findings(data, path)
            return
    raise RetroError(f"no finding with id {finding_id}")


# ------------------------------------------------------------------------ CLI


def main(argv=None):
    parser = argparse.ArgumentParser(prog="retro.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("triage", "scan", "check"):
        sub.add_parser(name).add_argument("path")
    p_build = sub.add_parser("build")
    p_build.add_argument("draft")
    p_build.add_argument("--out", required=True)
    p_approve = sub.add_parser("approve")
    p_approve.add_argument("path")
    p_approve.add_argument("finding_id")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "triage":
            result = triage(args.path)
            if result["trivial"]:
                print(
                    f"retro: trivial session ({result['tool_calls']} tool calls, "
                    f"{result['edits']} edits); nothing to learn, skipping."
                )
                return 3
            print(json.dumps(result))
        elif args.cmd == "scan":
            print(json.dumps(scan(args.path), indent=2))
        elif args.cmd == "build":
            data = build(json.loads(Path(args.draft).read_text(encoding="utf-8")))
            problems = validate(data)
            if problems:
                print("\n".join(problems), file=sys.stderr)
                return 1
            write_findings(data, args.out)
            print(f"wrote {args.out}")
        elif args.cmd == "check":
            problems = validate(_load_findings(args.path))
            if problems:
                print("\n".join(problems), file=sys.stderr)
                return 1
            print("findings valid")
        elif args.cmd == "approve":
            approve(args.path, args.finding_id)
            print(f"approved {args.finding_id}")
    except (RetroError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"retro: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

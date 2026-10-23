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

RULE_CODE = re.compile(r"\b[A-Z]{1,4}\d{2,4}\b")
TEST_NODE = re.compile(r"[\w./-]+\.py::[\w:\[\]./-]+")
EXIT_LINE = re.compile(r"Exit code [1-9][0-9]*")
# Wrapper commands, each with the options that take a separate operand.
WRAPPERS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "npx": {"-p", "--package"},
    "command": set(),
    "time": {"-f", "--format", "-o", "--output"},
    "sudo": {"-u", "-g", "-C", "-h", "-p", "-U", "-D", "-R", "-T"},
}
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
        for ordinal, block in enumerate(content, 1):
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
                    "input": block["input"] if isinstance(block.get("input"), dict) else {},
                }
            elif kind == "tool_result":
                body = block.get("content", "")
                if isinstance(body, list):
                    body = "\n".join(str(b.get("text", "")) for b in body if isinstance(b, dict))
                yield {
                    "kind": "tool_result",
                    "line": number,
                    "id": block.get("tool_use_id"),
                    "block": ordinal,
                    "has_flag": "is_error" in block,
                    "error": block.get("is_error") is True,
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


def _python_module_at(words, i):
    """Index of the module word of `python <options> -m <module>`, else None.

    Interpreter options are skipped first: no-operand flags (`-I`, `-B`, combined
    like `-IB`; `-x` included), `-W`/`-X`/`--check-hash-based-pycs` with an operand, and their
    attached forms (`-Wdefault`, `-Xdev`). `-c` or a script path gives None.
    """
    while i < len(words):
        word = words[i]
        if word == "-m":
            return i + 1 if i + 1 < len(words) else None
        if re.fullmatch(r"-m.+", word):
            return i
        if word in ("-W", "-X", "--check-hash-based-pycs"):
            i += 2
        elif re.fullmatch(r"-[WX].+|--check-hash-based-pycs=.+", word):
            i += 1
        elif re.fullmatch(r"-[IBEsSuOqvbdiPRx]+", word):
            i += 1
        else:
            return None
    return None


def _tool(command):
    """The command word a shell line runs.

    Grammar: take the last command of a simple `&&` / `;` / `||` / `|` chain (so
    `cd d && pytest` and `a || pytest` are `pytest`); skip leading variable assignments; skip a wrapper (env, npx,
    command, time, sudo) with its options, and the operand of each option that
    takes one (`env -u NAME`); skip `python <options> -m`. Quoting is not parsed.
    """
    parts = [p for p in re.split(r"&&|;|\|\|?", command) if p.strip()]
    words = parts[-1].split() if parts else []
    i = 0
    wrapper = None
    while i < len(words):
        word = words[i]
        if word in WRAPPERS:
            wrapper = word
            i += 1
        elif re.match(r"^[A-Za-z_]\w*=", word):
            i += 1
        elif word.startswith("-") and wrapper:
            i += 2 if word in WRAPPERS[wrapper] else 1
        elif word.startswith("-"):
            i += 1
        elif re.match(r"^python[\d.]*$", word) and _python_module_at(words, i + 1) is not None:
            i = _python_module_at(words, i + 1)
            if words[i].startswith("-m"):
                words[i] = words[i][2:]
        else:
            return word.rsplit("/", 1)[-1]
    return ""


def failed_run(event):
    """True only for an error result that starts with a non-zero exit-status line."""
    if event["error"] is not True:
        return False
    first = event["text"].split("\n", 1)[0].rstrip("\r")
    return EXIT_LINE.fullmatch(first) is not None


def _identifiers(text):
    return sorted(set(RULE_CODE.findall(text)) | set(TEST_NODE.findall(text)))


def scan(path):
    """Group failed runs by (tool, identifier); list the person's correction turns.

    A run failed only when its result is an error that begins with a non-zero
    exit-status line. Summary text is never read for pass or fail.
    """
    commands = {}
    groups = {}
    unclassified = {}
    corrections = []
    results = 0
    flagged = 0
    for event in events(path):
        if event["kind"] == "tool_use":
            if isinstance(event["id"], str) and event["id"]:
                commands[event["id"]] = str(event["input"].get("command", ""))
        elif event["kind"] == "tool_result":
            results += 1
            flagged += event["has_flag"]
            if not failed_run(event):
                continue
            if not isinstance(event["id"], str) or event["id"] not in commands:
                continue
            tool = _tool(commands[event["id"]])
            if not tool:
                continue
            excerpt = next((ln for ln in event["text"].splitlines()[1:] if ln.strip()), "")[:120]
            # Parallel results share a record, so the id keeps each ref distinct.
            ref = f"line {event['line']}, tool_use_id {event['id']}"
            instance = {"ref": ref, "excerpt": excerpt}
            idents = _identifiers(event["text"])
            if not idents:
                unclassified.setdefault(tool, []).append(instance)
            for ident in idents:
                groups.setdefault((tool, ident), []).append(instance)
        elif event["kind"] == "user" and CORRECTION_CUE.search(event["text"]):
            corrections.append({"ref": f"line {event['line']}", "excerpt": event["text"][:160]})
    out = {
        "mechanical_candidates": [
            {"class": f"{tool}:{ident}", "count": len(inst), "instances": inst}
            for (tool, ident), inst in sorted(groups.items())
            if len(inst) >= MIN_INSTANCES
        ],
        "unclassified": [
            {"tool": tool, "count": len(inst)} for tool, inst in sorted(unclassified.items())
        ],
        "corrections": corrections,
    }
    if results and not flagged:
        out["notice"] = "no is_error fields in this transcript; failed runs cannot be identified"
    return out


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
    markers = [
        name for name in ("mechanical", "obsolete_rule", "learning") if item.get(name)
    ]
    if len(markers) > 1:
        raise RetroError(
            f"finding {item.get('id', '?')}: conflicting type markers ({', '.join(markers)})"
        )
    if item.get("mechanical") and seen >= MIN_INSTANCES:
        return "encode-in-structure"
    wants_learning = (
        item.get("learning") and not item.get("already_documented") and not item.get("diff")
    )
    if not item.get("mechanical") and not wants_learning and (
        item.get("obsolete_rule") or seen >= MIN_INSTANCES
    ):
        return "skill-or-rule-edit"
    if item.get("learning") and not item.get("already_documented"):
        return "repo-learning"
    return None


def build(draft):
    findings = []
    dropped = []
    if not isinstance(draft, dict):
        raise RetroError("draft must be a JSON object")
    raw = draft.get("findings", [])
    if not isinstance(raw, list):
        raise RetroError("draft findings must be a list")
    for item in raw:
        if not isinstance(item, dict):
            raise RetroError("each draft finding must be an object")
        if "severity" in item and not isinstance(item["severity"], str):
            raise RetroError(f"finding {item.get('id', '?')}: severity must be a string")
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
        ident = item.get("id")
        if not isinstance(ident, str) or not ident.strip():
            problems.append(f"{label}: id must be a non-empty string")
        elif sum(1 for f in findings if isinstance(f, dict) and f.get("id") == ident) > 1:
            problems.append(f"{label}: duplicate id")
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
    matches = [f for f in data.get("findings", []) if f.get("id") == finding_id]
    if len(matches) != 1:
        raise RetroError(f"expected exactly one finding with id {finding_id}, found {len(matches)}")
    item = matches[0]
    if item.get("type") != "skill-or-rule-edit":
        raise RetroError(f"{finding_id} is not an edit finding")
    item["approved"] = True
    write_findings(data, path)


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
    except (RetroError, OSError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"retro: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

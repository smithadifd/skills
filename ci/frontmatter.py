#!/usr/bin/env python3
"""Read the frontmatter of SKILL.md files and name every problem.

Usage: python3 -E -P ci/frontmatter.py FILE...

(-E -P keep PyYAML in the user site reachable and keep the current directory
off the import path.)

Grammar, per file:
  - the text is split into lines at each newline character;
  - line 1 is exactly `---`;
  - the frontmatter closes at the first later line that is exactly `---` at
    column 0 (no indent, no trailing text); an indented `---` inside a block
    scalar is body text;
  - `yaml.safe_load` of the lines between the two markers returns a mapping;
  - no top-level key of that mapping equals "hooks".

Output: one line per problem, `<path>: <reason>`, the reason one of
  no opening ---, no closing ---, does not parse, not a mapping, declares hooks.
A file that cannot be read or decoded is reported as `does not parse`.

Exit: 0 when every file is clean, 1 when any file has a problem, 2 when PyYAML
cannot be imported (the one line `PyYAML cannot be imported`), 64 on no files.

ci/lint-skills.sh (rules skill-hooks, frontmatter, frontmatter-parser) and
ci/check.sh (step install) both call this one parser, so no evasion lives in a
gap between two parsers.
"""

import sys

try:
    import yaml
except ImportError:
    print("PyYAML cannot be imported")
    sys.exit(2)


def problem(path):
    """The reason `path` has no usable frontmatter, or None when it is clean."""
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return "does not parse"
    lines = text.split("\n")
    if lines[0] != "---":
        return "no opening ---"
    end = None
    for number, line in enumerate(lines[1:], 1):
        if line == "---":
            end = number
            break
    if end is None:
        return "no closing ---"
    try:
        meta = yaml.safe_load("\n".join(lines[1:end]))
    except Exception:
        return "does not parse"
    if not isinstance(meta, dict):
        return "not a mapping"
    if "hooks" in meta:
        return "declares hooks"
    return None


def main(paths):
    if not paths:
        print("usage: frontmatter.py FILE...", file=sys.stderr)
        return 64
    status = 0
    for path in paths:
        reason = problem(path)
        if reason is not None:
            print("%s: %s" % (path, reason))
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

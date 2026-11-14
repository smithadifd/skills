"""The grammar of ci/frontmatter.py, the one parser the lint and the ledger e2e check share.

Accepted form: line 1 is exactly `---`; the frontmatter closes at the first later
line that is exactly `---` at column 0; the lines between parse (PyYAML safe_load)
as a mapping; no top-level key is "hooks". Each near miss refuses with its named
reason, one `<path>: <reason>` line, exit 1. A missing PyYAML exits 2 with one line.
"""

import subprocess
import sys
from pathlib import Path

import pytest

PARSER = Path(__file__).resolve().parents[3] / "ci" / "frontmatter.py"
GOOD = "---\nname: demo\ndescription: >\n  Use when asked. NOT for real work.\n---\n\nBody.\n"


def run(*paths, flags=("-E", "-P")):
    done = subprocess.run(
        [sys.executable, *flags, str(PARSER), *map(str, paths)], capture_output=True, text=True, check=False
    )
    return done.returncode, done.stdout.splitlines()


def write(tmp_path, text, name="SKILL.md"):
    path = tmp_path / name
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return path


def test_a_clean_file_is_accepted_with_no_output(tmp_path):
    assert run(write(tmp_path, GOOD)) == (0, [])


def test_an_indented_marker_inside_a_block_scalar_is_body_text_not_the_close(tmp_path):
    text = "---\nname: demo\ndescription: |\n  one\n  ---\n  two\n---\n\nBody.\n"
    assert run(write(tmp_path, text)) == (0, [])


def test_a_hooks_key_after_an_indented_marker_is_still_found(tmp_path):
    text = "---\nname: demo\ndescription: |\n  one\n  ---\nhooks: {}\n---\n"
    path = write(tmp_path, text)
    assert run(path) == (1, [f"{path}: declares hooks"])


@pytest.mark.parametrize(
    "text",
    [
        "name: demo\n---\n",
        " ---\nname: demo\n---\n",
        "--- \nname: demo\n---\n",
        "---\r\nname: demo\r\n---\r\n",
        "\ufeff---\nname: demo\n---\n",
        "",
    ],
    ids=["no-marker", "indented", "trailing-space", "crlf", "bom", "empty"],
)
def test_a_missing_or_malformed_opening_marker_is_refused(tmp_path, text):
    path = write(tmp_path, text)
    assert run(path) == (1, [f"{path}: no opening ---"])


@pytest.mark.parametrize(
    "text",
    [
        "---\nname: demo\n",
        "---\nname: demo\n ---\n",
        "---\nname: demo\n--- \n",
        "---\nname: demo\n---x\n",
        "---\nname: demo\n---\r\n",
        "---\nname: demo\n----\n",
    ],
    ids=["none", "indented", "trailing-space", "trailing-text", "crlf", "four-dashes"],
)
def test_a_missing_or_malformed_closing_marker_is_refused(tmp_path, text):
    path = write(tmp_path, text)
    assert run(path) == (1, [f"{path}: no closing ---"])


@pytest.mark.parametrize(
    "body",
    [b"---\nname: [unclosed\n---\n", b"---\nname: \xff\xfe\n---\n", b"---\n\tname: demo\n---\n"],
    ids=["bad-yaml", "bad-utf8", "tab-indent"],
)
def test_frontmatter_that_does_not_parse_is_refused(tmp_path, body):
    path = write(tmp_path, body)
    assert run(path) == (1, [f"{path}: does not parse"])


def test_a_missing_file_is_reported_as_not_parsing(tmp_path):
    path = tmp_path / "SKILL.md"
    assert run(path) == (1, [f"{path}: does not parse"])


@pytest.mark.parametrize(
    "text",
    ["---\n- a\n- b\n---\n", "---\njust text\n---\n", "---\n---\n", "---\n42\n---\n"],
    ids=["list", "scalar", "empty", "number"],
)
def test_frontmatter_that_is_not_a_mapping_is_refused(tmp_path, text):
    path = write(tmp_path, text)
    assert run(path) == (1, [f"{path}: not a mapping"])


@pytest.mark.parametrize(
    "key",
    ["hooks: {}", '"hooks": {}', "'hooks': []", "hooks:", "? hooks\n: {}", '"ho\\u006fks": 1'],
    ids=["plain", "double-quoted", "single-quoted", "null", "explicit-key", "escaped"],
)
def test_a_top_level_hooks_key_is_refused_however_it_is_spelled(tmp_path, key):
    path = write(tmp_path, f"---\nname: demo\n{key}\n---\n")
    assert run(path) == (1, [f"{path}: declares hooks"])


@pytest.mark.parametrize(
    "extra",
    ["metadata:\n  hooks: not-top-level", "hookshot: 1", "Hooks: 1", "description: the hooks key"],
    ids=["nested", "prefix", "case", "value"],
)
def test_only_a_top_level_key_exactly_hooks_counts(tmp_path, extra):
    assert run(write(tmp_path, f"---\nname: demo\n{extra}\n---\n")) == (0, [])


def test_every_file_is_reported_in_order_and_the_exit_is_one(tmp_path):
    good = write(tmp_path, GOOD, "good.md")
    bad = write(tmp_path, "---\nname: demo\nhooks: {}\n---\n", "bad:colon.md")
    unclosed = write(tmp_path, "---\nname: demo\n", "unclosed.md")
    assert run(good, bad, unclosed) == (
        1,
        [f"{bad}: declares hooks", f"{unclosed}: no closing ---"],
    )


def test_no_files_is_a_usage_error_not_a_pass():
    code, out = run()
    assert (code, out) == (64, [])


def test_a_missing_pyyaml_exits_two_with_one_line(tmp_path):
    # -S drops site-packages, so PyYAML cannot be imported.
    assert run(write(tmp_path, GOOD), flags=("-I", "-S")) == (2, ["PyYAML cannot be imported"])

"""Stdlib-only tests for redaction, the size cap and the fixed note layout."""
import json
import os
import tempfile

import handoff

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "session.json")

FAKE_SECRETS = [
    "AKIAABCDEFGHIJKLMNOP",
    "ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2",
    "sk-" + "fakefakefakefakefake1234",
    "Bearer fakefakefake.token.value",
    "-----BEGIN RSA PRIVATE KEY-----\nZmFrZWZha2U=\n-----END RSA PRIVATE KEY-----",
    "DB_PASSWORD=hunter2fake",
    "MY_TOKEN=fake-value",
    "SECRET='fake value with spaces'",
    '"api_key": "fakevalue"',
]
NEEDLES = ("ABCDEFGHIJKLMNOP", "a1b2c3d4", "fakefakefakefake", "ZmFrZWZha2U",
           "hunter2fake", "fake-value", "fake value", "fakevalue")


def test_redact_covers_each_secret_shape():
    for secret in FAKE_SECRETS:
        out = handoff.redact("before %s after" % secret)
        assert handoff.REDACTED in out, secret
        for needle in NEEDLES:
            assert needle not in out, (secret, out)


def test_redact_keeps_ordinary_text():
    text = "Ran pytest; 12 passed. See src/app.py and commit abc1234."
    assert handoff.redact(text) == text


def test_cap_leaves_small_text_alone():
    text, truncated = handoff.cap("short")
    assert (text, truncated) == ("short", False)


def test_cap_truncates_and_says_so():
    text, truncated = handoff.cap("x" * (handoff.CAP_BYTES * 2))
    assert truncated
    assert len(text.encode("utf-8")) <= handoff.CAP_BYTES
    assert "[truncated: note exceeded 20 KB" in text


def test_cap_never_splits_a_multibyte_character():
    text, truncated = handoff.cap("é" * handoff.CAP_BYTES)
    assert truncated
    text.encode("utf-8")
    assert len(text.encode("utf-8")) <= handoff.CAP_BYTES


def test_oversized_note_is_capped_end_to_end():
    note = handoff.build_note({"Goal": "g " * handoff.CAP_BYTES}, "repo", "slug")
    assert len(note.encode("utf-8")) <= handoff.CAP_BYTES
    assert "[truncated:" in note


def test_fixture_session_has_every_section_in_order(private_tmp):
    with open(FIXTURE, encoding="utf-8") as fh:
        content = json.load(fh)
    path = handoff.write_note(content, "demo-repo", "retry-helper")
    assert os.path.dirname(path) == tempfile.gettempdir()
    assert os.path.basename(path) == "handoff-demo-repo-retry-helper.md"
    note = open(path, encoding="utf-8").read()
    positions = [note.index("## " + name) for name in handoff.SECTIONS]
    assert positions == sorted(positions)
    assert "Written: " in note
    assert "ignored" not in note


def test_secrets_section_is_always_none_redacted_and_input_is_scrubbed():
    with open(FIXTURE, encoding="utf-8") as fh:
        content = json.load(fh)
    content["Goal"] = "Use token ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2" + " for the call."
    note = handoff.build_note(content, "demo-repo", "retry-helper")
    assert note.split("## Secrets")[1].strip() == "none, redacted"
    assert "a1b2c3d4" not in note
    assert "fake-not-a-real-value" not in note


def test_empty_section_is_written_as_none():
    note = handoff.build_note({}, "r", "s")
    for name in handoff.SECTIONS:
        assert "## " + name in note
    assert note.count("\nnone\n") == len(handoff.SECTIONS) - 1


def test_default_path_is_in_the_temporary_directory(private_tmp):
    path = handoff.default_path("my repo", "a/b")
    assert os.path.dirname(path) == str(private_tmp)
    assert os.path.basename(path) == "handoff-my-repo-a-b.md"


def test_cli_writes_to_explicit_out(private_tmp):
    out = os.path.join(str(private_tmp), "named.md")
    rc = handoff.main(["--repo", "r", "--slug", "s", "--input", FIXTURE, "--out", out])
    assert rc == 0 and os.path.isfile(out)

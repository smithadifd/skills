"""Stdlib-only tests for redaction, the size cap, the note layout and file safety."""
import json
import os
import stat
import tempfile

import pytest

import handoff

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "session.json")

FAKE_SECRETS = [
    "AKIAABCDEFGHIJKLMNOP",
    "ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2",
    "xoxb-" + "1234567890-fakefakefake",
    "sk-" + "fakefakefakefakefake1234",
    "Bearer fakefakefake.token.value",
    "Authorization: Basic ZmFrZTpmYWtlcGFzcw==",
    "-----BEGIN RSA PRIVATE KEY-----\nZmFrZWZha2U=\n-----END RSA PRIVATE KEY-----",
    "DB_PASSWORD=hunter2fake",
    "MY_TOKEN=fake-value",
    "SECRET='fake value with spaces'",
    '"api_key": "fakevalue"',
    "password: hunter2fake",
    "api_key: fakevalue",
    "client_secret: 'fake value'",
    "curl --token fake-value https://example.com",
    "run --password=hunter2fake now",
    "tool --api-key fakevalue --verbose",
]
NEEDLES = ("ABCDEFGHIJKLMNOP", "a1b2c3d4", "fakefakefakefake", "ZmFrZWZha2U", "ZmFrZTpm",
           "hunter2fake", "fake-value", "fake value", "fakevalue", "1234567890-fake")


def test_redact_covers_each_secret_shape():
    for secret in FAKE_SECRETS:
        out = handoff.redact("before %s after" % secret)
        assert handoff.REDACTED in out, secret
        for needle in NEEDLES:
            assert needle not in out, (secret, out)


def test_flag_redaction_keeps_the_next_flag():
    out = handoff.redact("tool --api-key fakevalue --verbose")
    assert "--verbose" in out
    assert handoff.redact("tool --token --verbose") == "tool --token --verbose"


def test_escaped_quote_in_json_value_leaves_no_tail():
    out = handoff.redact('{"api_key": "prefix\\"secret-tail", "n": 1}')
    assert "secret-tail" not in out and "prefix" not in out
    assert '"n": 1' in out


def test_escaped_quote_in_assignment_leaves_no_tail():
    for text in ['TOKEN="pre\\"secret-tail"', "PASSWORD='pre\\'secret-tail'",
                 'password: "pre\\"secret-tail"']:
        out = handoff.redact(text)
        assert "secret-tail" not in out, out


def test_redact_keeps_ordinary_text():
    text = "Ran pytest; 12 passed. See src/app.py and commit abc1234."
    assert handoff.redact(text) == text


def test_small_note_is_not_truncated():
    note, truncated = handoff.build_note({"Goal": "short"}, "r", "s")
    assert truncated is False and "[truncated" not in note


def test_oversized_section_keeps_every_heading_and_says_so():
    content = {"Goal": "g " * handoff.CAP_BYTES, "Open questions": ["still here?"]}
    note, truncated = handoff.build_note(content, "repo", "slug")
    assert truncated
    assert len(note.encode("utf-8")) <= handoff.CAP_BYTES
    for name in handoff.SECTIONS:
        assert "## " + name in note
    assert "still here?" in note
    assert "[truncated: note exceeded 20 KB" in note
    assert note.split("## Secrets")[1].split("\n\n")[1] == "none, redacted"


def test_huge_repo_and_slug_cannot_push_the_note_over_the_cap():
    huge = "r" * handoff.CAP_BYTES * 2
    content = {name: "x" * handoff.CAP_BYTES for name in handoff.SECTIONS}
    note, truncated = handoff.build_note(content, huge, huge + "\nnewline")
    assert truncated and len(note.encode("utf-8")) <= handoff.CAP_BYTES
    for name in handoff.SECTIONS:
        assert "## " + name in note
    first = note.split("\n")[0]
    assert len(first) < 200 and "..." in first


def test_huge_header_with_small_body_stays_under_the_cap():
    note, _ = handoff.build_note({"Goal": "g"}, "r" * 50000, "s" * 50000)
    assert len(note.encode("utf-8")) <= handoff.CAP_BYTES


def test_many_large_sections_all_survive_trimming():
    content = {name: "x" * handoff.CAP_BYTES for name in handoff.SECTIONS}
    note, truncated = handoff.build_note(content, "repo", "slug")
    assert truncated and len(note.encode("utf-8")) <= handoff.CAP_BYTES
    for name in handoff.SECTIONS:
        assert "## " + name in note


def test_multibyte_text_is_cut_on_a_character_boundary():
    note, truncated = handoff.build_note({"Goal": "é" * handoff.CAP_BYTES}, "r", "s")
    assert truncated
    assert len(note.encode("utf-8")) <= handoff.CAP_BYTES


def test_fixture_session_has_every_section_in_order(private_tmp):
    with open(FIXTURE, encoding="utf-8") as fh:
        content = json.load(fh)
    path, truncated = handoff.write_note(content, "demo-repo", "retry-helper")
    assert truncated is False
    assert os.path.dirname(path) == os.path.join(tempfile.gettempdir(), "handoff-notes-%d" % os.getuid())
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
    note, _ = handoff.build_note(content, "demo-repo", "retry-helper")
    assert note.split("## Secrets")[1].strip() == "none, redacted"
    assert "a1b2c3d4" not in note
    assert "fake-not-a-real-value" not in note


def test_empty_section_is_written_as_none():
    note, _ = handoff.build_note({}, "r", "s")
    for name in handoff.SECTIONS:
        assert "## " + name in note
    assert note.count("\nnone\n") == len(handoff.SECTIONS) - 1


def test_notes_dir_is_private(private_tmp):
    path = handoff.notes_dir()
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o700
    assert os.lstat(path).st_uid == os.getuid()


def test_notes_dir_refuses_symlink_and_loose_mode(private_tmp):
    target = private_tmp / "elsewhere"
    target.mkdir()
    link = private_tmp / ("handoff-notes-%d" % os.getuid())
    link.symlink_to(target)
    with pytest.raises(handoff.NoteError):
        handoff.notes_dir()
    link.unlink()
    link.mkdir(mode=0o755)
    os.chmod(link, 0o755)
    with pytest.raises(handoff.NoteError):
        handoff.notes_dir()


def test_note_file_is_mode_0600(private_tmp):
    old = os.umask(0o022)
    try:
        path, _ = handoff.write_note({"Goal": "g"}, "r", "s")
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_existing_loose_note_is_tightened(private_tmp):
    out = private_tmp / "n.md"
    out.write_text("old")
    os.chmod(out, 0o644)
    handoff.write_note({"Goal": "g"}, "r", "s", out=str(out))
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


def test_write_does_not_follow_a_planted_symlink(private_tmp):
    victim = private_tmp / "victim.txt"
    victim.write_text("keep")
    out = private_tmp / "planted.md"
    out.symlink_to(victim)
    with pytest.raises(OSError):
        handoff.write_note({"Goal": "g"}, "r", "s", out=str(out))
    assert victim.read_text() == "keep"


def test_cli_reports_truncation_status(private_tmp, capsys):
    out = private_tmp / "named.md"
    big = private_tmp / "big.json"
    big.write_text(json.dumps({"Goal": "g " * handoff.CAP_BYTES}))
    assert handoff.main(["--repo", "r", "--slug", "s", "--input", str(big), "--out", str(out)]) == 0
    assert "truncated: yes" in capsys.readouterr().out
    assert handoff.main(["--repo", "r", "--slug", "s", "--input", FIXTURE, "--out", str(out)]) == 0
    assert "truncated: no" in capsys.readouterr().out


def test_cli_missing_input_is_a_clean_error(private_tmp, capsys):
    rc = handoff.main(["--repo", "r", "--slug", "s", "--input", str(private_tmp / "nope.json")])
    assert rc == 1
    assert "handoff:" in capsys.readouterr().err


def test_cli_unwritable_output_is_a_clean_error(private_tmp, capsys):
    out = private_tmp / "no-such-dir" / "n.md"
    assert handoff.main(["--repo", "r", "--slug", "s", "--input", FIXTURE, "--out", str(out)]) == 1
    assert "handoff:" in capsys.readouterr().err

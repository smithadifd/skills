"""Stdlib-only tests for redaction, the size cap, the note layout and file safety."""
import json
import os
import re
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


CRED_SLUGS = [
    ("x Authorization: Bearer abc123", "abc123"),
    ("token=" + "ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2", "a1b2c3d4"),
]


@pytest.mark.parametrize("slug,needle", CRED_SLUGS)
def test_default_path_and_stdout_carry_no_slug_credential(private_tmp, capsys, slug, needle):
    assert handoff.main(["--repo", "r", "--slug", slug, "--input", FIXTURE]) == 0
    out = capsys.readouterr().out
    path = out.splitlines()[0]
    assert re.fullmatch(r"handoff-r-redacted-[0-9a-f]{8}\.md", os.path.basename(path))
    assert os.path.exists(path)
    assert needle not in out and needle not in path
    assert needle not in os.listdir(os.path.dirname(path))[0]


@pytest.mark.parametrize("slug,needle", CRED_SLUGS)
def test_default_path_helper_uses_redacted_stem_with_suffix_for_a_credential(private_tmp, slug, needle):
    path = handoff.default_path("r", slug)
    assert re.fullmatch(r"handoff-r-redacted-[0-9a-f]{8}\.md", os.path.basename(path))


def test_credential_in_repo_argument_is_not_a_file_name(private_tmp, capsys):
    repo = "token=" + "ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2"
    assert handoff.main(["--repo", repo, "--slug", "s", "--input", FIXTURE]) == 0
    out = capsys.readouterr().out
    assert "a1b2c3d4" not in out
    assert re.fullmatch(r"handoff-redacted-[0-9a-f]{8}-s\.md", os.path.basename(out.splitlines()[0]))


def test_ordinary_slug_path_is_unchanged(private_tmp):
    path = handoff.default_path("my-repo", "plain slug 1")
    assert os.path.basename(path) == "handoff-my-repo-plain-slug-1.md"


def test_explicit_out_path_is_unaffected_by_slug_redaction(private_tmp, capsys):
    out = private_tmp / "named.md"
    slug = "x Authorization: Bearer zq9w8e7r"
    assert handoff.main(["--repo", "r", "--slug", slug, "--input", FIXTURE, "--out", str(out)]) == 0
    assert capsys.readouterr().out.splitlines()[0] == str(out)
    assert "zq9w8e7r" not in out.read_text()


def test_two_credential_slugs_keep_both_notes(private_tmp, capsys):
    paths = []
    for slug in ("token=" + "ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2", "x Authorization: Bearer zq9w8e7r"):
        assert handoff.main(["--repo", "r", "--slug", slug, "--input", FIXTURE]) == 0
        paths.append(capsys.readouterr().out.splitlines()[0])
    assert paths[0] != paths[1]
    assert all(os.path.exists(p) for p in paths)


def test_joined_pieces_that_look_like_a_secret_are_not_a_file_name(private_tmp):
    path = handoff.default_path("r", "rotate sk-live abcdefghijklmnop")
    assert "abcdefghijklmnop" not in path
    assert re.fullmatch(r"handoff-r-redacted-[0-9a-f]{8}\.md", os.path.basename(path))


def test_token_straddling_the_header_limit_leaves_no_bytes_in_the_note():
    token = "ghp_" + "QZ" * 12
    slug = "a" * (handoff.HEADER_FIELD_MAX - 10) + " " + token
    note, _ = handoff.build_note({"Goal": "g"}, "r", slug)
    assert "QZQZ" not in note and "ghp_" not in note


def test_token_straddling_the_repo_limit_is_redacted_before_the_cut():
    # No Authorization key, so the header stays a header; the token is
    # replaced before the field is shortened, so no piece of it survives.
    token = "sk-" + "Zk9" * 8
    repo = "r" * (handoff.HEADER_FIELD_MAX - 12) + " " + token
    note, _ = handoff.build_note({"Goal": "g"}, repo, "s")
    first = note.split("\n", 1)[0]
    assert first.startswith("# Handoff: ") and first.endswith(" / s")
    assert "Zk9" not in note and "sk-" not in note


NEWLINE_SECRET = 'PASSWORD="hunter2\nsecretpart"'


def test_quoted_secret_spanning_a_newline_is_not_in_the_title():
    slug = "x" * 50 + " " + NEWLINE_SECRET + "y" * 30 + '"'
    note, _ = handoff.build_note({"Goal": "g"}, "r", slug)
    assert "hunter2" not in note and "secretpa" not in note


def test_quoted_secret_spanning_a_newline_is_not_a_file_name(private_tmp, capsys):
    assert handoff.main(["--repo", "r", "--slug", NEWLINE_SECRET, "--input", FIXTURE]) == 0
    out = capsys.readouterr().out
    assert "hunter2" not in out and "secretpart" not in out
    assert re.fullmatch(r"handoff-r-redacted-[0-9a-f]{8}\.md", os.path.basename(out.splitlines()[0]))


def test_same_credential_slug_twice_gives_two_names(private_tmp):
    slug = "token=" + "ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2"
    assert handoff.default_path("r", slug) != handoff.default_path("r", slug)


def test_forced_suffix_collision_keeps_both_notes(private_tmp, monkeypatch, capsys):
    suffixes = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(handoff.secrets, "token_hex", lambda n: next(suffixes))
    slug = "token=" + "ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2"
    paths = []
    for _ in range(2):
        assert handoff.main(["--repo", "r", "--slug", slug, "--input", FIXTURE]) == 0
        paths.append(capsys.readouterr().out.splitlines()[0])
    assert [os.path.basename(p) for p in paths] == ["handoff-r-redacted-aaaaaaaa.md", "handoff-r-redacted-bbbbbbbb.md"]
    assert all(os.path.getsize(p) > 0 for p in paths)


def test_persistent_collision_fails_cleanly(private_tmp, monkeypatch, capsys):
    monkeypatch.setattr(handoff.secrets, "token_hex", lambda n: "cccccccc")
    slug = "token=" + "ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2"
    assert handoff.main(["--repo", "r", "--slug", slug, "--input", FIXTURE]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    before = open(first).read()
    assert handoff.main(["--repo", "r", "--slug", slug, "--input", FIXTURE]) == 1
    assert "handoff:" in capsys.readouterr().err
    assert open(first).read() == before


REPO_CRED = "token=" + "ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2"


def _plant(private_tmp, name, data=b"existing note\n"):
    """Create a file in the private notes directory and return its path."""
    path = os.path.join(handoff.notes_dir(), name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def test_credential_in_repo_never_overwrites_a_colliding_name(private_tmp, monkeypatch, capsys):
    suffixes = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(handoff.secrets, "token_hex", lambda n: next(suffixes))
    planted = _plant(private_tmp, "handoff-redacted-aaaaaaaa-s.md")
    assert handoff.main(["--repo", REPO_CRED, "--slug", "s", "--input", FIXTURE]) == 0
    written = capsys.readouterr().out.splitlines()[0]
    assert os.path.basename(written) == "handoff-redacted-bbbbbbbb-s.md"
    with open(planted, "rb") as fh:
        assert fh.read() == b"existing note\n"
    assert os.path.getsize(written) > len(b"existing note\n")


def test_credential_in_repo_fails_when_every_name_collides(private_tmp, monkeypatch, capsys):
    monkeypatch.setattr(handoff.secrets, "token_hex", lambda n: "cccccccc")
    planted = _plant(private_tmp, "handoff-redacted-cccccccc-s.md")
    assert handoff.main(["--repo", REPO_CRED, "--slug", "s", "--input", FIXTURE]) == 1
    assert "could not pick an unused file name" in capsys.readouterr().err
    with open(planted, "rb") as fh:
        assert fh.read() == b"existing note\n"
    assert os.listdir(os.path.dirname(planted)) == [os.path.basename(planted)]


def test_plain_repo_and_slug_reuse_one_file(private_tmp, capsys):
    first_input, second_input = private_tmp / "first.json", private_tmp / "second.json"
    first_input.write_text(json.dumps({"Goal": "first run goal"}))
    second_input.write_text(json.dumps({"Goal": "second run goal"}))
    paths = []
    for src in (first_input, second_input):
        assert handoff.main(["--repo", "my-repo", "--slug", "plain slug", "--input", str(src)]) == 0
        paths.append(capsys.readouterr().out.splitlines()[0])
    assert paths[0] == paths[1]
    assert os.path.basename(paths[0]) == "handoff-my-repo-plain-slug.md"
    assert os.listdir(os.path.dirname(paths[0])) == ["handoff-my-repo-plain-slug.md"]
    text = open(paths[0], encoding="utf-8").read()
    assert "second run goal" in text and "first run goal" not in text

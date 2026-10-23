"""Authorization redaction: a section that holds a recognised key is redacted whole.

Every case in references/authorization-redaction.md is here. The case id in
each test is the id in that file.
"""
import time

import pytest

import handoff

R = handoff.REDACTED
B64 = "dXNlcjpwYXNz"
GHP = "ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nprefixsecret\n-----END RSA PRIVATE KEY-----"
# Bytes that must not survive in any output below: the credential after the
# key, and the secret-shaped text before it.
SECRETS = ("tailsecret", B64, "prefixsecret", "a1b2c3d4", "dGFpbHNlY3JldA", "abcdefghijkl")

# (case id, section body). Each must redact to exactly R.
KEYED = [
    # Key shapes and the five names.
    ("K01", "Authorization: Digest username=\"u\", response=\"tailsecret\""),
    ("K02", 'curl -H "Authorization: Bearer tailsecret" https://x'),
    ("K03", "curl -H 'Authorization: Basic %s' https://x" % B64),
    ("K04", 'headers["Authorization"] = "Basic tailsecret"  # comment'),
    ("K05", '{"Authorization": "Bearer tailsecret", "Accept": "json"}'),
    ("K06", '[("Authorization", "Basic %s")]' % B64),
    ("K07", "headers.add('Authorization', 'Basic %s')" % B64),
    ("K08", "my %%h = ('Authorization' => 'Basic %s');" % B64),
    ("K09", '"Authorization" = "Basic %s"' % B64),
    ("K10", 'proxy_set_header Authorization "Basic %s";' % B64),
    ("K11", "Fixed it: the Authorization header is required.\nNext: tailsecret"),
    ("K12", "AUTHORIZATION=Basic %s" % B64),
    ("K13", "authorization: basic %s" % B64),
    ("K14", "Proxy-Authorization: Basic %s" % B64),
    ("K15", "HTTP_AUTHORIZATION=Basic %s" % B64),
    ("K16", "REDIRECT_HTTP_AUTHORIZATION=Basic %s" % B64),
    ("K17", "HTTP_PROXY_AUTHORIZATION=Basic %s" % B64),
    # Multi-line values and later lines.
    ("K18", 'h = ("Authorization: Bearer "\n"tailsecret")'),
    ("K19", 'curl -H "Authorization: Bearer pre\\"\ntailsecret"'),
    ("K20", 'curl -H "Authorization: Bearer x" \\\n-d tailsecret \\\ntailsecret2\necho fourth'),
    ("K21", "first Authorization: Bearer a\nthen Proxy-Authorization: Basic tailsecret"),
    ("K22", 'h = "Authorization: Bearer safe" if False else "tailsecret"'),
    ("K23", 'h=$(printf %s "Authorization: Bearer x")tailsecret'),
    ("K24", '$h = "Authorization: Bearer x" . q(tailsecret);'),
    ("K25", 'curl -H "Authorization: Basic dGFpbHNlY3JldA==">response.txt'),
    # A secret value before the key that runs into it (earlier leaks).
    ("L01", 'API_TOKEN="prefixsecret Authorization: Bearer tailsecret"'),
    ("L02", 'password: "prefixsecret Authorization: Bearer tailsecret"'),
    ("L03", '{"api_key": "prefixsecret Authorization: Bearer tailsecret"}'),
    ("L04", 'tool --api-key "prefixsecret Authorization: Bearer tailsecret"'),
    ("L05", 'API_TOKEN="prefixsecret Authorization: Bearer tailsecret'),
    ("L06", "client_secret: 'prefixsecret Authorization: Bearer tailsecret"),
    # The same with an odd run of backslashes right before the key.
    ("L07", 'API_TOKEN="prefixsecret\\Authorization: Bearer tailsecret'),
    ("L08", 'password: "prefixsecret\\\\\\Authorization: Bearer tailsecret'),
    ("L09", '{"api_key": "prefixsecret\\Authorization: Bearer tailsecret'),
    ("L10", 'tool --api-key "prefixsecret\\Authorization: Bearer tailsecret'),
    # A quoted value broken by a real line break before the key.
    ("L11", 'TOKEN="prefixsecret x\nAuthorization: Bearer tailsecret"'),
    ("L12", 'password: "prefixsecret x\nAuthorization: Bearer tailsecret"'),
    ("L13", '{"api_key": "prefixsecret x\nAuthorization: Bearer tailsecret"}'),
    # Token shapes before the key, with and without a NAME= prefix.
    ("L14", GHP + " then curl -H 'Authorization: Basic %s'" % B64),
    ("L15", "Bearer abcdefghijkl\nAuthorization: Basic %s" % B64),
    ("L16", PEM + "\nAuthorization: Basic %s" % B64),
    ("L17", "GH=" + GHP + " Authorization: Basic %s" % B64),
    ("L18", "TOKEN=Bearer abcdefghijkl Authorization: Basic %s" % B64),
    # The key is the value of another secret pattern.
    ("L19", "--token Authorization: Basic %s" % B64),
    ("L20", "SECRET=Authorization: Basic %s" % B64),
    ("L21", "export AUTH_TOKEN=Authorization:Basic %s" % B64),
    # A key right after a written escape or a percent-encoded byte.
    ("E01", 'printf "GET / HTTP/1.1\\r\\nAuthorization: Basic %s\\r\\n\\r\\n"' % B64),
    ("E02", 'req = "Accept: json\\nAuthorization: Basic %s"' % B64),
    ("E03", 's = "x\\tAuthorization: Basic %s"' % B64),
    ("E04", 's = "x\\\\r\\\\nAuthorization: Basic %s"' % B64),
    ("E05", 's = "x\\x0aAuthorization: Basic %s"' % B64),
    ("E06", '{"h": "x\\u000aAuthorization: Basic %s"}' % B64),
    ("E07", "q=%%7B%%22Authorization%%22%%3A%%22Basic %s%%22%%7D" % B64),
    ("E08", "next=%%2522Authorization%%2522%%253A%%2522Basic %s" % B64),
    # A dash before the name, as in custom headers.
    ("D01", "X-Authorization: Basic %s" % B64),
    ("D02", 'X-Proxy-Authorization: Digest username="u", response="tailsecret"'),
    ("D03", 'curl -H "x-authorization: Token tailsecret" https://x'),
    ("D04", "the Authorization-related fix landed\nnext: tailsecret"),
]

# Text where the name runs on into a longer word: not a key, kept unchanged.
NOT_KEYS = [
    ("N01", "the reauthorization flow is done"),
    ("N02", "two authorizations are pending"),
    ("N03", "see authorization_url in the config"),
    # Escape markers are lowercase only: an uppercase letter after the
    # backslash is not an escape, so the name runs on from a letter.
    ("N05", "x\\NAuthorization"),
    ("N06", "x\\RAuthorization"),
    ("N07", "x\\TAuthorization"),
    ("N08", "x\\X0aAuthorization"),
    ("N09", "x\\U000aAuthorization"),
]


@pytest.mark.parametrize("case,text", KEYED, ids=[c[0] for c in KEYED])
def test_section_with_a_key_is_redacted_whole(case, text):
    out = handoff.redact(text)
    assert out == R
    for secret in SECRETS:
        assert secret not in out


@pytest.mark.parametrize("case,text", NOT_KEYS, ids=[c[0] for c in NOT_KEYS])
def test_name_inside_a_longer_word_is_not_a_key(case, text):
    assert handoff.redact(text) == text


def test_N04_section_without_a_key_keeps_the_other_patterns():
    text = "kept line\nDB_PASSWORD=hunter2fake and " + GHP
    assert handoff.redact(text) == "kept line\nDB_PASSWORD=" + R + " and " + R


def _body(note, name):
    return note.split("## %s\n\n" % name, 1)[1].split("\n\n## ", 1)[0]


def test_S01_key_in_a_later_section_leaves_earlier_sections_alone():
    content = {
        "Goal": "kept goal text",
        "Where we stopped": ["ran it", "curl -H 'Authorization: Bearer tailsecret'", "also gone"],
    }
    note, _ = handoff.build_note(content, "r", "s")
    assert _body(note, "Goal") == "kept goal text"
    assert _body(note, "Where we stopped") == R
    assert "tailsecret" not in note and "ran it" not in note and "also gone" not in note


def test_S02_key_in_an_earlier_section_leaves_later_sections_alone():
    content = {
        "Goal": '"Authorization: Bearer x\ntailsecret\nmore tailsecret',
        "Where we stopped": "next body kept",
        "Open questions": ["kept question"],
    }
    note, _ = handoff.build_note(content, "r", "s")
    assert _body(note, "Goal") == R
    assert _body(note, "Where we stopped") == "next body kept"
    assert _body(note, "Open questions") == "- kept question"
    assert "tailsecret" not in note


def test_S03_key_in_the_header_redacts_the_header_whole():
    note, _ = handoff.build_note({"Goal": "kept goal text"}, "r", "x Authorization: Bearer tailsecret")
    # The blank line that ends the header is layout and is kept, so the first
    # heading stays a heading.
    assert note.startswith(R + "\n\n## Goal\n\n")
    assert _body(note, "Goal") == "kept goal text"
    assert "tailsecret" not in note and "Written:" not in note


def test_S04_header_without_a_key_is_unchanged():
    note, _ = handoff.build_note({"Goal": "g"}, "r", "s")
    assert note.startswith("# Handoff: r / s\n\nWritten: ")


def test_S05_key_in_the_repo_redacts_the_header_whole():
    note, _ = handoff.build_note({"Goal": "kept goal text"}, "x Authorization: Bearer tailsecret", "s")
    assert note.startswith(R + "\n\n## Goal\n\n")
    assert _body(note, "Goal") == "kept goal text"
    assert "tailsecret" not in note and "Written:" not in note


def test_S06_key_past_the_header_field_limit_redacts_the_header_whole():
    # The key starts beyond the point where a long field is cut, so it is
    # only seen when the field is judged before the cut.
    slug = "a" * (handoff.HEADER_FIELD_MAX + 20) + " Authorization: Bearer tailsecret"
    note, _ = handoff.build_note({"Goal": "kept goal text"}, "r", slug)
    assert note.startswith(R + "\n\n## Goal\n\n")
    assert _body(note, "Goal") == "kept goal text"
    for gone in ("tailsecret", "Bearer", "Authorization", "aaaa", "Written:"):
        assert gone not in note


# Case T01. Inputs that make a scan do the most work per character:
# candidates that almost form a key, and a key only at the very end.
LONG_SECTIONS = {
    "near-keys": lambda n: ("xAuthorization -Authorization _HTTP_ HTTP_PROXY_ REDIRECT_ %2 \\x0 " * n)[:n],
    "key-at-end": lambda n: ("a" * n) + " Authorization: Bearer tailsecret",
}
SHORT_LENGTH = 40_000
LONG_LENGTH = 4 * SHORT_LENGTH
REPETITIONS = 9
LIMIT_SECONDS = 1.0
RATIO_BOUND = 8


def _fastest(text):
    best = float("inf")
    for _ in range(REPETITIONS):
        start = time.perf_counter()
        handoff.redact(text)
        call = time.perf_counter() - start
        if call > LIMIT_SECONDS:
            return call
        best = min(best, call)
    return best


@pytest.mark.parametrize("shape", list(LONG_SECTIONS))
def test_T01_large_section_redacts_in_linear_time(shape):
    # Four times the input should take about four times as long (sixteen if
    # quadratic); the bound of eight sits between. The fastest of several
    # calls is kept because runner noise only ever adds time.
    short = _fastest(LONG_SECTIONS[shape](SHORT_LENGTH))
    assert short <= LIMIT_SECONDS
    long = _fastest(LONG_SECTIONS[shape](LONG_LENGTH))
    assert long <= LIMIT_SECONDS
    assert long <= RATIO_BOUND * max(short, 1e-4), (short, long)
    assert handoff.redact(LONG_SECTIONS["key-at-end"](LONG_LENGTH)) == R

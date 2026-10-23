"""Authorization redaction: from any recognised key to the end of the section.

Every case in references/authorization-redaction.md is here with its exact
expected output. The case number in each test id is the number in that file.
"""
import time

import pytest

import handoff

R = handoff.REDACTED
SECRET = "tailsecret"
B64 = "dXNlcjpwYXNz"

# (case number, input section body, exact redact() output)
CASES = [
    # Carried from the earlier designs.
    ("01", """curl -H "Authorization: Digest username="'"u", response="tailsecret"' https://x""",
     'curl -H "Authorization' + R),
    ("02", 'curl -H "Authorization: Digest a=","response=tailsecret" https://x',
     'curl -H "Authorization' + R),
    ("03", "curl -H 'Authorization: Digest a=','response=tailsecret' https://x",
     "curl -H 'Authorization" + R),
    ("04", 'curl -H "Authorization: Bearer "}tailsecret https://x',
     'curl -H "Authorization' + R),
    ("05", 'curl -H "Authorization: Bearer "]tailsecret https://x',
     'curl -H "Authorization' + R),
    ("06", 'headers = ["Authorization: Digest a=1, response=tailsecret"], retries = 3',
     'headers = ["Authorization' + R),
    ("07", 'headers["Authorization"] = "Basic tailsecret"  # comment',
     'headers["Authorization' + R),
    ("08", '{"Authorization": "Bearer tailsecret", "Accept": "json"}',
     '{"Authorization' + R),
    ("09", 'Authorization: Digest username="u", response="tailsecret"',
     "Authorization" + R),
    ("10", 'curl -H "Authorization: Bearer tail\\"secret" https://x',
     'curl -H "Authorization' + R),
    ("11", '["Authorization: Bearer x","Accept: tailsecret"]',
     '["Authorization' + R),
    ("12", """curl -H "Authorization: Digest algorithm=MD5",username='"u"',nonce='"tailsecret"' https://x""",
     'curl -H "Authorization' + R),
    ("13", 'curl -H "Authorization: Basic dGFpbHNlY3JldA==">response.txt',
     'curl -H "Authorization' + R),
    ("14", '["Authorization: Bearer x" "tailsecret"]',
     '["Authorization' + R),
    ("15", 'h = "Authorization: Bearer " + "tailsecret"',
     'h = "Authorization' + R),
    ("16", '"Authorization: Bearer x" f"tailsecret"',
     '"Authorization' + R),
    ("17", 'h = "Authorization: Bearer safe" if False else "tailsecret"',
     'h = "Authorization' + R),
    ("18", 'h = "Authorization: Bearer x" or "tailsecret"',
     'h = "Authorization' + R),
    ("19a", 'curl -H "Authorization: Bearer x" <"tailsecret"',
     'curl -H "Authorization' + R),
    ("19b", 'curl -H "Authorization: Bearer x">"tailsecret"',
     'curl -H "Authorization' + R),
    ("20", "curl -H \"Authorization: Bearer x\"'tailsecret'",
     'curl -H "Authorization' + R),
    ("21a", 'curl -H "Authorization: Bearer x" `echo tailsecret`',
     'curl -H "Authorization' + R),
    ("21b", 'curl -H "Authorization: Bearer x" “tailsecret”',
     'curl -H "Authorization' + R),
    ("22", 'curl -H "Authorization: Bearer x" https://example.com',
     'curl -H "Authorization' + R),
    ("23", 'curl -s -H "Authorization: Bearer tailsecret" -X POST | jq .',
     'curl -s -H "Authorization' + R),
    ("24", 'echo $(curl -H "Authorization: Bearer tailsecret") done',
     'echo $(curl -H "Authorization' + R),
    ("25", 'h=$(printf %s "Authorization: Bearer x")tailsecret',
     'h=$(printf %s "Authorization' + R),
    ("26", 'h="Authorization: Bearer x"\\\ntailsecret\necho third',
     'h="Authorization' + R),
    ("27", '$h = "Authorization: Bearer x" . q(tailsecret);',
     '$h = "Authorization' + R),
    ("28", 'curl -H "Authorization: Bearer x\ntailsecret\nmore" https://x\necho kept',
     'curl -H "Authorization' + R),
    ("30", 'curl -H "Authorization: Bearer x" \\\n-d tailsecret \\\ntailsecret2\necho fourth',
     'curl -H "Authorization' + R),
    ("31", "Authorization: Bearer x\\\ntailsecret",
     "Authorization" + R),
    ("32", 'curl -H "Authorization: Bearer tailsecret',
     'curl -H "Authorization' + R),
    # Added with this rule.
    ("33", 'h = ("Authorization: Bearer "\n"tailsecret")',
     'h = ("Authorization' + R),
    ("34", '[("Authorization", "Basic %s")]' % B64,
     '[("Authorization' + R),
    ("35", "HTTP_AUTHORIZATION=Basic %s" % B64,
     "HTTP_AUTHORIZATION" + R),
    ("36", "REDIRECT_HTTP_AUTHORIZATION=Basic %s" % B64,
     "REDIRECT_HTTP_AUTHORIZATION" + R),
    ("37", 'curl -H "Authorization: Bearer pre\\"\ntailsecret"',
     'curl -H "Authorization' + R),
    ("39", "first Authorization: Bearer a\nthen Proxy-Authorization: Basic tailsecret",
     "first Authorization" + R),
    ("40", "Fixed it: the Authorization header is required.\nNext: tailsecret",
     "Fixed it: the Authorization" + R),
    ("41", "Proxy-Authorization: Basic %s" % B64,
     "Proxy-Authorization" + R),
    ("42", "HTTP_PROXY_AUTHORIZATION=Basic %s" % B64,
     "HTTP_PROXY_AUTHORIZATION" + R),
    ("43", "my %%h = ('Authorization' => 'Basic %s');" % B64,
     "my %h = ('Authorization" + R),
    ("44", "headers.add('Authorization', 'Basic %s')" % B64,
     "headers.add('Authorization" + R),
    ("45", '"Authorization" = "Basic %s"' % B64,
     '"Authorization' + R),
    ("46a", "AUTHORIZATION=Basic %s" % B64,
     "AUTHORIZATION" + R),
    ("46b", "authorization: basic %s" % B64,
     "authorization" + R),
    ("47", 'proxy_set_header Authorization "Basic %s";' % B64,
     "proxy_set_header Authorization" + R),
    ("48", "ran ok\nthen curl -H 'proxy-authorization: Basic %s' https://x" % B64,
     "ran ok\nthen curl -H 'proxy-authorization" + R),
    # A secret value that starts before the key and runs into it: the part
    # before the key is still redacted by the other patterns.
    ("52a", 'API_TOKEN="prefixsecret Authorization: Bearer tailsecret"',
     "API_TOKEN=" + R + "Authorization" + R),
    ("52b", 'password: "prefixsecret Authorization: Bearer tailsecret"',
     "password: " + R + "Authorization" + R),
    ("52c", '{"api_key": "prefixsecret Authorization: Bearer tailsecret"}',
     '{"api_key": "' + R + "Authorization" + R),
    ("52d", 'tool --api-key "prefixsecret Authorization: Bearer tailsecret"',
     "tool --api-key " + R + "Authorization" + R),
    # The same with no closing quote anywhere, and two patterns overlapping
    # on one value before the key (one marker for both).
    ("52e", 'API_TOKEN="prefixsecret Authorization: Bearer tailsecret',
     "API_TOKEN=" + R + "Authorization" + R),
    ("52f", "client_secret: 'prefixsecret Authorization: Bearer tailsecret",
     "client_secret: " + R + "Authorization" + R),
    ("52g", "MY_TOKEN=ghp_" + "a1b2c3d4e5f6g7h8i9j0k1l2 Authorization: Basic %s" % B64,
     "MY_TOKEN=" + R + " Authorization" + R),
    # The key is the value of another secret pattern: the key must still be
    # found, so the cut is decided on the text as given.
    ("53a", "--token Authorization: Basic %s" % B64,
     "--token Authorization" + R),
    ("53b", "SECRET=Authorization: Basic %s" % B64,
     "SECRET=Authorization" + R),
    ("53c", "export AUTH_TOKEN=Authorization:Basic %s" % B64,
     "export AUTH_TOKEN=Authorization" + R),
    # A key right after a written escape sequence (the text holds a
    # backslash and a letter, not a control character).
    ("54a", 'printf "GET / HTTP/1.1\\r\\nAuthorization: Basic %s\\r\\n\\r\\n"' % B64,
     'printf "GET / HTTP/1.1\\r\\nAuthorization' + R),
    ("54b", 'req = "Accept: json\\nAuthorization: Basic %s"' % B64,
     'req = "Accept: json\\nAuthorization' + R),
    ("54c", 's = "x\\tAuthorization: Basic %s"' % B64,
     's = "x\\tAuthorization' + R),
    ("54d", 's = "x\\\\r\\\\nAuthorization: Basic %s"' % B64,
     's = "x\\\\r\\\\nAuthorization' + R),
    ("54e", 's = "x\\x0aAuthorization: Basic %s"' % B64,
     's = "x\\x0aAuthorization' + R),
    ("54f", '{"h": "x\\u000aAuthorization: Basic %s"}' % B64,
     '{"h": "x\\u000aAuthorization' + R),
    # A key right after a percent-encoded byte, once or twice encoded.
    ("55a", "q=%%7B%%22Authorization%%22%%3A%%22Basic %s%%22%%7D" % B64,
     "q=%7B%22Authorization" + R),
    ("55b", "next=%%2522Authorization%%2522%%253A%%2522Basic %s" % B64,
     "next=%2522Authorization" + R),
]

# Text where the name runs on into a longer word: not a key, kept unchanged.
NOT_KEYS = [
    ("50a", "the reauthorization flow is done"),
    ("50b", "two authorizations are pending"),
    ("50c", "see authorization_url in the config"),
]


@pytest.mark.parametrize("case,text,expected", CASES, ids=[c[0] for c in CASES])
def test_authorization_case_redacts_to_the_end_of_the_section(case, text, expected):
    out = handoff.redact(text)
    assert out == expected
    assert SECRET not in out and B64 not in out


@pytest.mark.parametrize("case,text", NOT_KEYS, ids=[c[0] for c in NOT_KEYS])
def test_name_inside_a_longer_word_is_not_a_key(case, text):
    assert handoff.redact(text) == text


def _body(note, name):
    return note.split("## %s\n\n" % name, 1)[1].split("\n\n## ", 1)[0]


def test_29_unterminated_value_redacts_to_the_section_end_only():
    content = {
        "Goal": '"Authorization: Bearer x\ntailsecret\nmore tailsecret',
        "Where we stopped": "next body kept",
    }
    note, _ = handoff.build_note(content, "r", "s")
    assert _body(note, "Goal") == '"Authorization' + R
    assert _body(note, "Where we stopped") == "next body kept"
    assert SECRET not in note


def test_38_key_in_a_later_section_leaves_earlier_sections_alone():
    content = {
        "Goal": "kept goal text",
        "Where we stopped": "kept stop text",
        "Verified": ["ran it", "curl -H 'Authorization: Bearer tailsecret'", "also gone"],
        "Open questions": ["kept question"],
    }
    note, _ = handoff.build_note(content, "r", "s")
    assert _body(note, "Goal") == "kept goal text"
    assert _body(note, "Where we stopped") == "kept stop text"
    assert _body(note, "Verified") == "- ran it\n- curl -H 'Authorization" + R
    assert _body(note, "Open questions") == "- kept question"
    assert SECRET not in note and "also gone" not in note


def test_49_key_in_the_header_stays_in_the_header():
    note, _ = handoff.build_note({"Goal": "kept goal text"}, "r", "x Authorization: Bearer tailsecret")
    # The header's own text is redacted to its end; the blank line that ends
    # the header is layout and is kept, so the first heading stays a heading.
    assert note.startswith("# Handoff: r / x Authorization" + R + "\n\n## Goal\n\n")
    assert _body(note, "Goal") == "kept goal text"
    assert SECRET not in note


# Case 51. Inputs that make a scan do the most work per character: candidates that
# almost form a key, and a key only at the very end of a large section.
LONG_SECTIONS = {
    "near-keys": lambda n: ("xAuthorization -Authorization _HTTP_ HTTP_PROXY_ REDIRECT_ " * n)[:n],
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
def test_large_section_redacts_in_linear_time(shape):
    # Four times the input should take about four times as long (sixteen if
    # quadratic); the bound of eight sits between. The fastest of several
    # calls is kept because runner noise only ever adds time.
    short = _fastest(LONG_SECTIONS[shape](SHORT_LENGTH))
    assert short <= LIMIT_SECONDS
    long = _fastest(LONG_SECTIONS[shape](LONG_LENGTH))
    assert long <= LIMIT_SECONDS
    assert long <= RATIO_BOUND * max(short, 1e-4), (short, long)
    out = handoff.redact(LONG_SECTIONS["key-at-end"](LONG_LENGTH))
    assert out.endswith(" Authorization" + R) and SECRET not in out

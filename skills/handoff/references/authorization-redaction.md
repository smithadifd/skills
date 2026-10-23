# Authorization redaction: from the key to the end of the section

DESIGN: from any recognised Authorization key, redact everything after the key through the end of the section it appears in; over-redact, never leak, and no condition keeps text after the key.

Why: the earlier designs kept the text after the credential on some condition (after a closing quote, after the end of a line, outside a continuation), and every review found a form that leaked through the kept part: shell and Python string concatenation, `"safe" if False else "secret"`, `<` read as a comparison, `)tail` after a command substitution, a Perl `. q(tail)` literal, a value continued on the next line, an escaped quote across a line break. Deciding which kept text is harmless needs a shell, Python and Perl grammar. This rule needs none: it finds a key and stops looking.

## 1. The key grammar

A key is one of these names, compared case-insensitively:

- `Authorization`
- `Proxy-Authorization`
- `HTTP_AUTHORIZATION`
- `REDIRECT_HTTP_AUTHORIZATION`
- `HTTP_PROXY_AUTHORIZATION`

The name must stand alone as a token: the character before it is not a letter, digit, underscore or dash, and the character after it is not a letter, digit or underscore. So `reauthorization`, `authorizations` and `authorization_url` are not keys, and a prefix such as `X-` or `MY_` is not part of a key unless it forms one of the names above.

Escapes and encodings: a letter or digit before the name still counts as a boundary when it ends a written escape sequence or a percent-encoded byte, because then it stands for a separator, not a word character. The accepted forms are `\n`, `\r`, `\t`, `\xHH` and `\uHHHH` (so `"…\r\nAuthorization: …"` and `"x\tAuthorization: …"` are keys, and so is the doubly escaped `\\r\\nAuthorization`), and `%HH` and `%25HH` (so `%22Authorization%22%3A…` and the twice-encoded `%2522Authorization` are keys).

Percent-encoded letters inside the name (`%41uthorization`) are not decoded. URL encoders do not encode letters, digits, `-` or `_` (RFC 3986 calls them unreserved), so a real encoded key keeps the name in plain letters, as in `%22Authorization%22`. Only a hand-built string would encode them, and matching that would mean a second, decoding pass for no input seen in practice.

Nothing else is required. A key needs no separator, quote or bracket around it, so every shape is covered by the same test: a bare header (`Authorization: …`), an assignment (`AUTHORIZATION=…`, `"Authorization" = …`), a mapping key (`{"Authorization": …}`, `'Authorization' => …`), a quoted-key tuple or call argument (`("Authorization", …)`, `headers.add('Authorization', …)`), and a space-separated config line (`proxy_set_header Authorization "…";`).

Prose: a mention in a sentence ("the Authorization header is required") is also a key, and the rest of that section is redacted. This is the over-redacting side on purpose. A rule that told prose from a key would need a separator, and real credential forms have none (`proxy_set_header Authorization "Basic …"`, `RequestHeader set Authorization "…"`, `Authorization header: Basic …`), so any such condition would reopen a leak.

## 2. Where redaction starts and stops

- Start: right after the key's last character. The key stays visible; everything after it (the closing quote of a quoted key, the separator, the scheme word, the value) is redacted.
- Stop: the end of the section. `handoff.py` redacts each section body as one string (list items arrive as `- ` lines of that string), and the note header separately. Redaction never reaches the next section, and a key in a later section never touches an earlier one.
- The first key in a section decides it; later keys in the same section are already inside the redacted span.
- The header: the blank line that ends the note header is layout added by the script, not content, so it is kept and the first heading stays a heading.

## 3. The marker

The redacted span becomes one `[REDACTED]`, so the output is the section text up to and including the key, then `[REDACTED]`, and nothing after it: `Authorization[REDACTED]`, `curl -H "Authorization[REDACTED]`, `{"Authorization[REDACTED]`. No closing quote is added.

## 4. The other patterns, before the key

The other `redact()` patterns (token shapes, `*_TOKEN=` assignments, `NAME: value`, JSON pairs, `--token`-style flags) still redact the text before the key. Two orderings each leak, so neither is used:

- Running them first lets one of them consume the key: `--token Authorization: Basic <b64>` becomes `--token [REDACTED] Basic <b64>`, and the key is gone before it is looked for.
- Cutting at the key first removes the closing quote they need: in `API_TOKEN="prefixsecret Authorization: …"` the quoted value no longer closes, so `prefixsecret` is kept.

Instead, the key is found on the original text, and the other patterns are matched on the original text too. Every part they would replace that starts before the key is masked, and a part that runs into the key is masked up to the key. They are matched once more on the text before the key closed by a `"` and once by a `'`, so a quoted value that has no closing quote at all is masked too. Overlapping or adjacent masks become one `[REDACTED]`. The output is the masked text before the key, the key, and `[REDACTED]`. A section with no key is redacted by the other patterns exactly as before.

## 5. Cost

Lost: everything after the key in that section. That includes the benign rest of a header line (`-X POST`, a URL, a trailing comment, the other pairs of a JSON object), every later line of the section (the rest of a multi-line command, later list items), and, after a prose mention, the rest of that section's prose. Other sections are untouched.

This is acceptable for a resume note: it records where the work stood, and a command can be rebuilt from the repository or shell history, while a credential persisted in a note cannot be taken back. Keep Authorization mentions out of the sections that matter most, or put them last.

Not covered: a custom header whose name is not one of the five above (for example `X-Authorization: …` or `MY_AUTHORIZATION=…`). The bearer-token and `NAME=value` patterns may still catch its value; the name list can grow if one is found in practice. Also not covered: a name run into a longer word by a plain letter, as in curl's unspaced `-HAuthorization:…` or `setAuthorizationHeader(…)`, and percent-encoded letters inside the name (section 1).

## 6. Required tests

Secret markers `tailsecret` and `dXNlcjpwYXNz`: neither may appear in the output. `R` stands for `[REDACTED]`. Each expected output is exact. A `\n` is a line break inside the same section; `\` at the end of a line is a literal backslash. Cases 1–51 are from the first round; 52–55 were added in the second.

Carried from the v2 design (32 cases; 19 and 21 have two inputs each):

| # | Input | Expected |
|---|---|---|
| 1 | `curl -H "Authorization: Digest username="'"u", response="tailsecret"' https://x` | `curl -H "AuthorizationR` |
| 2 | `curl -H "Authorization: Digest a=","response=tailsecret" https://x` | `curl -H "AuthorizationR` |
| 3 | `curl -H 'Authorization: Digest a=','response=tailsecret' https://x` | `curl -H 'AuthorizationR` |
| 4 | `curl -H "Authorization: Bearer "}tailsecret https://x` | `curl -H "AuthorizationR` |
| 5 | `curl -H "Authorization: Bearer "]tailsecret https://x` | `curl -H "AuthorizationR` |
| 6 | `headers = ["Authorization: Digest a=1, response=tailsecret"], retries = 3` | `headers = ["AuthorizationR` |
| 7 | `headers["Authorization"] = "Basic tailsecret"  # comment` | `headers["AuthorizationR` |
| 8 | `{"Authorization": "Bearer tailsecret", "Accept": "json"}` | `{"AuthorizationR` |
| 9 | `Authorization: Digest username="u", response="tailsecret"` | `AuthorizationR` |
| 10 | `curl -H "Authorization: Bearer tail\"secret" https://x` | `curl -H "AuthorizationR` |
| 11 | `["Authorization: Bearer x","Accept: tailsecret"]` | `["AuthorizationR` |
| 12 | `curl -H "Authorization: Digest algorithm=MD5",username='"u"',nonce='"tailsecret"' https://x` | `curl -H "AuthorizationR` |
| 13 | `curl -H "Authorization: Basic dGFpbHNlY3JldA==">response.txt` | `curl -H "AuthorizationR` |
| 14 | `["Authorization: Bearer x" "tailsecret"]` | `["AuthorizationR` |
| 15 | `h = "Authorization: Bearer " + "tailsecret"` | `h = "AuthorizationR` |
| 16 | `"Authorization: Bearer x" f"tailsecret"` | `"AuthorizationR` |
| 17 | `h = "Authorization: Bearer safe" if False else "tailsecret"` | `h = "AuthorizationR` |
| 18 | `h = "Authorization: Bearer x" or "tailsecret"` | `h = "AuthorizationR` |
| 19a | `curl -H "Authorization: Bearer x" <"tailsecret"` | `curl -H "AuthorizationR` |
| 19b | `curl -H "Authorization: Bearer x">"tailsecret"` | `curl -H "AuthorizationR` |
| 20 | `curl -H "Authorization: Bearer x"'tailsecret'` | `curl -H "AuthorizationR` |
| 21a | ``curl -H "Authorization: Bearer x" `echo tailsecret` `` | `curl -H "AuthorizationR` |
| 21b | `curl -H "Authorization: Bearer x" “tailsecret”` | `curl -H "AuthorizationR` |
| 22 | `curl -H "Authorization: Bearer x" https://example.com` | `curl -H "AuthorizationR` |
| 23 | `curl -s -H "Authorization: Bearer tailsecret" -X POST \| jq .` | `curl -s -H "AuthorizationR` |
| 24 | `echo $(curl -H "Authorization: Bearer tailsecret") done` | `echo $(curl -H "AuthorizationR` |
| 25 | `h=$(printf %s "Authorization: Bearer x")tailsecret` | `h=$(printf %s "AuthorizationR` |
| 26 | `h="Authorization: Bearer x"\` `\n` `tailsecret` `\n` `echo third` | `h="AuthorizationR` (the third line is now redacted too) |
| 27 | `$h = "Authorization: Bearer x" . q(tailsecret);` | `$h = "AuthorizationR` |
| 28 | `curl -H "Authorization: Bearer x` `\n` `tailsecret` `\n` `more" https://x` `\n` `echo kept` | `curl -H "AuthorizationR` (`echo kept` is now redacted too) |
| 29 | Section `"Authorization: Bearer x` `\n` `tailsecret` `\n` `more tailsecret`, then a next section `next body kept` | First section `"AuthorizationR`; next section unchanged |
| 30 | `curl -H "Authorization: Bearer x" \` `\n` `-d tailsecret \` `\n` `tailsecret2` `\n` `echo fourth` | `curl -H "AuthorizationR` (the fourth line is now redacted too) |
| 31 | `Authorization: Bearer x\` `\n` `tailsecret` | `AuthorizationR` |
| 32 | `curl -H "Authorization: Bearer tailsecret` | `curl -H "AuthorizationR` |

Added with this rule:

| # | Input | Expected |
|---|---|---|
| 33 | `h = ("Authorization: Bearer "` `\n` `"tailsecret")` | `h = ("AuthorizationR` |
| 34 | `[("Authorization", "Basic dXNlcjpwYXNz")]` | `[("AuthorizationR` |
| 35 | `HTTP_AUTHORIZATION=Basic dXNlcjpwYXNz` | `HTTP_AUTHORIZATIONR` |
| 36 | `REDIRECT_HTTP_AUTHORIZATION=Basic dXNlcjpwYXNz` | `REDIRECT_HTTP_AUTHORIZATIONR` |
| 37 | `curl -H "Authorization: Bearer pre\"` `\n` `tailsecret"` | `curl -H "AuthorizationR` |
| 38 | Sections `kept goal text`, `kept stop text`, then a list `ran it` / `curl -H 'Authorization: Bearer tailsecret'` / `also gone`, then a list `kept question` | The two earlier sections and the later one unchanged; the list section is `- ran it` `\n` `- curl -H 'AuthorizationR` |
| 39 | `first Authorization: Bearer a` `\n` `then Proxy-Authorization: Basic tailsecret` (two keys) | `first AuthorizationR` |
| 40 | `Fixed it: the Authorization header is required.` `\n` `Next: tailsecret` (prose) | `Fixed it: the AuthorizationR` (prose is over-redacted, by decision) |
| 41 | `Proxy-Authorization: Basic dXNlcjpwYXNz` | `Proxy-AuthorizationR` |
| 42 | `HTTP_PROXY_AUTHORIZATION=Basic dXNlcjpwYXNz` | `HTTP_PROXY_AUTHORIZATIONR` |
| 43 | `my %h = ('Authorization' => 'Basic dXNlcjpwYXNz');` | `my %h = ('AuthorizationR` |
| 44 | `headers.add('Authorization', 'Basic dXNlcjpwYXNz')` | `headers.add('AuthorizationR` |
| 45 | `"Authorization" = "Basic dXNlcjpwYXNz"` | `"AuthorizationR` |
| 46a | `AUTHORIZATION=Basic dXNlcjpwYXNz` | `AUTHORIZATIONR` |
| 46b | `authorization: basic dXNlcjpwYXNz` | `authorizationR` |
| 47 | `proxy_set_header Authorization "Basic dXNlcjpwYXNz";` | `proxy_set_header AuthorizationR` |
| 48 | `ran ok` `\n` `then curl -H 'proxy-authorization: Basic dXNlcjpwYXNz' https://x` | `ran ok` `\n` `then curl -H 'proxy-authorizationR` |
| 49 | Note header whose slug is `x Authorization: Bearer tailsecret` | Header `# Handoff: r / x AuthorizationR`, then the blank line, then `## Goal` with its body unchanged |
| 50a | `the reauthorization flow is done` | unchanged (not a key) |
| 50b | `two authorizations are pending` | unchanged (not a key) |
| 50c | `see authorization_url in the config` | unchanged (not a key) |
| 51 | A large section: 40 000 and 160 000 characters of near-keys (`xAuthorization -Authorization _HTTP_ HTTP_PROXY_ REDIRECT_ …`), and of plain text with one key at the very end | Each call under a second; four times the input takes at most eight times as long (linear); the key at the end is still found |

Added in review round 2. In cases 54a–54f, `\r`, `\n`, `\t`, `\x0a` and `\u000a` are written escapes (a backslash and letters in the text), not line breaks.

| # | Input | Expected |
|---|---|---|
| 52a | `API_TOKEN="prefixsecret Authorization: Bearer tailsecret"` | `API_TOKEN=RAuthorizationR` |
| 52b | `password: "prefixsecret Authorization: Bearer tailsecret"` | `password: RAuthorizationR` |
| 52c | `{"api_key": "prefixsecret Authorization: Bearer tailsecret"}` | `{"api_key": "RAuthorizationR` |
| 52d | `tool --api-key "prefixsecret Authorization: Bearer tailsecret"` | `tool --api-key RAuthorizationR` |
| 52e | `API_TOKEN="prefixsecret Authorization: Bearer tailsecret` (no closing quote) | `API_TOKEN=RAuthorizationR` |
| 52f | `client_secret: 'prefixsecret Authorization: Bearer tailsecret` (no closing quote) | `client_secret: RAuthorizationR` |
| 52g | `MY_TOKEN=ghp_<token> Authorization: Basic dXNlcjpwYXNz` (two patterns overlap on one value) | `MY_TOKEN=R AuthorizationR` |
| 53a | `--token Authorization: Basic dXNlcjpwYXNz` | `--token AuthorizationR` |
| 53b | `SECRET=Authorization: Basic dXNlcjpwYXNz` | `SECRET=AuthorizationR` |
| 53c | `export AUTH_TOKEN=Authorization:Basic dXNlcjpwYXNz` | `export AUTH_TOKEN=AuthorizationR` |
| 54a | `printf "GET / HTTP/1.1\r\nAuthorization: Basic dXNlcjpwYXNz\r\n\r\n"` | `printf "GET / HTTP/1.1\r\nAuthorizationR` |
| 54b | `req = "Accept: json\nAuthorization: Basic dXNlcjpwYXNz"` | `req = "Accept: json\nAuthorizationR` |
| 54c | `s = "x\tAuthorization: Basic dXNlcjpwYXNz"` | `s = "x\tAuthorizationR` |
| 54d | `s = "x\\r\\nAuthorization: Basic dXNlcjpwYXNz"` | `s = "x\\r\\nAuthorizationR` |
| 54e | `s = "x\x0aAuthorization: Basic dXNlcjpwYXNz"` | `s = "x\x0aAuthorizationR` |
| 54f | `{"h": "x\u000aAuthorization: Basic dXNlcjpwYXNz"}` | `{"h": "x\u000aAuthorizationR` |
| 55a | `q=%7B%22Authorization%22%3A%22Basic dXNlcjpwYXNz%22%7D` | `q=%7B%22AuthorizationR` |
| 55b | `next=%2522Authorization%2522%253A%2522Basic dXNlcjpwYXNz` | `next=%2522AuthorizationR` |

## 7. Mutations (suite-level kills)

Each mutation is applied on a scratch copy and must turn the named tests red; restored, all are green.

| Mutation | Killed by |
|---|---|
| Drop the alias names (only `Authorization` itself is a key) | 35, 36, 41, 42, 48 |
| Drop the tuple / quoted-key shape (a key followed by a closing quote is not a key) | 7, 8, 34, 43, 44, 45 |
| Stop the redaction at the end of the line instead of the end of the section | 26, 28, 29, 30, 31, 33, 37, 38, 39, 40, 49 |
| Start the redaction after the value's closing quote instead of right after the key | 7, 8, 15, 33, 34, 43, 44, 45, 47 |
| Cut at the key first, then run the other patterns on what is left | 52a–52f, 53a–53c |
| Cut moved after the other patterns (look for the key only in their output) | 52a–52f, 53a–53c (53a–53c also leak `dXNlcjpwYXNz`) |
| Drop the escape boundaries (`\n`, `\r`, `\t`, `\xHH`, `\uHHHH`) | 54a–54f |
| Drop the percent-encoded byte boundaries (`%HH`, `%25HH`) | 55a, 55b |
| Match the other patterns on the original text only, not also on the quote-closed text before the key | 52e, 52f |

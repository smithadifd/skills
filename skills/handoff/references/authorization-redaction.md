# Authorization redaction: the whole section

DESIGN: when a section holds a recognised Authorization key, the whole section, from its first character to its last, becomes `[REDACTED]`; nothing before or after the key is kept, and nothing composes with the other patterns.

Why: earlier designs kept some of the section. One kept the text after the credential on some condition, and each review found a form that leaked through it: shell and Python string concatenation, `"safe" if False else "secret"`, a Perl `. q(tail)` literal, a value continued on the next line, an escaped quote across a line break. The next one kept the text before the key and ran the other patterns over it. That leaked a secret whose quoted value ran into the key, and the fixes for it leaked in turn: a missing closing quote, an odd run of backslashes before the key, a value broken by a line break. Each fix needed more grammar. Redacting the whole section needs none: it finds a key and returns one marker.

## 1. The key grammar

A key is one of these names, compared case-insensitively (only the name; the escape markers below are not):

- `Authorization`
- `HTTP_AUTHORIZATION`
- `REDIRECT_HTTP_AUTHORIZATION`
- `HTTP_PROXY_AUTHORIZATION`

The name must stand alone as a token: the character before it is not a letter, digit or underscore, and the character after it is not a letter, digit or underscore. So `reauthorization`, `authorizations` and `authorization_url` are not keys, and a prefix such as `MY_` is not part of a key unless it forms one of the names above. A dash is a boundary on both sides, so `Proxy-Authorization`, `X-Authorization`, `X-Proxy-Authorization` and `Authorization-related` are all keys through the plain name. This keeps every form the earlier single-header pattern (`\bAuthorization` followed by `:` or `=` and a scheme) redacted; see section 6 for the comparison.

Escapes and encodings: a letter or digit before the name still counts as a boundary when it ends a written escape sequence or a percent-encoded byte, because then it stands for a separator, not a word character. The accepted forms are the lowercase `\n`, `\r`, `\t`, `\xHH` and `\uHHHH` (so `"…\r\nAuthorization: …"` and `"x\tAuthorization: …"` are keys, and so is the doubly escaped `\\r\\nAuthorization`), and `%HH` and `%25HH` (so `%22Authorization%22%3A…` and the twice-encoded `%2522Authorization` are keys). An uppercase `\N`, `\X0a` or `\U000a` is not an escape, so a name right after it runs on from a letter and is not a key.

Percent-encoded letters inside the name (`%41uthorization`) are not decoded. URL encoders do not encode letters, digits, `-` or `_` (RFC 3986 calls them unreserved), so a real encoded key keeps the name in plain letters, as in `%22Authorization%22`.

Nothing else is required. A key needs no separator, quote or bracket, so every shape is covered by the same test: a bare header (`Authorization: …`), an assignment (`AUTHORIZATION=…`, `"Authorization" = …`), a mapping key (`{"Authorization": …}`, `'Authorization' => …`), a quoted-key tuple or call argument (`("Authorization", …)`, `headers.add('Authorization', …)`), a space-separated config line (`proxy_set_header Authorization "…";`), and a prose mention ("the Authorization header is required"). Prose is redacted on purpose: telling prose from a key would need a separator, and real credential lines have none.

## 2. What is redacted

- A section is one string: `handoff.py` redacts each section body on its own (list items arrive as `- ` lines of that string), and the note header on its own.
- A section that holds a key anywhere becomes exactly `[REDACTED]`: one marker, nothing before it, nothing after it. The same marker as every other pattern, so the note has one redaction mark.
- A section with no key is redacted by the other patterns (token shapes, `NAME=value`, `NAME: value`, JSON pairs, `--token`-style flags) exactly as before.
- Other sections are untouched: a key in one section never changes another, earlier or later.
- The header: when the header text holds a key (it carries the repository name, the slug and the date), the header text becomes `[REDACTED]`. Each field is judged whole, on its one-line form, before a long field is shortened, so a key past the cut still counts. A header with no key has each field redacted by the other patterns before it is shortened, so the cut never leaves part of a secret. The assembled header is then redacted once more, so a key or a token shape that the cut itself makes (`Authorizationxyz` cut to `Authorization...`) is caught too. The blank line that ends the header is layout added by the script, not content, so it is kept and the first heading stays a heading.

## 3. Cost

Lost: the whole text of every section that holds a key, including the parts that held no secret (the commands around a curl line, earlier and later list items, a sentence that only mentions the header). The heading stays, so the reader sees which section was removed.

This is acceptable for a resume note: it records where the work stood, and a command can be rebuilt from the repository or shell history, while a credential persisted in a note cannot be taken back. Keep Authorization mentions in their own short section, or out of the note.

Not covered: a name joined to a prefix by an underscore that is not one of the names above (for example `MY_AUTHORIZATION=…`), a name joined to a longer word by a plain letter (curl's unspaced `-HAuthorization:…`, `setAuthorizationHeader(…)`), other escapes before the name (octal `\012`, Python's `\U0000000a`, percent-encoding three times or more), and percent-encoded letters inside the name. The other patterns may still catch their values.

## 4. Required tests

`R` stands for `[REDACTED]`. Unless noted, the input is one section body and the expected output is exactly `R`; no byte of `tailsecret`, `dXNlcjpwYXNz`, `prefixsecret`, the `ghp_` token or `abcdefghijkl` may remain. A `\n` written in a case is a line break inside the section, except in E01–E06, where `\r`, `\n`, `\t`, `\x0a` and `\u000a` are written escapes (a backslash and letters in the text).

Key shapes and names:

| # | Input |
|---|---|
| K01 | `Authorization: Digest username="u", response="tailsecret"` |
| K02 | `curl -H "Authorization: Bearer tailsecret" https://x` |
| K03 | `curl -H 'Authorization: Basic dXNlcjpwYXNz' https://x` |
| K04 | `headers["Authorization"] = "Basic tailsecret"  # comment` |
| K05 | `{"Authorization": "Bearer tailsecret", "Accept": "json"}` |
| K06 | `[("Authorization", "Basic dXNlcjpwYXNz")]` |
| K07 | `headers.add('Authorization', 'Basic dXNlcjpwYXNz')` |
| K08 | `my %h = ('Authorization' => 'Basic dXNlcjpwYXNz');` |
| K09 | `"Authorization" = "Basic dXNlcjpwYXNz"` |
| K10 | `proxy_set_header Authorization "Basic dXNlcjpwYXNz";` |
| K11 | `Fixed it: the Authorization header is required.` `\n` `Next: tailsecret` (prose) |
| K12 | `AUTHORIZATION=Basic dXNlcjpwYXNz` |
| K13 | `authorization: basic dXNlcjpwYXNz` |
| K14 | `Proxy-Authorization: Basic dXNlcjpwYXNz` |
| K15 | `HTTP_AUTHORIZATION=Basic dXNlcjpwYXNz` |
| K16 | `REDIRECT_HTTP_AUTHORIZATION=Basic dXNlcjpwYXNz` |
| K17 | `HTTP_PROXY_AUTHORIZATION=Basic dXNlcjpwYXNz` |
| K18 | `h = ("Authorization: Bearer "` `\n` `"tailsecret")` |
| K19 | `curl -H "Authorization: Bearer pre\"` `\n` `tailsecret"` |
| K20 | `curl -H "Authorization: Bearer x" \` `\n` `-d tailsecret \` `\n` `tailsecret2` `\n` `echo fourth` |
| K21 | `first Authorization: Bearer a` `\n` `then Proxy-Authorization: Basic tailsecret` (two keys) |
| K22 | `h = "Authorization: Bearer safe" if False else "tailsecret"` |
| K23 | `h=$(printf %s "Authorization: Bearer x")tailsecret` |
| K24 | `$h = "Authorization: Bearer x" . q(tailsecret);` |
| K25 | `curl -H "Authorization: Basic dGFpbHNlY3JldA==">response.txt` |

Earlier leaks, now regression tests:

| # | Input |
|---|---|
| L01 | `API_TOKEN="prefixsecret Authorization: Bearer tailsecret"` |
| L02 | `password: "prefixsecret Authorization: Bearer tailsecret"` |
| L03 | `{"api_key": "prefixsecret Authorization: Bearer tailsecret"}` |
| L04 | `tool --api-key "prefixsecret Authorization: Bearer tailsecret"` |
| L05 | `API_TOKEN="prefixsecret Authorization: Bearer tailsecret` (no closing quote) |
| L06 | `client_secret: 'prefixsecret Authorization: Bearer tailsecret` (no closing quote) |
| L07 | `API_TOKEN="prefixsecret\Authorization: Bearer tailsecret` (one backslash) |
| L08 | `password: "prefixsecret\\\Authorization: Bearer tailsecret` (three backslashes) |
| L09 | `{"api_key": "prefixsecret\Authorization: Bearer tailsecret` |
| L10 | `tool --api-key "prefixsecret\Authorization: Bearer tailsecret` |
| L11 | `TOKEN="prefixsecret x` `\n` `Authorization: Bearer tailsecret"` |
| L12 | `password: "prefixsecret x` `\n` `Authorization: Bearer tailsecret"` |
| L13 | `{"api_key": "prefixsecret x` `\n` `Authorization: Bearer tailsecret"}` |
| L14 | `ghp_<token> then curl -H 'Authorization: Basic dXNlcjpwYXNz'` |
| L15 | `Bearer abcdefghijkl` `\n` `Authorization: Basic dXNlcjpwYXNz` |
| L16 | A private-key block, then `Authorization: Basic dXNlcjpwYXNz` on the next line |
| L17 | `GH=ghp_<token> Authorization: Basic dXNlcjpwYXNz` |
| L18 | `TOKEN=Bearer abcdefghijkl Authorization: Basic dXNlcjpwYXNz` |
| L19 | `--token Authorization: Basic dXNlcjpwYXNz` |
| L20 | `SECRET=Authorization: Basic dXNlcjpwYXNz` |
| L21 | `export AUTH_TOKEN=Authorization:Basic dXNlcjpwYXNz` |

Escapes and encodings:

| # | Input |
|---|---|
| E01 | `printf "GET / HTTP/1.1\r\nAuthorization: Basic dXNlcjpwYXNz\r\n\r\n"` |
| E02 | `req = "Accept: json\nAuthorization: Basic dXNlcjpwYXNz"` |
| E03 | `s = "x\tAuthorization: Basic dXNlcjpwYXNz"` |
| E04 | `s = "x\\r\\nAuthorization: Basic dXNlcjpwYXNz"` |
| E05 | `s = "x\x0aAuthorization: Basic dXNlcjpwYXNz"` |
| E06 | `{"h": "x\u000aAuthorization: Basic dXNlcjpwYXNz"}` |
| E07 | `q=%7B%22Authorization%22%3A%22Basic dXNlcjpwYXNz%22%7D` |
| E08 | `next=%2522Authorization%2522%253A%2522Basic dXNlcjpwYXNz` |

A dash before or after the name:

| # | Input |
|---|---|
| D01 | `X-Authorization: Basic dXNlcjpwYXNz` |
| D02 | `X-Proxy-Authorization: Digest username="u", response="tailsecret"` |
| D03 | `curl -H "x-authorization: Token tailsecret" https://x` |
| D04 | `the Authorization-related fix landed` `\n` `next: tailsecret` (prose, redacted) |

Not keys, and sections without a key:

| # | Input | Expected |
|---|---|---|
| N01 | `the reauthorization flow is done` | unchanged |
| N02 | `two authorizations are pending` | unchanged |
| N03 | `see authorization_url in the config` | unchanged |
| N04 | `kept line` `\n` `DB_PASSWORD=hunter2fake and ghp_<token>` | `kept line` `\n` `DB_PASSWORD=R and R` (the other patterns as before) |
| N05 | `x\NAuthorization` | unchanged (uppercase is not an escape) |
| N06 | `x\RAuthorization` | unchanged |
| N07 | `x\TAuthorization` | unchanged |
| N08 | `x\X0aAuthorization` | unchanged |
| N09 | `x\U000aAuthorization` | unchanged |

Sections, the header and size:

| # | Input | Expected |
|---|---|---|
| S01 | Section 1 `kept goal text`; section 2 a list `ran it` / `curl -H 'Authorization: Bearer tailsecret'` / `also gone` | Section 1 unchanged; section 2 is `R` (its other items too) |
| S02 | Section 1 `"Authorization: Bearer x` `\n` `tailsecret` `\n` `more tailsecret`; later sections `next body kept` and a list `kept question` | Section 1 is `R`; the later sections unchanged |
| S03 | Note header whose slug is `x Authorization: Bearer tailsecret` | The note starts `R`, a blank line, then `## Goal` with its body unchanged; no `Written:` line |
| S04 | Note header with no key | `# Handoff: r / s`, a blank line, `Written: …` as before |
| S05 | Note header whose repository is `x Authorization: Bearer tailsecret` | As S03 |
| S06 | Note header whose slug is 100 `a`s, then ` Authorization: Bearer tailsecret` (the key starts past the cut) | As S03; no byte of the slug remains |
| S07 | Note header whose slug is 63 `a`s, then ` Authorizationxyzw` (no key until the cut leaves `Authorization...`) | As S03 |
| S08 | Note header whose slug is 56 `a`s, then ` AKIAABCDEFGHIJKLMNOPQRSTU` (too long for the AWS key shape until the cut) | The header stays a header, with that token `R`; no `AKIA` remains |
| T01 | A large section: 40 000 and 160 000 characters of near-keys (`xAuthorization -Authorization _HTTP_ HTTP_PROXY_ REDIRECT_ %2 \x0 …`), and of plain text with one key at the very end | Each call under a second; four times the input takes at most eight times as long (linear); the key at the end still gives `R` |

## 5. Mutations (suite-level kills)

Each mutation is applied on a scratch copy and must turn the named tests red; restored, all are green.

| Mutation | Killed by |
|---|---|
| Drop the `REDIRECT_HTTP_` name | K16 |
| Drop the `HTTP_PROXY_` name | K17 |
| Drop the `HTTP_` name | K15 |
| A dash before the name blocks the key | K14, D01–D03 |
| Drop the quoted-key shape (a key followed by a quote is not a key) | K04–K09 |
| Match the name case-sensitively | every K, L, E and D case except K12, K15–K17, plus S01–S03, T01 and the general secret-shape test |
| Case-insensitive over the whole pattern, escape markers included | N05–N09 |
| Drop the `\n` `\r` `\t` escape boundary | E01–E04 |
| Drop the `\xHH` escape boundary | E05 |
| Drop the `\uHHHH` escape boundary | E06 |
| Drop the `%HH` boundary | E07 |
| Drop the `%25HH` boundary | E08 |
| Redact from the key instead of the section start | every K, L and E case, S01–S03, T01 |
| Redact the header from the key instead of whole | S03 |
| Drop the whole-header check (redact each field only) | S03, S05, S06 |
| Shorten a header field before judging it for a key | S06 |
| Drop the redaction of the assembled header (after the cut) | S07, S08 |
| A key in one section also redacts every later section | S02 |
| A key in any section redacts every section | S01, S02 |

## 6. Comparison with the earlier pattern

Before this rule, one pattern redacted the value after `\bAuthorization` followed by `:` or `=` and a `Basic`, `Bearer`, `Token` or `Digest` scheme. Every section that pattern matches holds a key under this rule, so it is redacted whole: the earlier pattern's word boundary is a non-word character before the name, and this rule's boundary accepts every such character (a dash included), plus escapes and percent bytes.

Checked by a generated probe: 50 prefixes (dashes, underscores, letters, digits, quotes, brackets, escapes in both cases, percent bytes, non-ASCII), 14 spellings of the names (mixed case, dotless and dotted i), 9 separators, 7 schemes and 5 surroundings, 220 500 inputs. The earlier pattern matches 106 470 of them; this rule finds a key in every one, and no input whose credential the earlier code removed keeps it here. Run against the version before the dash fix, the same probe reports 12 375 such inputs (all with a dash before the name).

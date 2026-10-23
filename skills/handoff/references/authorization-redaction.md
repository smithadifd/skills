# Authorization redaction: the accepted grammar

`handoff.py`'s `redact()` removes Authorization credentials from a note before it is written. A note holds text copied from shell commands, Python, JSON and logs, and the same characters mean different things in each, so this page fixes what the redaction recognizes and what it does when the reading is unclear.

**Invariant.** When a line can be read more than one way, redaction removes more of the line, never less. A credential left in a note is a defect; a non-secret suffix removed from a line is an accepted cost. Every rule below scans one line at a time; nothing crosses a newline.

## 1. Where a credential starts

A credential starts right after an Authorization key, matched case-insensitively, in any of these forms:

| Form | Example |
|---|---|
| bare header | `Authorization: Bearer abc` |
| bare assignment | `Authorization=Bearer abc` |
| quoted key | `"Authorization": "Bearer abc"`, `'Authorization': ...` |
| bracketed key | `headers["Authorization"] = "Basic abc"`, `h['Authorization']=...` |

The key is followed by optional whitespace, then `:` or `=`, then optional whitespace. The credential is everything from there on, read by § 2 or § 3. The scheme word (`Basic`, `Bearer`, `Token`, `Digest`, or none) does not change where the credential ends; it is removed with the rest.

## 2. The value is a quoted span

The value is a quoted span when either:

- (a) the first character after the separator is `"` or `'` (the key/value forms: `"Authorization": "Bearer abc"`); or
- (b) the key itself sits inside a quoted string that opened with a quote immediately before `Authorization` (the header-string form: `"Authorization: Digest ..."` inside a `curl -H` argument or a Python list).

**Which quote is `q`.** (a) is checked first and wins: when the first character after the separator is a quote, that quote is `q`, whether or not the key is quoted. (b) applies only when (a) does not and the quote immediately before `Authorization` is still open at the separator, that is, no unescaped matching quote appears between it and the separator. A quoted or bracketed key (`"Authorization":`, `headers["Authorization"] =`) closes its quote before the separator, so (b) never applies to it: its value is either an (a) span or, when unquoted, § 3.

| Line | `q` |
|---|---|
| `"Authorization": "Bearer x"` | the `"` after `: ` (a) |
| `headers["Authorization"] = 'Basic x'` | the `'` after `= ` (a) |
| `-H "Authorization: Bearer x"` | the `"` before `Authorization` (b) |
| `"Authorization": Bearer x` | none: § 3 | The span ends at the first `q` that is not preceded by an odd number of backslashes (escape-aware). Treating a backslash as an escape inside single quotes is deliberate even though a shell does not: it can only make the span longer, which is the safe direction.

**Clean end.** The suffix after the closing quote is kept only when the closing quote is followed by:

- whitespace or the end of the line; or
- a shell metacharacter that ends a word: `;` `&` `|` `<` `>` `(` `)`. Whatever follows one of these is a new token (a redirect target, a pipe, the next command), never part of the header argument; or
- a run of one or more of `,` `]` `}` that is itself followed by whitespace, the end of the line, or one of the metacharacters above. A shell treats `,` `]` `}` as ordinary word characters, so the run alone does not end the argument; only what follows it can.

Anything else is not a clean end, and the value is redacted to the end of the line (§ 3). In particular:

| After the closing quote | Reading | Result |
|---|---|---|
| ` -X POST`, end of line | span is closed | suffix kept |
| `>response.txt`, `\|jq .`, `;echo done`, `&&next` | shell metacharacter ends the word | suffix kept |
| `], retries = 3`, `)` + EOL, `},` + space, `])` | span closed by a list/call/dict | suffix kept |
| `"` or `'` (`"a=","response=x"` has `,` then `"`) | shell concatenates adjacent words; a list item with no space after the comma reads the same way | to end of line |
| a letter, digit or other word character (`"..."tail`) | shell concatenation | to end of line |
| `]x`, `}x`, `,x` (closer run not followed by whitespace) | shell treats `]` `}` `,` as ordinary word characters | to end of line |
| no closing quote on the line | unterminated | to end of line |

So a Python or JSON line written without a space after its separators (`["Authorization: Bearer x","Accept: y"]`) loses its suffix. That is the accepted cost.

The replacement keeps the opening quote and, on a clean end, the closing quote: `"Authorization: [REDACTED]"` / `"Authorization": "[REDACTED]"`.

## 3. The value is not a quoted span

Everything after the separator to the end of the line is removed. This covers unquoted headers (`Authorization: Digest username="u", response="x"`, where the unquoted rule on main stops at the first space and leaves `response=`), log lines, and every case § 2 sends here.

## 4. Out of scope

- Credentials split across lines (a backslash-continued shell command whose header value starts on the next line). The line rule cannot see them; the other patterns in `redact()` (Bearer token shape, `*_TOKEN=` assignments) still apply.
- Header names other than `Authorization`. `Proxy-Authorization` is covered, because the key match starts at a word boundary and `-` is one. Other names (`X-Api-Key`, `Cookie`) are left to the existing name- and shape-based patterns.

## 5. Required tests

Each row below is a test; the secret marker (`tailsecret`) must be absent from the note, and the stated suffix must be present or absent as given.

1. `curl -H "Authorization: Digest username="'"u", response="tailsecret"' https://x` → no `tailsecret`.
2. `curl -H "Authorization: Digest a=","response=tailsecret" https://x` → no `tailsecret`.
3. `curl -H 'Authorization: Digest a=','response=tailsecret' https://x` → no `tailsecret`.
4. `curl -H "Authorization: Bearer "}tailsecret https://x` → no `tailsecret`.
5. `curl -H "Authorization: Bearer "]tailsecret https://x` → no `tailsecret`.
6. `headers = ["Authorization: Digest a=1, response=tailsecret"], retries = 3` → no `tailsecret`; `], retries = 3` kept.
7. `headers["Authorization"] = "Basic tailsecret"  # comment` → no `tailsecret`; `# comment` kept.
8. `{"Authorization": "Bearer tailsecret", "Accept": "json"}` → no `tailsecret`; `"Accept": "json"` kept.
9. `Authorization: Digest username="u", response="tailsecret"` → no `tailsecret`.
10. `curl -H "Authorization: Bearer tail\"secret" https://x` (escaped quote) → no `secret`; ` https://x` kept.
11. `["Authorization: Bearer x","Accept: tailsecret"]` → no `tailsecret` (over-redacted, by design).
12. `curl -H "Authorization: Digest algorithm=MD5",username='"u"',nonce='"tailsecret"' https://x` → no `tailsecret`.
13. `curl -H "Authorization: Basic dGFpbHNlY3JldA==">response.txt` → no `dGFpbHNlY3JldA==`; `>response.txt` kept.

Cases 2–5 and 12 are inputs that leaked under the previous attempt. Each test is observed red under a targeted mutation of the clean-end rule (for 2–5, 11 and 12: accept a closer run as a clean end whatever follows it; for 1: accept any following character; for 6–8 and 13: always redact to the end of the line) and green after restoration.

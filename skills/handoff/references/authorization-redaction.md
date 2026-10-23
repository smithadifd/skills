# Authorization redaction: the simple rule

DESIGN: after a quoted Authorization header value, keep the rest of the line only when the closing quote ends cleanly and that rest holds no quote character at all; otherwise redact to the end of the line. Over-redact, never leak.

Why this rule: a credential that survives the closing quote has to be built by more code, and in every leak found so far (shell concatenation, Python implicit concatenation, `"safe" if False else "secret"`, `<` read as a comparison) that code contained a second string literal. A string literal needs a quote character, so "no quote in the rest of the line" closes the whole class without parsing shell or Python. Everything is per line; nothing crosses a newline.

## 1. Where the value starts

A credential starts after an `Authorization` key (case-insensitive, at a word boundary, so `Proxy-Authorization` counts), optionally quoted or bracketed (`"Authorization":`, `h['Authorization'] =`), then optional whitespace, `:` or `=`, optional whitespace. The scheme word (`Basic`, `Bearer`, `Digest`, none) is part of the value.

## 2. Quote characters

These count as a quote, wherever they appear in the rest of the line, escaped or not: `"` `'` and the backtick, plus the typographic `‘` `’` `‚` `“` `”` `„`. A backslash before a quote does not exempt it. Typographic quotes are included although no shell or Python treats them as quotes: text pasted from a document or chat may carry them around a real header, and counting one only costs a suffix. Apostrophes in prose (`don't`) count too; that is an accepted cost. Other characters (guillemets, `«»`) are not counted.

## 3. Quoted value and clean end

The value is quoted when the first character after the separator is a quote `q` (`"Authorization": "Bearer x"`), or when the key sits inside a string opened by a quote `q` just before `Authorization` and still open at the separator (`-H "Authorization: Bearer x"`). The first form wins when both could apply. The span ends at the first `q` not preceded by an odd number of backslashes.

The closing quote ends cleanly when it exists and the next character is the end of the line, whitespace, or one of `;` `&` `|` `<` `>` `)`. Those end a shell word, so what follows is a new token (a redirect target, a pipe, the next command), not more of the value. Anything else directly after the closing quote (`,` `]` `}` `.` a letter, a digit, another quote) is not clean: shell and Python join it to the value, so the line is redacted to its end. No closing quote on the line is also not clean.

## 4. What is redacted

- Clean end and no quote character in the rest of the line: the value is replaced and the suffix is kept. `"Authorization: [REDACTED]"` keeps both quotes.
- Anything else: the value and the rest of the line to the newline are replaced by the marker `[REDACTED]`. The opening quote is kept; no closing quote is added.
- Unquoted value (`Authorization: Digest username="u", response="x"`, `Authorization=Bearer x`): everything after the separator to the end of the line is replaced.
- Multi-line: each line is judged alone. A value that continues on the next line (a backslash-continued command, a wrapped header) is not seen by this rule; the other `redact()` patterns (token shapes, `*_TOKEN=` assignments) still apply. Triple-quoted `"""` openers fall out of the rule: the second `"` closes an empty span and the third is a quote in the rest, so the line is redacted.

## 5. Required tests

The secret marker is `tailsecret`. Each case: the marker is absent from the note, and the stated suffix is present or absent as given.

Leak cases (marker absent, rest of line redacted):

1. `curl -H "Authorization: Digest username="'"u", response="tailsecret"' https://x`
2. `curl -H "Authorization: Digest a=","response=tailsecret" https://x` (shell concatenation)
3. `curl -H 'Authorization: Digest a=','response=tailsecret' https://x`
4. `curl -H "Authorization: Bearer "}tailsecret https://x`
5. `curl -H "Authorization: Bearer "]tailsecret https://x`
6. `["Authorization: Bearer x" "tailsecret"]` (implicit concatenation after whitespace)
7. `h = "Authorization: Bearer " + "tailsecret"`
8. `"Authorization: Bearer x" f"tailsecret"`
9. `h = "Authorization: Bearer safe" if False else "tailsecret"` (conditional selection)
10. `h = "Authorization: Bearer x" or "tailsecret"` (boolean selection)
11. `curl -H "Authorization: Bearer x" <"tailsecret"` and `curl -H "Authorization: Bearer x">"tailsecret"` (`<` `>` with a following literal)
12. `curl -H "Authorization: Digest algorithm=MD5",username='"u"',nonce='"tailsecret"' https://x`
13. `["Authorization: Bearer x","Accept: tailsecret"]` (over-redacted by design)
14. `{"Authorization": "Bearer tailsecret", "Accept": "json"}`: no `tailsecret`; `"Accept": "json"` is gone (it holds quotes).
15. `Authorization: Digest username="u", response="tailsecret"` (unquoted value)
16. `curl -H "Authorization: Bearer tail\"secret" https://x`: no `secret`; the escaped quote does not close the span, and ` https://x` is kept (no quote in it).
17. `curl -H "Authorization: Bearer tailsecret` (no closing quote)
18. `curl -H "Authorization: Bearer tailsecret"'extra'`
19. `` curl -H "Authorization: Bearer x" `echo tailsecret` `` and `curl -H "Authorization: Bearer x" “tailsecret”` (backtick and typographic quote)

Keep-the-suffix cases (marker absent, suffix kept exactly):

20. `curl -H "Authorization: Bearer tailsecret" https://example.com` keeps ` https://example.com`.
21. `curl -H "Authorization: Basic dGFpbHNlY3JldA==">response.txt` keeps `>response.txt`.
22. `headers["Authorization"] = "Basic tailsecret"  # comment` keeps `  # comment`.
23. `curl -s -H "Authorization: Bearer tailsecret" -X POST | jq .` keeps ` -X POST | jq .`.
24. `echo $(curl -H "Authorization: Bearer tailsecret") done` keeps `) done`.

Each leak case is observed red against a mutation that keeps the suffix whenever the closing quote exists (no quote scan), and against a mutation that accepts any character after the closing quote as clean; the keep cases are red against "always redact to the end of the line". All are green after restoration.

## 6. Cost

Over-redaction examples, all accepted:

- `{"Authorization": "Bearer x", "Accept": "json"}`: the `Accept` pair is lost.
- `requests.get(u, headers={"Authorization": "Bearer x"})`: `})` is lost (`}` is not a clean end).
- `headers = ["Authorization: Bearer x"], retries = 3`: `], retries = 3` is lost.
- `curl -H "Authorization: Bearer x" "https://host/path"`: the quoted URL is lost.
- A comment such as `# don't reuse` after the value is lost.

Each loses non-secret context from a resume note, which the reader can rebuild from the source; a leaked credential in a persisted note cannot be taken back. The rule also needs no shell or Python grammar, so no new expression form can reopen it: only a new string-literal delimiter could, and adding that character to § 2 is a one-line change.

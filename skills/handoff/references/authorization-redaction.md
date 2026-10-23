# Authorization redaction: the simple rule

DESIGN: after an Authorization credential (quoted or unquoted header value), always redact to the end of the logical line, where a logical line continues through a trailing backslash and through the line holding the closing quote of an unterminated quoted value, and runs to the end of the input section if no closing quote appears; over-redact, never leak, and no condition keeps a suffix.

Why: every leak found so far (shell concatenation, Python implicit concatenation, `"safe" if False else "secret"`, `<` read as a comparison, `)tail` after a command substitution, a Perl `. q(tail)` literal, a value continued on the next line) lets the rest of the line, or the next line, carry part of the credential. Deciding which suffixes are harmless needs a shell, Python and Perl grammar, and each grammar attempt was wrong on one more form. Dropping the suffix exception needs no grammar.

## 1. Where a credential starts

An `Authorization` key (case-insensitive, at a word boundary, so `Proxy-Authorization` counts), optionally quoted or bracketed (`"Authorization":`, `h['Authorization'] =`), then optional whitespace, `:` or `=`, optional whitespace. The scheme word (`Basic`, `Bearer`, `Digest`, none) is part of the credential. The credential begins at the separator's end.

## 2. Quotes and the end of the redaction

Quote characters: `"` `'` backtick, and the typographic `‘ ’ ‚ “ ” „`. Typographic ones are included because pasted text may wrap a real header in them; no other character is treated as a quote.

A value is quoted when its first character after the separator is a quote `q`, or when the key sits inside a string opened by a quote `q` just before `Authorization` and still open at the separator (`-H "Authorization: Bearer x"`). The first form wins. Typographic quotes close with any typographic quote. `q` closes at the next unescaped `q` (not preceded by an odd number of backslashes). Escaped quotes do not close.

Redaction runs from the credential start to the end of the logical line:

- A physical line ends at a newline. A trailing backslash (after any trailing spaces are ignored) joins it to the next physical line, repeatedly: a chain of any length is one logical line.
- Quoted value with a closing quote on the line: the logical line is that line plus its continuations. The closing quote and everything after it are redacted.
- Quoted value with no closing quote on the line (unterminated): the logical line extends through the physical line that holds the next unescaped `q`, and through that line's own continuations. If no such quote appears before the end of the input section, redaction runs to the end of the section.
- Unquoted value: the logical line (the line and its continuations).

Section boundary: `handoff.py` redacts each section body as one string (list items arrive as `- ` lines of that string), and the note header separately. The end of a section is the end of that string; redaction never reaches into the next section.

## 3. The marker

Everything redacted becomes one `[REDACTED]`, placed right after the separator and any opening quote, so `Authorization: [REDACTED]`, `"Authorization: [REDACTED]` and `"Authorization": "[REDACTED]`. No closing quote is added. The other `redact()` patterns (token shapes, `*_TOKEN=` assignments and the rest) still run on the whole text before and after, and are not weakened by this rule.

## 4. Required tests

Secret marker `tailsecret`: it must be absent from the note. Unless a case names kept text, the expected output is the text up to the separator, the marker, and nothing after it on the logical line. Lines after the logical line are unchanged.

Carried from the earlier design:

1. `curl -H "Authorization: Digest username="'"u", response="tailsecret"' https://x`
2. `curl -H "Authorization: Digest a=","response=tailsecret" https://x`
3. `curl -H 'Authorization: Digest a=','response=tailsecret' https://x`
4. `curl -H "Authorization: Bearer "}tailsecret https://x`
5. `curl -H "Authorization: Bearer "]tailsecret https://x`
6. `headers = ["Authorization: Digest a=1, response=tailsecret"], retries = 3` gives `headers = ["Authorization: [REDACTED]`.
7. `headers["Authorization"] = "Basic tailsecret"  # comment` gives `headers["Authorization"] = "[REDACTED]` (the comment is gone).
8. `{"Authorization": "Bearer tailsecret", "Accept": "json"}` gives `{"Authorization": "[REDACTED]`.
9. `Authorization: Digest username="u", response="tailsecret"`
10. `curl -H "Authorization: Bearer tail\"secret" https://x`: no `secret` (the escaped quote does not close).
11. `["Authorization: Bearer x","Accept: tailsecret"]`
12. `curl -H "Authorization: Digest algorithm=MD5",username='"u"',nonce='"tailsecret"' https://x`
13. `curl -H "Authorization: Basic dGFpbHNlY3JldA==">response.txt`: no `dGFpbHNlY3JldA==`, no `>response.txt`.
14. `["Authorization: Bearer x" "tailsecret"]`
15. `h = "Authorization: Bearer " + "tailsecret"`
16. `"Authorization: Bearer x" f"tailsecret"`

Carried from the first version, plus the former keep cases, now redacted:

17. `h = "Authorization: Bearer safe" if False else "tailsecret"`
18. `h = "Authorization: Bearer x" or "tailsecret"`
19. `curl -H "Authorization: Bearer x" <"tailsecret"` and `curl -H "Authorization: Bearer x">"tailsecret"`
20. `curl -H "Authorization: Bearer x"'tailsecret'`
21. `` curl -H "Authorization: Bearer x" `echo tailsecret` `` and `curl -H "Authorization: Bearer x" “tailsecret”`
22. `curl -H "Authorization: Bearer x" https://example.com` gives `curl -H "Authorization: [REDACTED]`.
23. `curl -s -H "Authorization: Bearer tailsecret" -X POST | jq .` gives `curl -s -H "Authorization: [REDACTED]`.
24. `echo $(curl -H "Authorization: Bearer tailsecret") done` gives `echo $(curl -H "Authorization: [REDACTED]`.

New from design review:

25. `h=$(printf %s "Authorization: Bearer x")tailsecret`
26. `h="Authorization: Bearer x"\` then a next line `tailsecret`: both lines redacted; a third line after it is kept.
27. Perl: `$h = "Authorization: Bearer x" . q(tailsecret);`
28. Unterminated multi-line: `curl -H "Authorization: Bearer x` / `tailsecret` / `more" https://x` / `echo kept`: the first three lines redacted, `echo kept` kept.
29. Unterminated to the end of the section: `"Authorization: Bearer x` with `tailsecret` on later lines and no closing quote: all of them redacted, and the next section's body is unchanged.
30. Continuation chain of 3 lines: `curl -H "Authorization: Bearer x" \`, `-d tailsecret \`, `tailsecret2`: all three redacted (the last has no backslash and ends the chain); a fourth line is kept.
31. Unquoted continuation: `Authorization: Bearer x\` then `tailsecret`.
32. No closing quote, one line: `curl -H "Authorization: Bearer tailsecret`.

## 5. Mutations (suite-level kills)

Each mutation is applied on a scratch copy and must turn the named tests red; restored, all are green.

| Mutation | Killed by |
|---|---|
| Drop continuation handling (treat the physical line as the logical line) | 26, 30, 31 |
| Drop unterminated-quote handling (stop at the end of the physical line when no closing quote) | 28, 29 |
| Drop the unquoted-value case (only quoted values redact) | 9, 31 |
| Re-introduce any suffix keep (for example keep text after a closing quote that ends a word, or keep it when it holds no quote) | 6, 7, 8, 13, 22, 23, 24, 25, 27, plus 4 and 5 |

## 6. Cost

Lost: the benign suffix of a header line, for example `-X POST`, `https://example.com`, a trailing `# comment`, the other pairs of a JSON object, `})`, `], retries = 3`, and the first following lines of a backslash-continued curl command. A prose apostrophe after an opening quote can also pull following lines into an unterminated span, up to the end of the section.

This is acceptable for a resume note: it records where the work stood, and the command can be rebuilt from the repository or shell history, while a credential persisted in a note cannot be taken back. The rule also names no grammar, so a new expression form cannot reopen it.

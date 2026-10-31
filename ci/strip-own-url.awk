# Hide the repository's own URL forms from a deny-list scan. Input records
# are `<line>:<text>` (grep -n output); output is `<line>\001<text>` with the
# own-URL words (owner and repo from -v owner= -v repo=) removed. Shared by
# ci/lint-skills.sh and ci/check.sh; see the lint for the allowed forms.
BEGIN {
  sq = "\047"
  o = tolower(owner)
  r = tolower(repo)
  nf = 0
  if (o != "" && r != "") {
    forms[++nf] = "https://github.com/" o "/" r
    forms[++nf] = "http://github.com/" o "/" r
    forms[++nf] = "ssh://git@github.com/" o "/" r
    forms[++nf] = "git@github.com:" o "/" r
    forms[++nf] = "github.com/" o "/" r
  }
}
# formlen(c): length of the allowed form at the start of c when the
# rest is nothing, or .git and/or a tail starting with / ? or #; else 0.
function formlen(c,    i, f, rem) {
  for (i = 1; i <= nf; i++) {
    f = forms[i]
    if (substr(c, 1, length(f)) != f) continue
    rem = substr(c, length(f) + 1)
    if (substr(rem, 1, 4) == ".git") rem = substr(rem, 5)
    if (rem == "" || index("/?#", substr(rem, 1, 1)) > 0) return length(f)
  }
  return 0
}
# span(c, base): set gs/gl to the 1-based start and length, within the
# word, of the text to hide when c (starting after base characters of
# the word) is a form or a one-word Markdown link to one.
function span(c, base,    fl, i, t, inner) {
  fl = formlen(c)
  if (fl > 0) { gs = base + 1; gl = fl; return 1 }
  if (substr(c, 1, 1) == "[" && substr(c, length(c), 1) == ")") {
    i = index(c, "](")
    if (i > 0) {
      t = substr(c, 2, i - 2)
      if (index(t, "[") == 0 && index(t, "]") == 0) {
        inner = substr(c, i + 2, length(c) - i - 2)
        fl = formlen(inner)
        if (fl > 0) { gs = base + i + 2; gl = fl; return 1 }
      }
    }
  }
  return 0
}
function pair(op, cl) {
  return (op == "(" && cl == ")") || (op == "<" && cl == ">") ||
    (op == "\"" && cl == "\"") || (op == sq && cl == sq) || (op == "`" && cl == "`")
}
# hide(w): w without the hidden form when w is exempt, else w.
function hide(w,    len, tr, wr, e, b, ee, c) {
  len = length(w)
  for (tr = 0; tr <= 1; tr++) {
    e = len
    if (tr) {
      if (len < 1 || index(".,;:!?", substr(w, len, 1)) == 0) continue
      e = len - 1
    }
    for (wr = 0; wr <= 1; wr++) {
      b = 0
      ee = e
      if (wr) {
        if (e < 2 || !pair(substr(w, 1, 1), substr(w, e, 1))) continue
        b = 1
        ee = e - 1
      }
      c = tolower(substr(w, b + 1, ee - b))
      if (span(c, b)) return substr(w, 1, gs - 1) substr(w, gs + gl)
    }
  }
  return w
}
{
  body = $0
  match(body, /^[0-9]+:/)
  pre = substr(body, 1, RLENGTH - 1)
  body = substr(body, RLENGTH + 1)
  cr = ""
  if (substr(body, length(body), 1) == "\r") {
    cr = "\r"
    body = substr(body, 1, length(body) - 1)
  }
  out = ""
  while (nf > 0 && match(body, /^[ \t]*[^ \t]+/)) {
    seg = substr(body, 1, RLENGTH)
    body = substr(body, RLENGTH + 1)
    match(seg, /[^ \t]+$/)
    out = out substr(seg, 1, RSTART - 1) hide(substr(seg, RSTART))
  }
  print pre "\001" out body cr
}

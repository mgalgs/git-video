"""Word-level diffs of a run of old lines against the new lines that replace
them, as plain data the editor can animate: which tokens stay (and where they
move), which go, which arrive.

Tokens are words, runs of whitespace and single punctuation marks; the diff
is difflib's on the token texts (the `-`/`+` lines are already in the patch,
so no second git call). Whitespace tokens are never reported: there is
nothing to see of them.
"""
import difflib
import re

TOKEN = re.compile(r'\w+|\s+|[^\w\s]')
MAX_TOKENS = 4000   # per side; the matcher is quadratic and a screenful never gets near this


def tokens(lines):
    """[(row, col, text)] of `lines` (tabs already expanded), with a newline
    token closing each line so the diff keeps line structure."""
    out = []
    for row, line in enumerate(lines):
        for m in TOKEN.finditer(line):
            out.append((row, m.start(), m.group()))
        out.append((row, len(line), '\n'))
    return out


def ops(old_lines, new_lines):
    """[[kind, old row, old col, new row, new col, text]] with kind `equal`,
    `delete` or `insert`, rows and cols 0-based within the runs, -1 on the side
    a token is not on; in reading order of the new side, deletions of a
    replaced stretch before its insertions. None when a side has more than
    MAX_TOKENS tokens (too much to morph)."""
    a, b = tokens(old_lines), tokens(new_lines)
    if len(a) > MAX_TOKENS or len(b) > MAX_TOKENS:
        return None
    sm = difflib.SequenceMatcher(None, [t for _, _, t in a], [t for _, _, t in b], autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            out += [['equal', *a[i][:2], *b[j][:2], a[i][2]] for i, j in zip(range(i1, i2), range(j1, j2))]
            continue
        if tag in ('delete', 'replace'):
            out += [['delete', *a[i][:2], -1, -1, a[i][2]] for i in range(i1, i2)]
        if tag in ('insert', 'replace'):
            out += [['insert', -1, -1, *b[j][:2], b[j][2]] for j in range(j1, j2)]
    return [o for o in out if o[5].strip()]


def line_edits(old, new):
    """How one line becomes another, word by word: [(col, delete, insert)] left
    to right, `col` in the old line, `delete` the columns removed there and
    `insert` the text put in their place. Whitespace counts here (unlike
    `ops`), so applying the edits gives `new` exactly."""
    a = [t for _, _, t in tokens([old])][:-1]
    b = [t for _, _, t in tokens([new])][:-1]
    cols = [0]
    for t in a:
        cols.append(cols[-1] + len(t))
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return [(cols[i1], cols[i2] - cols[i1], ''.join(b[j1:j2]))
            for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != 'equal']

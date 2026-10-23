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
from collections import Counter

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


PAIR_SHARE = 0.4   # an old and a new line sharing this much of their words are one line edited


def _diff(a, b):
    """[[kind, old row, old col, new row, new col, text]] of token lists `a`
    and `b` (see `ops`), whitespace included."""
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
    return out


def _size(line):
    return sum(1 for m in TOKEN.finditer(line) if m.group().strip())


def _pairs(old_lines, new_lines, raw):
    """[(old row, new row)] in order: the lines that are one line edited (they
    share at least PAIR_SHARE of their words in the diff `raw`), most shared
    words first."""
    shared = Counter((o[1], o[3]) for o in raw if o[0] == 'equal' and o[5].strip())
    pairs = []
    for (i, j), c in sorted(shared.items(), key=lambda kv: (-kv[1], kv[0])):
        if c < max(1.0, PAIR_SHARE * max(_size(old_lines[i]), _size(new_lines[j]))):
            continue
        if all((pi - i) * (pj - j) > 0 for pi, pj in pairs):
            pairs.append((i, j))
    return sorted(pairs)


def pair_rows(old_lines, new_lines):
    """[(old row, new row)]: the lines of a replaced run that are one line edited."""
    a, b = tokens(old_lines), tokens(new_lines)
    if not a or not b or len(a) > MAX_TOKENS or len(b) > MAX_TOKENS:
        return []
    return _pairs(old_lines, new_lines, _diff(a, b))


def ops(old_lines, new_lines):
    """[[kind, old row, old col, new row, new col, text]] with kind `equal`,
    `delete` or `insert`, rows and cols 0-based within the runs, -1 on the side
    a token is not on; in reading order of the new side, deletions of a
    replaced stretch before its insertions. None when a side has more than
    MAX_TOKENS tokens (too much to morph).

    Words are matched only within a line edited (an old and a new line that
    share enough words, `pair_rows`): a line with no such partner is wholly
    removed or wholly new, so a `(` or `self` never flies from one line to
    another."""
    a, b = tokens(old_lines), tokens(new_lines)
    if len(a) > MAX_TOKENS or len(b) > MAX_TOKENS:
        return None
    pairs = _pairs(old_lines, new_lines, _diff(a, b)) if a and b else []
    out = []
    i = j = 0

    def gone(upto):
        nonlocal i
        for r in range(i, upto):
            out.extend(['delete', r, c, -1, -1, t] for _, c, t in tokens([old_lines[r]]) if t != '\n')
        i = max(i, upto)

    def fresh(upto):
        nonlocal j
        for r in range(j, upto):
            out.extend(['insert', -1, -1, r, c, t] for _, c, t in tokens([new_lines[r]]) if t != '\n')
        j = max(j, upto)

    for pi, pj in pairs:
        gone(pi)
        fresh(pj)
        for o in _diff(tokens([old_lines[pi]])[:-1], tokens([new_lines[pj]])[:-1]):
            o[1], o[3] = (pi if o[1] >= 0 else -1), (pj if o[3] >= 0 else -1)
            out.append(o)
        i, j = pi + 1, pj + 1
    gone(len(old_lines))
    fresh(len(new_lines))
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

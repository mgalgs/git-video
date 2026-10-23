"""Commit-message sentences matched to the hunks they talk about.

The body is split into sentences (and bullets); each sentence's identifiers
(backticked spans, snake_case, dotted.names, file names, `call()`s,
camelCase, --flags, and any word that is exactly the name of a definition
the change touches) are scored against every hunk, and the best-scoring hunk
gets the sentence. A sentence that names nothing in the change goes nowhere.
Plain English never matches: "the", "file" or "test" are not identifiers.
"""
import os
import re

BULLET = re.compile(r'^\s*(?:[-*•]|\d+[.)])\s+')
SPLIT = re.compile(r'(?<=[.!?])\s+(?=[^\s])')
BACKTICK = re.compile(r'`([^`]+)`')
WORDISH = re.compile(r'--?[A-Za-z][\w-]*|[A-Za-z_][\w./-]*\w(?:\(\))?|[A-Za-z_]\w*(?:\(\))?')
CAMEL = re.compile(r'[a-z][A-Z]')
MIN_PLAIN = 4   # a plain word must be this long to match a definition's name

W_DEF, W_WORD, W_PATH = 3, 2, 1


def sentences(body):
    """The body as units to caption: one per bullet, else one per sentence of
    each paragraph (lines joined)."""
    out = []
    for para in re.split(r'\n\s*\n', body.strip()):
        lines = [ln for ln in para.split('\n') if ln.strip()]
        if not lines:
            continue
        if BULLET.match(lines[0]):
            cur = None
            for ln in lines:
                if BULLET.match(ln):
                    if cur:
                        out.append(cur)
                    cur = BULLET.sub('', ln).strip()
                else:
                    cur = f'{cur} {ln.strip()}' if cur else ln.strip()
            if cur:
                out.append(cur)
            continue
        text = ' '.join(ln.strip() for ln in lines)
        out += [s.strip() for s in SPLIT.split(text) if s.strip()]
    return out


def _clean(tok):
    tok = tok.strip('\'"()[]{},;:!?')
    if tok.endswith('()'):
        tok = tok[:-2]
    return tok.rstrip('.')


def _looks_like_code(tok):
    if '.' in tok and min(len(x) for x in tok.split('.')) < 2:
        return False   # e.g, i.e
    return ('_' in tok or '.' in tok or '/' in tok or tok.startswith('-') or bool(CAMEL.search(tok)))


def identifiers(sentence, names=()):
    """The identifiers a sentence mentions, in order, each once: everything in
    backticks, plus words that look like code (`_`, `.`, `/`, `()`, camelCase,
    --flags), plus plain words that are exactly one of `names` (touched
    definitions). A file name also yields its basename and stem."""
    out = []

    def add(tok):
        tok = _clean(tok)
        if len(tok.lstrip('-')) < 2 or tok.lstrip('-').isdigit():
            return
        for t in (tok, *_file_parts(tok)):
            if t and t not in out:
                out.append(t)

    for span in BACKTICK.findall(sentence):
        for m in WORDISH.finditer(span):
            add(m.group())
    rest = BACKTICK.sub(' ', sentence)
    for m in WORDISH.finditer(rest):
        raw = m.group()
        tok = _clean(raw)
        if raw.endswith('()') or _looks_like_code(tok) or (len(tok) >= MIN_PLAIN and tok in names):
            add(raw)
    return out


def _file_parts(tok):
    """`a/b/pyproject.toml` -> (`pyproject.toml`, `pyproject`): a file name's
    basename and stem, when it has an extension."""
    base = os.path.basename(tok)
    stem, ext = os.path.splitext(base)
    if not ext or not stem or not ext[1:].isalnum():
        return ()
    return tuple(x for x in (base, stem) if x != tok)


def _keys(ident):
    """(names, paths) an identifier is compared as: a --flag by its name, a
    dotted name also by its last part (`sidebar.draw` -> `draw`); as a path,
    by itself, its head, its basename and its stem."""
    name = ident.lstrip('-')
    head, _, last = name.rpartition('.')
    base = os.path.basename(name)
    return ({name, last} if head else {name}), {name, head, base, os.path.splitext(base)[0]} - {''}


def score(idents, info):
    """(score, best identifier) of a sentence's identifiers against one hunk:
    W_DEF for a touched definition's name, W_WORD for a word on the hunk's
    lines, W_PATH for the hunk's file name."""
    base = os.path.basename(info['path'])
    here = {base, os.path.splitext(base)[0]}
    total, best, best_w = 0, None, 0
    for ident in idents:
        names, paths = _keys(ident)
        w = 0
        hit = None
        if names & info['defs']:
            w += W_DEF
            hit = sorted(names & info['defs'])[0]
        if names & info['words']:
            w += W_WORD
            hit = hit or sorted(names & info['words'])[0]
        if paths & here:
            w += W_PATH
        total += w
        if w > best_w and hit:
            best, best_w = hit, w
    return total, best


def match(units, hunks, names=()):
    """[(unit index, hunk index, target word or None, score)] for every unit
    that names something in a hunk: the best-scoring hunk wins, ties go to
    the earliest. `hunks`: dicts with `path`, `words` and `defs` (sets);
    `names`: plain words that count as identifiers (touched definitions)."""
    out = []
    for k, unit in enumerate(units):
        idents = identifiers(unit, names)
        if not idents:
            continue
        best = None
        for j, info in enumerate(hunks):
            s, word = score(idents, info)
            if s > 0 and (best is None or s > best[0]):
                best = (s, j, word)
        if best is not None:
            out.append((k, best[1], best[2], best[0]))
    return out

"""The narration: what a colleague would say while showing you the change.

`script(plan, patches)` writes it from a plan, by heuristics: one phrase per
shot or beat, each tied to a shot by its `plan.shot_key` (an edit shot's
hunks by keys of their own). The opening turns the commit subject into a
sentence and says the change's size in words; each file gets a lead-in;
a hunk gets the commit sentence the callout matcher tied to it, shortened
to a spoken length, or a few words naming the definition written there, or
nothing; the first dive per file says what it calls; a --series card says
the next patch; the closing says so, and the next commit when there is one.

What is said is plain text, captioned as written. How an identifier, a
path or a flag is said (`spoken`) goes to videobuilder's `say` as its
`lexicon`, so `draw_card` is heard as "draw card" and captioned as itself.
Trailers, URLs, hashes, symbols and code are never read out.
"""
import hashlib
import json
import math
import os
import re
import wave
from dataclasses import dataclass

from videobuilder import speech

from . import callouts as callouts_mod, collect, edits, plan as planmod, xref

SPEED = 1.0         # Kokoro's speaking rate (part of the cache key)
GAP = 0.35          # seconds between two phrases said over one shot (say's own gap)
BREATH = 0.5        # after a shot's last phrase, before the shot may end
DUCK_DB = -10.0     # the sound effects dip this far under the voice ...
TYPING_UNDER_VOICE = -6.0   # ... and typing this much more
MAX_WORDS = 20      # a phrase is cut to this many words ...
MIN_WORDS = 6       # ... at a clause boundary that leaves at least this many

# Extensions as said after "dot"; whole segments said as a word rather than spelled.
EXT = {'py': 'pie', 'md': 'M D', 'js': 'J S', 'ts': 'T S', 'yml': 'yammel', 'yaml': 'yammel', 'toml': 'tommel',
       'json': 'jason', 'txt': 'text', 'sh': 'S H', 'rs': 'R S', 'c': 'C', 'h': 'H', 'html': 'H T M L',
       'css': 'C S S', 'el': 'E L', 'rb': 'R B', 'go': 'go', 'cfg': 'config', 'ini': 'I N I', 'jsx': 'J S X',
       'tsx': 'T S X', 'cpp': 'C P P', 'hpp': 'H P P', 'java': 'java', 'lock': 'lock', 'rst': 'R S T'}
WORDS = {'xref': 'x ref', 'json': 'jason', 'sql': 'sequel', 'gui': 'gooey', 'todo': 'to do', 'yaml': 'yammel', 'toml': 'tommel'}
KINDS = {'def': 'function', 'function': 'function', 'func': 'function', 'fn': 'function', 'defun': 'function',
         'defmacro': 'macro', 'class': 'class', 'struct': 'struct', 'enum': 'enum', 'trait': 'trait',
         'type': 'type', 'interface': 'interface', 'union': 'union', 'const': 'constant', 'let': 'variable',
         'var': 'variable'}
IRREGULAR = {'have': 'has', 'do': 'does', 'go': 'goes', 'be': 'is', 'undo': 'undoes', "don't": "doesn't",
             'redo': 'redoes'}
# Verbs a subject's second clause may start with ("Land a move, and sweep it"),
# conjugated like its first.
VERBS = set('''add allow avoid build bump cache call caption carry check clean close count cut document draw drop
duck explain expose extract fix give handle hide hold inline join keep land let load make mark merge move name note
open pace pin place rework play put read remove rename render replace return reuse run save say scroll set show simplify
skip slow speed split start stop support sweep take test tidy treat trim turn type update use wrap write'''.split())
ADVERBS = {'never', 'always', 'also', 'only', 'now', 'still', 'just', 'really', 'actually'}


@dataclass(frozen=True)
class Phrase:
    """One thing said: its `text` (as captioned), the `key` of the shot (or
    edit-shot hunk) it is said over, and its `slot` there: which of the
    shot's anchors it waits for (a hunk's second callout, say)."""
    text: str
    key: tuple
    slot: int = 0


# ---- numbers ----
_ONES = ('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen '
         'seventeen eighteen nineteen').split()
_TENS = 'twenty thirty forty fifty sixty seventy eighty ninety'.split()


def number_words(n):
    """`n` (0 <= n < 10**6) in words, without "and": 213 -> "two hundred thirteen"."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t - 2] + (f'-{_ONES[o]}' if o else '')
    if n < 1000:
        h, r = divmod(n, 100)
        return f'{_ONES[h]} hundred' + (f' {number_words(r)}' if r else '')
    th, r = divmod(n, 1000)
    return f'{number_words(th)} thousand' + (f' {number_words(r)}' if r else '')


def rounded(n):
    """A count as said aloud: exact under 20, else rounded (to 5, 10, 50 or
    100 as it grows) with "about" when that changed it."""
    step = 1 if n < 20 else 5 if n < 100 else 10 if n < 300 else 50 if n < 1000 else 100
    r = int(round(n / step)) * step
    return ('about ' if r != n else '') + number_words(r)


def size_sentence(files, lines):
    """'Three files, about two hundred lines.'"""
    f = f'{number_words(files)} file{"s" if files != 1 else ""}'
    s = f'{f}, {rounded(lines)} line{"s" if lines != 1 else ""}.'
    return s[0].upper() + s[1:]


# ---- sentences ----
def third_person(word):
    """The third person singular of a verb: fix -> fixes, carry -> carries."""
    low = word.lower()
    if low in IRREGULAR:
        return IRREGULAR[low]
    if re.search(r'(s|sh|ch|x|z|o)$', low):
        return low + 'es'
    if re.search(r'[^aeiou]y$', low):
        return low[:-1] + 'ies'
    return low + 's'


_CONJUGATED = {'is', 'was', 'has', 'does', 'goes'}
# Verbs whose stem ends like a past tense ("embed", "speed up").
_ED_STEMS = {'bed', 'embed', 'imbed', 'shed', 'shred', 'wed', 'sled'}


def conjugated(word):
    """Whether a subject's first verb is already past, progressive or third
    person ("counted", "counting", "counts") rather than imperative ("ring",
    "speed", "focus")."""
    if word in _CONJUGATED:
        return True
    if word.endswith('ing'):   # a progressive keeps a vowel before it: "making", not "ring"
        return bool(re.search('[aeiouy]', word[:-3]))
    if word.endswith('ed'):
        return not (word.endswith('eed') or word in _ED_STEMS)
    return word.endswith('s') and not re.search(r'(ss|us|is|as)$', word) and word not in IRREGULAR


_PREFIX = re.compile(r'^(?:\[[^\]]*\]\s*|[\w-]+(?:\([^)]*\))?!?:\s+)')


def sentence_from_subject(subject, lead='This change'):
    """An imperative commit subject as a sentence about the change:
    ('Count wrapped rows', 'This change') -> 'This change counts wrapped rows.'
    A conventional-commit prefix (`fix(cli):`) or `[tag]` goes first; a
    subject that does not start with a word is quoted as its title."""
    s = ' '.join(subject.split())
    while True:
        m = _PREFIX.match(s)
        if not m or m.end() >= len(s):
            break
        s = s[m.end():]
    s = s.rstrip('.!? ')
    words = s.split(' ')
    if not words or not re.fullmatch(r"[A-Za-z][A-Za-z']*", words[0]):
        return finish(f'{lead} is titled: {subject.strip()}')
    k = 1 if words[0].lower() in ADVERBS and len(words) > 1 and re.fullmatch(r"[A-Za-z']+", words[1]) else 0
    head = [w.lower() if not w.isupper() or len(w) == 1 else w for w in words[:k]]
    verb = words[k]
    low = verb.lower()
    if conjugated(low):
        conj = low if not verb.isupper() else verb   # already past, progressive or third person
        if low.endswith('ing'):
            head = ['is'] + head
    else:
        conj = third_person(verb)
        rest = words[k + 1:]
        for j in range(1, len(rest)):   # "..., and sweep it" -> "..., and sweeps it"
            if rest[j - 1].lower() in ('and', 'then') and rest[j].lower() in VERBS:
                rest[j] = third_person(rest[j])
        return shorten(f'{lead} {" ".join(head + [conj] + rest)}')
    return shorten(f'{lead} {" ".join(head + [conj] + words[k + 1:])}')


# ---- cleaning what the commit says ----
_URL = re.compile(r'(?:https?://|www\.)\S+')
_HASH = re.compile(r'(?<![\w/])(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![\w/])')
_ASIDE = re.compile(r'\s*\([^()]*[^()\s][^()]*\)')
_SYMBOLS = re.compile(r'->|=>|==|!=|<=|>=|:=|&&|\|\||[+*&|#$@%^~<>\\{}\[\]=]')
_DASH = re.compile(r'\s+[-–—]+\s+|\s*—\s*')
_TRAILER = re.compile(r'^[A-Za-z0-9][\w]*(?:-[\w]+)+:\s')
_CODE_CHARS = re.compile(r'[(){}\[\]=;<>|\\]')


def _codeish(text):
    """Whether a unit is code rather than prose: a fence, or a line where many
    tokens carry code punctuation."""
    if '```' in text or text.lstrip().startswith(('$ ', '>>> ')):
        return True
    toks = text.split()
    if not toks:
        return True
    bare = [re.sub(r'\(\)$', '', t.strip('.,:!?').lstrip('(').rstrip(')')) for t in toks if t not in ('->', '=>')]
    punct = sum(1 for t in bare if _CODE_CHARS.search(t))
    return punct >= max(2, 0.4 * len(toks))


def clean(text):
    """A commit sentence as it can be said, or None when nothing sayable is
    left: no trailer, code, URL, hash, aside in parentheses or symbol; backticks
    dropped (their text kept); a dash as a comma."""
    if text is None or _TRAILER.match(text.strip()) or _codeish(text):
        return None
    # A clause pointing at a URL or a hash ("see https://...") goes with it;
    # in the sentence's first clause, just the link does.
    s = _ASIDE.sub('', text.strip())   # first, so a split never opens an aside
    parts = re.split(r'(?<=[,;:])\s+', s)
    s = ' '.join(p for k, p in enumerate(parts) if k == 0 or not (_URL.search(p) or _HASH.search(p)))
    s = _URL.sub(' ', s)
    s = _HASH.sub(' ', s)
    s = s.replace('`', '')
    s = _DASH.sub(', ', s)
    s = re.sub(r'(?<=\w)\(\)', '\0', s)   # keep a call's () through the symbol pass
    s = _SYMBOLS.sub(' ', s)
    s = re.sub(r'[()]', ' ', s).replace('\0', '()')   # a stray parenthesis
    s = ' '.join(s.split())
    s = re.sub(r'\s+([,.;:!?])', r'\1', s)
    s = re.sub(r'([,;:])(?:[,;:\s])+', r'\1 ', s).strip(' ,;:')
    s = re.sub(r'^[,.;:!?\s]+', '', s)
    s = re.sub(r'^[\w.-]+:\s+(?=\w)', '', s)   # a leading label ("Xref: ...")
    if not re.search(r'[A-Za-z]{2}', s):
        return None
    return s


_CLAUSE_AFTER = re.compile(r'[,;:]$')
_CLAUSE_BEFORE = {'and', 'but', 'so', 'which', 'while', 'because', 'or', 'then', 'when', 'where'}


_DANGLING = {'and', 'but', 'or', 'so', 'with', 'the', 'a', 'an', 'to', 'of', 'in', 'on', 'at', 'for', 'by',
             'from', 'which', 'that', 'while', 'when', 'where', 'then', 'its', 'their', 'is', 'are', 'as'}
# what can never end a clause, even one that ends at a comma ("what it is," can)
_NEVER_LAST = {'and', 'but', 'or', 'with', 'the', 'a', 'an', 'to', 'of', 'in', 'on', 'at', 'for', 'by', 'from',
               'its', 'their'}
_END = re.compile(r'(?<=[.!?])\s+(?=[A-Za-z_`])')


def finish(s, cut=False):
    """`s` as a phrase: no dangling comma (nor, when `cut` short, a dangling
    conjunction or preposition; `cut` may be the set of such words) at its end, a full stop, a capital first
    letter (unless it starts with an identifier)."""
    words = s.strip().rstrip(',;: ').split()
    dangling = _DANGLING if cut is True else cut or ()
    while len(words) > 1 and words[-1].lower().strip(',;:') in dangling:
        words.pop()
    s = ' '.join(words).rstrip(',;: ')
    if not s:
        return s
    if not re.search(r'[.!?]$', s):
        s += '.'
    first = s.split()[0]
    first = first.rstrip('.,;:')
    code = callouts_mod._looks_like_code(first) or first.endswith('()')
    return s if code else s[0].upper() + s[1:]


def shorten(text, most=MAX_WORDS, least=MIN_WORDS):
    """`text` cut to at most `most` words: whole sentences while they fit, else
    the first cut at its last clause boundary (after a comma, semicolon or
    colon; before "and", "but", "which" ...) that keeps `least` words, else at
    `most`; ending with a full stop."""
    sentences = _END.split(text.strip())
    kept = []
    for x in sentences:
        if len((' '.join(kept + [x])).split()) > most:
            break
        kept.append(x)
    if kept:
        return finish(' '.join(kept))
    words = sentences[0].split()
    cut = None
    for k in range(least, most + 1):   # cut before words[k]
        if k < len(words) and (_CLAUSE_AFTER.search(words[k - 1]) or words[k].lower() in _CLAUSE_BEFORE):
            cut = k
    # a hard cut can end anywhere ("... to the"); a clause boundary only on a
    # word no clause ends with ("... the patch speed and,")
    if cut is None:
        return finish(' '.join(words[:most]), cut=len(words) > most)
    return finish(' '.join(words[:cut]), cut=_NEVER_LAST)


_FRAGMENT = re.compile(r'^\s*(?:(?:and|but|or|so|for|then|which|while)\b|["\u201d]|\.\.\.|\u2026)')


def fragment(text):
    """Whether a unit is a piece of a sentence split inside a quotation
    ('And _edit." for a body ...'): unbalanced quotes, or a start no sentence
    has (a lower-case conjunction, a closing quote, an ellipsis)."""
    t = text or ''
    return bool(t.count('"') % 2 or t.count('\u201c') != t.count('\u201d') or _FRAGMENT.match(t))


def say_sentence(text):
    """A commit sentence ready to be said: cleaned and shortened, or None (a
    fragment is better silent)."""
    if fragment(text):
        return None
    s = clean(text)
    return shorten(s) if s else None


# ---- spoken forms ----
_SEGMENTS = re.compile(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+')


def _segment(seg):
    low = seg.lower()
    if low in WORDS:
        return WORDS[low]
    if seg.isdigit():
        return seg
    if (seg.isupper() and 2 <= len(seg) <= 3) or (len(seg) <= 3 and not re.search('[aeiouy]', low)):
        return ' '.join(low)   # spelled out: URL -> u r l, vb -> v b
    return low


def _name(part):
    return ' '.join(_segment(s) for chunk in re.split(r'[_\-]+', part) for s in _SEGMENTS.findall(chunk))


def spoken(token):
    """How an identifier, path or flag is said: `draw_card` -> "draw card",
    `kickerTitle` -> "kicker title", `git_video/film.py` -> "film dot pie",
    `--voice` -> "dash dash voice", `vb.speech` -> "v b dot speech"."""
    t = token.strip()
    if t.endswith('()'):
        t = t[:-2]
    if t.startswith('--') and len(t) > 2:
        return 'dash dash ' + spoken(t[2:])
    if '/' in t.rstrip('/'):
        parts = t.rstrip('/').split('/')
        if t.endswith('/') or '.' in parts[-1] and parts[-1].rsplit('.', 1)[-1].lower() in EXT or len(parts) > 2:
            t = parts[-1]   # a path: its base name
        else:   # "a/b" in prose: either
            return ' or '.join(spoken(x) for x in parts)
    parts = [p for p in t.split('.') if p]
    if not parts:
        return t
    said = [_name(p) for p in parts]
    if len(parts) > 1 and parts[-1].lower() in EXT:
        said[-1] = EXT[parts[-1].lower()]
    return ' dot '.join(s for s in said if s) or t


_CALL = re.compile(r'(?<![\w.])([A-Za-z_][\w.]*\w)\(\)')
_TOKEN = re.compile(r'--[A-Za-z][\w-]*|[A-Za-z_][\w./-]*\w')


def lexicon_for(phrases):
    """{word: respelling} for `say`: every identifier, path or flag in the
    phrases whose spoken form is not how it is spelled."""
    out = {}
    for p in phrases:
        toks = [m.group() for m in _TOKEN.finditer(p.text)]
        idents = [t for t in toks if callouts_mod._looks_like_code(t.rstrip('.')) or t.startswith('--')
                  or (t.isupper() and len(t) > 1) or re.search(r'[a-z][A-Z]', t)]
        idents += [m.group(0) for m in _CALL.finditer(p.text)]
        for ident in idents:
            ident = ident.rstrip('.')
            said = spoken(ident)
            if said and said.casefold() != ident.casefold():
                out[ident] = said
    return dict(sorted(out.items()))


# ---- the script ----
def base(path):
    return os.path.basename(path)


def _kind_word(d):
    if d.kind in ('def', 'func', 'fn', 'function') and d.owner in xref.CLASS_KINDS:
        return 'method'
    return KINDS.get(d.kind, d.kind if d.kind.isalpha() else 'definition')


def written_lines(change, f, hunks):
    """{hunk index: line} for the hunks of `f` no commit sentence explains,
    each naming the one definition its changes sit in: "A new function,
    draw_card." for each that adds a definition's own line ("A new test:
    ..." for a test, by its name's words), and "A change to callout_box." for
    the biggest of the rest; the others say nothing (silence is better than
    filler)."""
    new, changed = {}, []
    for hi in hunks:
        defs = planmod.hunk_defs(change, f, f.hunks[hi])
        if len(defs) != 1:
            continue
        name, d, is_new = defs[0]
        kind = _kind_word(d)
        test = re.match(r'test_?(?=[A-Za-z])', name) if name.lower().startswith('test') else None
        if is_new:
            if test and kind in ('function', 'method'):
                new[hi] = f'A new test: {_name(name[test.end():])}.'
            else:
                new[hi] = f'A new {kind}, {name}.'
        elif not test:
            h = f.hunks[hi]
            size = sum(b - a + 1 for a, b in h.added) + h.old_count
            changed.append((-size, hi, name, kind))
    if changed:
        _, hi, name, kind = min(changed)
        new[hi] = f'A change to {_called(name, kind)}.'
    return new


def _called(name, kind):
    """A definition as a line names it: "the constructor", "callout_box", "the draw function"."""
    if name == '__init__':
        return 'the constructor'
    if _test_name(name):
        return f'the test {_test_name(name)}'
    return name if len(name.strip('_')) > 4 or not name.isalpha() else f'the {name} {kind}'


_PLURAL = {'class': 'classes', 'method': 'methods', 'function': 'functions', 'macro': 'macros',
           'struct': 'structs', 'enum': 'enums', 'trait': 'traits', 'type': 'types', 'interface': 'interfaces',
           'union': 'unions', 'constant': 'constants', 'variable': 'variables', 'definition': 'definitions'}


def _listed(names):
    """'a', 'a and b', 'a, b and c', 'a, b and two more'."""
    if len(names) > 3:
        names = names[:2] + [f'{number_words(len(names) - 2)} more']
    return names[0] if len(names) == 1 else f'{", ".join(names[:-1])} and {names[-1]}'


def _test_name(name):
    m = re.match(r'test_?(?=[A-Za-z])', name) if name.lower().startswith('test') else None
    return m and _name(name[m.end():])


def _tests_of(path):
    """What a test file tests, from its name: tests/test_graph.py -> "the graph"."""
    stem = base(path).rsplit('.', 1)[0]
    stem = re.sub(r'^tests?_|_tests?$|_spec$|\.test$', '', stem)
    return f'the {_name(stem)}' if stem and collect.is_test(path) and stem.lower() not in ('test', 'tests') else None


def hunk_line(change, f, h):
    """One short line on what hunk `h` of `f` changes, from the definitions it
    touches: "Three new tests for the graph.", "Two new functions, edge_path
    and _rounded.", "Changes to draw and layout."; None when it touches none
    (no filler: silence is better)."""
    defs = planmod.hunk_defs(change, f, h)
    tests = [n for n, d, new in defs if new and _test_name(n) and _kind_word(d) in ('function', 'method')]
    new = [(n, d) for n, d, is_new in defs if is_new and n not in tests]
    changed = [n for n, d, is_new in defs if not is_new]
    if len(tests) > 1:
        what = _tests_of(f.path)
        return f'{number_words(len(tests)).capitalize()} new tests{" for " + what if what else ""}.'
    if tests and not new:
        return f'A new test: {_test_name(tests[0])}.'
    if len(new) == 1:
        return f'A new {_kind_word(new[0][1])}, {new[0][0]}.'
    if new:
        kinds = {_kind_word(d) for _, d in new}
        kind = _PLURAL.get(kinds.pop(), 'definitions') if len(kinds) == 1 else 'definitions'
        return f'{number_words(len(new)).capitalize()} new {kind}, {_listed([n for n, _ in new])}.'
    if changed:
        kinds = {n: _kind_word(d) for n, d, is_new in defs if not is_new}
        called = [_called(n, kinds[n]) for n in changed]
        return f'A change to {called[0]}.' if len(called) == 1 else f'Changes to {_listed(called)}.'
    return _top_level_line(f, h)


_IMPORT = re.compile(r'^\s*(?:from\s+(\S+)\s+import\s+(.+)|import\s+(.+)|(?:const|let|var)\s+\w+\s*=\s*require\(.+\)|'
                     r'#include\s*[<"](.+)[>"]|use\s+(.+);)')
_CONSTANT = re.compile(r'^([A-Z][A-Z0-9_]*[A-Z0-9])\s*(?::[^=]+)?=(?!=)')


def _top_level_line(f, h):
    """A line on a hunk outside every definition: the imports it adds, the
    constants it sets, or the file's opening docstring; else None."""
    lines = f.text.split('\n')
    added = [lines[n - 1] for a, b in h.added for n in range(a, b + 1) if 0 < n <= len(lines) and lines[n - 1].strip()]
    if not added:
        return None
    consts = list(dict.fromkeys(m.group(1) for m in map(_CONSTANT.match, added) if m))
    if consts and len(consts) * 2 >= len(added):
        return (f'A new setting, {consts[0]}.' if len(consts) == 1 else
                f'{number_words(len(consts)).capitalize()} settings, {_listed(consts)}.')
    if all(_IMPORT.match(ln) for ln in added):
        names = []
        for ln in added:
            m = _IMPORT.match(ln)
            what = m.group(2) or m.group(3) or m.group(4) or m.group(5) or ''
            names += [w.split(' as ')[-1].strip(' ()') for w in what.split(',') if w.strip(' ()')]
        names = [n for n in dict.fromkeys(names) if re.fullmatch(r'[\w.]+', n)]
        return f'It now imports {_listed(names)}.' if names else None
    m = _DOCSTRING.match(f.text)
    if m:
        end = f.text.find(m.group(1), m.end())
        first, last = f.text[:m.start(1)].count('\n') + 1, f.text[:max(end, m.end())].count('\n') + 1
        if all(first <= n <= last for a, b in h.added for n in range(a, b + 1)):
            return "The file's opening comment changes."
    return None


_DOCSTRING = re.compile(r'\s*(?:#[^\n]*\n\s*)*[rRuU]?("""|\'\'\')')


def dive_line(s):
    """What a dive into a definition says: "That calls frob, defined in lib.py."."""
    d = s['definition']
    verb = 'calls' if d.get('kind') in xref.FUNCTION_KINDS and d.get('kind') not in xref.CLASS_KINDS else 'uses'
    where = 'in this file' if d['path'] == s['path'] else f'in {base(d["path"])}'
    return f'That {verb} {s["name"]}, defined {where}.'


def _hunk_of(change, path, index):
    f = next((f for f in change.files if f.path == path), None)
    if f is None or f.text is None or index >= len(f.hunks):
        return None, None
    return f, f.hunks[index]


def _lead_ins(shots):
    """{enter shot index: its lead-in}: per patch "First, a.py.", "Next, b.py.",
    "Finally, c.py." (of three or more), or for a patch of one file "This is
    all in a.py."; "the tests in t.py" for the first of a run of test files;
    a file the develop mode deletes "..., which goes."."""
    out = {}
    by_patch = {}
    for i, s in enumerate(shots):
        if s['kind'] in ('dive', 'travel', 'open'):
            by_patch.setdefault(s.get('patch', 0), []).append(i)
    for idx in by_patch.values():
        paths = [shots[i].get('to', shots[i]['path']) for i in idx]
        for n, (i, path) in enumerate(zip(idx, paths)):
            nxt = shots[i + 1] if i + 1 < len(shots) else {}
            gone = ', which goes' if nxt.get('kind') == 'edit' and nxt.get('status') == 'D' else ''
            name = base(path)
            if collect.is_test(path) and not (n and collect.is_test(paths[n - 1])):
                name = f'the tests in {name}'   # once, for a run of test files
            if len(idx) == 1:
                line = f'This is all in {name}{gone}.'
            elif n == 0:
                line = f'First, {name}{gone}.'
            elif n == len(idx) - 1 and len(idx) >= 3:
                line = f'Finally, {name}{gone}.'
            else:
                line = f'{"Next" if n % 2 else "Then"}, {name}{gone}.'
            out[i] = line
    return out


def _opening(plan):
    c = plan.commit
    n_patches = len(plan.patches or ()) or 1
    out = []
    if n_patches > 1:
        out.append(f'This is a series of {number_words(n_patches)} patches.')
        out.append(sentence_from_subject(plan.patches[0]['commit']['subject'], 'The first patch'))
    elif plan.is_range:
        n = len(c.get('commits') or ())
        out.append(f'This is a range of {number_words(n)} commit{"s" if n != 1 else ""}.')
    else:
        out.append(sentence_from_subject(c['subject']))
    files = len(plan.files)
    lines = sum(f['added'] + f['deleted'] for f in plan.files)
    if files:
        out.append(size_sentence(files, lines))
    return out


_BEFORE = re.compile(r"\b(?:was|were|could|did|used to|had|wasn't|couldn't|didn't)\b")
# a past tense after one of these is in a subordinate clause ("edits each
# file from what it was"), not the main verb; "now" says the clause is today
_NOT_MAIN = re.compile(r"\b(?:what|that|which|who|whom|whose|where|when|how|if|whether|because|while|since|"
                       r"until|after|before|so|now)\b", re.I)


def _was(s):
    """Whether sentence `s` tells how things were: a past-tense marker in its
    first clause, ahead of any subordinate clause, and no "now" there."""
    first = re.split(r'[,;:]', s)[0]
    m = _BEFORE.search(first)
    return bool(m) and not _NOT_MAIN.search(first[:m.start()]) and not re.search(r'\bnow\b', first, re.I)


def _why(plan, patches, used):
    """What the change is for: the first sentence of the commit body's first
    paragraph, when no hunk's callout says it and the paragraph is prose
    rather than a list; one that tells how things were starts "Before,".
    None for a range."""
    if plan.is_range or len(patches) != 1:
        return None
    return why_line(plan.commit.get('body') or '', used)


def why_line(body, used=()):
    """`_why` of a commit body."""
    para = re.split(r'\n\s*\n', body.strip())[0]
    units = callouts_mod.sentences(para)
    if not units or units[0] in used or callouts_mod.BULLET.match(para):
        return None
    s = clean(units[0])
    if not s:
        return None
    if _was(s) and not callouts_mod._looks_like_code(s.split()[0]):
        s = 'Before, ' + s[0].lower() + s[1:]
    return shorten(s)


def script(plan, patches, fill=()):
    """The narration of `plan` (the change of each patch in `patches`), as
    [Phrase] in the order they are said, with a line on what changes (see
    `fill_lines`) for each hunk key in `fill`. Deterministic: the same plan
    and `fill` give the same script, however long its shots are."""
    fills = fill_lines(plan, patches) if fill else {}
    out = [Phrase(t, ('title',)) for t in _opening(plan)]
    used = {c['text'] for s in plan.shots for c in s.get('callouts') or ()}
    if plan.mode == 'develop':
        used |= {t for ch in patches for ts in planmod.develop_sentences(ch).values() for t in ts}
    why = _why(plan, patches, used)
    if why:   # over the repo map, or the title card without one
        out.append(Phrase(why, ('map',) if any(s['kind'] == 'map' for s in plan.shots) else ('title',)))
    lead = _lead_ins(plan.shots)
    develop = {k: planmod.develop_sentences(ch) for k, ch in enumerate(patches)} if plan.mode == 'develop' else {}
    unmatched = {}     # (patch, path) -> the hunks shown that no sentence explains
    for s in plan.shots:
        k = s.get('patch', 0)
        if s['kind'] == 'hunk' and not s.get('callouts'):
            unmatched.setdefault((k, s['path']), []).append(s['index'])
        elif s['kind'] == 'edit' and s.get('status') != 'D':
            unmatched.setdefault((k, s['path']), []).extend(
                hi for hi in hunks_in(s['steps']) if not develop[k].get((s['path'], hi)))
    written = {}
    for (k, path), hunks in unmatched.items():
        ch = patches[min(k, len(patches) - 1)]
        f = next((f for f in ch.files if f.path == path), None)
        if f is not None and f.text is not None:
            written[k, path] = written_lines(ch, f, [hi for hi in hunks if hi < len(f.hunks)])
    dived = set()      # (patch, path) a dive's line was said from
    for i, s in enumerate(plan.shots):
        kind, k = s['kind'], s.get('patch', 0)
        key = planmod.shot_key(s)
        here = written.get((k, s.get('path')), {})
        if i in lead:
            out.append(Phrase(lead[i], key))
        elif kind == 'patch':
            out.append(Phrase(sentence_from_subject(plan.patches[k]['commit']['subject'], 'The next patch'), key))
        elif kind == 'hunk':
            lines = [(n, say_sentence(c['text'])) for n, c in enumerate(s.get('callouts') or ())]
            lines = [(n, t) for n, t in lines if t] or ([(0, here[s['index']])] if s['index'] in here else [])
            if not lines and key in fill and key in fills:
                lines = [(0, fills[key])]
            out += [Phrase(t, key, n) for n, t in lines]
        elif kind == 'xref-push' and (k, s['path']) not in dived:
            dived.add((k, s['path']))
            out.append(Phrase(dive_line(s), key))
        elif kind == 'edit' and s.get('status') != 'D':
            for hi in hunks_in(s['steps']):
                lines = [(n, say_sentence(x)) for n, x in enumerate(develop[k].get((s['path'], hi), ()))]
                lines = [(n, t) for n, t in lines if t] or ([(0, here[hi])] if hi in here else [])
                hkey = ('edit', k, s['path'], hi)
                if not lines and hkey in fill and hkey in fills:
                    lines = [(0, fills[hkey])]
                out += [Phrase(t, hkey, n) for n, t in lines]
        elif kind == 'end':
            out.append(Phrase("That's the series." if len(plan.patches or ()) > 1 else "That's the change.", key))
            if plan.next:
                out.append(Phrase(sentence_from_subject(plan.next['subject'], 'The next commit'), key))
    return out


def fill_lines(plan, patches):
    """{hunk key: line} for the hunks the script says nothing over, each a
    line on what it changes (`hunk_line`) that its file has not heard: what
    a quiet stretch of a file may say (see `quiet_keys`)."""
    phrases = script(plan, patches)
    said = {}
    for p in phrases:
        if len(p.key) > 2:
            said.setdefault((p.key[1], p.key[2]), set()).add(p.text)
    keyed = {p.key for p in phrases}
    out = {}
    for s in plan.shots:
        k = s.get('patch', 0)
        if s['kind'] == 'hunk':
            todo = [(planmod.shot_key(s), s['index'])]
        elif s['kind'] == 'edit' and s.get('status') != 'D':
            todo = [(('edit', k, s['path'], hi), hi) for hi in hunks_in(s['steps'])]
        else:
            continue
        ch = patches[min(k, len(patches) - 1)]
        f = next((f for f in ch.files if f.path == s['path']), None)
        if f is None or f.text is None:
            continue
        heard = said.setdefault((k, s['path']), set())
        for key, hi in todo:
            if key in keyed or hi >= len(f.hunks):
                continue
            line = hunk_line(ch, f, f.hunks[hi])
            if line and line not in heard:
                heard.add(line)
                out[key] = line
    return out


QUIET = 8.0         # a file's stretch silent longer than this gets a line on a hunk in it ...
QUIET_EDGE = 1.5    # ... said at least this long after the phrase before it, and before the next


def quiet_keys(plan, phrases, seconds, candidates):
    """The keys of `candidates` (hunk keys a line could be said over) to say
    one over, so that no file's stretch (from the way into it to the way into
    the next) of the timed `plan` stays silent longer than QUIET where a hunk
    could break the silence: in each long gap, the one nearest its middle,
    and again in what is left."""
    starts, _ = planmod.frame_starts(plan.shots, plan.fps)
    timed = Timed(phrases, seconds)
    spans, at = [], {}
    for i, s in enumerate(plan.shots):
        t0 = starts[i] / plan.fps
        for key, anchor in anchors(s).items():
            first = anchor[0] if isinstance(anchor, (list, tuple)) else anchor
            if key in timed:
                spans += [(t0 + a, t0 + b) for a, b in timed.fit(key, anchor)[0]]
            elif key in candidates:
                at[key] = (t0 + first, i)
    stretch, cur = {}, -1   # shot index -> its file stretch
    for i, s in enumerate(plan.shots):
        if s['kind'] in ('dive', 'travel', 'open'):
            cur = i
        elif s['scene'] == 'card' or s['kind'] == 'map':
            cur = -1
        stretch[i] = cur
    bounds = {}
    for i, c in stretch.items():
        if c >= 0:
            a, b = bounds.get(c, (starts[i], starts[i]))
            bounds[c] = (min(a, starts[i]), starts[i] + round(plan.shots[i]['dur'] * plan.fps))
    chosen = set()
    for c, (f0, f1) in bounds.items():
        lo, hi = f0 / plan.fps, f1 / plan.fps
        cands = sorted((t, key) for key, (t, i) in at.items() if stretch[i] == c)
        said = sorted((a, b) for a, b in spans if lo - 1e-6 <= a < hi)
        while True:
            edges = [lo] + [x for sp in said for x in sp] + [hi]
            gaps = [(edges[j], edges[j + 1]) for j in range(0, len(edges), 2)]
            pick = None
            for g0, g1 in gaps:
                if g1 - g0 <= QUIET:
                    continue
                inside = [(t, key) for t, key in cands if g0 + QUIET_EDGE <= t <= g1 - QUIET_EDGE and key not in chosen]
                if inside:
                    pick = min(inside, key=lambda x: abs(x[0] - (g0 + g1) / 2))
                    break
            if pick is None:
                break
            chosen.add(pick[1])
            said = sorted(said + [(pick[0], pick[0] + 2.0)])
    return chosen


def hunks_in(steps):
    """The hunk indices an edit script performs, in the order it reaches them."""
    return list(dict.fromkeys(st['hunk'] for st in steps))


def keys_of(s):
    """The phrase keys shot `s` carries: its `plan.shot_key`, or for an edit
    shot one per hunk it performs."""
    if s['kind'] == 'edit':
        return [('edit', s.get('patch', 0), s['path'], hi) for hi in hunks_in(s['steps'])]
    key = planmod.shot_key(s)
    return [key] if key else []


def shot_index(shots):
    """{phrase key: the index of the shot it is said over}."""
    return {key: i for i, s in enumerate(shots) for key in keys_of(s)}


# ---- the voice: engine, cache, synthesis ----
class VoiceError(RuntimeError):
    """Narration is not possible: no speech extra, no model files, no such voice."""


def make_engine():
    """videobuilder's Kokoro engine; VoiceError with videobuilder's own
    message about what to install when the speech extra is missing."""
    try:
        return speech.Kokoro()
    except speech.SpeechUnavailable as e:
        raise VoiceError(str(e)) from None


def check_voice(engine, name):
    """Stop before anything renders when the model files are missing or
    `name` is not one of the engine's voices (the error lists them)."""
    try:
        names = engine.voices()
    except speech.SpeechUnavailable as e:
        raise VoiceError(str(e)) from None
    model = getattr(engine, 'model_path', '')
    if model is None or (model and not os.path.isfile(model)):
        raise VoiceError(f'Kokoro model files not found. {speech.INSTALL_HINT}')
    if name not in names:
        raise VoiceError(f'no Kokoro voice {name!r}; the voices are: {", ".join(names)}')


def cache_root():
    """$XDG_CACHE_HOME/git-video/voice (~/.cache by default): never in the repo filmed."""
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'git-video', 'voice')


def script_dir(phrases, lexicon, voice, speed=SPEED):
    """The cache directory of a script said in `voice` at `speed`: a hash of
    what is said and how, so a second render of the same change finds it."""
    key = json.dumps({'phrases': [p.text for p in phrases], 'lexicon': lexicon, 'voice': voice, 'speed': speed},
                     sort_keys=True, ensure_ascii=False)
    return os.path.join(cache_root(), hashlib.sha256(key.encode()).hexdigest()[:16])


BED = 'bed.wav'


def _write_bed(path):
    """Half a second of silence: the music bed videobuilder ducks the effects
    with (it only dips them under speech when the film has music)."""
    if os.path.isfile(path):
        return
    tmp = f'{path}.{os.getpid()}.tmp'
    with wave.open(tmp, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(speech.RATE)
        w.writeframes(bytes(2 * speech.RATE // 2))
    os.replace(tmp, path)


def clip_name(k):
    return f'{k:03d}.wav'


def synth(phrases, lexicon, voice, engine, out_dir):
    """Say each phrase into `out_dir`/NNN.wav (and its .vtt/.json) with
    videobuilder's `say`, which writes nothing (and runs no model) for a
    phrase whose files already match. Returns each phrase's length in s."""
    os.makedirs(out_dir, exist_ok=True)
    seconds = []
    try:
        for k, p in enumerate(phrases):
            doc = speech.say(p.text, os.path.join(out_dir, clip_name(k)), engine, voice=voice, speed=SPEED,
                             lexicon=lexicon)
            seconds.append(doc['seconds'])
    except speech.SpeechUnavailable as e:
        raise VoiceError(str(e)) from None
    _write_bed(os.path.join(out_dir, BED))
    return seconds


# ---- the voice on the film's clock ----
class Timed:
    """The script with each phrase's length: `fit(key, anchor)` lays out the
    phrases said over one shot (or edit-shot hunk). Each starts at its
    anchor (seconds into the shot; a list gives one per slot) or GAP after
    the one before, whichever is later. Returns ([(start, end)], need): need
    is when the shot may end, BREATH after its last phrase (0 for none)."""

    def __init__(self, phrases, seconds):
        self.by_key = {}
        for p, sec in zip(phrases, seconds):
            self.by_key.setdefault(p.key, []).append((p.slot, sec))

    def __contains__(self, key):
        return key in self.by_key

    def slots(self, key):
        """The slot of each phrase said over `key`, in order (as `fit` returns their spans)."""
        return [slot for slot, _ in self.by_key.get(key, ())]

    def fit(self, key, anchor=0.0):
        spans, end = [], None
        for slot, sec in self.by_key.get(key, ()):
            a = anchor[min(slot, len(anchor) - 1)] if isinstance(anchor, (list, tuple)) else anchor
            start = a if end is None else max(a, end + GAP)
            end = start + sec
            spans.append((start, end))
        return spans, (end + BREATH if spans else 0.0)


def read_start(s):
    """When a tour hunk shot's read begins: after its scroll and its morph."""
    return planmod.scroll_time(s) + (planmod.MORPH_HOLD + planmod.MORPH_DUR if s.get('morph') else 0)


def anchors(s):
    """{phrase key: anchor} for shot `s`: where in it each of its phrases may
    start (`Timed.fit`). The planner stretches a shot from these and `place`
    puts the phrases there, so the two agree."""
    kind = s['kind']
    if kind == 'title':
        return {('title',): planmod.VOICE_TITLE_AT}
    if kind == 'map':
        return {('map',): planmod.VOICE_MAP_AT}
    if kind == 'end':
        return {('end',): planmod.VOICE_END_AT}
    if kind == 'patch':
        return {planmod.shot_key(s): planmod.VOICE_PATCH_AT}
    if kind in ('dive', 'travel', 'open'):
        return {planmod.shot_key(s): 0.0}
    if kind == 'hunk':
        return {planmod.shot_key(s): [c['at'] for c in s.get('callouts') or ()] or read_start(s)}
    if kind == 'xref-push':
        return {planmod.shot_key(s): planmod.XREF_CLICK}
    if kind == 'edit':
        first = {}
        for st in s['steps']:
            first.setdefault(st['hunk'], st['at'])
        return {('edit', s.get('patch', 0), s['path'], hi): edits.shot_time(at, s['clock'])
                for hi, at in first.items()}
    return {}


def place(plan, phrases, seconds, voice, out_dir, lexicon):
    """The plan's `voice`: each phrase on the film's clock, starting on the
    first frame at or after its place in its shot (as the sound effects do),
    with the clip that says it."""
    starts, _ = planmod.frame_starts(plan.shots, plan.fps)
    timed = Timed(phrases, seconds)
    where = {}
    for i, s in enumerate(plan.shots):
        for key, anchor in anchors(s).items():
            if key in timed:
                where[key] = (i, iter(timed.fit(key, anchor)[0]))
    out = []
    for k, (p, sec) in enumerate(zip(phrases, seconds)):
        i, spans = where[p.key]
        rel, _ = next(spans)
        frame = starts[i] + math.ceil(rel * plan.fps - 1e-9)
        out.append({'text': p.text, 'shot': i, 'start': round(frame / plan.fps, 3),
                    'end': round(frame / plan.fps + sec, 3), 'audio': clip_name(k)})
    return {'voice': voice, 'speed': SPEED, 'dir': out_dir, 'lexicon': lexicon, 'bed': BED, 'phrases': out}


def narrate(change, patches, verbose, mode, engine, name):
    """The plan of `change` narrated in voice `name`: the script written from
    the plan, said (or found in the cache), and placed on its shots."""
    first = planmod.build(change, verbose, mode, patches)
    phrases = script(first, patches)
    lexicon = lexicon_for(phrases)
    out_dir = script_dir(phrases, lexicon, name)
    seconds = synth(phrases, lexicon, name, engine, out_dir)
    # Again, each shot as long as its phrases need: the same shots, so the
    # same script (its keys do not depend on how long shots are).
    final = planmod.build(change, verbose, mode, patches, voice=Timed(phrases, seconds))
    # A long silence in a file: a line on a hunk in it, said again and placed again.
    fill = quiet_keys(final, phrases, seconds, fill_lines(final, patches))
    if fill:
        phrases = script(first, patches, fill)
        lexicon = lexicon_for(phrases)
        out_dir = script_dir(phrases, lexicon, name)
        seconds = synth(phrases, lexicon, name, engine, out_dir)
        final = planmod.build(change, verbose, mode, patches, voice=Timed(phrases, seconds))
    if script(final, patches, fill) != phrases:
        raise AssertionError('the narrated plan says something else than the plan it was written from')
    final.voice = place(final, phrases, seconds, name, out_dir, lexicon)
    return final

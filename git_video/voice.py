"""The narration: what a colleague would say while showing you the change.

A narrator writes the script: a list of phrases, each tied to a shot by its
`plan.shot_key` (an edit shot's hunks by keys of their own). `script(plan,
patches)` is the heuristic narrator: sparse, it says only what the commit
says about why. The opening turns the commit subject into a sentence and
says the change's size in words; the body's lead sentence goes over the
repo map; a body sentence that gives a reason goes over the hunk the callout
matcher tied it to, shortened to a spoken length; a dive says what it calls
when the body names it; a --series card says the next patch; the closing
says so, and the next commit when there is one. Nothing narrates what the
editor shows: long silences are expected. (`narrator` has the other one.)

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

from . import callouts as callouts_mod, edits, narrator, plan as planmod, xref

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
IRREGULAR = {'have': 'has', 'do': 'does', 'go': 'goes', 'be': 'is', 'undo': 'undoes', "don't": "doesn't",
             'redo': 'redoes'}
# Verbs a subject's second clause may start with ("Land a move, and sweep it"),
# conjugated like its first.
VERBS = set('''add allow avoid build bump cache call caption carry check clean close count cut document draw drop
duck explain expose extract fix give handle hide hold inline join keep land let load make mark merge move name note
open pace pin place rework play put read remove rename render replace return reuse run save say scroll set show simplify
skip slow speed split start stop support sweep take test tidy treat trim turn type update use wrap write'''.split())
ADVERBS = {'never', 'always', 'also', 'only', 'now', 'still', 'just', 'really', 'actually'}
# Words a verb's object may start with: a word between a comma or "and" and
# one of these is a verb of the same predicate ("and plan the tour shots").
DETERMINERS = {'the', 'a', 'an', 'its', 'their', 'it', 'them', 'each', 'every', 'all', 'this', 'that', 'these',
               'those', 'our', 'his', 'her', 'any', 'one', 'both'}
# Subjects that open on a noun phrase rather than a verb ("Initial commit", "Minor fixes").
NOUN_STARTS = {'initial', 'first', 'wip', 'misc', 'miscellaneous', 'minor', 'various', 'more', 'new', 'typo',
               'typos', 'version', 'release', 'changes', 'updates', 'fixes', 'tweaks', 'cleanup', 'refactoring'}
_FIRST_COMMIT = re.compile(r"(?:initial|first)(?:\s+(?:commit|import|version|revision|check-?in))?", re.I)


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
    s = ' '.join(subject.replace('`', '').split())
    while True:
        m = _PREFIX.match(s)
        if not m or m.end() >= len(s):
            break
        s = s[m.end():]
    s = s.rstrip('.!? ')
    words = s.split(' ')
    if not words or not re.fullmatch(r"[A-Za-z][A-Za-z']*", words[0]):
        return finish(f'{lead} is titled: {subject.strip()}')
    if _FIRST_COMMIT.fullmatch(s):
        return 'This is the first commit.' if lead == 'This change' else f'{lead} is the first commit.'
    if words[0].lower() in NOUN_STARTS and not (len(words) > 1 and words[1].lower() in DETERMINERS):
        return shorten(f'{lead} brings {s[0].lower() + s[1:] if not words[0].isupper() else s}')
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
        return shorten(f'{lead} {" ".join(head + [conj] + _coordinated(words[k + 1:]))}')
    return shorten(f'{lead} {" ".join(head + [conj] + words[k + 1:])}')


_FUNCTION_WORDS = {'and', 'or', 'but', 'then', 'not', 'nor', 'so', 'yet', 'with', 'without', 'to', 'of', 'in', 'on',
                   'at', 'for', 'by', 'from', 'into', 'onto', 'over', 'under', 'per', 'via', 'than', 'as', 'after',
                   'before', 'between', 'within', 'like', 'up', 'down', 'out', 'off', 'about', 'across', 'instead',
                   'which', 'that', 'while', 'when', 'where', 'if', 'unless', 'until', 'once', 'too', 'only'}


def _coordinated(rest):
    """The words after a subject's first verb, every other verb of its
    predicate conjugated like the first: "and streak" right after it, a
    known verb after "and" or "then", a word before an object ("and plan the
    tour shots", ", test the side bar"), and whatever opens a clause after a
    semicolon ("; keep the target on top"). A list after a colon is nouns."""
    rest = list(rest)
    for j, w in enumerate(rest):
        if ':' in w and j < len(rest) - 1:
            break
        prev = rest[j - 1] if j else None
        low = w.lower()
        nxt = rest[j + 1].lower().strip(",;:'") if j + 1 < len(rest) else ''
        if (not re.fullmatch(r"[a-z][a-z']*", w) or low in DETERMINERS or low in _FUNCTION_WORDS
                or conjugated(low) or low in ADVERBS):
            continue
        if prev is None:
            continue
        after_and = prev.lower() in ('and', 'then', 'or')
        if (j == 1 and after_and                                 # "Ramp and streak ..."
                or prev.endswith(';')                            # "...; keep ..."
                or after_and and (low in VERBS or nxt in DETERMINERS)
                or prev.endswith(',') and nxt in DETERMINERS):
            rest[j] = third_person(w)
    return rest


# ---- cleaning what the commit says ----
_URL = re.compile(r'(?:https?://|www\.)\S+')
_HASH = re.compile(r'(?<![\w/])(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![\w/])')
_ASIDE = re.compile(r'\s*\([^()]*[^()\s][^()]*\)')
_SYMBOLS = re.compile(r'->|=>|==|!=|<=|>=|:=|&&|\|\||[+*&|#$@%^~<>\\{}\[\]=]')
_DASH = re.compile(r'\s+[-–—]+\s+|\s*—\s*')
_UNSAID = re.compile(r'[(){}\[\]=;<>]|:\s|:$')
_BARE_SYMBOL = re.compile(r'[-=<>|&*+#$@%^~\\{}\[\]!/]+[,.;:]?')
_TRAILER = re.compile(r'^[A-Za-z0-9][\w]*(?:-[\w]+)+:\s')
_CODE_CHARS = re.compile(r'[(){}\[\]=;<>|\\]')


def _dangles(clause):
    """Whether a clause's symbols (marked \\x02) are words of it: one after a
    preposition or conjunction, or before "or" or "and" ("after ( or ,")."""
    words = clause.split()
    for j, w in enumerate(words):
        if w.startswith('\x02'):
            prev = words[j - 1].lower().strip(',;:') if j else ''
            nxt = words[j + 1].lower().strip(',;:') if j + 1 < len(words) else ''
            if prev in _FUNCTION_WORDS or prev in _DANGLING or nxt in ('or', 'and', 'nor'):
                return True
    return False


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
    # A backticked span of code ("`def f(x):`") or of bare symbols is not
    # said: it is marked, and an aside holding it goes whole.
    s = re.sub(r'`([^`]*)`', lambda m: ' \x01 ' if _UNSAID.search(re.sub(r'\(\)$', '', m.group(1)))
               or not re.search(r'\w', m.group(1)) else m.group(0), text.strip())
    s = _ASIDE.sub('', s)   # first, so a split never opens an aside
    parts = re.split(r'(?<=[,;:])\s+', s)
    s = ' '.join(p for k, p in enumerate(parts) if k == 0 or not (_URL.search(p) or _HASH.search(p)))
    s = _URL.sub(' ', s)
    s = _HASH.sub(' ', s)
    # A clause whose words are symbols ("matched after `(` or `,`") cannot be
    # said without them: it goes whole, rather than leave "after or".
    s = s.replace('`', '')
    s = _DASH.sub(', ', s)
    s = ' '.join('\x02' if _BARE_SYMBOL.fullmatch(w) else w for w in s.split())
    s = re.sub(r'\s+([,;:.])', r'\1', s)
    if '\x01' in s or '\x02' in s:   # code goes with its clause; a bare symbol when it leaves words dangling
        clauses = [c for c in re.split(r'(?<=[,;:.])\s+', s)]
        broken = ['\x01' in c or _dangles(c) for c in clauses]
        if broken[0]:
            return None
        s = ' '.join(c for c, b in zip(clauses, broken) if not b).replace('\x02', ' ')
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
               'its', 'their', 'only', 'not'}
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


# Words that make a sentence about the tests or the tooling rather than the change.
_TEST_WORDS = {'test', 'tests', 'keyframe', 'keyframes', 'stills', 'fixture', 'fixtures', 'assert', 'asserts',
               'mock', 'mocks', 'pinned', 'pins', 'snapshot', 'snapshots', 'testcase', 'unittest'}
_AS_IS = re.compile(r'\bcalled as is\b', re.I)   # "X's Y, called as is": where code comes from
_STILL = re.compile(r"\b(?:the|a|its|one|each|every|[\w-]+-[\w-]+)\s+still\b", re.I)   # the noun, not the adverb


def jargon(text):
    """Whether a commit sentence is mostly test or implementation detail,
    which reads badly aloud ("A keyframe at the first dive's route mid-way is
    the graph-mid-dive still."): mostly identifiers, hyphenated names, or
    about tests and stills by name; or about the code itself rather than
    what the change does: two code names ("draw_kicker_title draws ..."
    after kicker_title_progress) or a library part "called as is". One
    name, the definition changed, is fine. Judged on what would be said."""
    text = re.sub(r'`[^`\w]+`', ' ', _ASIDE.sub('', text))   # what is said: no aside, no lone symbol
    words = re.findall(r"[\w'`.()/-]+", text)
    if not words:
        return False
    names = [w for w in words if callouts_mod._looks_like_code(w.strip('`.,()')) or w.count('-') >= 2
             or '`' in w]
    code = {w.strip("`.,()'").removesuffix("'s") for w in words if callouts_mod._looks_like_code(w.strip('`.,()'))
            and '/' not in w and not w.startswith('-') and not re.search(r'\.(?:py|el|toml|md|txt|json|sh)\W*$', w)}
    tests = [w for w in words if w.lower().strip('.,') in _TEST_WORDS] + _STILL.findall(text)
    return (len(names) >= 3 or len(names) >= 0.3 * len(words) or len(tests) >= 2
            or bool(tests) and bool(names) or len(code) >= 2 or bool(_AS_IS.search(text)))


def say_sentence(text, most=MAX_WORDS):
    """A commit sentence ready to be said: cleaned and shortened to `most`
    words, or None (a fragment, or test and implementation jargon, is better
    silent)."""
    if fragment(text):
        return None
    s = clean(text)
    s = shorten(s, most) if s else None
    return None if s is None or jargon(s) else s


def say_reason(text):
    """A commit sentence ready to be said when it gives a reason, else None:
    shortened to MAX_WORDS, or as far as narrator.MAX_WORDS when the reason
    is in a clause the shorter cut would drop ("..., but keeping the list
    keeps 1:1 consistent")."""
    for most in (MAX_WORDS, narrator.MAX_WORDS):
        s = say_sentence(text, most)
        if s and reason(s):
            return s
    return None


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


def dive_line(s):
    """What a dive into a definition says: "That calls frob, defined in lib.py."."""
    d = s['definition']
    verb = 'calls' if d.get('kind') in xref.FUNCTION_KINDS and d.get('kind') not in xref.CLASS_KINDS else 'uses'
    where = 'in this file' if d['path'] == s['path'] else f'in {base(d["path"])}'
    return f'That {verb} {s["name"]}, defined {where}.'


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
    Cut to MAX_WORDS, unless the reason it gives is further on. None for a
    range."""
    if plan.is_range or len(patches) != 1:
        return None
    body = plan.commit.get('body') or ''
    short = why_line(body, used)
    if short and not reason(short):
        long = why_line(body, used, narrator.MAX_WORDS)
        return long if long and reason(long) else short
    return short


def why_line(body, used=(), most=MAX_WORDS):
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
    s = shorten(s, most)
    return None if jargon(s) else s


# A sentence that says why, not only what: a reason or purpose ("so", "because",
# "to keep"), a constraint ("never", "without", "unless") or how things were.
_REASON = re.compile(r"\b(?:because|so that|so as to|in order to|instead|rather than|otherwise|that way|"
                     r"which means|no longer|never|without|unless|lets?|keeps?|avoids?|prevents?|allows?|"
                     r"needs?|must|can't|cannot|was|were|had|used to|could|couldn't|didn't|wasn't)\b|"
                     r",\s+so\b|\bto\s+(?:" + '|'.join(sorted(VERBS)) + r")\b", re.I)


def reason(text):
    """Whether a sentence states a purpose or a reason, rather than only what
    the code now does: what a sparse narration says over a hunk."""
    return bool(_REASON.search(text))


def _bodies(plan, k):
    """The commit bodies of patch `k` of `plan` (every commit's, for a range)."""
    commit = plan.patches[k]['commit'] if len(plan.patches or ()) > 1 else plan.commit
    return [c.get('body') or '' for c in commit.get('commits') or ()] or [commit.get('body') or '']


def _named(name, bodies):
    return any(re.search(rf'(?<![\w.]){re.escape(name)}(?!\w)', b) for b in bodies)


def script(plan, patches):
    """The heuristic narration of `plan` (the change of each patch in
    `patches`), as [Phrase] in the order they are said. Sparse: the opening
    (what the change does and its size), the body's lead sentence over the
    repo map, a body sentence that gives a reason over the hunk the callout
    matcher tied it to, a dive's line when the body names what it dives
    into, each --series patch's subject, and the closing; nothing that only
    says what the editor shows. Long silences are expected. Deterministic:
    the same plan gives the same script, however long its shots are."""
    out = [Phrase(' '.join(_opening(plan)), ('title',))]
    used = {c['text'] for s in plan.shots for c in s.get('callouts') or ()}
    if plan.mode == 'develop':
        used |= {t for ch in patches for ts in planmod.develop_sentences(ch).values() for t in ts}
    why = _why(plan, patches, used)
    # over the repo map (or the title card without one): the body's lead,
    # then the reasons it gives that no hunk's callout took
    framing = [why] if why else []
    if len(patches) == 1 and not plan.is_range:
        units = callouts_mod.sentences(plan.commit.get('body') or '')
        framing += [t for t in map(say_reason, (u for u in units[1:] if u not in used)) if t]
    at = ('map',) if any(s['kind'] == 'map' for s in plan.shots) else ('title',)
    out += [Phrase(t, at) for t in framing[:narrator.PER_SHOT - (at == ('title',))]]
    develop = {k: planmod.develop_sentences(ch) for k, ch in enumerate(patches)} if plan.mode == 'develop' else {}

    def reasons(texts):
        lines = [(n, say_reason(t)) for n, t in enumerate(texts)]
        return [(n, t) for n, t in lines if t]

    for s in plan.shots:
        kind, k = s['kind'], s.get('patch', 0)
        key = planmod.shot_key(s)
        if kind == 'patch':
            out.append(Phrase(sentence_from_subject(plan.patches[k]['commit']['subject'], 'The next patch'), key))
        elif kind == 'hunk':
            out += [Phrase(t, key, n) for n, t in reasons(c['text'] for c in s.get('callouts') or ())]
        elif kind == 'xref-push' and _named(s['name'], _bodies(plan, k)):
            out.append(Phrase(dive_line(s), key))
        elif kind == 'edit' and s.get('status') != 'D':
            for hi in hunks_in(s['steps']):
                hkey = ('edit', k, s['path'], hi)
                out += [Phrase(t, hkey, n) for n, t in reasons(develop[k].get((s['path'], hi), ()))]
        elif kind == 'end':
            end = "That's the series." if len(plan.patches or ()) > 1 else "That's the change."
            if plan.next:
                end += ' ' + sentence_from_subject(plan.next['subject'], 'The next commit')
            out.append(Phrase(end, key))
    return out


def hunks_in(steps):
    """The hunk indices an edit script performs, in the order it reaches them
    (not those an `apply` step puts in at once)."""
    return list(dict.fromkeys(st['hunk'] for st in steps if st['kind'] != 'apply'))


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
            if st['kind'] != 'apply':
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


def narrate(change, patches, verbose, mode, engine, name, narrate_with=None):
    """The plan of `change` narrated in voice `name`: the script (written by
    `narrate_with(plan)`, a narrator.Narrator, else the heuristic `script`)
    cut to the narrator limits, said (or found in the cache), and placed on
    its shots."""
    first = planmod.build(change, verbose, mode, patches)
    phrases = narrate_with(first) if narrate_with else script(first, patches)
    phrases = narrator.trim(phrases, first, strict=False)
    while True:
        lexicon = lexicon_for(phrases)
        out_dir = script_dir(phrases, lexicon, name)
        seconds = synth(phrases, lexicon, name, engine, out_dir)
        # Again, each shot as long as its phrases need: the same shots, so the
        # same keys (they do not depend on how long shots are).
        final = planmod.build(change, verbose, mode, patches, voice=Timed(phrases, seconds))
        kept = narrator.trim(phrases, first, seconds, final.total, strict=False)   # too much speech: say less
        if kept == phrases:
            break
        phrases = kept
    index = shot_index(final.shots)
    if any(p.key not in index for p in phrases):
        raise AssertionError('the narrated plan lost a shot the script is said over')
    final.voice = place(final, phrases, seconds, name, out_dir, lexicon)
    return final

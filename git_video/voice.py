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

from . import callouts as callouts_mod, edits, plan as planmod, xref

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
    if low.endswith('ed') or low.endswith('ing') or (low.endswith('s') and not low.endswith('ss')
                                                    and low not in IRREGULAR):
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
_SYMBOLS = re.compile(r'->|=>|==|!=|<=|>=|:=|&&|\|\||[+*&|#$@%^~<>\\{}\[\]=;]')
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
    parts = re.split(r'(?<=[,;:])\s+', text.strip())
    s = ' '.join(p for k, p in enumerate(parts) if k == 0 or not (_URL.search(p) or _HASH.search(p)))
    s = _URL.sub(' ', s)
    s = _HASH.sub(' ', s)
    s = _ASIDE.sub('', s)
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
_END = re.compile(r'(?<=[.!?])\s+(?=[A-Za-z_`])')


def finish(s):
    """`s` as a phrase: no dangling comma or conjunction at its end, a full
    stop, a capital first letter (unless it starts with an identifier)."""
    words = s.strip().rstrip(',;: ').split()
    while len(words) > 1 and words[-1].lower().strip(',;:') in _DANGLING:
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
    return finish(' '.join(words[:cut or most]))


def say_sentence(text):
    """A commit sentence ready to be said: cleaned and shortened, or None."""
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


def written_line(change, f, h, said):
    """What is written in a hunk no commit sentence explains, naming the one
    definition its changes sit in: "A new function, draw_card." when it adds
    the definition's own line ("A new test: ..." for a test, by its name's
    words); "A change to callout_box." the first time a file's existing
    definition is named, "And _edit." after; else None (silence is better
    than filler). `said`: the definitions already named in this file."""
    defs = planmod.hunk_defs(change, f, h)
    if len(defs) != 1:
        return None
    name, d, new = defs[0]
    if (name, d.line) in said:
        return None
    said.add((name, d.line))
    kind = _kind_word(d)
    test = re.match(r'test_?(?=[A-Za-z])', name) if name.lower().startswith('test') else None
    if test and kind in ('function', 'method'):
        return f'A new test: {_name(name[test.end():])}.' if new else None
    called = ('the constructor' if name == '__init__' else
              name if len(name.strip('_')) > 4 or not name.isalpha() else f'the {name} {kind}')
    if new:
        return f'A new {kind}, {name}.'
    return f'A change to {called}.' if len(said) == 1 else f'And {called}.'


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
    all in a.py."; a file the develop mode deletes "..., which goes."."""
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


def script(plan, patches):
    """The narration of `plan` (the change of each patch in `patches`), as
    [Phrase] in the order they are said. Deterministic: the same plan gives
    the same script, however long its shots are."""
    out = [Phrase(t, ('title',)) for t in _opening(plan)]
    lead = _lead_ins(plan.shots)
    said = {}          # (patch, path) -> definitions named there
    dived = set()      # (patch, path) a dive's line was said from
    develop = {}       # patch -> {(path, hunk): [commit sentence]}
    for i, s in enumerate(plan.shots):
        kind, k = s['kind'], s.get('patch', 0)
        key = planmod.shot_key(s)
        ch = patches[k] if k < len(patches) else patches[-1]
        if i in lead:
            out.append(Phrase(lead[i], key))
        elif kind == 'patch':
            out.append(Phrase(sentence_from_subject(plan.patches[k]['commit']['subject'], 'The next patch'), key))
        elif kind == 'hunk':
            lines = [(n, say_sentence(c['text'])) for n, c in enumerate(s.get('callouts') or ())]
            lines = [(n, t) for n, t in lines if t]
            if not lines and not s.get('callouts'):
                f, h = _hunk_of(ch, s['path'], s['index'])
                t = written_line(ch, f, h, said.setdefault((k, s['path']), set())) if h else None
                lines = [(0, t)] if t else []
            out += [Phrase(t, key, n) for n, t in lines]
        elif kind == 'xref-push' and (k, s['path']) not in dived:
            dived.add((k, s['path']))
            out.append(Phrase(dive_line(s), key))
        elif kind == 'edit' and s.get('status') != 'D':
            if k not in develop:
                develop[k] = planmod.develop_sentences(ch)
            for hi in hunks_in(s['steps']):
                hkey = ('edit', k, s['path'], hi)
                texts = [t for t in (say_sentence(x) for x in develop[k].get((s['path'], hi), ())) if t]
                if not texts and not develop[k].get((s['path'], hi)):
                    f, h = _hunk_of(ch, s['path'], hi)
                    t = written_line(ch, f, h, said.setdefault((k, s['path']), set())) if h else None
                    texts = [t] if t else []
                out += [Phrase(t, hkey, n) for n, t in enumerate(texts)]
        elif kind == 'end':
            out.append(Phrase("That's the series." if len(plan.patches or ()) > 1 else "That's the change.", key))
            if plan.next:
                out.append(Phrase(sentence_from_subject(plan.next['subject'], 'The next commit'), key))
    return out


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
    if script(final, patches) != phrases:
        raise AssertionError('the narrated plan says something else than the plan it was written from')
    final.voice = place(final, phrases, seconds, name, out_dir, lexicon)
    return final

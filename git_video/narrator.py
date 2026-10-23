"""Narrators: who writes the voice-over's script.

A script is a list of phrases, each anchored to a shot of the plan (by the
shot id `shot_rows` gives it) with the text to say. Two narrators write one:
`heuristic` (voice.script, from the commit message) and `fork-sandbox` (an
agent session that reads the change; see `fork_sandbox`). Whichever wrote
it, the script is held to the same sparse limits (`check`, `trim`): about one
phrase per SECONDS_PER_PHRASE of the unvoiced film plus the opening and the
closing, each at most MAX_WORDS words, and speech at most SPEECH_SHARE of the
voiced film. How a phrase is said (spoken forms, captions) and how the shots
stretch to it is voice's job, the same for both.
"""
import json
import re
from dataclasses import dataclass

from . import edits, plan as planmod

SECONDS_PER_PHRASE = 8.0   # at most one phrase per this much unvoiced film ...
MAX_WORDS = 35             # ... each at most this many words ...
SPEECH_SHARE = 0.45        # ... and all of them at most this share of the voiced film
WORDS_PER_SECOND = 2.5     # speech, for the brief's budget in seconds
PER_SHOT = 2               # phrases said over one shot (or edit-shot hunk), at most


class NarratorError(RuntimeError):
    """A narrator could not write a script that passes."""


@dataclass(frozen=True)
class Row:
    """One shot (or edit-shot hunk) a phrase may be said over: its `id`, the
    phrase key it stands for, when it starts and how long it lasts in the
    unvoiced plan (seconds), what it shows, and the file and lines."""
    id: str
    key: tuple
    start: float
    dur: float
    what: str
    where: str = ''


def _hunk(patches, k, path, index):
    ch = patches[min(k, len(patches) - 1)]
    f = next((f for f in ch.files if f.path == path), None)
    if f is None or index >= len(f.hunks):
        return ch, f, None
    return ch, f, f.hunks[index]


def _lines(h):
    if h is None:
        return ''
    a, n = h.new_start, h.new_count
    return f'line {a}' if n <= 1 else f'lines {a}-{a + n - 1}'


def _defs(ch, f, h):
    if f is None or h is None or f.text is None:
        return ''
    names = [n for n, _, _ in planmod.hunk_defs(ch, f, h)]
    return f', in {", ".join(names[:4])}' if names else ''


def _clip(text, most=16):
    w = text.split()
    return ' '.join(w[:most]) + (' ...' if len(w) > most else '')


def _what(plan, patches, s, hi=None):
    kind, k = s['kind'], s.get('patch', 0)
    n_patches = len(plan.patches or ()) or 1
    if kind == 'title':
        return 'opening card: the repository name, the commit subject, the lines added and removed, the author'
    if kind == 'map':
        return 'map of the repository, the changed files lit'
    if kind in ('dive', 'travel'):
        return f'camera moves into {s.get("to", s["path"])}'
    if kind == 'enter':
        return f'camera moves into {s["path"]}'
    if kind == 'open':
        return f'opens {s["path"]}'
    if kind == 'patch':
        subject = plan.patches[k]['commit']['subject']
        return f'card: patch {k + 1} of {n_patches}, "{subject}"'
    if kind == 'end':
        nxt = f'; the next commit, "{plan.next["subject"]}"' if plan.next else ''
        return f'closing card: a recap of the files visited and definitions dived into{nxt}'
    if kind == 'xref-push':
        d = s['definition']
        return f'clicks {s["name"]} and dives into its definition in {d["path"]} line {d["line"]}'
    if kind == 'hunk':
        ch, f, h = _hunk(patches, k, s['path'], s['index'])
        n = len(f.hunks) if f else 0
        how = 'morphs old text into new' if s.get('morph') else 'highlights added lines'
        said = ''.join(f'; a card shows the commit sentence "{_clip(c["text"])}"' for c in s.get('callouts') or ())
        return f'hunk {s["index"] + 1} of {n} ({how}){_defs(ch, f, h)}{said}'
    if kind == 'edit':
        ch, f, h = _hunk(patches, k, s['path'], hi)
        n = len(f.hunks) if f else 0
        verb = 'deletes the file' if s.get('status') == 'D' else f'types hunk {hi + 1} of {n}'
        return f'{verb}{_defs(ch, f, h)}'
    return kind


def shot_rows(plan, patches):
    """[Row] for every shot of `plan` a phrase may be said over, in order: an
    edit shot gives one row per hunk it performs. Ids are `s<shot index>`,
    and `s<shot index>h<hunk>` for an edit shot's hunks; they name the same
    keys in any plan of the same change, however long its shots."""
    starts, _ = planmod.frame_starts(plan.shots, plan.fps)
    rows = []
    for i, s in enumerate(plan.shots):
        t0, dur = starts[i] / plan.fps, s['dur']
        if s['kind'] == 'edit':
            at = {}
            for st in s['steps']:
                at.setdefault(st['hunk'], edits.shot_time(st['at'], s['clock']))
            his = list(at)
            for n, hi in enumerate(his):
                a = at[hi] if n else 0.0
                b = at[his[n + 1]] if n + 1 < len(his) else dur
                _, _, h = _hunk(patches, s.get('patch', 0), s['path'], hi)
                rows.append(Row(f's{i}h{hi}', ('edit', s.get('patch', 0), s['path'], hi), round(t0 + a, 1),
                                round(b - a, 1), _what(plan, patches, s, hi), f'{s["path"]} {_lines(h)}'.strip()))
            continue
        key = planmod.shot_key(s)
        if key is None:
            continue
        where = ''
        if s['kind'] == 'hunk':
            _, _, h = _hunk(patches, s.get('patch', 0), s['path'], s['index'])
            where = f'{s["path"]} {_lines(h)}'.strip()
        elif s['kind'] == 'xref-push':
            where = f'{s["path"]} line {s["line"]}'
        rows.append(Row(f's{i}', key, round(t0, 1), round(dur, 1), _what(plan, patches, s), where))
    return rows


def shot_table(rows):
    """The rows as the brief lists them, one line each."""
    out = []
    for r in rows:
        where = f'  [{r.where}]' if r.where else ''
        out.append(f'{r.id:<8} {r.start:6.1f}s {r.dur:5.1f}s  {r.what}{where}')
    return '\n'.join(out)


# ---- the limits ----
def words(text):
    return len(text.split())


def limits(plan):
    """(most phrases, most seconds of speech) for a script of `plan` (unvoiced):
    one phrase per SECONDS_PER_PHRASE plus the opening and the closing, and
    speech SPEECH_SHARE of its length (a guide: the voiced film is longer)."""
    return int(plan.total // SECONDS_PER_PHRASE) + 2, round(SPEECH_SHARE * plan.total)


def check(phrases, plan, seconds=None, total=None):
    """What is wrong with `phrases` as a script of `plan`, as [str] (empty
    when nothing is): too many phrases, one too long, more than PER_SHOT over
    one shot, a key no shot has, out of shot order, not opening on the title
    card; with `seconds` (each phrase's length) and `total` (the voiced
    film's), more speech than SPEECH_SHARE of it."""
    out = []
    index = _positions(plan)
    most, _ = limits(plan)
    if not phrases:
        return ['no phrases']
    if len(phrases) > most:
        out.append(f'{len(phrases)} phrases, more than {most}')
    for p in phrases:
        if words(p.text) > MAX_WORDS:
            out.append(f'{words(p.text)} words, more than {MAX_WORDS}: {p.text!r}')
    per = {}
    for p in phrases:
        per[p.key] = per.get(p.key, 0) + 1
    out += [f'{n} phrases over one shot {key}' for key, n in per.items() if n > PER_SHOT]
    missing = [p.key for p in phrases if p.key not in index]
    out += [f'no shot {key}' for key in missing]
    if not missing:
        order = [index[p.key] for p in phrases]
        if order != sorted(order):
            out.append('phrases out of shot order')
    if phrases[0].key != ('title',):
        out.append('the first phrase is not on the opening card')
    if seconds is not None and total:
        if sum(seconds) > SPEECH_SHARE * total + 1e-6:
            out.append(f'{sum(seconds):.1f} s of speech, more than {SPEECH_SHARE:.0%} of {total:.1f} s')
    return out


def _positions(plan):
    """{phrase key: (shot index, place among the shot's keys)}: where it comes in the film."""
    from . import voice
    return {key: (i, j) for i, s in enumerate(plan.shots) for j, key in enumerate(voice.keys_of(s))}


def trim(phrases, plan, seconds=None, total=None, strict=True):
    """`phrases` cut to `check`'s limits by dropping whole phrases: one over
    MAX_WORDS, a shot's phrases past PER_SHOT, then from the end while there
    are too many or (given `seconds` and `total`) too much speech. The opening
    phrase and the closing go last. NarratorError, when `strict`, if the
    script cannot be cut safely: a key no shot has, out of order, not opening
    on the title card, or the opening itself over the limits; not strict,
    what is left."""
    index = _positions(plan)
    bad = [x for x in check(phrases, plan) if x.startswith(('no shot', 'phrases out of', 'the first', 'no phrases'))]
    if bad:
        if strict:
            raise NarratorError('; '.join(bad))
        phrases = sorted((p for p in phrases if p.key in index), key=lambda p: index[p.key])
    secs = list(seconds) if seconds is not None else [0.0] * len(phrases)
    keep = list(range(len(phrases)))
    if strict and phrases and words(phrases[0].text) > MAX_WORDS:
        raise NarratorError(f'the opening is {words(phrases[0].text)} words, more than {MAX_WORDS}')
    seen = {}
    for j in list(keep):
        p = phrases[j]
        seen[p.key] = seen.get(p.key, 0) + 1
        if j and (words(p.text) > MAX_WORDS or seen[p.key] > PER_SHOT):
            keep.remove(j)
    most, _ = limits(plan)
    closing = [j for j in keep if phrases[j].key == ('end',) and j]

    def over():
        if len(keep) > most:
            return True
        return seconds is not None and total and sum(secs[j] for j in keep) > SPEECH_SHARE * total + 1e-6

    middle = [j for j in keep if j and j not in closing]
    while over() and (middle or closing):
        keep.remove(middle.pop() if middle else closing.pop())
    if over() and strict:
        raise NarratorError('the opening alone is over the limits')
    return [phrases[j] for j in keep]


# ---- a script as JSON ----
def parse(text, plan, patches):
    """[voice.Phrase] from a narrator's `{"phrases": [{"shot", "text"}]}`, its
    shot ids those of `shot_rows`; a phrase's slot is its place among its
    shot's. NarratorError for anything else (not JSON, an unknown shot id,
    an empty text), or a script `trim` cannot save."""
    from . import voice
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise NarratorError(f'not JSON: {e}') from None
    items = doc.get('phrases') if isinstance(doc, dict) else None
    if not isinstance(items, list) or not items:
        raise NarratorError('no "phrases" list')
    ids = {r.id: r.key for r in shot_rows(plan, patches)}
    out, slots = [], {}
    for n, it in enumerate(items):
        if not isinstance(it, dict) or not isinstance(it.get('shot'), str) or not isinstance(it.get('text'), str):
            raise NarratorError(f'phrase {n + 1}: needs a "shot" and a "text"')
        text = ' '.join(it['text'].split())
        if not re.search(r'[A-Za-z]', text):
            raise NarratorError(f'phrase {n + 1}: nothing to say')
        if not re.search(r'[.!?]$', text):
            text += '.'
        if it['shot'] not in ids:
            raise NarratorError(f'phrase {n + 1}: no shot {it["shot"]!r}')
        key = ids[it['shot']]
        slots[key] = slots.get(key, -1) + 1
        out.append(voice.Phrase(text, key, slots[key]))
    return trim(out, plan)


def dump(phrases, plan, patches):
    """`phrases` as `parse` reads them."""
    ids = {r.key: r.id for r in shot_rows(plan, patches)}
    return json.dumps({'phrases': [{'shot': ids[p.key], 'text': p.text} for p in phrases]}, indent=1,
                      ensure_ascii=False)


def timed_script(plan):
    """The voiced `plan`'s script as text, one phrase a line with its start and end."""
    out = []
    for q in plan.voice['phrases']:
        s = plan.shots[q['shot']]
        out.append(f'{q["start"]:7.2f}-{q["end"]:7.2f}  [{s["kind"]} {s.get("path", "")}]'.replace(' ]', ']')
                   + f'  {q["text"]}')
    return '\n'.join(out)

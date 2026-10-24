"""Narrators: who writes the voice-over's script.

A script is a list of phrases, each anchored to a shot of the plan (by the
shot id `shot_rows` gives it) with the text to say. Three narrators write
one: `heuristic` (voice.script, from the commit message), and two agents
that read the change from the same brief: `fork-sandbox` (a session in a
sandboxed clone; see `run_fork_sandbox`) and `claude` (Claude Code run
headless and read-only in a worktree of the change; see `run_claude`).
Whichever wrote it, the script is held to the same sparse limits (`check`,
`trim`): about one phrase per SECONDS_PER_PHRASE of the unvoiced film plus
the opening and the closing, each at most MAX_WORDS words (fewer for a
short --duration, `max_words`), and speech at most SPEECH_SHARE of the
voiced film. How a phrase is said (spoken forms, captions) and how the
shots stretch to it is voice's job, the same for all three.
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace

from . import edits, plan as planmod

SECONDS_PER_PHRASE = 8.0   # at most one phrase per this much unvoiced film ...
MAX_WORDS = 35             # ... each at most this many words (a --duration target's third, at least MIN_WORDS) ...
MIN_WORDS = 12
SPEECH_SHARE = 0.45        # ... and all of them at most this share of the voiced film
WORDS_PER_SECOND = 2.5     # speech, for the brief's budget in seconds
PER_SHOT = 2               # phrases said over one shot (or edit-shot hunk), at most
CALLOUT_WORDS = 12         # a narrator's callout card, at most this many words


class NarratorError(RuntimeError):
    """A narrator could not write a script that passes: `reason` in a line,
    and fork-sandbox's `run_dir` when it got that far."""
    reason = ''
    run_dir = None


@dataclass(frozen=True)
class Row:
    """One shot (or edit-shot hunk) a phrase may be said over: its `id`, the
    phrase key it stands for, when it starts and how long it lasts in the
    unvoiced plan (seconds), what it shows, and the file and lines; the
    names of the definitions it shows (`defs`) and the files (`files`),
    for `align`."""
    id: str
    key: tuple
    start: float
    dur: float
    what: str
    where: str = ''
    defs: tuple = ()
    files: tuple = ()


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


def _def_names(ch, f, h):
    if f is None or h is None or f.text is None:
        return []
    return [n for n, _, _ in planmod.hunk_defs(ch, f, h)]


def _defs(ch, f, h):
    names = _def_names(ch, f, h)
    return f', in {", ".join(names[:4])}' if names else ''


def _shows(plan, patches, s, hi=None):
    """(definition names, file paths) shot `s` (edit shot hunk `hi`) shows."""
    kind = s['kind']
    if kind in ('hunk', 'edit'):
        ch, f, h = _hunk(patches, s.get('patch', 0), s['path'], s['index'] if kind == 'hunk' else hi)
        return tuple(_def_names(ch, f, h)), (s['path'],)
    if kind == 'xref-push':
        return (s['name'],), tuple(dict.fromkeys((s['path'], s['definition']['path'])))
    if kind in ('dive', 'travel', 'enter', 'open'):
        return (), (s.get('to', s['path']),)
    return (), ()


def _uses(patches, row, names):
    """The identifiers a hunk row's changed lines use that name a definition
    another row shows (`names`), at most four: what else the hunk is about.
    A plain word counts only as code: called, or after a dot (`read(`,
    `.read`), so a comment's "read" is not a use."""
    if row.key[0] not in ('hunk', 'edit'):
        return []
    ch, f, h = _hunk(patches, row.key[1], row.key[2], row.key[3])
    if f is None or h is None or f.text is None:
        return []
    words = planmod.hunk_info(ch, f, h)['words']
    lines = f.text.split('\n')
    text = '\n'.join([lines[n - 1] for a, b in h.added for n in range(a, b + 1) if n <= len(lines)]
                     + [x for seg in h.segments for x in seg[1]])

    def code(n):
        return '_' in n or bool(re.search(rf'(?<!\w)\.{re.escape(n)}(?!\w)|(?<![\w.]){re.escape(n)}\(', text))
    return sorted(n for n in names if n in words and n not in row.defs and code(n))[:4]


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
        shows = lambda hi=None, s=s: dict(zip(('defs', 'files'), _shows(plan, patches, s, hi)))
        t0, dur = starts[i] / plan.fps, s['dur']
        if s['kind'] == 'edit':
            at = {}
            for st in s['steps']:
                if st['kind'] != 'apply':   # applied at once (--duration): nothing to say over
                    at.setdefault(st['hunk'], edits.shot_time(st['at'], s['clock']))
            his = list(at)
            for n, hi in enumerate(his):
                a = at[hi] if n else 0.0
                b = at[his[n + 1]] if n + 1 < len(his) else dur
                _, _, h = _hunk(patches, s.get('patch', 0), s['path'], hi)
                rows.append(Row(f's{i}h{hi}', ('edit', s.get('patch', 0), s['path'], hi), round(t0 + a, 1),
                                round(b - a, 1), _what(plan, patches, s, hi), f'{s["path"]} {_lines(h)}'.strip(),
                                **shows(hi)))
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
        rows.append(Row(f's{i}', key, round(t0, 1), round(dur, 1), _what(plan, patches, s), where, **shows()))
    # A hunk that uses what another shot shows says so, so a phrase about it
    # can be put where it is seen.
    names = {d for r in rows for d in r.defs}
    return [replace(r, what=f'{r.what}; uses {", ".join(u)}') if (u := _uses(patches, r, names)) else r
            for r in rows]


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


def max_words(duration):
    """The most words in a phrase of a video `duration` s long (None: no
    target): MAX_WORDS, or a third of the target in words, at least MIN_WORDS."""
    if duration is None:
        return MAX_WORDS
    return max(MIN_WORDS, min(MAX_WORDS, round(duration / 3)))


def limits(plan):
    """(most phrases, most seconds of speech) for a script of `plan` (unvoiced):
    one phrase per SECONDS_PER_PHRASE plus the opening and the closing, and
    speech SPEECH_SHARE of its length (a guide: the voiced film is longer);
    with --duration, of the target's length."""
    length = plan.duration or plan.total
    return int(length // SECONDS_PER_PHRASE) + 2, round(SPEECH_SHARE * length)


def drop_to_fit(phrases, seconds, over):
    """`phrases` (said in `seconds` each) with enough dropped to save `over`
    seconds of speech, at least one, from the end of the middle; the opening
    and the closing are always kept (unchanged when there is no middle)."""
    keep = list(range(len(phrases)))
    middle = [j for j in keep if j and phrases[j].key != ('end',)]
    saved = 0.0
    while (saved < over or len(keep) == len(phrases)) and middle:
        j = middle.pop()
        keep.remove(j)
        saved += seconds[j] + 0.35
    return [phrases[j] for j in keep]


def check(phrases, plan, seconds=None, total=None):
    """What is wrong with `phrases` as a script of `plan`, as [str] (empty
    when nothing is): too many phrases, one too long, more than PER_SHOT over
    one shot, a key no shot has, out of shot order, not opening on the title
    card; with `seconds` (each phrase's length) and `total` (the voiced
    film's), more speech than SPEECH_SHARE of it."""
    out = []
    index = _positions(plan)
    most, _ = limits(plan)
    longest = max_words(plan.duration)
    if not phrases:
        return ['no phrases']
    if len(phrases) > most:
        out.append(f'{len(phrases)} phrases, more than {most}')
    for p in phrases:
        if words(p.text) > longest:
            out.append(f'{words(p.text)} words, more than {longest}: {p.text!r}')
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
    longest = max_words(plan.duration)
    if strict and phrases and words(phrases[0].text) > longest:
        raise NarratorError(f'the opening is {words(phrases[0].text)} words, more than {longest}')
    seen = {}
    for j in list(keep):
        p = phrases[j]
        seen[p.key] = seen.get(p.key, 0) + 1
        if j and (words(p.text) > longest or seen[p.key] > PER_SHOT):
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
def _warn(log, what):
    print(f'git video: warning: narrator {what}', file=log if log is not None else sys.stderr)


def parse(text, plan, patches, log=None):
    """[voice.Phrase] from a narrator's `{"phrases": [{"shot", "text"}]}`
    (`read`, without its callouts)."""
    return read(text, plan, patches, log)[0]


def read(text, plan, patches, log=None):
    """([voice.Phrase], callouts) from a narrator's `{"phrases": [{"shot",
    "text"}], "callouts": [{"shot", "text"}]}`, its shot ids those of
    `shot_rows`; a phrase's slot is its place among its shot's. The callouts
    ({phrase key: [card text]}) are None when the answer has no "callouts"
    (the commit sentences stay), else they replace them: an empty list
    means no cards at all. A card not on a hunk shot, empty, over
    CALLOUT_WORDS words or past CALLOUT_MAX_PER_HUNK on its hunk is dropped
    with a warning to `log`. NarratorError for a bad phrase (not JSON, an
    unknown shot id, an empty text), or a script `trim` cannot save."""
    from . import voice
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise NarratorError(f'not JSON: {e}') from None
    items = doc.get('phrases') if isinstance(doc, dict) else None
    if not isinstance(items, list) or not items:
        raise NarratorError('no "phrases" list')
    rows = shot_rows(plan, patches)
    ids = {r.id: r.key for r in rows}
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
    return trim(align(out, plan, rows, log), plan), _callouts(doc.get('callouts'), rows, log)


def _subjects(text, rows):
    """(definition names, file names) phrase `text` names among what `rows`
    show: by an identifier that looks like code (callouts.identifiers, with
    no plain words: a definition named `cards` is not "cards") or by its
    spoken form of two words or more ("draw card" for draw_card)."""
    from . import callouts as callouts_mod, voice
    names = {d for r in rows for d in r.defs}
    files = {os.path.basename(f) for r in rows for f in r.files}
    defs, paths = set(), set()
    for ident in callouts_mod.identifiers(text):
        n, p = callouts_mod._keys(ident)
        defs |= n & names
        paths |= p
    low = ' '.join(re.findall(r'\w+', text.lower()))
    for x in names | files:
        said = ' '.join(re.findall(r'\w+', voice.spoken(x).lower()))
        if ' ' in said and re.search(rf'(?<!\w){re.escape(said)}(?!\w)', low):
            (defs if x in names else paths).add(x)
    return defs, paths


def _shown(row, defs, paths):
    """Whether `row` shows one of `defs` or a file one of `paths` names."""
    here = {x for f in row.files for b in [os.path.basename(f)] for x in (f, b, os.path.splitext(b)[0])}
    return bool(defs & set(row.defs) or paths & here)


def align(phrases, plan, rows, log=None):
    """`phrases` with each that names a definition or a file (`_subjects`)
    its own shot does not show moved to the nearest shot that does (the
    earlier on a tie), when shot order and PER_SHOT still hold there; else
    it stays. The opening and the closing never move. Each move is a
    warning to `log`. Slots are renumbered."""
    from . import voice
    index = _positions(plan)
    at = {r.key: n for n, r in enumerate(rows)}
    ids = {r.key: r.id for r in rows}
    frame = {('title',), ('end',)}
    out = list(phrases)
    for j, p in enumerate(out):
        if p.key in frame or p.key not in at:
            continue
        defs, paths = _subjects(p.text, rows)
        if not (defs or paths) or _shown(rows[at[p.key]], defs, paths):
            continue
        near = sorted((abs(n - at[p.key]), n) for n, r in enumerate(rows)
                      if r.key not in frame and r.key in index and _shown(r, defs, paths))
        for d, n in near:
            if d != near[0][0]:
                break
            key = rows[n].key
            lo = index[out[j - 1].key] if j and out[j - 1].key in index else (-1, -1)
            hi = index[out[j + 1].key] if j + 1 < len(out) and out[j + 1].key in index else (len(plan.shots), 0)
            if lo <= index[key] <= hi and sum(q.key == key for q in out) < PER_SHOT:
                _warn(log, f'phrase {j + 1} moved from {ids[p.key]} to {ids[key]}, which shows what it names: '
                           f'{p.text!r}')
                out[j] = voice.Phrase(p.text, key)
                break
    slots = {}
    for j, p in enumerate(out):
        slots[p.key] = slots.get(p.key, -1) + 1
        out[j] = voice.Phrase(p.text, p.key, slots[p.key])
    return out


def _callouts(items, rows, log):
    """{phrase key: [card text]} of a narrator's "callouts" (None without
    any), each on a hunk shot (or an edit shot's hunk) of `rows`."""
    if items is None:
        return None
    if not isinstance(items, list):
        _warn(log, 'callouts dropped: "callouts" is not a list')
        return {}
    hunks = {r.id: r.key for r in rows if r.key[0] in ('hunk', 'edit')}
    out = {}
    for n, it in enumerate(items):
        shot = it.get('shot') if isinstance(it, dict) and isinstance(it.get('shot'), str) else None
        text = ' '.join(it['text'].split()) if isinstance(it, dict) and isinstance(it.get('text'), str) else ''
        if shot not in hunks:
            _warn(log, f'callout {n + 1} dropped: {shot!r} is not a hunk shot')
        elif not re.search(r'[A-Za-z]', text):
            _warn(log, f'callout {n + 1} dropped: nothing to show')
        elif words(text) > CALLOUT_WORDS:
            _warn(log, f'callout {n + 1} dropped: {words(text)} words, more than {CALLOUT_WORDS}')
        elif len(out.get(hunks[shot], ())) >= planmod.CALLOUT_MAX_PER_HUNK:
            _warn(log, f'callout {n + 1} dropped: more than {planmod.CALLOUT_MAX_PER_HUNK} on {shot}')
        else:
            out.setdefault(hunks[shot], []).append(text)
    return out


def dump(phrases, plan, patches, callouts=None):
    """`phrases` (and the narrator's `callouts`, when there are) as `read` reads them."""
    ids = {r.key: r.id for r in shot_rows(plan, patches)}
    doc = {'phrases': [{'shot': ids[p.key], 'text': p.text} for p in phrases]}
    if callouts is not None:
        doc['callouts'] = [{'shot': ids[key], 'text': t} for key, ts in callouts.items() if key in ids for t in ts]
    return json.dumps(doc, indent=1, ensure_ascii=False)


def timed_script(plan):
    """The voiced `plan`'s script as text, one phrase a line with its start and end."""
    out = []
    for q in plan.voice['phrases']:
        s = plan.shots[q['shot']]
        out.append(f'{q["start"]:7.2f}-{q["end"]:7.2f}  [{s["kind"]} {s.get("path", "")}]'.replace(' ]', ']')
                   + f'  {q["text"]}')
    return '\n'.join(out)


# ---- the brief ----
BRIEF = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'narrator_brief.md')
_SLOT = re.compile(r'\{(range|commits|base|tip|max_phrases|max_speech_s|max_words|length|shot_table|'
                   r'max_callouts|callout_words|output|output_how|output_end)\}')
# How each agent hands its script back: (the intro's line, the Output
# section's opening, its last sentence).
OUTPUT = {
    'fork-sandbox': ('Your only output is one file:\n`outbox/narration.json`.',
                     'Write `outbox/narration.json`, in your artifact outbox (not in the\nrepository):',
                     'Write the file, check that it parses as JSON, and end your turn.'),
    'claude': ('Your only output is your final\nmessage: the narration as JSON.',
               'You can read the repository and run `git log`, `git show` and\n`git diff`, nothing else: '
               'you cannot write files. Your final message is\nthe narration, one JSON object and nothing else '
               '(a fenced `json` block\nis fine):',
               'Check that it parses as JSON, then end your turn with it as your final\nmessage.'),
}


def _indent(text, by='    '):
    return '\n'.join(by + ln if ln.strip() else '' for ln in text.split('\n'))


def brief(change, plan, patches, narrator='fork-sandbox'):
    """The narrator's handoff: narrator_brief.md with the change (its range
    and commit messages, trailers removed), the video's length (the
    --duration target, when there is one), the limits, the shot table of
    `plan` (unvoiced; condensed, it lists only what is kept), and how
    `narrator` (an agent of OUTPUT) hands its answer back filled in."""
    commits = change.commits
    if change.is_range:
        what = f'{len(commits)} commit{"s" if len(commits) != 1 else ""} {change.base}..{change.target}'
    else:
        what = f'commit {change.target} {commits[-1].subject}'
    msgs = '\n\n'.join(f'commit {c.sha}\n{_indent(c.subject)}' + (f'\n\n{_indent(c.body)}' if c.body.strip() else '')
                       for c in commits)
    most, speech = limits(plan)
    slots = {'range': what, 'commits': msgs, 'base': change.base, 'tip': change.target,
             'max_phrases': str(most), 'max_speech_s': str(speech), 'max_words': str(max_words(plan.duration)),
             'length': f'{round(plan.duration or plan.total)} seconds',
             'shot_table': shot_table(shot_rows(plan, patches)),
             'max_callouts': str(planmod.CALLOUT_MAX_PER_HUNK), 'callout_words': str(CALLOUT_WORDS),
             **dict(zip(('output', 'output_how', 'output_end'), OUTPUT[narrator]))}
    with open(BRIEF, encoding='utf-8') as f:
        template = f.read()
    return _SLOT.sub(lambda m: slots[m.group(1)], template)


# ---- choosing a narrator ----
NARRATORS = ('auto', 'fork-sandbox', 'claude', 'heuristic')
AGENTS = ('fork-sandbox', 'claude')
# Having `claude` on PATH is not consent to spend: auto runs it only when
# one of these is set, the way CI opts in.
CLAUDE_CREDENTIALS = ('CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY')


def resolve(choice, which=shutil.which, environ=None):
    """(narrator, fall back to the heuristic on failure) for --narrator
    `choice`: auto is fork-sandbox when the command is on PATH, else claude
    when the command is on PATH and a CLAUDE_CREDENTIALS variable is set in
    `environ` (os.environ), both falling back; else the heuristic."""
    if choice != 'auto':
        return choice, False
    environ = os.environ if environ is None else environ
    if which('fork-sandbox'):
        return 'fork-sandbox', True
    if which('claude') and any(environ.get(v) for v in CLAUDE_CREDENTIALS):
        return 'claude', True
    return 'heuristic', False


class Narrator:
    """The script writer for one render: `narrator(plan)` is the script of
    the unvoiced `plan` of `change` as [voice.Phrase], and `callouts` the
    cards it chose ({phrase key: [text]}; None, the heuristic's: the commit
    sentences). `ran` is the narrator that wrote it; fork-sandbox's run
    dir, when it ran, is `run_dir`. On an agent's failure under `fallback`,
    a one-line warning goes to `log` and the heuristic writes the script."""

    def __init__(self, name, change, patches, mode='tour', series=False, verbose=0, fallback=False, log=None,
                 duration=None):
        self.name, self.change, self.patches = name, change, patches
        self.mode, self.series, self.verbose, self.duration = mode, series, verbose, duration
        self.fallback, self.log = fallback, log if log is not None else sys.stderr
        self.run_dir = None
        self.callouts = None
        self.ran = None

    def __call__(self, plan):
        from . import voice
        self.callouts, self.ran = None, self.name
        if self.name == 'heuristic':
            return voice.script(plan, self.patches)
        try:
            return self.agent(plan)
        except NarratorError as e:
            if not self.fallback:
                raise
            where = []
            if self.name == 'fork-sandbox':
                where.append(f'run dir {e.run_dir}' if e.run_dir else 'no run dir')
            said = _last_word(getattr(e, 'output', ''))
            where += [f'it said: {said}'] if said else []
            print(f'git video: warning: {self.name} narrator failed ({"; ".join([e.reason or str(e)] + where)}); '
                  f'using the heuristic narrator', file=self.log)
            self.ran = 'heuristic'
            return voice.script(plan, self.patches)

    def agent(self, plan):
        """The agent's script, from the cache when it has narrated this before."""
        model = os.environ.get('GIT_VIDEO_NARRATOR_MODEL') or DEFAULT_MODEL
        key = cache_key(self.change, plan, self.patches, self.mode, self.series, self.verbose, model, self.duration,
                        self.name)
        path = cache_path(key)
        if os.path.isfile(path):
            try:
                with open(path, encoding='utf-8') as f:
                    phrases, self.callouts = read(f.read(), plan, self.patches, self.log)
                return phrases
            except (OSError, NarratorError):
                pass   # a stale or broken entry: narrate again
        if self.name == 'claude':
            phrases, callouts = run_claude(self.change, plan, self.patches, model, self.log)
        else:
            (phrases, callouts), self.run_dir = run_fork_sandbox(self.change, plan, self.patches, model, key,
                                                                 self.log)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(dump(phrases, plan, self.patches, callouts))
        os.replace(tmp, path)
        self.callouts = callouts
        return phrases


# ---- the fork-sandbox narrator ----
DEFAULT_MODEL = 'opus'
VERSION = 3   # of the brief and how its answer is read: part of the cache key
TEMPLATE_ID = 'git-video-narrate-v1'
_RUN_DIR = re.compile(r'^\s*run dir:\s+(\S.*?)\s*$', re.M)
# fork-sandbox refuses a handoff that does not resolve under here, or that
# sits in its forks/ machinery dir: briefs are staged in a dir of our own.
SCRATCH_ROOT = '/var/tmp/claude-scratch'


def _error(reason, run_dir=None, output=''):
    where = f' (run dir {run_dir})' if run_dir else ''
    e = NarratorError(reason + where + (f':\n{output.strip()}' if output.strip() else ''))
    e.reason, e.run_dir, e.output = reason, run_dir, output
    return e


def _last_word(output):
    """The line of an agent's `output` that best says why it stopped:
    its last `Error:` line, else its last non-blank one."""
    lines = [x.strip() for x in output.splitlines() if x.strip()]
    errors = [x for x in lines if re.match(r'(\S+:\s*)?error:', x, re.I)]
    return (errors or lines or [''])[-1]


def cache_key(change, plan, patches, mode, series, verbose, model, duration=None, narrator='fork-sandbox'):
    """The narration cache's key: the range filmed, how (the --duration
    target too), by which narrator, model and narrator version, and the
    shots the brief lists."""
    shots = hashlib.sha256(shot_table(shot_rows(plan, patches)).encode()).hexdigest()
    doc = {'base': change.base, 'tip': change.target, 'mode': mode, 'series': bool(series), 'verbose': verbose,
           'model': model, 'version': VERSION, 'shots': shots, 'duration': duration}
    if narrator != 'fork-sandbox':   # fork-sandbox's keys have no narrator, so its cached scripts stay found
        doc['narrator'] = narrator
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def cache_path(key):
    """$XDG_CACHE_HOME/git-video/narration/<key>.json (~/.cache by default)."""
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'git-video', 'narration', f'{key[:24]}.json')


def command(change, model, key, handoff):
    """The fork-sandbox argv narrating `change` from the brief at `handoff`;
    GIT_VIDEO_NARRATOR_ARGS (shell-split) goes before the paths."""
    meta = json.dumps({'kind': 'investigate', 'prompt_template_id': TEMPLATE_ID}, separators=(',', ':'))
    extra = shlex.split(os.environ.get('GIT_VIDEO_NARRATOR_ARGS', ''))
    return ['fork-sandbox', 'run', '--foreground', '--harness', 'claude', '--model', model,
            '--checkout', change.target, '--branch', f'git-video-narrate-{key[:12]}',
            '--task-meta', meta, *extra, change.repo, handoff]


def run_fork_sandbox(change, plan, patches, model, key, log):
    """Run fork-sandbox on the brief, blocking, and read the script the
    session leaves in its outbox: (([voice.Phrase], callouts), run dir) (see
    `read`). NarratorError
    (with `reason` and `run_dir`) when it fails, prints no run dir, leaves
    no narration.json, or one that does not pass."""
    os.makedirs(SCRATCH_ROOT, mode=0o700, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix='git-video-narrate-', dir=SCRATCH_ROOT)
    try:
        handoff = os.path.join(tmp, 'narrate.md')
        with open(handoff, 'w', encoding='utf-8') as f:
            f.write(brief(change, plan, patches))
        argv = command(change, model, key, handoff)
        print('git video: narrating with fork-sandbox, a few minutes…', file=log, flush=True)
        try:
            r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as e:
            raise _error(f'cannot run fork-sandbox: {e.strerror}') from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = r.stdout.decode('utf-8', 'replace')
    m = _RUN_DIR.search(out)
    if not m:
        raise _error(f'fork-sandbox printed no run dir (exit {r.returncode})', None, out[-4000:])
    run_dir = m.group(1)
    if r.returncode != 0:
        raise _error(f'fork-sandbox exited {r.returncode}', run_dir)
    path = os.path.join(run_dir, 'outbox', 'narration.json')
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        raise _error('the session wrote no outbox/narration.json', run_dir) from None
    try:
        return read(text, plan, patches, log), run_dir
    except NarratorError as e:
        raise _error(f'its narration.json does not pass: {e}', run_dir) from None


# ---- the claude narrator ----
CLAUDE_TIMEOUT = 15 * 60   # seconds: a session that reads the code takes minutes, rarely this many
# Read-only: the file-reading tools, and Bash only for git's history and
# diffs. `--tools` is what the session has at all, `--allowedTools` what it
# may run unasked (dontAsk refuses the rest), `--disallowedTools` a backstop.
CLAUDE_TOOLS = ('Read', 'Grep', 'Glob', 'Bash')
CLAUDE_ALLOWED = ('Read', 'Grep', 'Glob', 'Bash(git log:*)', 'Bash(git show:*)', 'Bash(git diff:*)')
CLAUDE_DENIED = ('Write', 'Edit', 'MultiEdit', 'NotebookEdit', 'WebFetch', 'WebSearch')
_FENCED = re.compile(r'^```[\w-]*[ \t]*\n(.*?)^```[ \t]*$', re.S | re.M)


def claude_command(model):
    """The `claude` argv: headless (the brief on stdin), JSON out, read-only,
    no settings files, MCP servers or saved session. A rule is one argument
    each: "Bash(git log:*)" has a space in it."""
    return ['claude', '-p', '--output-format', 'json', '--model', model, '--restricted',
            '--permission-mode', 'dontAsk', '--strict-mcp-config', '--no-session-persistence',
            '--tools', ','.join(CLAUDE_TOOLS), '--allowedTools', *CLAUDE_ALLOWED, '--disallowedTools', *CLAUDE_DENIED]


def answer(stdout):
    """The narration JSON in `claude -p --output-format json`'s `stdout`: its
    result text, the last fenced block's when it has one, else from its
    first brace to its last (the whole text when they bracket nothing, for
    `read` to reject). NarratorError when there is no result."""
    try:
        doc = json.loads(stdout)
    except ValueError:
        raise _error("claude's output is not JSON", output=stdout[-4000:]) from None
    if not isinstance(doc, dict) or not isinstance(doc.get('result'), str):
        raise _error('claude gave no result', output=stdout[-4000:])
    if doc.get('is_error'):
        raise _error(f'claude failed: {_clip(doc["result"], 30)}', output=doc['result'])
    text = doc['result'].strip()
    blocks = _FENCED.findall(text)
    if blocks:
        return blocks[-1].strip()
    first, last = text.find('{'), text.rfind('}')
    if 0 < first < last:
        return text[first:last + 1]
    return text


def run_claude(change, plan, patches, model, log, timeout=None):
    """Run `claude` on the brief in a worktree of `change`'s tip, blocking,
    and read the script its final message gives: ([voice.Phrase], callouts)
    (see `read`). NarratorError (with `reason`) when claude cannot run,
    exits non-zero, times out, or answers with no script that passes. The
    worktree is removed after; nothing is written in the repository's own."""
    timeout = CLAUDE_TIMEOUT if timeout is None else timeout
    prompt = brief(change, plan, patches, 'claude')
    tmp = tempfile.mkdtemp(prefix='git-video-narrate-')
    tree = os.path.join(tmp, 'tree')
    try:
        add = subprocess.run(['git', '-C', change.repo, 'worktree', 'add', '--detach', tree, change.target],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if add.returncode != 0:
            raise _error(f'cannot check out {change.target[:12]}', output=add.stderr)
        print('git video: narrating with claude, a few minutes…', file=log, flush=True)
        try:
            r = subprocess.run(claude_command(model), input=prompt.encode(), cwd=tree, capture_output=True,
                               timeout=timeout)
        except OSError as e:
            raise _error(f'cannot run claude: {e.strerror}') from None
        except subprocess.TimeoutExpired:
            raise _error(f'claude timed out after {timeout:g}s') from None
    finally:
        subprocess.run(['git', '-C', change.repo, 'worktree', 'remove', '--force', tree],
                       stdin=subprocess.DEVNULL, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    out, err = r.stdout.decode('utf-8', 'replace'), r.stderr.decode('utf-8', 'replace')
    if r.returncode != 0:
        raise _error(f'claude exited {r.returncode}', output=(err + out)[-4000:])
    text = answer(out)
    try:
        return read(text, plan, patches, log)
    except NarratorError as e:
        raise _error(f'its answer does not pass: {e}') from None

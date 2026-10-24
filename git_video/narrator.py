"""Narrators: who writes the voice-over's script.

A script is a list of phrases, each anchored to a shot of the plan (by the
shot id `shot_rows` gives it) with the text to say. Two narrators write one:
`heuristic` (voice.script, from the commit message) and `fork-sandbox` (an
agent session that reads the change; see `fork_sandbox`). Whichever wrote
it, the script is held to the same sparse limits (`check`, `trim`): about one
phrase per SECONDS_PER_PHRASE of the unvoiced film plus the opening and the
closing, each at most MAX_WORDS words (fewer for a short --duration,
`max_words`), and speech at most SPEECH_SHARE of the voiced film. How a phrase is said (spoken forms, captions) and how the shots
stretch to it is voice's job, the same for both.
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
from dataclasses import dataclass

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
                if st['kind'] != 'apply':   # applied at once (--duration): nothing to say over
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
    return trim(out, plan), _callouts(doc.get('callouts'), rows, log)


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
        shot = it.get('shot') if isinstance(it, dict) else None
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
                   r'max_callouts|callout_words)\}')


def _indent(text, by='    '):
    return '\n'.join(by + ln if ln.strip() else '' for ln in text.split('\n'))


def brief(change, plan, patches):
    """The narrator's handoff: narrator_brief.md with the change (its range
    and commit messages, trailers removed), the video's length (the
    --duration target, when there is one), the limits, and the shot table
    of `plan` (unvoiced; condensed, it lists only what is kept) filled in."""
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
             'max_callouts': str(planmod.CALLOUT_MAX_PER_HUNK), 'callout_words': str(CALLOUT_WORDS)}
    with open(BRIEF, encoding='utf-8') as f:
        template = f.read()
    return _SLOT.sub(lambda m: slots[m.group(1)], template)


# ---- choosing a narrator ----
NARRATORS = ('auto', 'fork-sandbox', 'heuristic')


def resolve(choice, which=shutil.which):
    """(narrator, fall back to the heuristic on failure) for --narrator
    `choice`: auto is fork-sandbox when the command is on PATH (falling
    back), else the heuristic."""
    if choice == 'auto':
        return ('fork-sandbox', True) if which('fork-sandbox') else ('heuristic', False)
    return choice, False


class Narrator:
    """The script writer for one render: `narrator(plan)` is the script of
    the unvoiced `plan` of `change` as [voice.Phrase], and `callouts` the
    cards it chose ({phrase key: [text]}; None, the heuristic's: the commit
    sentences). fork-sandbox's run dir, when it ran, is `run_dir`; on failure
    under `fallback`, a one-line warning goes to `log` and the heuristic
    writes the script."""

    def __init__(self, name, change, patches, mode='tour', series=False, verbose=0, fallback=False, log=None,
                 duration=None):
        self.name, self.change, self.patches = name, change, patches
        self.mode, self.series, self.verbose, self.duration = mode, series, verbose, duration
        self.fallback, self.log = fallback, log if log is not None else sys.stderr
        self.run_dir = None
        self.callouts = None

    def __call__(self, plan):
        from . import voice
        self.callouts = None
        if self.name == 'heuristic':
            return voice.script(plan, self.patches)
        try:
            return self.fork_sandbox(plan)
        except NarratorError as e:
            if not self.fallback:
                raise
            where = f'run dir {e.run_dir}' if e.run_dir else 'no run dir'
            said = _last_word(e.output)
            where += f'; it said: {said}' if said else ''
            print(f'git video: warning: fork-sandbox narrator failed ({e.reason or e}; {where}); '
                  f'using the heuristic narrator', file=self.log)
            return voice.script(plan, self.patches)

    def fork_sandbox(self, plan):
        model = os.environ.get('GIT_VIDEO_NARRATOR_MODEL') or DEFAULT_MODEL
        key = cache_key(self.change, plan, self.patches, self.mode, self.series, self.verbose, model, self.duration)
        path = cache_path(key)
        if os.path.isfile(path):
            try:
                with open(path, encoding='utf-8') as f:
                    phrases, self.callouts = read(f.read(), plan, self.patches, self.log)
                return phrases
            except (OSError, NarratorError):
                pass   # a stale or broken entry: narrate again
        (phrases, callouts), self.run_dir = run_fork_sandbox(self.change, plan, self.patches, model, key, self.log)
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
    """The line of fork-sandbox's `output` that best says why it stopped:
    its last `Error:` line, else its last non-blank one."""
    lines = [x.strip() for x in output.splitlines() if x.strip()]
    errors = [x for x in lines if re.match(r'(\S+:\s*)?error:', x, re.I)]
    return (errors or lines or [''])[-1]


def cache_key(change, plan, patches, mode, series, verbose, model, duration=None):
    """The narration cache's key: the range filmed, how (the --duration
    target too), by which model and narrator version, and the shots the
    brief lists."""
    shots = hashlib.sha256(shot_table(shot_rows(plan, patches)).encode()).hexdigest()
    doc = {'base': change.base, 'tip': change.target, 'mode': mode, 'series': bool(series), 'verbose': verbose,
           'model': model, 'version': VERSION, 'shots': shots, 'duration': duration}
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

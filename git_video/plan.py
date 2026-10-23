"""The shot plan: what the tour shows, in order, and for how long.

A plan is plain JSON. Each shot names a `scene` (the actor on stage: `card`
or `editor` this round), a `kind` within it and a `dur` in seconds, plus the
fields its kind needs. The planner here is deterministic heuristics; the JSON
is the seam a later pass (an LLM, say) can rewrite before the film is built.
"""
import bisect
import json
import os
import re
from dataclasses import asdict, dataclass, field

from videobuilder.type.typewriter import schedule

from . import callouts as callouts_mod, edits, graph as graphmod, highlight, repomap, worddiff, xref
from .collect import is_test

VERSION = 2
FPS = 30
PLAN_ROWS = 26            # rows the planner assumes are on screen: the fewest any format shows (1x1 with --voice)
# The film re-scrolls to keep the focus line off the window's last row
# (film.fit_top), so an xref is only picked from the rows above it.
XREF_ROWS = PLAN_ROWS - 1
# The film never scrolls sideways, so an xref is only picked from the columns
# every format shows: the fewest is 9x16 with a six-digit gutter and the
# minimap strip (default font).
PLAN_COLS = 60
MAX_HUNK_SHOTS = 60
XREF_CAP_PER_HUNK = 3     # --verbose=3
XREF_CAP_TOTAL = 24       # --verbose=3

TITLE_DUR = 3.4
TITLE_ROLL = (1.0, 2.4)   # the +a and −d counters roll up
TITLE_STILL = 1.85        # the title card's key moment: counters mid-roll, the name bar in
TITLE_BAR_IN = 0.9        # the author's name bar slides in ...
TITLE_BAR_OUT = 0.45      # ... and out, this long before the card ends
TITLE_AVATARS = 1.5       # co-authors spread out beside it
END_DUR = 3.0
END_REVEAL = 1.2          # the closing card's panels fade up one after another over this long
VOICE_TITLE_AT = 0.6      # with --voice: when the title card's phrases may start ...
VOICE_END_AT = 0.6        # ... the closing card's
VOICE_PATCH_AT = 0.5      # ... a --series card's
VOICE_MAP_AT = 0.3        # ... and the repo map's

# Shot timings, shared by the film (what is seen) and the sounds (what is heard).
PER_CHAR = 0.055          # mean seconds per typed character
OPEN_KEYS = (0.08, 0.22)  # C-x, C-f
OPEN_PROMPT = 0.35        # "Find file: " appears
OPEN_TYPE = 0.45          # the first letter of the path
OPEN_RET_GAP = 0.15       # last letter -> RET
OPEN_HOLD = 0.6           # the new buffer on screen before the next shot

HUNK_SCROLL = 0.6         # the scroll to the hunk; the sweep starts as it lands
HUNK_SCROLL_FAR = 1.2     # ... when it travels more than FAR_ROWS: ramped up and down, not a long linear crawl
FAR_ROWS = 2 * PLAN_ROWS
HUNK_SWEEP = 0.5
HUNK_READ_BASE, HUNK_READ_PER_LINE, HUNK_READ_MAX = 1.5, 0.08, 6.0
MORPH_HOLD = 0.9          # the old text on screen, removed words struck through, before it morphs
MORPH_DUR = 1.4           # removed words dissolve, kept ones glide, added ones decode in
MORPH_MAX_ROWS = PLAN_ROWS   # a hunk taller than a screenful is swept, not morphed
FOCUS_IN = 0.35           # the rest of the buffer dims once the scroll lands ...
FOCUS_OUT = 0.3           # ... and comes back up before the shot ends
CALLOUT_IN = 0.8          # a commit sentence's card fades in and its line runs to the words
CALLOUT_EACH = 2.6        # each callout stays at least this long (the hunk's read grows to fit)
CALLOUT_MAX_PER_HUNK = 2
INTER_DUR = 2.6           # the card between two patches of a --series ...
INTER_REVEAL = 1.2        # ... the next patch's card comes to the top of the deck over this long

# Develop mode's pacing. The performance (its edit shots) aims at
# SECONDS_PER_LINE per changed line, never under PERF_FLOOR; typing runs up to
# NATURAL_MAX times its natural pace, and past that each file after its first
# FAST_LEAD natural seconds is a speed-up sequence at a peak in
# [FAST_PEAK_MIN, FAST_PEAK_MAX], ramped in and out over FAST_RAMP s.
SECONDS_PER_LINE = 60 / 500
PERF_FLOOR = 15.0
NATURAL_MAX = 4.0
FAST_LEAD = 2.0
FAST_MIN = 2.0            # natural seconds a file must have left to be sped up
FAST_PEAK_MIN = 8.0
FAST_PEAK_MAX = 64.0
FAST_PEAK_CONDENSED = 256.0   # ... with --duration, as fast as the target needs, up to this
FAST_RAMP = 0.4
ELIDE_LINES = {0: 30, 1: 80}   # --verbose -> the typed run long enough to elide (2 and 3 never)
DEVELOP_CALLOUT_MIN = 1.2      # a develop callout shorter than this is dropped
FILL_SHOWN = 1.0               # an elided run's fold fills in over at least this long on screen
                               # (its natural length if shorter), whatever the speed around it
APPLY_SHOWN = 0.6              # ... and an applied run's marker (--duration) is up this long

XREF_TRAVEL = 0.7         # the pointer glides to the word
XREF_CLICK = 0.7          # ... and clicks it
XREF_RIPPLE = 0.35
XREF_KEYS = (1.1, 1.2)    # M, .
XREF_SWITCH = 1.4         # the definition's buffer starts to open out of the word ...
XREF_REVEAL = 0.4         # ... and fills the pane (taken out of the definition's dwell)
XREF_SWEEP = 0.5
XREF_DUR = 4.4
POP_KEYS = (0.08, 0.18)   # M, ,
POP_SWITCH = 0.35         # the definition starts to close back into the word ...
POP_REVEAL = 0.35         # ... and is gone
POP_DUR = 1.3

MAP_DUR = 2.2             # the repo map, wide, its changed tiles lighting one by one
MAP_LIGHT_START = 0.5
MAP_LIGHT_SPAN = 1.0      # the most the staggered starts spread over
MAP_LIGHT_STEP = 0.12     # ... and the most between two starts
MAP_LIGHT_FADE = 0.3      # one tile's lighting
MAP_MAX_TICKS = 12        # tile ticks heard, at most
DIVE_DUR = 1.0            # map -> a file's tile -> the editor
DIVE_CAM = 0.7            # the camera flies to the tile ...
DIVE_MORPH = (0.3, 0.95)  # ... while the tile grows into the pane (landed a frame before the cut)
DIVE_SWITCH = 0.5         # the side bar's current file changes
TRAVEL_DUR = 1.3          # the editor -> back out -> the next file's tile -> the editor
TRAVEL_SHRINK = 0.4
TRAVEL_CAM = (0.25, 1.0)
TRAVEL_GROW = (0.85, 1.25)
TRAVEL_SWITCH = 0.85


def camera_scale(s):
    """How far a dive's or travel's camera timings are compressed: its length
    over the nominal one (--duration may shrink it), never past 1 (a longer
    one, stretched for the voice, holds at the end)."""
    return min(1.0, s['dur'] / (DIVE_DUR if s['kind'] == 'dive' else TRAVEL_DUR))


def light_start(k, n):
    """When the k-th of n lit tiles starts to light, seconds into the map shot."""
    return MAP_LIGHT_START + k * min(MAP_LIGHT_STEP, MAP_LIGHT_SPAN / max(1, n))


def open_timing(path):
    """(key times, typed-letter times, RET time, duration) of an `open` shot."""
    ts, span = schedule(path, path, PER_CHAR)
    letters = [OPEN_TYPE + t for t in ts]
    ret = OPEN_TYPE + span + OPEN_RET_GAP
    return OPEN_KEYS, letters, ret, max(1.6, round(ret + OPEN_HOLD, 3))


def hunk_read(visible_added, callouts=0):
    """How long a hunk is read for, after its scroll and morph: longer for more
    lines, and long enough for its callouts one after another."""
    return max(min(HUNK_READ_MAX, HUNK_READ_BASE + HUNK_READ_PER_LINE * visible_added), callouts * CALLOUT_EACH)


def scroll_time(shot):
    """How long a hunk shot's scroll takes: HUNK_SCROLL_FAR when it is `far`."""
    return HUNK_SCROLL_FAR if shot.get('scroll') == 'far' else HUNK_SCROLL


def hunk_dwell(visible_added, *, morph=False, far=False, callouts=0):
    """A hunk shot's length: the scroll, the morph when there is one, the read."""
    return ((HUNK_SCROLL_FAR if far else HUNK_SCROLL) + (MORPH_HOLD + MORPH_DUR if morph else 0)
            + hunk_read(visible_added, callouts))


def morph_segments(f, h):
    """The hunk's segments as plan data, {'at', 'old', 'new', 'ops'} each, or
    None when it has nothing to morph (no removed lines), is taller than a
    screenful, or a segment is too big to diff by words."""
    if not h.segments or max(h.old_count, h.new_count) > MORPH_MAX_ROWS:
        return None
    lines = f.text.split('\n')
    out = []
    for at, old, new in h.segments:
        old = [x.expandtabs(highlight.TAB) for x in old]
        ops = worddiff.ops(old, [x.expandtabs(highlight.TAB) for x in lines[at:at + new]])
        if ops is None:
            return None
        out.append({'at': at, 'old': old, 'new': new, 'ops': ops})
    return out


def scroll_top(focus, lines, rows=PLAN_ROWS):
    """The 0-based first visible line that puts 1-based line `focus` a third of
    the way down a window of `rows`."""
    return max(0, min(focus - 1 - rows // 3, lines - rows))


def shot(scene, kind, dur, **fields):
    return {'scene': scene, 'kind': kind, 'dur': round(dur, 3), **fields}


@dataclass
class Plan:
    version: int
    fps: int
    base: str
    target: str
    label: str
    is_range: bool
    commit: dict
    files: list
    verbose: int
    shots: list = field(default_factory=list)
    total: float = 0.0
    repo: str = ''            # the repository's directory name, for the title card's kicker
    next: dict | None = None  # {'short', 'subject'} of the commit after the target on HEAD's line
    map: dict | None = None   # the repo map's groups and tiles (repomap.tiles)
    graph: dict | None = None # the call graph's nodes and edges (graph.build)
    mode: str = 'tour'        # tour or develop
    # One entry per patch performed or toured: the whole change set, or with
    # --series each commit of the range (see `patch_entry`).
    patches: list = field(default_factory=list)
    # With --voice, the narration (voice.place): the voice, the cache dir its
    # clips are in, and each phrase's text, shot and times. Left out of the
    # JSON without --voice, so the plan is the same as without the flag.
    voice: dict | None = None
    # With --duration, the target length in seconds (condense); left out of
    # the JSON without it.
    duration: float | None = None

    def to_json(self):
        d = asdict(self)
        for k in ('voice', 'duration'):
            if d[k] is None:
                del d[k]
        return json.dumps(d, indent=2, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, text):
        return cls(**json.loads(text))


def repo_name(path):
    """The repository's directory name (a bare repository's without `.git`)."""
    name = os.path.basename(os.path.normpath(path))
    return name[:-4] if name.endswith('.git') and len(name) > 4 else name


def _commit_info(change):
    last = change.commits[-1]
    info = {'subject': last.subject, 'body': last.body, 'short': last.short, 'author': last.author,
            'date': last.date, 'coauthors': list(last.coauthors),
            'commits': [{'short': c.short, 'subject': c.subject, 'author': c.author, 'date': c.date,
                         'body': c.body} for c in change.commits]}
    if change.is_range:
        a, b = change.label.split('-', 1)
        n = len(change.commits)
        info['subject'] = f'{a}..{b}: {n} commit{"s" if n != 1 else ""}'
        info['body'] = ''
        info['coauthors'] = list(dict.fromkeys(n for c in change.commits for n in c.coauthors))
    if change.merged:   # small --series patches played as one beat: its card says so
        info['subject'] = f'{change.merged} small commits'
    return info


def _budget(verbose):
    """(total, per file, per hunk) xref visits allowed."""
    big = 10 ** 9
    return {0: (1, big, big), 1: (big, 1, big), 2: (big, big, 1)}.get(
        verbose, (XREF_CAP_TOTAL, big, XREF_CAP_PER_HUNK))


def _hunk_candidates(change, f, top, visible):
    """[(rank key, candidate, definition)] for the visible added lines of a hunk, best first."""
    lang = xref.lang_of(f.path)
    if lang is None or not visible:
        return []
    defs = xref.index(change.repo, change.target, lang)
    out = []
    for c in xref.candidates(f.text, f.path, visible):
        if c.col + len(c.name) > PLAN_COLS:
            continue
        d = xref.resolve(c, defs, f.path, f.text)
        if d is not None and change.text(d.path) is not None:
            out.append((xref.rank_key(c, d, f.path), c, d))
    out.sort(key=lambda r: r[0])   # stable: equal keys keep their order of appearance
    return out


_WORDS = re.compile(r'[A-Za-z_][\w-]*\w|[A-Za-z_]\w*')


def hunk_info(change, f, h):
    """What a commit sentence can match in a hunk: `words` on its added and
    removed lines, `defs`, the names of the definitions holding its changed
    lines."""
    lines = f.text.split('\n')
    changed = [n for a, b in h.added for n in range(a, b + 1)]
    words = set()
    for t in [lines[n - 1] for n in changed if n <= len(lines)] + [x for s in h.segments for x in s[1]]:
        for w in _WORDS.findall(t):
            words.add(w)
            words.update(p for p in re.split(r'-', w) if p)
    fd = xref.file_defs(change.repo, change.target, f.path)
    holders = xref.enclosures(lines, fd)
    defs = set()
    for n in changed + [s[0] for s in h.segments]:
        e = holders[n - 1] if 0 < n <= len(holders) else None
        if e:
            defs.add(e[0])
    return {'path': f.path, 'words': words, 'defs': defs}


def hunk_defs(change, f, h):
    """[(name, Definition, new)]: the definitions holding a hunk's changed
    lines, each once, in line order; `new` when the definition's own line is
    one the hunk adds."""
    lines = f.text.split('\n')
    holders = xref.enclosures(lines, xref.file_defs(change.repo, change.target, f.path))
    added = {n for a, b in h.added for n in range(a, b + 1)}
    out = {}
    for n in sorted(added | {s[0] for s in h.segments}):
        e = holders[n - 1] if 0 < n <= len(holders) else None
        if e and e[1].line not in out:
            out[e[1].line] = (e[0], e[1], e[1].line in added)
    return [out[k] for k in sorted(out)]


def callout_target(f, word, top, prefer, fallback):
    """(line, col, length) of `word` on screen in a window from `top`: its first
    whole-word use on one of the `prefer` lines, else on any line in view
    (within PLAN_COLS); else the `fallback` line's text, or with no `prefer`
    lines (a pure deletion), the gap before the `fallback` line (length 0)."""
    lines = f.text.split('\n')
    rows = range(top + 1, min(len(lines), top + PLAN_ROWS) + 1)
    for w in ((word, '--' + word) if word else ()):   # a --flag is matched by its name
        pat = re.compile(rf'(?<![\w$-]){re.escape(w)}(?![\w$-])')
        for pool in ([n for n in prefer if n in rows], rows):
            for n in pool:
                m = pat.search(lines[n - 1].expandtabs(highlight.TAB))
                if m and m.end() <= PLAN_COLS:
                    return n, m.start(), len(w)
    n = min(max(1, fallback), max(1, len(lines)))
    if not prefer:
        return n, 0, 0
    text = lines[n - 1].expandtabs(highlight.TAB) if lines else ''
    col = len(text) - len(text.lstrip())
    return n, min(col, PLAN_COLS - 1), max(1, min(len(text.rstrip()) - col, PLAN_COLS - col))


def _match_callouts(change, hunks):
    """{index into `hunks` ((file, hunk) pairs): [(unit index, score, word, text,
    commit)]}: the commit sentences that name something in each hunk, each
    hunk matched against the commit that owns it."""
    infos = [hunk_info(change, f, h) for f, h in hunks]
    names = {d for info in infos for d in info['defs']}
    chosen = {}
    for c in change.commits:
        units = callouts_mod.sentences(c.body) if c.body else []
        mine = [k for k, (_, h) in enumerate(hunks) if h.commit == c.sha]
        if not units or not mine:
            continue
        for u, j, word, sc in callouts_mod.match(units, [infos[k] for k in mine], names):
            chosen.setdefault(mine[j], []).append((u, sc, word, units[u], c))
    return chosen


def _picks(matched):
    """The best CALLOUT_MAX_PER_HUNK of a hunk's matched sentences, in the
    order the message gives them."""
    return sorted(sorted(matched, key=lambda x: (-x[1], x[0]))[:CALLOUT_MAX_PER_HUNK], key=lambda x: x[0])


def develop_sentences(change):
    """{(path, hunk index): [commit sentence]}: what develop mode's callouts
    say over each hunk of `change` (before any is dropped for being too
    short on screen)."""
    pairs = [(f, hi) for f in order_files(change) if f.text is not None for hi in range(len(f.hunks))]
    chosen = _match_callouts(change, [(f, f.hunks[hi]) for f, hi in pairs])
    return {(pairs[k][0].path, pairs[k][1]): [x[3] for x in _picks(v)] for k, v in chosen.items()}


def _place_callouts(change, shots, readings, voice=None):
    """Give each hunk shot the commit sentences that explain it (`callouts`),
    each with its time on screen and the words its line points at, and stretch
    the hunk's read to fit them. A range uses each hunk's own commit. With a
    `voice` (voice.Timed), the shot lasts at least as long as what is said
    over it, and each callout comes up as its sentence is said."""
    chosen = _match_callouts(change, [(f, h) for _, f, h, _, _ in readings])
    for k, (si, f, h, top, visible) in enumerate(readings):
        s = shots[si]
        picks = _picks(chosen.get(k, []))
        morph, far = bool(s['morph']), s['scroll'] == 'far'
        start = scroll_time(s) + (MORPH_HOLD + MORPH_DUR if morph else 0)
        read = hunk_read(visible, len(picks))
        slot = read / max(1, len(picks))
        added = [n for a, b in h.added for n in range(a, b + 1)]
        out = []
        for n, (_, _, word, text, c) in enumerate(picks):
            line, col, length = callout_target(f, word, top, added, s['focus'][0])
            at = start + n * slot
            out.append({'text': text, 'commit': c.short, 'line': line, 'col': col, 'len': length,
                        'at': round(at, 3), 'until': round(at + slot - 0.2, 3)})
        s['callouts'] = out
        s['dur'] = round(hunk_dwell(visible, morph=morph, far=far, callouts=len(picks)), 3)
        if voice is not None and shot_key(s) in voice:
            _voice_callouts(s, voice, start)


def _voice_callouts(s, voice, read):
    """A narrated hunk shot: long enough for its phrases (each waits for its
    callout, or the read's start), each callout up from its phrase's start
    to just before the next callout or the shot's end."""
    key = shot_key(s)
    out = s['callouts']
    spans, need = voice.fit(key, [c['at'] for c in out] or read)
    s['dur'] = round(max(s['dur'], need), 3)
    said = dict(zip(voice.slots(key), spans))
    floor = 0.0
    for n, c in enumerate(out):
        a, z = said.get(n, (max(c['at'], floor), None))
        c['at'] = round(a, 3)
        floor = (z if z is not None else a) + 0.35
    for c, nxt in zip(out, out[1:] + [None]):
        c['until'] = round((nxt['at'] if nxt else s['dur']) - 0.2, 3)


def file_entries(change):
    """The plan's `files`: what the side bar lists and the fringe marks, per file."""
    return [{'path': f.path, 'old_path': f.old_path, 'status': f.status, 'added': f.added,
             'deleted': f.deleted, 'binary': f.binary, 'lines': f.lines,
             'marks': {'added': [list(r) for h in f.hunks for r in h.added],
                       'removed_at': [r for h in f.hunks for r in h.removed_at]}} for f in change.files]


def patch_entry(change, first_shot):
    """One patch of the plan: its commit and files (what the side bar shows while
    it plays), its size, and from which shot it plays. The develop planner adds
    its budget: `lines` performed, the `budget`, the `natural` and the `perf`
    (played) length of its edit shots, in seconds. With --duration, condense
    adds its content `target` (seconds), the `hunks` it chose from, the hunks
    `kept` ({path: [{'index', 'score', 'why'}]}, in story order) and the files
    `left_out` (not visited, the most important first)."""
    return {'commit': _commit_info(change), 'files': file_entries(change), 'added': change.added,
            'deleted': change.deleted, 'lines': None, 'budget': None, 'natural': None, 'perf': None,
            'first_shot': first_shot, 'target': None, 'hunks': None, 'kept': None, 'left_out': []}


def _tour_body(change, verbose, patch, voice=None, keep=None, xref_cap=None):
    """The tour of one change: per file an `enter` (resolved later) and its hunk
    shots, the commit sentences placed, the xref visits inserted after their
    hunks. Shot indices in the body are its own, from 0. With `keep`
    ({path: hunk indices}, --duration) only those hunks are shown and only
    their files visited; `xref_cap` holds the visits to that many."""
    shots = []
    hunk_shots = 0
    hunks = []   # (index into shots of the hunk shot, file, candidates)
    readings = []   # (index into shots, file, hunk, top, visible added lines)
    for f in change.files:
        if f.text is None:
            continue   # deleted or binary: listed in the side bar, not visited
        if keep is not None and f.path not in keep:
            continue   # condensed away: lit on the map, listed on the closing card
        shots.append(shot('map', 'enter', 0, path=f.path, patch=patch))   # resolved once the xrefs are known
        last_top = 0   # where the view is when the scroll to the next hunk starts
        for i, h in enumerate(f.hunks):
            if keep is not None and i not in keep[f.path]:
                continue
            if hunk_shots >= MAX_HUNK_SHOTS and i > 0:
                break
            hunk_shots += 1
            focus = h.added[0][0] if h.added else max(1, h.removed_at[0] if h.removed_at else h.new_start)
            top = scroll_top(focus, f.lines)
            segments = morph_segments(f, h)
            if segments:   # raise the window so the hunk fits both before and after
                need = h.new_start - 1 + max(h.old_count, h.new_count) - PLAN_ROWS
                top = max(0, top, min(need, focus - 2))
            visible = [n for a, b in h.added for n in range(a, b + 1) if top < n <= top + PLAN_ROWS]
            shown = []
            for n in visible:
                if shown and shown[-1][1] == n - 1:
                    shown[-1][1] = n
                else:
                    shown.append([n, n])
            first = min([a for a, _ in h.added] + [s[0] + 1 for s in h.segments] or [focus])
            last = max([b for _, b in h.added] + [s[0] for s in h.segments] or [focus])
            span = [max(first, top + 1), max(min(last, top + PLAN_ROWS), first - 1)]
            far = abs(top - last_top) > FAR_ROWS
            last_top = top
            scroll = HUNK_SCROLL_FAR if far else HUNK_SCROLL
            morph = {'at': round(scroll + MORPH_HOLD, 3), 'dur': MORPH_DUR, 'segments': segments} \
                if segments else None
            shots.append(shot('editor', 'hunk', hunk_dwell(len(visible), morph=bool(morph), far=far), path=f.path,
                              index=i, top=top, added=shown, removed_at=list(h.removed_at), focus_line=focus,
                              focus=span, scroll='far' if far else 'near', morph=morph, patch=patch))
            readings.append((len(shots) - 1, f, h, top, len(visible)))
            pickable = [n for n in visible if n <= top + XREF_ROWS]
            hunks.append((len(shots) - 1, f, top, _hunk_candidates(change, f, top, pickable)))
    _place_callouts(change, shots, readings, voice)

    # Xref visits, within the --verbose budget, inserted after their hunk.
    total, per_file, per_hunk = _budget(verbose)
    picks = {}   # hunk shot index -> [(candidate, definition)]
    seen = set()
    if verbose == 0:
        best = [(r[0], k, r) for k, (_, _, _, cands) in enumerate(hunks) for r in cands[:1]]
        if best:
            _, k, (_, c, d) = min(best, key=lambda b: (b[0], b[1]))
            picks[hunks[k][0]] = [(c, d)]
    else:
        file_used = {}
        for si, f, top, cands in hunks:
            for _, c, d in cands:
                if total <= 0 or file_used.get(f.path, 0) >= per_file or len(picks.get(si, ())) >= per_hunk:
                    break
                if c.name in seen:
                    continue
                seen.add(c.name)
                picks.setdefault(si, []).append((c, d))
                file_used[f.path] = file_used.get(f.path, 0) + 1
                total -= 1
    if xref_cap is not None:   # the first few, in story order
        for si in sorted(picks):
            picks[si] = picks[si][:max(0, xref_cap)]
            xref_cap -= len(picks[si])
    tops = {si: top for si, _, top, _ in hunks}
    out = []
    for si, s in enumerate(shots):
        out.append(s)
        for c, d in picks.get(si, ()):
            lines = change.text(d.path).count('\n') + 1
            out.append(shot('editor', 'xref-push', XREF_DUR, path=s['path'], line=c.line, col=c.col, name=c.name,
                            definition={'path': d.path, 'line': d.line, 'kind': d.kind, 'col': d.col},
                            def_top=scroll_top(d.line, lines), patch=patch))
            _stretch(out[-1], voice, XREF_CLICK)
            out.append(shot('editor', 'xref-pop', POP_DUR, path=s['path'], back_top=tops[si], line=c.line,
                            col=c.col, name=c.name, patch=patch))
    return out


def _stretch(s, voice, anchor):
    """With a voice: shot `s` long enough for its phrases from `anchor` on."""
    if voice is not None:
        s['dur'] = round(max(s['dur'], voice.fit(shot_key(s), anchor)[1]), 3)
    return s


def _resolve_enters(shots, tiled, voice=None):
    """Each `enter` becomes the way into its file: a dive from the map for a
    patch's first file, a travel over the map from the file before, or `C-x
    C-f` for a file already seen in the patch (a definition visit opened it) or
    not on the map."""
    out = []
    current, opened, patch = None, set(), None   # the file on screen; files a definition visit has opened
    for s in shots:
        if s.get('patch') != patch:
            current, opened, patch = None, set(), s.get('patch')
        if s['kind'] == 'enter':
            path = s['path']
            if path in opened or path not in tiled:   # already seen (or not on the map): open it the editor's way
                *_, dur = open_timing(path)
                s = shot('editor', 'open', dur, path=path, keys=f'C-x C-f {path}', patch=s['patch'])
            elif current is None:
                s = shot('map', 'dive', DIVE_DUR, path=path, patch=s['patch'])
            else:
                s = shot('map', 'travel', TRAVEL_DUR, path=current, to=path, patch=s['patch'])
            current = path
            _stretch(s, voice, 0.0)
        elif s['kind'] == 'xref-push':
            opened.add(s['definition']['path'])
        out.append(s)
    return out


def _performed(change, f):
    """Whether develop mode types `f`: a text file with hunks, whose old text
    is known (empty when it is new)."""
    return (not f.binary and bool(f.hunks) and (f.text is not None or f.status == 'D')
            and change.old_text(f.path) is not None)


def order_files(change):
    """The files develop mode performs, top to bottom: a file whose change
    writes a definition (its line, or a line of its body) that another file's
    added lines use goes before that file (Kahn's algorithm, diff order
    breaking ties; a cycle leaves the rest in diff order), test files last in
    diff order."""
    files = [f for f in change.files if _performed(change, f)]
    src = [f for f in files if not is_test(f.path)]
    added = {f.path: {n for h in f.hunks for a, b in h.added for n in range(a, b + 1)} for f in src}
    written = {}   # path -> definition lines its change writes
    for f in src:
        if f.text is None:
            continue
        holders = xref.enclosures(f.text.split('\n'), xref.file_defs(change.repo, change.target, f.path))
        written[f.path] = {holders[n - 1][1].line for n in added[f.path] if 0 < n <= len(holders) and holders[n - 1]}
    after = {f.path: set() for f in src}   # path -> paths that use what it defines
    for g in src:
        lang = xref.lang_of(g.path)
        if g.text is None or lang is None or not added[g.path]:
            continue
        index = xref.index(change.repo, change.target, lang)
        for c in xref.candidates(g.text, g.path, sorted(added[g.path])):
            d = xref.resolve(c, index, g.path, g.text)
            if d is not None and d.path != g.path and d.line in written.get(d.path, ()):
                after[d.path].add(g.path)
    need = {f.path: 0 for f in src}
    for a, bs in after.items():
        for b in bs:
            need[b] += 1
    out, left = [], list(src)
    while left:
        ready = [f for f in left if need[f.path] == 0]
        f = ready[0] if ready else left[0]   # a cycle: diff order for what is left
        if not ready:
            out += left
            break
        out.append(f)
        left.remove(f)
        for b in after[f.path]:
            need[b] -= 1
    return out + [f for f in files if is_test(f.path)]


def develop_budget(lines):
    """Seconds the performance of `lines` changed lines aims at."""
    return max(PERF_FLOOR, lines * SECONDS_PER_LINE)


def _busiest(steps, length, lo, hi):
    """Where a window `length` natural seconds long, inside [lo, hi], covers
    the most typing (keys, and an elided run's characters): where a shortened
    speed-up saves the most, leaving the rest to be read."""
    keys = sorted(st['at'] + x for st in steps if st['kind'] == 'type' for x in st['times'])
    keys += [x for st in steps if st['kind'] == 'fill' for x in [st['at']] * len(st['text'])]
    keys.sort()
    starts = [lo, hi - length] + [st['at'] for st in steps if lo <= st['at'] <= hi - length]
    return max(starts, key=lambda a: (bisect.bisect_right(keys, a + length) - bisect.bisect_left(keys, a), -a))


def _pace(scripts, budget, verbose, most=FAST_PEAK_MAX):
    """(s0, peak, {file index: (natural start, natural end)}): the speed every
    script plays at and, when that is not enough for the budget, the speed-up
    sequences and their peak, chosen so the performance meets the budget: the
    peak rises from FAST_PEAK_MIN to FAST_PEAK_MAX over whole files' spans;
    below FAST_PEAK_MIN the spans shorten instead (a ×5 badge would hardly
    read as a fast-forward next to typing already at NATURAL_MAX). `most`
    is the highest peak allowed."""
    natural = sum(sc['natural'] for sc in scripts)
    if verbose >= 3 or natural <= 0:
        return 1.0, None, {}
    s0 = min(NATURAL_MAX, max(1.0, natural / budget))
    if natural / s0 <= budget + 1e-6:
        return s0, None, {}
    r = FAST_RAMP
    avail = {}
    for k, sc in enumerate(scripts):
        end = sc['steps'][-1]['at'] + sc['steps'][-1]['dur'] if sc['steps'] else 0.0
        if end - FAST_LEAD >= FAST_MIN:
            avail[k] = (FAST_LEAD, end)
    while avail:
        F = len(avail)
        R = sum(b - a for a, b in avail.values())
        room = budget - (natural - R) / s0 - F * r
        peak = (R - F * r * s0) / room if room > 0 else most
        if peak >= FAST_PEAK_MIN:
            peak = min(most, peak)
            spans = avail
        else:   # the whole spans at the least peak would undershoot: run shorter ones, where most is typed
            peak = FAST_PEAK_MIN
            want = (natural / s0 + F * r - F * r * s0 / peak - budget) / (1 / s0 - 1 / peak)
            spans = {}
            for k, (a, b) in avail.items():
                at = _busiest(scripts[k]['steps'], (b - a) * want / R, a, b)
                spans[k] = (at, at + (b - a) * want / R)
        short = [k for k, (a, b) in spans.items() if not edits.ramp_fits(b - a, s0, peak, r)]
        if not short:
            return s0, peak, spans
        if spans is avail:   # whole spans: the longest few, at a peak they all have room to ramp to
            return _longest_spans(avail, natural, budget, s0, r, most)
        avail = {k: v for k, v in avail.items() if k not in short}
    return s0, None, {}


def _longest_spans(avail, natural, budget, s0, r, most=FAST_PEAK_MAX):
    """When not every file's span has room to ramp to the peak the budget
    needs: of the files with the m longest spans, for each m, the peak that
    meets the budget held to what the shortest of them can ramp to; the
    choice that plays shortest. (Dropping every file short at the needed peak
    and solving again raises the peak, so more drop, until few are left.)"""
    order = sorted(avail, key=lambda k: (-(avail[k][1] - avail[k][0]), k))
    best = None
    for m in range(1, len(order) + 1):
        ks = order[:m]
        F, R = m, sum(avail[k][1] - avail[k][0] for k in ks)
        room = budget - (natural - R) / s0 - F * r
        need = (R - F * r * s0) / room if room > 0 else most
        fit = (min(avail[k][1] - avail[k][0] for k in ks) / r - s0) * 0.99   # some plateau left
        peak = min(most, need, fit)
        if peak < FAST_PEAK_MIN:
            continue
        perf = (natural - R) / s0 + F * r + (R - F * r * s0) / peak
        if best is None or perf < best[0] - 1e-9:
            best = (perf, peak, {k: avail[k] for k in ks})
    if best is None:
        return s0, None, {}
    return s0, best[1], best[2]


def _clock(sc, s0, fast):
    """A script's clock at speed s0 with the speed-up `fast`, each fill held on
    screen for FILL_SHOWN, each applied run (--duration) for APPLY_SHOWN:
    (clock, duration, fast span, seconds the holds add)."""
    clk, dur, span = edits.clock(sc['natural'], s0, fast, FAST_RAMP)
    added = 0.0
    for st in sc['steps']:
        if st['kind'] in ('fill', 'apply'):
            shown = FILL_SHOWN if st['kind'] == 'fill' else APPLY_SHOWN
            clk, span, dur, extra = edits.hold(clk, span, dur, st['at'], st['at'] + st['dur'],
                                               min(st['dur'], shown))
            added += extra
    return clk, dur, span, added


def _hunk_steps(steps):
    """{hunk: (first step, last step)} of an edit script, in the order it reaches them."""
    out = {}
    for k, st in enumerate(steps):
        if st['kind'] == 'apply':
            continue   # applied, not performed
        a, _ = out.get(st['hunk'], (k, k))
        out[st['hunk']] = (a, k)
    return out


def _voice_holds(sc, clock, voice, key):
    """An edit script's clock with each narrated hunk played for at least its
    phrases before the next hunk starts: first its own steps held to play no
    faster than natural pace (`edits.hold`), then, if the phrase is longer,
    the beat after it held while point rests and the sentence finishes."""
    clk, dur, span, added = clock
    steps = sc['steps']
    hunks = list(_hunk_steps(steps).items())
    for n, (hi, (i0, i1)) in enumerate(hunks):
        need = voice.fit(key(hi), 0.0)[1]
        if not need:
            continue
        need += 2 / FPS   # the shot's start and its length each round to a frame
        a, b = steps[i0]['at'], steps[i1]['at'] + steps[i1]['dur']
        nxt = steps[hunks[n + 1][1][0]]['at'] if n + 1 < len(hunks) else sc['natural']
        clk, span, dur, _ = edits.hold(clk, span, dur, a, b, min(need, b - a))
        lo = min(b, nxt - 0.05)
        least = need - (edits.shot_time(lo, clk) - edits.shot_time(a, clk))
        if edits.shot_time(nxt, clk) - edits.shot_time(a, clk) < need - 1e-6:
            clk, span, dur, _ = edits.hold(clk, span, dur, lo, nxt, least)
    return clk, dur, span, added


def _develop_body(change, verbose, patch, voice=None, keep=None, budget=None, duration=False):
    """Develop mode for one change: per file (in `order_files` order) an
    `enter` and an `edit` shot playing its edit script, paced to the budget.
    Returns (shots, patch fields, {id(edit shot): {new line: time on screen it
    is written}}). With --duration (`duration` true): only the files in `keep`
    ({path: hunk indices}) are performed, their other hunks applied in a step
    each run of them; `budget` is what the performance aims at, typing at
    most at --verbose 2's pace, and the voice's holds come out of it."""
    files = order_files(change)
    if keep is not None:
        files = [f for f in files if f.path in keep]
    elide = ELIDE_LINES.get(verbose)
    scripts = []
    for f in files:
        old = edits.split_lines(change.old_text(f.path) or '')
        new = edits.split_lines(f.text or '')
        scripts.append(edits.build(old, new, f.hunks, elide=elide, seed=f.path,
                                   shown=keep[f.path] if keep is not None else None))
    lines = sum(edits.weight(sc['steps']) for sc in scripts)
    budget = develop_budget(lines) if budget is None else budget
    pace = min(verbose, 2) if duration else verbose
    hold = (lambda sc, c, f: _voice_holds(sc, c, voice, lambda hi: ('edit', patch, f.path, hi))) \
        if voice is not None else None
    target = budget   # what the speed-ups aim at: the budget less the time the folds (and the voice) hold
    for _ in range(12):
        s0, peak, spans = _pace(scripts, target, pace, FAST_PEAK_CONDENSED if duration else FAST_PEAK_MAX)
        clocks = [_clock(sc, s0, (*spans[fi], peak) if fi in spans else None) for fi, sc in enumerate(scripts)]
        held = sum(c[3] for c in clocks)
        if duration and hold is not None:   # narrated hunks take their time from the rest
            voiced = [hold(sc, c, f) for f, sc, c in zip(files, scripts, clocks)]
            held += sum(v[1] - c[1] for v, c in zip(voiced, clocks))
        if abs(budget - held - target) < 1e-4 or budget - held <= 1.0:
            break
        target = budget - held
    if hold is not None:
        clocks = [hold(sc, c, f) for f, sc, c in zip(files, scripts, clocks)]
    chosen = _match_callouts(change, [(f, h) for f in files if f.text is not None for h in f.hunks])
    keys = [(fi, hi) for fi, f in enumerate(files) if f.text is not None for hi in range(len(f.hunks))]
    shots, written, done = [], {}, 0
    for fi, (f, sc) in enumerate(zip(files, scripts)):
        clk, dur, span, _ = clocks[fi]
        on_screen = lambda x, clk=clk: round(edits.shot_time(x, clk), 4)
        steps = sc['steps']
        elisions = [{'line': st['line'] + 1, 'count': st['count'], 'at': on_screen(st['at']),
                     'until': on_screen(st['at'] + st['dur'])} for st in steps if st['kind'] == 'fill']
        callouts = []
        for k, (kf, hi) in enumerate(keys):
            span_h = sc['hunks'][hi] if kf == fi and hi < len(sc['hunks']) else None
            if kf != fi or not span_h or k not in chosen:
                continue
            if keep is not None and hi not in keep[f.path]:
                continue   # applied, not typed: nothing to say over it
            a, b = steps[span_h[0]], steps[span_h[1]]
            t0, t1 = on_screen(a['at']), on_screen(b['at'] + b['dur'])
            picks = _picks(chosen[k])
            # up while its hunk is typed, and no longer: the leader follows the cursor
            slot = min(CALLOUT_EACH, (t1 - t0) / len(picks))
            said = {}
            if voice is not None:   # each card up while its sentence is said, the last one till it ends
                key = ('edit', patch, f.path, hi)
                said = dict(zip(voice.slots(key), voice.fit(key, t0)[0]))
            for n, (_, _, _, text, c) in enumerate(picks):
                end = t1 if n == len(picks) - 1 else t0 + (n + 1) * slot - 0.2
                at = t0 + n * slot
                if said:
                    at = said[n][0] if n in said else at
                    if n == len(picks) - 1:
                        end = max(t1, max(z for _, z in said.values()))
                    elif n + 1 in said:
                        end = said[n + 1][0] - 0.2
                callouts.append({'text': text, 'commit': c.short, 'hunk': hi, 'at': round(at, 3),
                                 'until': round(end if said else min(end, t1), 3)})
        callouts.sort(key=lambda c: c['at'])
        for c, nxt in zip(callouts, callouts[1:] + [None]):
            c['until'] = round(min(c['until'], dur - 0.2, nxt['at'] - 0.05 if nxt else dur), 3)
        callouts = [c for c in callouts if c['until'] - c['at'] >= DEVELOP_CALLOUT_MIN]
        weight = edits.weight(steps)
        shots.append(shot('map', 'enter', 0, path=f.path, patch=patch))
        e = shot('editor', 'edit', dur, path=f.path, status=f.status, patch=patch, steps=steps,
                 natural=sc['natural'], clock=clk, fast=[span] if span else [], elisions=elisions,
                 callouts=callouts, done_before=done, lines=weight, total_lines=lines)
        shots.append(e)
        written[id(e)] = {j: on_screen(x) for j, x in sc['written'].items()}
        done += weight
    perf = round(sum(s['dur'] for s in shots), 3)
    return shots, {'lines': lines, 'budget': round(budget, 3), 'natural': round(sum(sc['natural'] for sc in scripts), 3),
                   'perf': perf, 'speed': s0, 'peak': peak}, written


def build(change, verbose=0, mode='tour', patches=None, voice=None, duration=None):
    """The Plan of `change` in `mode`: a tour, or with `patches` (the change's
    commits as changes of their own, oldest first) one tour per patch. With a
    `voice` (voice.Timed: the narration's phrases and their lengths), every
    shot lasts at least as long as what is said over it. With a `duration`
    (seconds), the plan is condensed to about that long (condense)."""
    patches = patches or [change]
    files = file_entries(change)
    name = repo_name(change.repo)
    tree_map = repomap.tiles(change.tree, files, root=name)
    tiled = {t['path'] for g in tree_map['groups'] for t in g['tiles'] if 'path' in t}
    shots = [_stretch(shot('card', 'title', TITLE_DUR, patch=0), voice, VOICE_TITLE_AT)]
    lit = [f['path'] for f in files if f['path'] in tiled]   # the changed files' tiles light in tour order
    if lit:   # nothing changed on the map (only deletions): nothing to show there
        shots.append(_stretch(shot('map', 'map', MAP_DUR, lit=lit, patch=0), voice, VOICE_MAP_AT))
    entries, written = [], {}
    if duration is not None:
        from . import condense
        budgets = condense.split_budget(duration, patches, bool(lit))
        xrefs = condense.xref_cap(duration)
    for k, ch in enumerate(patches):
        entries.append(patch_entry(ch, len(shots)))   # from its card, after the first
        if k:
            shots.append(_stretch(shot('card', 'patch', INTER_DUR, patch=k), voice, VOICE_PATCH_AT))
        if duration is not None:
            body, fields, w, used = condense.body(ch, verbose, mode, k, voice, budgets[k], xrefs)
            entries[-1].update(fields)
            written.update(w)
            xrefs = None if xrefs is None else xrefs - used
        elif mode == 'develop':
            body, fields, w = _develop_body(ch, verbose, k, voice)
            entries[-1].update(fields)
            written.update(w)
        else:
            body = _tour_body(ch, verbose, k, voice)
        shots += body
    shots.append(_stretch(shot('card', 'end', END_DUR, patch=len(patches) - 1), voice, VOICE_END_AT))
    out = _resolve_enters(shots, tiled, voice)   # one shot per shot: each patch's first_shot still holds
    if duration is not None:
        condense.resize(out, duration, voice)
    nxt = {'short': change.next.short, 'subject': change.next.subject} if change.next else None
    def make(ch, sub, k=0):
        if mode == 'develop':
            return graphmod.build_develop(ch, sub, {i: written[id(s)] for i, s in enumerate(sub) if id(s) in written})
        return graphmod.build(ch, sub)

    # With --series each patch has its graph, as it has its commit in the side bar.
    call_graph = graphmod.by_patch(patches, out, make) if len(patches) > 1 else make(change, out)
    return Plan(VERSION, FPS, change.base, change.target, change.label, change.is_range, _commit_info(change),
                files, verbose, out, round(sum(s['dur'] for s in out), 3), repo=name, next=nxt,
                map=tree_map, graph=call_graph, mode=mode, patches=entries, duration=duration)


def plan(change, verbose=0):
    """The tour of `change` as a Plan."""
    return build(change, verbose)


def frame_starts(shots, fps):
    """(first frame of each shot, frames in all): each shot starts on the frame
    its start in seconds rounds to."""
    starts, acc = [], 0.0
    for s in shots:
        starts.append(round(acc * fps))
        acc += s['dur']
    return starts, max(1, round(acc * fps))


def shot_key(s):
    """What a narration phrase is tied to in shot `s` (the same in any plan of
    the same change, however long its shots): ('title',), ('map',), ('end',),
    ('patch', k), ('enter', k, path) for the way into a file, ('hunk', k, path, index),
    ('xref', k, path, line, col, name); None for a shot nothing is said over.
    An edit shot's hunks have keys of their own, ('edit', k, path, hunk)."""
    kind, k = s['kind'], s.get('patch', 0)
    if kind in ('title', 'end', 'map'):
        return (kind,)
    if kind == 'patch':
        return ('patch', k)
    if kind in ('dive', 'travel', 'open', 'enter'):
        return ('enter', k, s.get('to', s['path']))
    if kind == 'hunk':
        return ('hunk', k, s['path'], s['index'])
    if kind == 'xref-push':
        return ('xref', k, s['path'], s['line'], s['col'], s['name'])
    return None


def perf_time(p, k=0):
    """Seconds the edit shots of patch k play for."""
    return sum(s['dur'] for s in p.shots if s['kind'] == 'edit' and s.get('patch', 0) == k)

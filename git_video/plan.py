"""The shot plan: what the tour shows, in order, and for how long.

A plan is plain JSON. Each shot names a `scene` (the actor on stage: `card`
or `editor` this round), a `kind` within it and a `dur` in seconds, plus the
fields its kind needs. The planner here is deterministic heuristics; the JSON
is the seam a later pass (an LLM, say) can rewrite before the film is built.
"""
import json
import os
import re
from dataclasses import asdict, dataclass, field

from videobuilder.type.typewriter import schedule

from . import callouts as callouts_mod, highlight, repomap, worddiff, xref

VERSION = 1
FPS = 30
PLAN_ROWS = 26            # rows the planner assumes are on screen: the fewest any format shows (16x9)
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

    def to_json(self):
        return json.dumps(asdict(self), indent=2, sort_keys=True, ensure_ascii=False)

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
            'commits': [{'short': c.short, 'subject': c.subject, 'author': c.author, 'date': c.date}
                        for c in change.commits]}
    if change.is_range:
        a, b = change.label.split('-', 1)
        n = len(change.commits)
        info['subject'] = f'{a}..{b}: {n} commit{"s" if n != 1 else ""}'
        info['body'] = ''
        info['coauthors'] = list(dict.fromkeys(n for c in change.commits for n in c.coauthors))
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
        d = xref.resolve(c, defs, f.path)
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
    defs = set()
    for n in changed + [s[0] for s in h.segments]:
        e = xref.enclosing(lines, fd, n)
        if e:
            defs.add(e[0])
    return {'path': f.path, 'words': words, 'defs': defs}


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


def _place_callouts(change, shots, readings):
    """Give each hunk shot the commit sentences that explain it (`callouts`),
    each with its time on screen and the words its line points at, and stretch
    the hunk's read to fit them. A range uses each hunk's own commit."""
    infos = [hunk_info(change, f, h) for _, f, h, _, _ in readings]
    names = {d for info in infos for d in info['defs']}
    chosen = {}   # reading index -> [(unit index, score, word, text, commit)]
    for c in change.commits:
        units = callouts_mod.sentences(c.body) if c.body else []
        mine = [k for k, (_, _, h, _, _) in enumerate(readings) if h.commit == c.sha]
        if not units or not mine:
            continue
        for u, j, word, sc in callouts_mod.match(units, [infos[k] for k in mine], names):
            chosen.setdefault(mine[j], []).append((u, sc, word, units[u], c))
    for k, (si, f, h, top, visible) in enumerate(readings):
        s = shots[si]
        picks = sorted(chosen.get(k, []), key=lambda x: (-x[1], x[0]))[:CALLOUT_MAX_PER_HUNK]
        picks.sort(key=lambda x: x[0])
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


def plan(change, verbose=0):
    """The tour of `change` as a Plan."""
    files = [{'path': f.path, 'old_path': f.old_path, 'status': f.status, 'added': f.added,
              'deleted': f.deleted, 'binary': f.binary, 'lines': f.lines,
              'marks': {'added': [list(r) for h in f.hunks for r in h.added],
                        'removed_at': [r for h in f.hunks for r in h.removed_at]}} for f in change.files]
    name = repo_name(change.repo)
    tree_map = repomap.tiles(change.tree, files, root=name)
    tiled = {t['path'] for g in tree_map['groups'] for t in g['tiles'] if 'path' in t}
    shots = [shot('card', 'title', TITLE_DUR)]
    lit = [f['path'] for f in files if f['path'] in tiled]   # the changed files' tiles light in tour order
    if lit:   # nothing changed on the map (only deletions): nothing to show there
        shots.append(shot('map', 'map', MAP_DUR, lit=lit))
    hunk_shots = 0
    hunks = []   # (index into shots of the hunk shot, file, candidates)
    readings = []   # (index into shots, file, hunk, top, visible added lines)
    for f in change.files:
        if f.text is None:
            continue   # deleted or binary: listed in the side bar, not visited
        shots.append(shot('map', 'enter', 0, path=f.path))   # resolved below, once the xrefs are known
        last_top = 0   # where the view is when the scroll to the next hunk starts
        for i, h in enumerate(f.hunks):
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
                              focus=span, scroll='far' if far else 'near', morph=morph))
            readings.append((len(shots) - 1, f, h, top, len(visible)))
            pickable = [n for n in visible if n <= top + XREF_ROWS]
            hunks.append((len(shots) - 1, f, top, _hunk_candidates(change, f, top, pickable)))
    shots.append(shot('card', 'end', END_DUR))
    _place_callouts(change, shots, readings)

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
    tops = {si: top for si, _, top, _ in hunks}
    out = []
    current, opened = None, set()   # the file on screen; files a definition visit has opened
    for si, s in enumerate(shots):
        if s['kind'] == 'enter':
            path = s['path']
            if path in opened or path not in tiled:   # already seen (or not on the map): open it the editor's way
                *_, dur = open_timing(path)
                s = shot('editor', 'open', dur, path=path, keys=f'C-x C-f {path}')
            elif current is None:
                s = shot('map', 'dive', DIVE_DUR, path=path)
            else:
                s = shot('map', 'travel', TRAVEL_DUR, path=current, to=path)
            current = path
        out.append(s)
        for c, d in picks.get(si, ()):
            lines = change.text(d.path).count('\n') + 1
            out.append(shot('editor', 'xref-push', XREF_DUR, path=s['path'], line=c.line, col=c.col, name=c.name,
                            definition={'path': d.path, 'line': d.line, 'kind': d.kind, 'col': d.col},
                            def_top=scroll_top(d.line, lines)))
            out.append(shot('editor', 'xref-pop', POP_DUR, path=s['path'], back_top=tops[si], line=c.line,
                            col=c.col, name=c.name))
            opened.add(d.path)

    nxt = {'short': change.next.short, 'subject': change.next.subject} if change.next else None
    return Plan(VERSION, FPS, change.base, change.target, change.label, change.is_range, _commit_info(change),
                files, verbose, out, round(sum(s['dur'] for s in out), 3), repo=name, next=nxt,
                map=tree_map)

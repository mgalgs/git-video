"""The shot plan: what the tour shows, in order, and for how long.

A plan is plain JSON. Each shot names a `scene` (the actor on stage: `card`
or `editor` this round), a `kind` within it and a `dur` in seconds, plus the
fields its kind needs. The planner here is deterministic heuristics; the JSON
is the seam a later pass (an LLM, say) can rewrite before the film is built.
"""
import json
import os
from dataclasses import asdict, dataclass, field

from videobuilder.type.typewriter import schedule

from . import xref

VERSION = 1
FPS = 30
PLAN_ROWS = 26            # rows the planner assumes are on screen: the fewest any format shows (16x9)
# The film re-scrolls to keep the focus line off the window's last row
# (film.fit_top), so an xref is only picked from the rows above it.
XREF_ROWS = PLAN_ROWS - 1
# The film never scrolls sideways, so an xref is only picked from the columns
# every format shows: the fewest is 9x16 with a six-digit gutter (default font).
PLAN_COLS = 64
MAX_HUNK_SHOTS = 60
XREF_CAP_PER_HUNK = 3     # --verbose=3
XREF_CAP_TOTAL = 24       # --verbose=3

TITLE_DUR = 3.0
END_DUR = 2.5

# Shot timings, shared by the film (what is seen) and the sounds (what is heard).
PER_CHAR = 0.055          # mean seconds per typed character
OPEN_KEYS = (0.08, 0.22)  # C-x, C-f
OPEN_PROMPT = 0.35        # "Find file: " appears
OPEN_TYPE = 0.45          # the first letter of the path
OPEN_RET_GAP = 0.15       # last letter -> RET
OPEN_HOLD = 0.6           # the new buffer on screen before the next shot

HUNK_SCROLL = 0.6         # the scroll to the hunk; the sweep starts as it lands
HUNK_SWEEP = 0.5
HUNK_READ_BASE, HUNK_READ_PER_LINE, HUNK_READ_MAX = 1.5, 0.08, 6.0

XREF_TRAVEL = 0.7         # the pointer glides to the word
XREF_CLICK = 0.7          # ... and clicks it
XREF_RIPPLE = 0.35
XREF_KEYS = (1.1, 1.2)    # M, .
XREF_SWITCH = 1.4         # the definition's buffer shows
XREF_SWEEP = 0.5
XREF_DUR = 4.4
POP_KEYS = (0.08, 0.18)   # M, ,
POP_SWITCH = 0.35
POP_DUR = 1.3


def open_timing(path):
    """(key times, typed-letter times, RET time, duration) of an `open` shot."""
    ts, span = schedule(path, path, PER_CHAR)
    letters = [OPEN_TYPE + t for t in ts]
    ret = OPEN_TYPE + span + OPEN_RET_GAP
    return OPEN_KEYS, letters, ret, max(1.6, round(ret + OPEN_HOLD, 3))


def hunk_dwell(visible_added):
    return HUNK_SCROLL + min(HUNK_READ_MAX, HUNK_READ_BASE + HUNK_READ_PER_LINE * visible_added)


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


def plan(change, verbose=0):
    """The tour of `change` as a Plan."""
    shots = [shot('card', 'title', TITLE_DUR)]
    hunk_shots = 0
    hunks = []   # (index into shots of the hunk shot, file, candidates)
    for f in change.files:
        if f.text is None:
            continue   # deleted or binary: listed in the side bar, not visited
        *_, dur = open_timing(f.path)
        shots.append(shot('editor', 'open', dur, path=f.path, keys=f'C-x C-f {f.path}'))
        for i, h in enumerate(f.hunks):
            if hunk_shots >= MAX_HUNK_SHOTS and i > 0:
                break
            hunk_shots += 1
            focus = h.added[0][0] if h.added else max(1, h.removed_at[0] if h.removed_at else h.new_start)
            top = scroll_top(focus, f.lines)
            visible = [n for a, b in h.added for n in range(a, b + 1) if top < n <= top + PLAN_ROWS]
            shown = []
            for n in visible:
                if shown and shown[-1][1] == n - 1:
                    shown[-1][1] = n
                else:
                    shown.append([n, n])
            shots.append(shot('editor', 'hunk', hunk_dwell(len(visible)), path=f.path, index=i, top=top,
                              added=shown, removed_at=list(h.removed_at), focus_line=focus))
            pickable = [n for n in visible if n <= top + XREF_ROWS]
            hunks.append((len(shots) - 1, f, top, _hunk_candidates(change, f, top, pickable)))
    shots.append(shot('card', 'end', END_DUR))

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
    for si, s in enumerate(shots):
        out.append(s)
        for c, d in picks.get(si, ()):
            lines = change.text(d.path).count('\n') + 1
            out.append(shot('editor', 'xref-push', XREF_DUR, path=s['path'], line=c.line, col=c.col, name=c.name,
                            definition={'path': d.path, 'line': d.line, 'kind': d.kind, 'col': d.col},
                            def_top=scroll_top(d.line, lines)))
            out.append(shot('editor', 'xref-pop', POP_DUR, path=s['path'], back_top=tops[si], line=c.line,
                            col=c.col, name=c.name))

    files = [{'path': f.path, 'old_path': f.old_path, 'status': f.status, 'added': f.added,
              'deleted': f.deleted, 'binary': f.binary, 'lines': f.lines} for f in change.files]
    nxt = {'short': change.next.short, 'subject': change.next.subject} if change.next else None
    return Plan(VERSION, FPS, change.base, change.target, change.label, change.is_range, _commit_info(change),
                files, verbose, out, round(sum(s['dur'] for s in out), 3), repo=repo_name(change.repo), next=nxt)

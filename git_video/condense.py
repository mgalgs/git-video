"""--duration: a video of any change set made to run about a target length.

Selection first, then speed. Every hunk is scored for importance
(`score_hunks`); the highest-scoring hunks that fit the time left after the
fixed costs (the cards, the map, at their floors) are kept, in story order
(`select`); files with nothing kept are not visited, and the closing card
lists them under "also changed". Only then is every shot sized within its
limits so the plan lands on the target (`resize`): the cards and camera
moves shrink first, as far as the kept content needs, so the hunks keep
their natural read when they fit at it.
"""
import argparse
import math
import os
import re
from dataclasses import dataclass

from . import collect, edits, plan as P, xref

# A shot's (floor, ceiling) in seconds: what it may shrink to (the film's
# fixed timings inside it still play) and grow to.
LIMITS = {'title': (2.6, 6.0), 'map': (1.8, 5.0), 'end': (2.0, 6.0), 'patch': (1.8, 4.0),
          'dive': (0.6, P.DIVE_DUR), 'travel': (0.8, P.TRAVEL_DUR), 'xref-push': (3.0, P.XREF_DUR)}
READ_MIN = 0.8            # a hunk's read, at the least ...
READ_STRETCH = 2.5        # ... and at the most, as a multiple of its natural read
CALLOUT_MIN_EACH = 1.4    # a callout's least time up when its hunk is squeezed
MORPH_MIN = (0.4, 0.8)    # MORPH_HOLD, MORPH_DUR at the least
HUNK_MIN = P.HUNK_SCROLL + READ_MIN            # the least one hunk shot costs
BEAT_CONTENT_MIN = LIMITS['dive'][0] + HUNK_MIN   # the least a --series beat's body costs
# Develop: what typing one hunk is budgeted at, natural seconds over
# NATURAL_MAX, held between these (the speed-ups take the rest).
DEV_HUNK_COST = (1.0, 6.0)
FIT_TRIES = 12            # times a beat drops its least important hunk when it will not fit

KIND_WEIGHT = {'source': 1.0, 'other': 0.6, 'doc': 0.4, 'test': 0.3}
README_BONUS = (3.0, 24.0)   # a top-level README changed, and added: the change introduces the project
FILLER_SHARE = 0.5           # a hunk is not kept in a gap left by one more than twice as important
DOC_EXT = {'.md', '.rst', '.txt', '.1', '.adoc'}


def parse_duration(text):
    """Seconds from `60`, `60s`, `90.5`, `2m` or `1m30s`; argparse's error otherwise."""
    m = re.fullmatch(r'\s*(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s?)?\s*', text or '')
    if not m or not any(m.groups()):
        raise argparse.ArgumentTypeError(
            f'invalid duration {text!r}: give seconds (60, 60s, 90.5) or minutes and seconds (2m, 1m30s)')
    mins, secs = m.groups()
    return float(mins or 0) * 60 + float(secs or 0)


def tolerance(target):
    """How far from `target` a plan may land: 10%, or 3 s under 30 s."""
    return 0.1 * target if target >= 30 else 3.0


def xref_cap(target):
    """The most xref dives a video of `target` s may make: none under 30 s, one
    under 60 s, else None (the --verbose budget)."""
    return 0 if target < 30 else 1 if target < 60 else None


# ---- fixed costs and --series beats ----
def fixed_min(beats, has_map=True):
    """The cards (and the map) at their floors for a video of `beats` patches."""
    return (LIMITS['title'][0] + (LIMITS['map'][0] if has_map else 0) + LIMITS['end'][0]
            + (beats - 1) * LIMITS['patch'][0])


def max_beats(target, has_map=True):
    """The most --series beats that each get a card and a hunk at `target` s."""
    room = target - fixed_min(1, has_map) - BEAT_CONTENT_MIN
    return max(1, 1 + int(room // (LIMITS['patch'][0] + BEAT_CONTENT_MIN)))


def size_of(change):
    return change.added + change.deleted


def fit_series(change, patches, target, mode='tour', verbose=0):
    """--series patches made few enough for a plan of `target` s to fit
    (`merge_series`): from `max_beats` beats, one fewer each time the plan
    built from them is still too long (a beat's least cost depends on what
    it shows: several files, callouts to read, a typed file)."""
    most = min(len(patches), max_beats(target))
    while True:
        beats = merge_series(patches, most)
        if len(beats) <= 1 or len(beats) > most:   # one beat, or nothing more can merge
            return beats
        p = P.build(change, verbose, mode, beats, duration=target)
        if p.total <= target + tolerance(target) / 2:
            return beats
        most = len(beats) - 1


def merge_series(patches, most):
    """--series patches as at most `most` beats: while there are more, the
    adjacent pair smallest together (the earliest on a tie) becomes one beat,
    a range change with `merged` set to the commits in it. A root commit is
    never merged (its base is no commit)."""
    beats = [[ch] for ch in patches]
    while len(beats) > most:
        pairs = [(sum(size_of(c) for c in a + b), k) for k, (a, b) in enumerate(zip(beats, beats[1:]))
                 if a[0].is_range or a[0].base != collect.empty_tree(a[0].repo)]
        if not pairs:
            break
        _, k = min(pairs)
        beats[k:k + 2] = [beats[k] + beats[k + 1]]
    out = []
    for b in beats:
        if len(b) == 1:
            out.append(b[0])
            continue
        ch = collect.collect(b[0].repo, f'{b[0].base}..{b[-1].target}')
        ch.merged = sum(max(1, c.merged) for c in b)
        out.append(ch)
    return out


def split_budget(target, patches, has_map=True):
    """Each patch's content budget, seconds: what the fixed costs at their
    floors leave of `target`, shared by the patches' sizes, at least
    BEAT_CONTENT_MIN each."""
    room = target - fixed_min(len(patches), has_map)
    sizes = [max(1, size_of(ch)) for ch in patches]
    return [max(BEAT_CONTENT_MIN, room * s / sum(sizes)) for s in sizes]


# ---- importance ----
@dataclass
class Score:
    value: float
    why: str


def kind_of(path):
    """'test', 'doc', 'source' (a language xref knows) or 'other'."""
    if collect.is_test(path):
        return 'test'
    parts = path.split('/')
    if os.path.splitext(path)[1].lower() in DOC_EXT or parts[0] in ('docs', 'doc', 'man'):
        return 'doc'
    return 'source' if xref.lang_of(path) else 'other'


def _is_readme(path):
    return '/' not in path and path.lower().startswith('readme')


def _hunk_size(h):
    return sum(b - a + 1 for a, b in h.added) + sum(len(old) for _, old, _ in h.segments)


def score_hunks(change):
    """{(path, hunk index): Score} for every hunk of `change`: the size of the
    change, new definitions, definitions other changed code calls (through
    xref's index, as order_files finds them), commit sentences that name it
    (the callout matcher), new files over edits, source over tests, docs and
    fixtures, and a top-level README changed (more when added: it says what
    the project is). The callers count on a log scale: a module the rest of
    the change leans on stands out from one called once. Computed once per
    change."""
    cached = getattr(change, '_scores', None)
    if cached is not None:
        return cached
    hunks = [(f, i, h) for f in change.files for i, h in enumerate(f.hunks)]
    texts = [(f, i, h) for f, i, h in hunks if f.text is not None]
    defs = {}    # (path, index) -> [(name, Definition, new)]
    owner = {}   # (path, definition line) -> (path, index) whose changed lines it holds
    for f, i, h in texts:
        defs[f.path, i] = P.hunk_defs(change, f, h)
        for name, d, _ in defs[f.path, i]:
            owner.setdefault((f.path, d.line), (f.path, i))
    names = {name for ds in defs.values() for name, _, _ in ds}
    called = {}
    for f, i, h in texts:
        lang = xref.lang_of(f.path)
        lines = sorted(n for a, b in h.added for n in range(a, b + 1))
        if lang is None or not lines:
            continue
        index = xref.index(change.repo, change.target, lang)
        seen = set()
        for c in xref.candidates(f.text, f.path, lines):
            if c.name not in names:
                continue
            d = xref.resolve(c, index, f.path, f.text)
            who = owner.get((d.path, d.line)) if d is not None else None
            if who is not None and who != (f.path, i) and who not in seen:
                seen.add(who)
                called[who] = called.get(who, 0) + 1
    named = {}
    matched = P._match_callouts(change, [(f, h) for f, _, h in texts])
    for k, picks in matched.items():
        f, i, _ = texts[k]
        named[f.path, i] = len(picks)
    out = {}
    for f, i, h in hunks:
        kind = kind_of(f.path)
        size = _hunk_size(h)
        new = [n for n, _, is_new in defs.get((f.path, i), ()) if is_new][:3]
        ncalled = called.get((f.path, i), 0)
        nnamed = min(2, named.get((f.path, i), 0))
        readme = README_BONUS[f.status == 'A'] if _is_readme(f.path) else 0.0
        value = (KIND_WEIGHT[kind] * (1 + math.log2(1 + size)) + 2.0 * len(new) + 2.0 * math.log2(1 + ncalled)
                 + 2.0 * nnamed + (1.0 if f.status == 'A' else 0) + readme)
        why = [kind, f'{size} line{"s" if size != 1 else ""}']
        if new:
            why.append('new def ' + ', '.join(new))
        if ncalled:
            why.append(f'called from {ncalled} hunk{"s" if ncalled != 1 else ""}')
        if nnamed:
            why.append('named in the message')
        if f.status == 'A':
            why.append('new file')
        if _is_readme(f.path):
            why.append('the README')
        out[f.path, i] = Score(round(value, 2), ', '.join(why))
    change._scores = out
    return out


# ---- selection ----
def select(items, budget, first=P.DIVE_DUR, then=P.TRAVEL_DUR):
    """The ids of the `items` (dicts with 'id', 'score', 'order' (story
    order), 'cost', 'group' (the file)) kept in `budget` seconds: the highest
    scores first (story order breaking ties), each taken while it fits with
    its file's way in (`first` for the first file, `then` for each after);
    the best one always. Once one does not fit, nothing under FILLER_SHARE
    of its score fills the gap it leaves: that time goes to reading what is
    kept."""
    taken, groups, left, missed = set(), set(), budget, None
    for it in sorted(items, key=lambda x: (-x['score'], x['order'])):
        enter = 0.0 if it['group'] in groups else first if not groups else then
        if missed is not None and it['score'] < FILLER_SHARE * missed:
            break
        if taken and it['cost'] + enter > left + 1e-9:
            missed = it['score'] if missed is None else missed
            continue
        taken.add(it['id'])
        groups.add(it['group'])
        left -= it['cost'] + enter
    return taken


def _keep_of(items, ids):
    """{path: set(hunk indices)} of the kept items."""
    out = {}
    for it in items:
        if it['id'] in ids:
            out.setdefault(it['group'], set()).add(it['id'][1])
    return out


def _tour_items(change, verbose, k, scores):
    body = P._tour_body(change, verbose, k)
    hunks = [s for s in body if s['kind'] == 'hunk']
    return [{'id': (s['path'], s['index']), 'score': scores[s['path'], s['index']].value, 'order': n,
             'cost': s['dur'], 'group': s['path']} for n, s in enumerate(hunks)]


def _develop_items(change, verbose, scores):
    elide = P.ELIDE_LINES.get(verbose)
    out, n = [], 0
    for f in P.order_files(change):
        sc = edits.build(edits.split_lines(change.old_text(f.path) or ''), edits.split_lines(f.text or ''), f.hunks,
                         elide=elide, seed=f.path)
        steps = sc['steps']
        for hi, span in enumerate(sc['hunks']):
            if span is None or hi >= len(f.hunks):
                continue
            a, b = steps[span[0]], steps[span[1]]
            natural = b['at'] + b['dur'] - a['at']
            lo, hi_cost = DEV_HUNK_COST
            out.append({'id': (f.path, hi), 'score': scores[f.path, hi].value, 'order': n,
                        'cost': min(hi_cost, max(lo, natural / P.NATURAL_MAX)), 'group': f.path})
            n += 1
    return out


def _visitable(change, mode):
    if mode == 'develop':
        return [f.path for f in P.order_files(change)]
    return [f.path for f in change.files if f.text is not None]


def _enters_min(n_files):
    return 0.0 if not n_files else LIMITS['dive'][0] + (n_files - 1) * LIMITS['travel'][0]


def body(change, verbose, mode, k, voice, budget, xrefs):
    """Patch k's body condensed to `budget` seconds: (shots, patch entry
    fields, develop's written times, xref dives used). `xrefs` is how many
    dives it may make (None: the --verbose budget)."""
    scores = score_hunks(change)
    items = _tour_items(change, verbose, k, scores) if mode == 'tour' else _develop_items(change, verbose, scores)
    ids = select(items, budget)
    left = budget - sum(it['cost'] for it in items if it['id'] in ids)
    groups = {it['group'] for it in items if it['id'] in ids}
    left -= P.DIVE_DUR + max(0, len(groups) - 1) * P.TRAVEL_DUR if groups else 0
    if mode == 'tour':
        fit = max(0, int(left // (P.XREF_DUR + P.POP_DUR)))
        xrefs = fit if xrefs is None else min(xrefs, fit)
        if verbose == 0 and xrefs is not None:
            xrefs = min(xrefs, 1)
    for _ in range(FIT_TRIES):
        keep = _keep_of(items, ids)
        if mode == 'tour':
            shots = P._tour_body(change, verbose, k, voice, keep=keep, xref_cap=xrefs)
            fields, written = {}, {}
        else:
            enters = P.DIVE_DUR + (len(keep) - 1) * P.TRAVEL_DUR if keep else 0.0
            shots, fields, written = P._develop_body(change, verbose, k, voice, keep=keep,
                                                     budget=max(1.0, budget - enters), duration=True)
        files = list(dict.fromkeys(s['path'] for s in shots if s['kind'] == 'enter'))
        least = sum(bounds(s, voice)[0] for s in shots if s['kind'] != 'enter') + _enters_min(len(files))
        if least <= budget + 0.25 or len(ids) <= 1:
            break
        if mode == 'tour' and xrefs:
            xrefs -= 1   # a side trip goes before a hunk does
            continue
        worst = min((it for it in items if it['id'] in ids), key=lambda x: (x['score'], -x['order']))
        ids.discard(worst['id'])
    used = sum(1 for s in shots if s['kind'] == 'xref-push')
    visited = set(files)
    kept = {}
    for it in sorted((it for it in items if it['id'] in ids), key=lambda x: x['order']):
        sc = scores[it['id']]
        kept.setdefault(it['group'], []).append({'index': it['id'][1], 'score': sc.value, 'why': sc.why})
    best = {}
    for (path, _), sc in scores.items():
        best[path] = max(best.get(path, 0.0), sc.value)
    left = sorted((p for p in _visitable(change, mode) if p not in visited), key=lambda p: -best.get(p, 0.0))
    fields.update(target=round(budget, 3), kept=kept, hunks=len(items), left_out=left)   # the most important first
    return shots, fields, written, used


# ---- sizing ----
def _hunk_parts(s):
    """(scroll, morph hold, morph dur, read) of a tour hunk shot."""
    scroll = P.scroll_time(s)
    m = s.get('morph')
    hold, md = (m['at'] - scroll, m['dur']) if m else (0.0, 0.0)
    return scroll, hold, md, s['dur'] - scroll - hold - md


def bounds(s, voice=None):
    """(floor, ceiling) of shot `s`'s length. A shot something is said over
    never shrinks below what the voice needs (it may grow)."""
    kind, dur = s['kind'], s['dur']
    voiced = voice is not None and P.shot_key(s) in voice
    if kind in LIMITS:
        lo, hi = LIMITS[kind]
        return dur if voiced else min(lo, dur), max(hi, dur)
    if kind == 'hunk':
        scroll, hold, md, read = _hunk_parts(s)
        if voiced:
            return dur, max(dur, scroll + hold + md + read * READ_STRETCH)
        n = len(s.get('callouts') or ())
        lo = scroll + (sum(MORPH_MIN) if s.get('morph') else 0) + max(READ_MIN, n * CALLOUT_MIN_EACH)
        return min(lo, dur), scroll + hold + md + read * READ_STRETCH
    return dur, dur


FIXED = frozenset(('title', 'map', 'end', 'patch', 'dive', 'travel'))   # the fixed costs: they shrink first


def _sized(shots, voice, fixed, content):
    """Each shot's length with the fixed costs scaled by `fixed` and the rest
    by `content`, within their bounds."""
    out = []
    for x in shots:
        lo, hi = bounds(x, voice)
        out.append(min(hi, max(lo, x['dur'] * (fixed if x['kind'] in FIXED else content))))
    return out


def _solve(total, target, lo, hi):
    """The x in [lo, hi] where the increasing `total(x)` meets `target` (an end when it never does)."""
    if total(lo) >= target:
        return lo
    if total(hi) <= target:
        return hi
    while hi - lo > 1e-5:
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if total(mid) < target else (lo, mid)
    return (lo + hi) / 2


def resize(shots, target, voice=None):
    """Size every shot of a plan, in place, within its bounds so the whole
    lasts `target` seconds (or as near as the bounds allow). Too long: the
    fixed costs (cards, map, camera moves) shrink first, by one factor, and
    only past their floors the content (hunks, dives into definitions). Too
    short: everything grows by one factor. A hunk's morph and callouts move
    with its read. Returns the (fixed, content) factors."""
    def total(f, c):
        return sum(_sized(shots, voice, f, c))
    if total(1.0, 1.0) > target:
        f = _solve(lambda x: total(x, 1.0), target, 0.0, 1.0)
        c = _solve(lambda x: total(0.0, x), target, 0.0, 1.0) if f <= 0.0 else 1.0
    else:
        f = c = _solve(lambda x: total(x, x), target, 1.0, 8.0)
    for x, d in zip(shots, _sized(shots, voice, f, c)):
        if abs(d - x['dur']) < 5e-4:
            continue
        if x['kind'] == 'hunk' and not (voice is not None and P.shot_key(x) in voice):
            _resize_hunk(x, d)
        x['dur'] = round(d, 3)
    return f, c


def _resize_hunk(s, d):
    """Hunk shot `s` made `d` s long: its scroll as it was, its morph and read
    scaled (the morph never past its natural pace, nor under MORPH_MIN), its
    callouts in even slots over the read."""
    scroll, hold, md, read = _hunk_parts(s)
    n = len(s.get('callouts') or ())
    var = d - scroll
    if s.get('morph'):
        if d < s['dur']:
            f = var / (hold + md + read)
            least = max(READ_MIN, n * CALLOUT_MIN_EACH)
            new_read = min(var - sum(MORPH_MIN), max(least, read * f))
            morph = var - new_read
            md2 = max(MORPH_MIN[1], morph * md / (hold + md))
            hold2 = morph - md2
        else:
            hold2, md2 = hold, md
            new_read = var - hold - md
        s['morph'] = dict(s['morph'], at=round(scroll + hold2, 3), dur=round(md2, 3))
    else:
        hold2 = md2 = 0.0
        new_read = var
    start = scroll + hold2 + md2
    slot = new_read / max(1, n)
    for k, c in enumerate(s.get('callouts') or ()):
        at = start + k * slot
        c['at'], c['until'] = round(at, 3), round(at + slot - 0.2, 3)


# ---- the summary ----
def summary(plan):
    """What a --duration plan kept and why, one line per kept hunk and per file
    left out, headed by the target and the plan's length."""
    stats = {f['path']: f for f in plan.files}
    entries = plan.patches or []
    kept_n = sum(len(v) for e in entries for v in (e.get('kept') or {}).values())
    files_n = sum(len(e.get('kept') or {}) for e in entries)
    all_hunks = sum(e.get('hunks', 0) for e in entries)
    all_files = sum(len(e.get('kept') or {}) + len(e.get('left_out') or []) for e in entries)
    out = [f'target {plan.duration:g}s, plan {plan.total:.1f}s: kept {kept_n} of {all_hunks} hunks '
           f'in {files_n} of {all_files} files']
    for k, e in enumerate(entries):
        lead = f'patch {k + 1}: ' if len(entries) > 1 else ''
        if len(entries) > 1:
            out.append(f'patch {k + 1}: {e["commit"]["subject"]}')
        for path, hs in (e.get('kept') or {}).items():
            for h in hs:
                out.append(f'{lead}{path}#{h["index"] + 1}: score {h["score"]:.1f}: {h["why"]}')
        for path in e.get('left_out') or []:
            f = stats.get(path) or next((x for x in e['files'] if x['path'] == path), {})
            out.append(f'{lead}{path}: left out (+{f.get("added", 0)} −{f.get("deleted", 0)})')
    return '\n'.join(out)

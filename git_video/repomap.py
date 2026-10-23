"""The repo map: every file at the target as a tile, sized by file size and
grouped by top-level directory, the changed files lit.

`tiles` is the planner's half: it picks which files get a tile of their own
and folds the rest into one "N more" tile per directory, so a big repository
still gives a legible map and every changed file always has its own tile.
The result is plain JSON and goes in the plan as `map`.
"""
import math
from dataclasses import dataclass

import videobuilder as vb
from videobuilder import text as vbtext
from videobuilder.capabilities.broadcast.heatmap_fill import ramp_color
from videobuilder.capabilities.broadcast.treemap_re_layout import treemap_layout

from .sidebar import MINUS, ellipsize

MAX_TILES = 36       # quiet (unchanged) tiles on the whole map, about
MAX_GROUPS = 10      # directories; the rest fold into one "k more directories" group
CHANGED_FLOOR = 0.06 # a changed tile's least share of the map's value ...
CHANGED_SHARE = 0.5  # ... lowered so the changed tiles' floors never pass this share together
QUIET_FLOOR = 0.012  # a quiet or folded tile's least share


def group_of(path):
    """(group key, label inside the group) of a path: grouped by the first
    directory; files at the top level go in the group keyed `''`, which no
    directory can be (so a top-level `django/` stays apart from the files
    beside it in a repo named django)."""
    head, sep, rest = path.partition('/')
    return (head, rest) if sep else ('', path)


def tiles(tree, files, root):
    """The map for the plan: `{'files': n, 'groups': [{'dir', 'label', 'tiles'}]}`.

    `tree` is [(path, size)] of every blob at the target; `files` the plan's
    file dicts (the changed files, in tour order). A changed tile is
    `{'path', 'size', 'added', 'deleted'}`, a quiet one `{'path', 'size'}`, a
    folded one `{'more': n, 'size': total}`. Groups holding a changed file
    come first, in the order of their first changed file, then by size. A
    group's `dir` is its directory (`''` for the top-level files) and its
    `label` what the map shows: `dir/`, or `root` for the top level."""
    sizes = dict(tree)
    changed = [f for f in files if f['path'] in sizes]
    order = {f['path']: k for k, f in enumerate(changed)}
    groups = {}
    for path, size in tree:
        groups.setdefault(group_of(path)[0], []).append((path, size))

    def rank(name):
        first = min((order[p] for p, _ in groups[name] if p in order), default=None)
        return (first is None, first or 0, -sum(s for _, s in groups[name]), name)

    names = sorted(groups, key=rank)
    lit = [n for n in names if not rank(n)[0]]
    rest = [n for n in names if n not in lit]
    room = max(0, MAX_GROUPS - len(lit))
    folded = []
    if len(rest) > room and len(rest) >= 2:   # one extra directory is fine; two or more fold
        rest, folded = rest[:max(0, room - 1)], rest[max(0, room - 1):]
        if len(folded) < 2:
            rest, folded = rest + folded, []
    kept = lit + rest

    # The quiet budget goes to the biggest quiet files across the kept groups.
    quiet = sorted(((s, p) for n in kept for p, s in groups[n] if p not in order), key=lambda x: (-x[0], x[1]))
    shown = {p for _, p in quiet[:max(0, MAX_TILES - len(changed))]}
    for n in kept:
        q = sorted(((s, p) for p, s in groups[n] if p not in order), key=lambda x: (-x[0], x[1]))
        left = [p for _, p in q if p not in shown]
        if len(left) == 1:   # a "1 more" tile says less than the file itself
            mine = [p for _, p in q if p in shown]
            if mine:
                shown.discard(mine[-1])
            else:
                shown.add(left[0])

    out = []
    for n in kept:
        entries = groups[n]
        ts = [{'path': f['path'], 'size': sizes[f['path']], 'added': f['added'], 'deleted': f['deleted'],
               'binary': bool(f.get('binary'))}
              for f in changed if group_of(f['path'])[0] == n]
        q = sorted(((s, p) for p, s in entries if p not in order), key=lambda x: (-x[0], x[1]))
        ts += [{'path': p, 'size': s} for s, p in q if p in shown]
        left = [(s, p) for s, p in q if p not in shown]
        if left:
            ts.append({'more': len(left), 'size': sum(s for s, _ in left)})
        out.append({'dir': n, 'label': f'{n}/' if n else root, 'tiles': ts})
    if folded:
        n = sum(len(groups[g]) for g in folded)
        label = f'{len(folded)} more directories'
        out.append({'dir': label, 'label': label,
                    'tiles': [{'more': n, 'size': sum(s for g in folded for _, s in groups[g])}]})
    return {'files': len(tree), 'groups': out}


def raw(tile):
    return math.sqrt(tile['size'] + 1)


def value(tile, total, changed=1):
    """A tile's area weight: the square root of its size (so a small changed
    file next to a big one still gets a readable tile, while bigger stays
    bigger), floored at a share of `total` (the sum of `raw` over the map).
    `changed` is how many changed tiles share the changed floor."""
    if 'added' in tile:
        floor = min(CHANGED_FLOOR, CHANGED_SHARE / max(1, changed))
    else:
        floor = QUIET_FLOOR
    return max(raw(tile), floor * total)


def key(tile, group):
    """A tile's name in marks and in the plan's `lit` list."""
    return tile['path'] if 'path' in tile else f'more:{group["dir"]}'


# ---- the film's half: geometry once per format, then drawing per frame ----
GROUP_GAP = 10
TILE_GAP = 4
HEADER = 28
PANEL = '#21242b'
PANEL_EDGE = '#3f444a'
QUIET = '#2f343f'
QUIET_EDGE = '#3a404c'
QUIET_INK = '#7a8291'
HEAD_INK = '#9aa3b2'
INK = '#1c1f24'
RAMP = ('#a8823a', '#cf9f4a', '#ecbe7b')   # lit tiles, by how much changed: low -> high


@dataclass(frozen=True)
class Tile:
    key: str
    box: vb.Box
    info: dict
    label: str          # the path inside its group, or "N more"
    heat: float | None  # (added + deleted) over the map's most changed file; None: not changed


@dataclass(frozen=True)
class Group:
    label: str
    box: vb.Box
    header: vb.Box


@dataclass(frozen=True)
class MapLayout:
    box: vb.Box
    groups: tuple
    tiles: tuple

    def tile(self, key):
        return next((t for t in self.tiles if t.key == key), None)


def layout(m, box):
    """Where every group and tile of the plan's map `m` goes in `box` (canvas px).
    Group boxes are a treemap of their tiles' summed values; inside each, under
    a header strip, the tiles are a treemap of their own."""
    box = vb.Box(*box)
    groups = [g for g in m['groups'] if g['tiles']]
    flat = [t for g in groups for t in g['tiles']]
    if not flat:
        return MapLayout(box, (), ())
    total = sum(raw(t) for t in flat)
    n_changed = sum('added' in t for t in flat)
    top = max([t['added'] + t['deleted'] for t in flat if 'added' in t] or [0]) or 1
    gvals = [[value(t, total, n_changed) for t in g['tiles']] for g in groups]
    gboxes = treemap_layout([sum(v) for v in gvals], *box, gap=GROUP_GAP)
    out_groups, out_tiles = [], []
    for g, vals, gb in zip(groups, gvals, gboxes):
        hh = min(HEADER, gb.h * 0.3)
        header = vb.Box(gb.x, gb.y, gb.w, hh)
        inner = vb.Box(gb.x + TILE_GAP, gb.y + hh, max(0.0, gb.w - 2 * TILE_GAP), max(0.0, gb.h - hh - TILE_GAP))
        out_groups.append(Group(g['label'], gb, header))
        for t, tb in zip(g['tiles'], treemap_layout(vals, *inner, gap=TILE_GAP)):
            if 'path' in t:
                label = group_of(t['path'])[1]
            else:
                label = f'{t["more"]} more'
            heat = (t['added'] + t['deleted']) / top if 'added' in t else None
            out_tiles.append(Tile(key(t, g), tb, t, label, heat))
    return MapLayout(box, tuple(out_groups), tuple(out_tiles))


def stat_parts(info):
    return [(f'+{info["added"]}', 'added'), (f'{MINUS}{info["deleted"]}', 'removed')]


def draw(cv, lay, lit, *, ui='sans-serif', mono='monospace', added='#98be65', removed='#ff6c6b',
         outline='#ecbe7b', skip=()):
    """The map as laid out by `layout`. `lit[key]` (0 -> 1) is how far each
    changed tile has lit up (missing: 0). Tiles in `skip` are left out (the
    tile a camera move is growing, which the caller draws itself). Marks
    `map.group` per group and `map.tile <key>` per tile."""
    head = vb.Font(mono, 17, weight=700)
    with cv.clip(*lay.box):
        for g in lay.groups:
            cv.rect(*g.box, PANEL, radius=8)
            cv.outline(g.box.x + 0.5, g.box.y + 0.5, g.box.w - 1, g.box.h - 1, PANEL_EDGE, width=1, radius=8)
            if g.header.h >= vbtext.line_height(head) * 0.9:
                label = ellipsize(g.label, head, g.header.w - 20)
                vbtext.draw(cv, [label], head, g.header.x + 10,
                            g.header.y + (g.header.h - vbtext.line_height(head)) / 2 + 1, HEAD_INK)
            cv.mark('map.group', *g.box)
        for t in lay.tiles:
            if t.key in skip:
                continue
            draw_tile(cv, t, lit.get(t.key, 0.0), ui=ui, mono=mono, added=added, removed=removed,
                      outline=outline)


def tile_fill(t, p):
    """A tile's colour: quiet, or blending to its heat's colour as it lights."""
    if t.heat is None or p <= 0:
        return QUIET
    q = vb.rgba(QUIET)
    return tuple(255 * a + (h - 255 * a) * vb.clamp01(p) for a, h in zip(q[:3], ramp_color(t.heat, RAMP)))


def draw_tile(cv, t, p, *, box=None, label_alpha=1.0, ui='sans-serif', mono='monospace', added='#98be65',
              removed='#ff6c6b', outline='#ecbe7b'):
    """One tile, lit `p` (0 -> 1), in its own box or `box` (a camera move's)."""
    b = box or t.box
    if t.heat is not None and p > 0 and box is None:
        b = b.scaled(1 + 0.06 * math.sin(math.pi * vb.clamp01(p)))
    radius = min(5.0, b.w / 4, b.h / 4)
    cv.rect(*b, tile_fill(t, p), radius=radius)
    if t.heat is None:
        cv.outline(b.x + 0.5, b.y + 0.5, max(0, b.w - 1), max(0, b.h - 1), QUIET_EDGE, width=1, radius=radius)
    elif p > 0:
        cv.outline(b.x - 1, b.y - 1, b.w + 2, b.h + 2, outline, width=2.5, radius=radius + 1, alpha=vb.clamp01(p))
    cv.mark(f'map.tile {t.key}', *b)
    if label_alpha <= 0:
        return
    if t.heat is not None and p > 0:
        _changed_label(cv, t, b, vb.clamp01(p) * label_alpha, mono, added, removed)
    else:
        _quiet_label(cv, t, b, label_alpha, mono)


def _fits(s, font, b, pad=8):
    return vbtext.measure(s, font) <= b.w - 2 * pad and vbtext.line_height(font) <= b.h - 2 * pad + 4


def _quiet_label(cv, t, b, alpha, mono):
    for size in (16, 14):
        font = vb.Font(mono, size)
        for s in dict.fromkeys((t.label, t.label.rsplit('/', 1)[-1])):
            if _fits(s, font, b):
                with cv.clip(*b):
                    vbtext.draw(cv, [s], font, b.x + 8, b.y + 6, QUIET_INK, alpha=alpha)
                return


def label_parts(info):
    """A changed tile's label as (text, role) pairs: `+a −d`, or `binary`
    for a file git has no line counts for (as the side bar says)."""
    return [('binary', 'muted')] if info.get('binary') else stat_parts(info)


def _changed_label(cv, t, b, alpha, mono, added, removed):
    """`+a −d` on a dark pill, and the file name above it when there is room.
    Too small a tile still gets its numbers, at the smallest size, on a pill
    that may reach past the tile (it stays inside the map)."""
    parts = label_parts(t.info)
    colors = {'added': added, 'removed': removed, 'muted': '#9aa3b2'}
    for size in (19, 17, 15, 13):
        stat = vb.Font(mono, size, weight=700)
        text = ' '.join(s for s, _ in parts)
        sw, sh = vbtext.measure(text, stat), vbtext.line_height(stat)
        pw, ph = sw + 14, sh + 4
        if pw <= b.w - 8 and ph <= b.h - 8:
            break
    name = vb.Font(mono, max(13, size - 2), weight=700)
    nh = vbtext.line_height(name)
    two = ph + nh + 6 <= b.h - 12
    if two:
        nm = t.label if vbtext.measure(t.label, name) <= b.w - 16 else t.label.rsplit('/', 1)[-1]
        two = vbtext.measure(nm, name) <= b.w - 16
    with cv.layer(opacity=alpha):
        if two:
            px, py = b.x + 8, b.y + 8 + nh + 4
            with cv.clip(*b):
                vbtext.draw(cv, [nm], name, b.x + 8, b.y + 8, INK)
        else:
            px, py = b.cx - pw / 2, b.cy - ph / 2
        cv.rect(px, py, pw, ph, INK, radius=ph / 2, alpha=0.92)
        x = px + 7
        for s, role in parts:
            vbtext.draw(cv, [s], stat, x, py + 2, colors[role])
            x += vbtext.measure(s + ' ', stat)
        cv.mark(f'map.label {t.key}', px, py, pw, ph)

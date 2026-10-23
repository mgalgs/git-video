"""The call graph side sheet: the definitions the change touches and the ones
the tour dives into, linked by the dives and by calls on changed lines.

`build` is the planner's half: nodes and edges as plain JSON (the plan's
`graph`), each stamped with the shot that discovers it, and the shots
stamped with their node (and a dive with its edge). `layout`, `state_at`
and `draw` are the film's half: positions once per box, what is on screen
at a moment, and the drawing, which grows as the tour discovers it.
"""
import math
import os
from dataclasses import dataclass

import videobuilder as vb
from videobuilder import text as vbtext
from videobuilder.capabilities.shapes.connector_network import draw_node
from videobuilder.capabilities.shapes.map_route_draw import draw_route, route_legs

from . import xref
from .sidebar import ellipsize

MAX_NODES = 12      # the side bar cannot label more legibly
MAX_COLUMNS = 3     # one column per file (2 in a tall box); more files share the last
STEP = 62           # the most px between two nodes of a column, at scale 1
HUB = 1.35          # the node on screen is this much larger (and solid)
POP = 0.35          # a node pops in over this long
GROW = (0.2, 0.7)   # a call edge grows over this part of its hunk shot
ROUTE = 0.5         # a dive's edge runs on this long past the definition's reveal


# ---- the plan's half ----
def _node(nodes, path, name, line, kind, touched, first):
    nid = f'{path}:{name}'
    if nid not in nodes:
        nodes[nid] = {'id': nid, 'path': path, 'name': name, 'line': line, 'kind': kind, 'touched': touched,
                      'first': first}
    else:
        nodes[nid]['touched'] = nodes[nid]['touched'] or touched
    return nid


def _hunk_of(change, path, index):
    for f in change.files:
        if f.path == path:
            return f, f.hunks[index]
    return None, None


def _changes(change):
    """The changes a plan's shots come from, by their `patch` field: a list
    (one per --series patch) or one change for all."""
    return change if isinstance(change, (list, tuple)) else [change]


def _patch(changes, s):
    """The index into `changes` of the change shot `s` reads."""
    return s.get('patch', 0) if len(changes) > 1 else 0


def _changed(h):
    return [n for a, b in h.added for n in range(a, b + 1)]


def build(change, shots):
    """{'nodes', 'edges'} for the plan's `shots`, which get `node` (hunk and
    dive shots) and `edge` (dives) fields. A node is a definition holding a
    hunk's changed lines (`touched`) or a dive's target; its `first` is the
    shot that discovers it. Edges: `xref` per dive, from the hunk's node to
    the definition; `call` from a touched definition to another node whose
    name it uses on a changed line. At most MAX_NODES nodes, by discovery.
    `change` may be a list of changes, one per --series patch: each shot is
    read against the change its `patch` names."""
    changes = _changes(change)
    nodes, edges = {}, []
    defs_of, holders_of = {}, {}

    def defs(k, path):
        if (k, path) not in defs_of:
            ch = changes[k]
            text = ch.text(path) or ''
            defs_of[k, path] = xref.file_defs(ch.repo, ch.target, path)
            holders_of[k, path] = xref.enclosures(text.split('\n'), defs_of[k, path])
        return defs_of[k, path]

    def enclosing(k, path, n):
        hs = holders_of[k, path]
        return hs[n - 1] if 0 < n <= len(hs) else None

    def holder(k, path, ns):
        defs(k, path)
        for n in ns:
            e = enclosing(k, path, n)
            if e:
                return e
        return None

    touched_ranges = {}   # (patch, path) -> set of (name, line) holding a changed line
    for k, ch in enumerate(changes):
        for f in ch.files:
            if f.text is None:
                continue
            defs(k, f.path)
            for h in f.hunks:
                for n in _changed(h):
                    e = enclosing(k, f.path, n)
                    if e:
                        touched_ranges.setdefault((k, f.path), set()).add((e[0], e[1].line))
    source, hunk_of_shot = None, {}
    for i, s in enumerate(shots):
        k = _patch(changes, s)
        if s['kind'] == 'hunk':
            f, h = _hunk_of(changes[k], s['path'], s['index'])
            defs(k, s['path'])
            e = holder(k, s['path'], [s['focus_line'], *_changed(h), *(g[0] for g in h.segments)]) if h else None
            source = _node(nodes, s['path'], e[0], e[1].line, e[1].kind, True, i) if e else None
            s['node'] = source
            hunk_of_shot[i] = h
        elif s['kind'] == 'xref-push':
            d = s['definition']
            defs(k, d['path'])
            touched = (s['name'], d['line']) in touched_ranges.get((k, d['path']), ())
            if source is None:   # a dive from top-level code: the file itself is the source
                source = _node(nodes, s['path'], os.path.basename(s['path']), 1, 'module', True, i)
            b = _node(nodes, d['path'], s['name'], d['line'], d['kind'], touched, i)
            s['node'] = b
            s['edge'] = len(edges)
            edges.append({'a': source, 'b': b, 'kind': 'xref', 'first': i})
    # calls from a touched definition to another node, found on the changed lines
    by_def = {(n['path'], n['line']): nid for nid, n in nodes.items()}
    seen = {(e['a'], e['b']) for e in edges}
    for i, s in enumerate(shots):
        a = s.get('node') if s['kind'] == 'hunk' else None
        h = hunk_of_shot.get(i)
        lang = xref.lang_of(s.get('path', '')) if a else None
        if not a or h is None or lang is None:
            continue
        ch = changes[_patch(changes, s)]
        index = xref.index(ch.repo, ch.target, lang)
        for c in xref.candidates(ch.text(s['path']), s['path'], _changed(h)):
            d = xref.resolve(c, index, s['path'])
            b = by_def.get((d.path, d.line)) if d else None
            if b and b != a and (a, b) not in seen:
                seen.add((a, b))
                edges.append({'a': a, 'b': b, 'kind': 'call', 'first': max(i, nodes[b]['first'])})
    kept = sorted(nodes.values(), key=lambda n: n['first'])[:MAX_NODES]
    ids = {n['id'] for n in kept}
    dive_edge = {id(s): edges[s['edge']] for s in shots if s.get('edge') is not None}   # before sorting
    out_edges = [e for e in sorted(edges, key=lambda e: e['first']) if e['a'] in ids and e['b'] in ids]
    at = {id(e): k for k, e in enumerate(out_edges)}
    for s in shots:
        if s.get('node') is not None and s['node'] not in ids:
            s['node'] = None
        if 'edge' in s:
            s['edge'] = at.get(id(dive_edge.get(id(s))))
    for s in shots:
        if s['kind'] == 'hunk':
            s.setdefault('node', None)
    return {'nodes': kept, 'edges': out_edges}


def build_develop(change, shots, written):
    """{'nodes', 'edges'} for a develop plan. A node is a definition an edit
    shot writes (its line or a line of its body; `touched`, `first` that
    shot, `at` when its first line is finished, in seconds on screen), or one
    a written line calls (not touched, popping in with the call). A `call`
    edge runs from a written definition to the node its written line names,
    at the later of the two. `written`: {edit shot index: {new line index
    (0-based): time on screen}}. At most MAX_NODES nodes, by when they appear."""
    changes = _changes(change)
    nodes, found = {}, []

    def node(path, name, d, touched, i, at):
        nid = _node(nodes, path, name, d.line, d.kind, touched, i)
        if nodes[nid]['first'] == i and 'at' not in nodes[nid]:
            nodes[nid]['at'] = round(at, 3)
        return nid

    for i, s in enumerate(shots):
        if s['kind'] != 'edit' or not written.get(i):
            continue
        ch = changes[_patch(changes, s)]
        text = ch.text(s['path'])
        if not text:
            continue
        holders = xref.enclosures(text.split('\n'), xref.file_defs(ch.repo, ch.target, s['path']))
        done = written[i]
        for j, tm in sorted(done.items(), key=lambda jt: (jt[1], jt[0])):
            e = holders[j] if j < len(holders) else None
            if e:
                node(s['path'], e[0], e[1], True, i, tm)
        found.append((i, s, ch, text, holders, done))
    edges, seen = [], set()
    for i, s, ch, text, holders, done in found:
        lang = xref.lang_of(s['path'])
        if lang is None:
            continue
        index = xref.index(ch.repo, ch.target, lang)
        for c in xref.candidates(text, s['path'], sorted(j + 1 for j in done)):
            e = holders[c.line - 1] if 0 < c.line <= len(holders) else None
            d = xref.resolve(c, index, s['path']) if c.is_call else None
            if not e or d is None:
                continue
            a = f'{s["path"]}:{e[0]}'
            tm = done[c.line - 1]
            b = node(d.path, c.name, d, False, i, max(tm, nodes[a].get('at', 0.0)))
            if a == b or (a, b) in seen:
                continue
            seen.add((a, b))
            first = max(nodes[a]['first'], nodes[b]['first'])
            ats = [nodes[x].get('at', 0.0) for x in (a, b) if nodes[x]['first'] == first]
            at = max([tm] + ats) if first == i else max(ats or [0.0])
            edges.append({'a': a, 'b': b, 'kind': 'call', 'first': first, 'at': round(at, 3)})
    kept = sorted(nodes.values(), key=lambda n: (n['first'], n.get('at', 0.0)))[:MAX_NODES]
    ids = {n['id'] for n in kept}
    out_edges = sorted((e for e in edges if e['a'] in ids and e['b'] in ids), key=lambda e: (e['first'], e['at']))
    return {'nodes': kept, 'edges': out_edges}


def node_id(path, name, patch=0):
    """A node's id: `path:name`, marked with its patch past the first of a
    --series (each patch has a graph of its own)."""
    return f'{path}:{name}' + (f'@{patch}' if patch else '')


def by_patch(changes, shots, make):
    """One graph per --series patch, joined: `make(change, shots, k)` builds
    patch k's from its own shots (indices local to them); its nodes get
    `patch` and ids from `node_id`, `first` becomes a plan shot index, and the
    shots' `node` and `edge` follow. Each patch keeps up to MAX_NODES."""
    nodes, edges = [], []
    for k, ch in enumerate(changes):
        idx = [i for i, s in enumerate(shots) if s.get('patch', 0) == k]
        sub = [shots[i] for i in idx]
        g = make(ch, sub, k)

        def ren(nid, k=k):
            return nid + (f'@{k}' if k else '')

        base = len(edges)
        nodes += [{**n, 'id': ren(n['id']), 'first': idx[n['first']], 'patch': k} for n in g['nodes']]
        edges += [{**e, 'a': ren(e['a']), 'b': ren(e['b']), 'first': idx[e['first']]} for e in g['edges']]
        for s in sub:
            if s.get('node') is not None:
                s['node'] = ren(s['node'])
            if s.get('edge') is not None:
                s['edge'] += base
    return {'nodes': nodes, 'edges': edges}


def part(graph, patch):
    """The graph of one --series patch (all of it without patches)."""
    if not any('patch' in n for n in graph['nodes']):
        return graph
    ids = {n['id'] for n in graph['nodes'] if n.get('patch', 0) == patch}
    return {'nodes': [n for n in graph['nodes'] if n['id'] in ids],
            'edges': [e for e in graph['edges'] if e['a'] in ids and e['b'] in ids]}


# ---- the film's half ----
@dataclass(frozen=True)
class Layout:
    box: vb.Box
    pos: dict          # node id -> (x, y)
    r: float
    labels: dict       # node id -> (text, x, y): left edge and top of its name, beside it
    heads: tuple       # (text, x, y, width, node ids) per column


def fonts(scale=1.0):
    return (vb.Font('monospace', 15 * scale), vb.Font('sans-serif', 13 * scale, weight=700, tracking=0.06))


def layout(graph, box, scale=1.0):
    """Node positions in `box`: one column per file (in order of discovery,
    at most MAX_COLUMNS), the file's name over it, its nodes spread down it,
    each with its name beside it."""
    box = vb.Box(*box)
    label_f, head_f = fonts(scale)
    paths = []
    for n in graph['nodes']:
        if n['path'] not in paths:
            paths.append(n['path'])
    cols = max(1, min(MAX_COLUMNS if box.w >= box.h else 2, len(paths)))
    col_of = {p: min(k, cols - 1) for k, p in enumerate(paths)}
    cw = box.w / cols
    hh = vbtext.line_height(head_f) + 10 * scale
    lh = vbtext.line_height(label_f)
    room = box.h - hh
    fit = max(1, int(room // (lh + 6 * scale)))   # rows a column holds before it wraps into another
    counts = [sum(1 for n in graph['nodes'] if col_of[n['path']] == c) for c in range(cols)]
    rows = max(1, min(max(counts or [1]), fit))
    step = min(STEP * scale, room / rows)
    r = max(3.0, min(10 * scale, step * 0.3))
    pos, labels, heads = {}, {}, []
    for c in range(cols):
        members = [n for n in graph['nodes'] if col_of[n['path']] == c]
        names = [p for p in paths if col_of[p] == c]
        x0 = box.x + c * cw
        head = os.path.basename(names[0]) + (f' +{len(names) - 1}' if len(names) > 1 else '')
        w = cw - 8 * scale
        heads.append((ellipsize(head, head_f, w), x0, box.y, w, tuple(n['id'] for n in members)))
        subs = max(1, math.ceil(len(members) / fit))
        sw = cw / subs
        for j, n in enumerate(members):
            nx = x0 + (j // fit) * sw + r * HUB + 2 * scale
            cy = box.y + hh + step * (j % fit + 0.5)
            pos[n['id']] = (nx, cy)
            labels[n['id']] = (ellipsize(n['name'], label_f, max(0.0, sw - 2 * r * HUB - 10 * scale)),
                               nx + r * HUB + 4 * scale, cy - lh / 2)
    return Layout(box, pos, r, labels, tuple(heads))


@dataclass(frozen=True)
class State:
    nodes: dict        # id -> 0..1 popped in
    edges: dict        # index -> 0..1 drawn
    current: str | None
    trail: tuple       # node ids of the xref stack, oldest first
    route: tuple | None   # (edge index, progress) of the dive running now


def route_span():
    """When a dive's edge runs, in seconds into its xref-push shot: from the
    click until the definition has opened."""
    from . import plan as P   # the plan imports this module for `build`
    return P.XREF_CLICK, P.XREF_SWITCH + P.XREF_REVEAL


def state_at(graph, shots, i, t, current, trail):
    """What the graph shows `t` s into shot `i`: everything discovered by
    earlier shots, and this shot's own discoveries coming in (a hunk's node
    popping in, its call edges growing; a dive running along its edge, its
    target popping in as the route lands)."""
    nodes, edges, route = {}, {}, None
    kind = shots[i]['kind']
    r0, r1 = route_span()
    for n in graph['nodes']:
        if n['first'] < i:
            nodes[n['id']] = 1.0
        elif n['first'] == i:
            if kind == 'xref-push' and shots[i].get('node') == n['id'] and shots[i].get('edge') is not None:
                nodes[n['id']] = vb.clamp01((t - (r1 - 0.15)) / POP)
            else:   # a node written while a develop shot types it pops in at its `at`
                nodes[n['id']] = vb.clamp01((t - n.get('at', 0.0)) / POP)
    for k, e in enumerate(graph['edges']):
        if e['first'] < i:
            edges[k] = 1.0
        elif e['first'] == i:
            if e['kind'] == 'xref':
                p = vb.clamp01((t - r0) / (r1 - r0))
                edges[k] = p
                if 0 < p < 1:
                    route = (k, p)
            elif 'at' in e:   # a call typed in a develop shot grows as its line is written
                edges[k] = vb.clamp01((t - e['at']) / POP)
            else:
                d = shots[i]['dur']
                edges[k] = vb.clamp01((t - GROW[0] * d) / ((GROW[1] - GROW[0]) * d))
    return State(nodes, edges, current, tuple(trail), route)


def _path(a, b, r):
    """An edge's points from node `a` to node `b`: a gentle arc (route_legs),
    bowed further when both sit in one column, so it clears the nodes between."""
    bow = 0.18 if abs(a[0] - b[0]) > 1 else -0.35
    return route_legs([a, b], bow=bow)[0], bow


def draw(cv, lay, graph, state, *, color='#51afef', call='#5b6270', ink='#bbc2cf', muted='#7a8291',
         accent='#ecbe7b', bg='#21242b', scale=1.0):
    """The graph as `state` says: edges under nodes (dives in `color`, calls in
    `call` with an arrowhead), the dive running now along its route with a
    marker, nodes as rings (the one on screen a solid, larger hub: shape and
    weight, not only colour), the xref stack's nodes numbered in stack order."""
    label_f, head_f = fonts(scale)
    bold = vb.Font(label_f.family, label_f.size, weight=700)
    with cv.clip(*lay.box.pad(-2)):
        for text, x, y, w, members in lay.heads:
            a = max([state.nodes.get(m, 0.0) for m in members] or [0.0])
            if a > 0:
                vbtext.draw(cv, [text], head_f, x, y, muted, alpha=vb.clamp01(a))
        for k, e in enumerate(graph['edges']):
            f = state.edges.get(k, 0.0)
            if f <= 0 or (state.route and state.route[0] == k):
                continue
            a, b = lay.pos.get(e['a']), lay.pos.get(e['b'])
            if a is None or b is None or a == b:
                continue
            pts, _ = _path(a, b, lay.r)
            vb.paths.draw(cv, vb.paths.reveal(pts, f), color if e['kind'] == 'xref' else call,
                          width=(2.5 if e['kind'] == 'xref' else 1.8) * scale, alpha=0.95)
            if f >= 1:   # an arrowhead says which way it goes
                _arrow(cv, pts, lay.r, color if e['kind'] == 'xref' else call, scale)
        if state.route:
            k, p = state.route
            e = graph['edges'][k]
            a, b = lay.pos.get(e['a']), lay.pos.get(e['b'])
            if a is not None and b is not None and a != b:
                draw_route(cv, [a, b], p, color=accent, width=3 * scale, bow=_path(a, b, lay.r)[1], ghost=None,
                           ring=bg, marker=True)
        for n in graph['nodes']:
            amount = state.nodes.get(n['id'], 0.0)
            if amount <= 0 or n['id'] not in lay.pos:
                continue
            x, y = lay.pos[n['id']]
            hub = n['id'] == state.current
            r = lay.r * (HUB if hub else 1.0)
            draw_node(cv, x, y, r, accent if hub else (color if n['touched'] else muted), amount, hub=hub)
            text, lx, ly = lay.labels[n['id']]
            vbtext.draw(cv, [text], bold if hub else label_f, lx, ly, '#e6e9ef' if hub else ink,
                        alpha=vb.clamp01(amount))
            cv.mark(f'graph.node {n["id"]}', x - r, y - r, 2 * r, 2 * r)
        for depth, nid in enumerate(state.trail):   # the xref stack as a numbered trail
            if nid in lay.pos and state.nodes.get(nid, 0) > 0:
                x, y = lay.pos[nid]
                br = 8 * scale
                bx, by = x - lay.r * 0.9, y - lay.r * 1.1
                cv.sector(bx, by, br, 0, 2 * math.pi, bg)
                cv.sector(bx, by, br - 1.5 * scale, 0, 2 * math.pi, accent)
                vbtext.draw(cv, [str(depth + 1)], vb.Font('sans-serif', 11 * scale, weight=800), bx - br,
                            by - vbtext.line_height(vb.Font('sans-serif', 11 * scale, weight=800)) / 2, bg,
                            align='center', width=2 * br)


def _arrow(cv, pts, r, color, scale):
    (x0, y0), (x1, y1) = pts[-3], pts[-1]
    ang = math.atan2(y1 - y0, x1 - x0)
    tx, ty = x1 - math.cos(ang) * (r + 3), y1 - math.sin(ang) * (r + 3)
    s = 7 * scale
    for side in (-1, 1):
        a = ang + math.pi + side * 0.45
        cv.line(tx, ty, tx + math.cos(a) * s, ty + math.sin(a) * s, color, width=1.8 * scale)

"""The tour as a videobuilder Film, built from a plan.

The frame is a stage: `stage(cv)` names its boxes per format (the main box,
the side bar's), and each shot's `scene` picks the draw function that plays
in the main box (`SCENES`): the title/end `card`, the `editor`, and the
repo `map`, whose camera moves (`dive`, `travel`) carry the tour from the
map into a file and from file to file. `draw(n, cv)` is pure: everything
it shows is derived from the shot list and tables built once here (the
treemap and the call graph per box, the morphs' highlighted old lines and
the minimaps among them).
"""
import bisect
import math
import re
from dataclasses import dataclass, replace

import videobuilder as vb
from videobuilder import text as vbtext
from videobuilder.capabilities.broadcast.callout_line_with_label import callout_line
from videobuilder.capabilities.broadcast.click_ripple_indicator import click_ripple
from videobuilder.capabilities.broadcast.cursor_path_animation import cursor_path
from videobuilder.capabilities.broadcast.end_card_with_subscribe_slot import end_card_slots, slot_reveal
from videobuilder.capabilities.broadcast.lower_third_slide_in_bar import lower_third_bar
from videobuilder.capabilities.broadcast.two_tier_kicker_and_title_stack import kicker_title_progress
from videobuilder.capabilities.broadcast.zoom_to_location_map import fly_to
from videobuilder.capabilities.typography.odometer_roll import draw_odometer
from videobuilder.capabilities.ui.avatar_stack_spread import draw_avatar_stack
from videobuilder.capabilities.ui.circular_clip_reveal import GROW, circular_reveal, reach
from videobuilder.capabilities.ui.cursor_state_change import draw_cursor as draw_state_cursor, hover_amount
from videobuilder.capabilities.principles.time_remapping import speed_ramp
from videobuilder.capabilities.ui.scroll_velocity_skew import scroll_velocity, velocity_skew
from videobuilder.capabilities.transitions.stacked_card_reveal import stacked_card_reveal
from videobuilder.capabilities.ui.progress_steps_connector_fill import draw_progress_steps

from . import edits, editor, graph as graphmod, highlight, plan as P, repomap, sidebar, sounds

THEME = editor.Theme()
FORMATS = ('16x9', '9x16', '1x1')
UI = 'sans-serif'
SETTLED = 0.45  # a swept band's strength once it has settled
FADE = 0.35   # an editor shot next to a card fades in or out over this long


# ---- the stage ----
@dataclass(frozen=True)
class Stage:
    main: vb.Box
    side: vb.Box | None
    compact: bool       # the side bar is the narrow 1:1 strip
    mono: vb.Font
    scale: float        # of the side bar's type


def _stage_16x9(cv, family):
    main, side = vb.Box.of(cv).pad(24).row(vb.fr, 600, gap=16)
    return Stage(main, side, False, vb.Font(family, 26, line_height=1.35), 1.0)


def _stage_9x16(cv, family):
    main, side = vb.Box.of(cv).pad(24).column(vb.fr * 1.6, vb.fr, gap=16)
    return Stage(main, side, False, vb.Font(family, 24, line_height=1.35), 1.15)


def _stage_1x1(cv, family):
    main, side = vb.Box.of(cv).pad(20).column(vb.fr, 112, gap=12)
    return Stage(main, side, True, vb.Font(family, 22, line_height=1.35), 1.0)


LAYOUT = {'16x9': _stage_16x9, '9x16': _stage_9x16, '1x1': _stage_1x1}


def stage(cv, family='monospace'):
    return vb.per_format(LAYOUT, cv)(cv, family)


# ---- per-shot state ----
@dataclass(frozen=True)
class View:
    """What the editor shows when a shot starts or ends."""
    path: str | None            # None: the *scratch* buffer
    top: float                  # 0-based first line, planned for PLAN_ROWS
    focus: int                  # 0-based line that must stay on screen
    cursor: tuple               # (line, col), 0-based
    stack: tuple                # ((name, path, line), ...), oldest first
    extra: int = 0              # rows a morph's old lines add past the file's end before they go
    patch: int = 0              # the --series patch whose text is shown
    variant: str = 'new'        # the file as the patch leaves it (`new`) or finds it (`old`)
    edit: int | None = None     # develop: the edit shot whose buffer is shown, ...
    end: bool = False           # ... as it ends (else as it starts)
    wash: int | None = None     # the hunk shot whose swept lines stay washed while this view shows them


SCRATCH_TEXT = (';; This buffer is for text that is not saved, and for Lisp evaluation.\n'
                ';; To create a file, visit it with C-x C-f and enter text in its buffer.\n')
SCRATCH = editor.Buffer('*scratch*', highlight.highlight_lines(SCRATCH_TEXT, 'scratch.el'), 'Lisp Interaction')


def fit_top(top, focus, rows, n):
    """`top`, moved as little as needed so line `focus` is on screen in a window
    of `rows` and the window doesn't run past the end of the file (`n` rows,
    counting the old lines a morph shows before they collapse)."""
    top = max(0.0, min(float(top), max(0, n - rows)))
    if top <= focus <= top + rows - 2:
        return top
    return float(max(0, min(focus - rows // 3, n - rows)))


def _mode(path):
    name = highlight.lexer_for(path).name
    return 'Text' if name == 'Text only' else name


def _first_col(runs):
    s = ''.join(t for _, t in runs)
    return len(s) - len(s.lstrip())


def _peak_moment(span, clk):
    """The middle of the longest stretch of a fast span's plateau that runs at
    the peak (a held fill inside the plateau plays near natural speed, and the
    badge is hidden there)."""
    a, b = span['times'][1], span['times'][2]
    pts = [(t, v) for t, v in zip(clk['times'], clk['speeds']) if a - 1e-6 <= t <= b + 1e-6]
    top = max((v for _, v in pts), default=0.0)
    best = None
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if v0 >= top * 0.999 and v1 >= top * 0.999 and (best is None or t1 - t0 > best[1] - best[0]):
            best = (t0, t1)
    return sum(best) / 2 if best else (a + b) / 2


class Tour:
    """A plan made into a film. `film` is the vb.Film; `shot_frames[i]` is the
    first frame of shot i (where a later narration track would hook in)."""

    def __init__(self, plan, change, formats=FORMATS, font='monospace', log=None, patches=None):
        for s in plan.shots:
            if s['scene'] not in SCENES:
                raise ValueError(f'unknown scene {s["scene"]!r} in a {s["kind"]!r} shot; '
                                 f'known: {", ".join(SCENES)}')
        self.plan, self.change, self.font = plan, change, font
        self.patches = list(patches) if patches else [change]   # the change of each --series patch
        self.fps = plan.fps
        self.shots = plan.shots
        starts, acc = [], 0.0
        for s in self.shots:
            starts.append(round(acc * self.fps))
            acc += s['dur']
        self.shot_frames = starts
        self.frames = max(1, round(acc * self.fps))
        # Buffers by (patch, path, variant); the fringe's +/− marks and tints by
        # (patch, path), from the plan.
        self.buffers, self.marks = {}, {}
        for k in range(len(self.patches)):
            for f in self.patch_info(k)['files']:
                m = f.get('marks') or {}
                added = [(a - 1, b - 1) for a, b in m.get('added', ())]
                self.marks[k, f['path']] = (sorted({n for a, b in added for n in range(a, b + 1)}),
                                            sorted(set(m.get('removed_at', ()))), added)
        self.develop = plan.mode == 'develop'
        for s in self.shots:
            k = s.get('patch', 0)
            if self.develop and s['kind'] in ('edit', 'open', 'dive', 'travel'):
                continue   # develop draws a performance's buffer, not a file's text
            for path in (s.get('path'), s.get('to'), (s.get('definition') or {}).get('path')):
                if path and (k, path, 'new') not in self.buffers:
                    ch = self.patches[k]
                    text = ch.text(path)
                    if text is None:
                        raise ValueError(f'{path} has no text at {ch.target[:12]} to show')
                    self.buffers[k, path, 'new'] = editor.Buffer(path, highlight.highlight_lines(text, path),
                                                                 _mode(path))
        self.views = self._views()
        self.segments = {}   # hunk shot index -> its morph's editor.Segments (old lines highlighted once)
        for i, s in enumerate(self.shots):
            if s['kind'] == 'hunk' and s.get('morph'):
                self.segments[i] = tuple(
                    editor.Segment(g['at'], highlight.highlight_lines('\n'.join(g['old']) + '\n', s['path']),
                                   g['new'], tuple(tuple(o) for o in g['ops']))
                    for g in s['morph']['segments'])
        self.graph_layouts = {}   # the call graph per side-bar box (and patch), laid out once
        self.graph_parts = {}
        self.graph_nodes = {n['id'] for n in (plan.graph or {}).get('nodes', ())}
        self.graph_home = []   # per shot: the node of the last hunk shown (or written) in its file
        self.graph_written = {}   # develop: edit shot -> [(at, node id)] it writes, in order
        for n in (plan.graph or {}).get('nodes', ()):
            if n['touched'] and self.shots[n['first']]['kind'] == 'edit':
                self.graph_written.setdefault(n['first'], []).append((n.get('at', 0.0), n['id']))
        home = {}
        for i, s in enumerate(self.shots):
            if s['kind'] == 'hunk':
                home[s['path']] = s.get('node')
            self.graph_home.append(dict(home))
            if s['kind'] == 'edit' and i in self.graph_written:
                home[s['path']] = sorted(self.graph_written[i])[-1][1]
        self.performances = {}   # develop: edit shot -> its edits.Performance, made on first use
        self.edit_texts = {}     # develop: edit shot -> (old lines' runs, new lines' runs, digits, mode)
        self.callout_text = {}   # (shot, callout, width) -> its wrapped lines, once per format
        self.minimaps = {}   # the minimap per buffer: bars and marks, computed once
        for (k, path, variant), buf in self.buffers.items():
            added, removed, ranges = self.marks.get((k, path), ((), (), ()))
            self.minimaps[k, path, variant] = editor.Minimap(editor.minimap_bars(buf.lines), tuple(ranges),
                                                             tuple(removed))
        self.maps = {}   # the treemap per main box, laid out once
        if plan.map:
            for f in formats:
                self.map_layout(stage(vb.film.as_format(f), font).main)
        self.timings = {i: P.open_timing(s['path']) for i, s in enumerate(self.shots) if s['kind'] == 'open'}
        self.film = vb.Film(self.draw, fps=self.fps, frames=self.frames, formats=formats, background=THEME.bg,
                            sounds=sounds.sounds_for(self.shots, self.shot_frames, self.fps, log=log),
                            keyframes=self.keyframes())

    def patch_info(self, k):
        """The plan's entry for patch k: its commit and files. With one patch
        (no --series, or a hand-written plan without patches) they are the
        plan's own."""
        ps = self.plan.patches or ()
        if len(ps) <= 1:
            return {**(ps[0] if ps else {}), 'commit': self.plan.commit, 'files': self.plan.files}
        return ps[k]

    def buffer(self, view):
        """The editor.Buffer `view` shows (the scratch buffer for no file)."""
        return self.buffers[view.patch, view.path, view.variant] if view.path else SCRATCH

    def performance(self, i):
        """The edit shot i's script played on its file's old text."""
        if i not in self.performances:
            s = self.shots[i]
            ch = self.patches[s.get('patch', 0)]
            old_text = ch.old_text(s['path']) or ''
            new_text = ch.text(s['path']) or ''
            old, new = edits.split_lines(old_text), edits.split_lines(new_text)
            self.performances[i] = edits.Performance(old, s['steps'])
            self.edit_texts[i] = (highlight.highlight_lines(old_text, s['path']) if old else (),
                                  highlight.highlight_lines(new_text, s['path']) if new else (),
                                  max(3, len(str(max(len(old), len(new)) + 1))), _mode(s['path']))
        return self.performances[i]

    def dive_after(self, i):
        """The path of the tile the camera dives into after map shot i, if it does."""
        nxt = self.shots[i + 1] if i + 1 < len(self.shots) else None
        return nxt['path'] if nxt is not None and nxt['kind'] == 'dive' else None

    def map_layout(self, box):
        k = tuple(box)
        if k not in self.maps:
            self.maps[k] = repomap.layout(self.plan.map or {'groups': []}, box)
        return self.maps[k]

    def _def_col(self, path, line, name, patch=0):
        """Where `name` sits on its definition line: the first whole-word match
        (a hand-written plan may lack the definition's `col`)."""
        lines = self.buffers[patch, path, 'new'].lines
        runs = lines[line - 1] if 0 < line <= len(lines) else ()
        m = re.search(rf'(?<![\w$]){re.escape(name)}(?![\w$])', ''.join(t for _, t in runs))
        return m.start() if m else 0

    def _views(self):
        """[(before, after)] View per shot."""
        out = []
        cur = View(None, 0, 0, (0, 0), ())
        hunk = None   # the hunk shot current now
        for s in self.shots:
            before, kind, k = cur, s['kind'], s.get('patch', 0)
            if kind == 'map':
                cur = View(None, 0, 0, (0, 0), cur.stack)
            elif kind in ('open', 'dive', 'travel') and self.develop:   # into the next edit, as it starts
                j = next((j for j in range(len(out) + 1, len(self.shots)) if self.shots[j]['kind'] == 'edit'), None)
                cur = View(s.get('to', s['path']), 0, 0, (0, 0), cur.stack, patch=k, variant='old', edit=j)
            elif kind == 'edit':
                cur = View(s['path'], 0, 0, (0, 0), cur.stack, patch=k, variant='new', edit=len(out), end=True)
            elif kind in ('open', 'dive', 'travel'):
                cur = View(s.get('to', s['path']), 0, 0, (0, 0), cur.stack, patch=k)
            elif kind == 'hunk':
                focus = s['focus_line'] - 1
                runs = self.buffers[k, s['path'], 'new'].lines
                col = _first_col(runs[focus]) if focus < len(runs) else 0
                grow = sum(max(0, len(g['old']) - g['new']) for g in (s.get('morph') or {}).get('segments', ()))
                cur = View(s['path'], s['top'], focus, (focus, col), cur.stack, grow, patch=k)
            elif kind == 'xref-push':
                d = s['definition']
                dl = d['line'] - 1
                entry = (s['name'], d['path'], d['line'])
                col = d['col'] if 'col' in d else self._def_col(d['path'], d['line'], s['name'], k)
                cur = View(d['path'], s['def_top'], dl, (dl, col),
                           cur.stack + (entry,), patch=k)
            elif kind == 'xref-pop':
                ln = s['line'] - 1
                cur = View(s['path'], s['back_top'], ln, (ln, s['col']), cur.stack[:-1], patch=k)
            if kind == 'hunk':
                hunk = len(out)
            elif s['scene'] == 'card' or kind == 'map':
                hunk = None
            h = self.shots[hunk] if hunk is not None else None
            if h is not None and cur.path == h['path'] and cur.patch == h.get('patch', 0) and cur.edit is None:
                cur = replace(cur, wash=hunk)   # the hunk is current until the next one: its wash stays
            out.append((before, cur))
        # A camera move into a file lands on its first hunk when that hunk has no
        # morph to open on: the pane arrives showing the lines the hunk sweeps,
        # rather than the file's top only to scroll away from it.
        for i, s in enumerate(self.shots[:-1]):
            nxt = self.shots[i + 1]
            if (s['kind'] in ('dive', 'travel') and not self.develop and nxt['kind'] == 'hunk'
                    and nxt['path'] == out[i][1].path and not nxt.get('morph')):
                land = replace(out[i + 1][1], wash=None, stack=out[i][1].stack)
                out[i] = (out[i][0], land)
                out[i + 1] = (land, out[i + 1][1])
        return out

    # ---- time ----
    def at(self, n):
        """(shot index, seconds into it) of frame n."""
        i = max(0, bisect.bisect_right(self.shot_frames, n) - 1)
        return i, (n - self.shot_frames[i]) / self.fps

    def state(self, n):
        """What the side bar shows on frame n: (current path, xref stack)."""
        i, t = self.at(n)
        before, after = self.views[i]
        kind = self.shots[i]['kind']
        switch = {'open': self.timings[i][2] if kind == 'open' else 0, 'xref-push': P.XREF_SWITCH,
                  'xref-pop': P.POP_SWITCH, 'dive': P.DIVE_SWITCH, 'travel': P.TRAVEL_SWITCH}.get(kind, 0)
        v = after if t >= switch else before
        return v.path, v.stack

    def keyframes(self):
        out, hunks, travels, seen, graphed = [], 0, 0, set(), False
        seen_edit = {}
        for i, s in enumerate(self.shots):
            f0, kind = self.shot_frames[i], s['kind']
            if kind == 'map':
                out.append((f0 + round((s['dur'] - 0.1) * self.fps), 'map'))
            elif kind == 'dive':
                out.append((f0 + round(0.65 * s['dur'] * self.fps), f'dive {s["path"]}'))
            elif kind == 'travel' and not travels:
                travels += 1
                out.append((f0 + round(0.6 * s['dur'] * self.fps), f'travel {s["to"]}'))
            elif kind == 'title':
                out.append((f0 + round(P.TITLE_STILL * self.fps), 'title'))
            elif kind == 'end':
                out.append((f0 + round((s['dur'] - 0.6) * self.fps), 'end'))
            elif kind == 'open':
                out.append((f0 + round((self.timings[i][2] + 0.3) * self.fps), f'open {s["path"]}'))
            elif kind == 'hunk':
                name = f'{s["path"]}#{s["index"] + 1}'
                if hunks < 12:
                    hunks += 1
                    t = P.scroll_time(s) + P.HUNK_SWEEP * 0.6
                    out.append((f0 + round(t * self.fps), f'hunk {name}'))
                if s.get('scroll') == 'far' and 'scroll' not in seen:
                    seen.add('scroll')
                    out.append((f0 + round(0.5 * P.HUNK_SCROLL_FAR * self.fps), f'scroll {name}'))
                cs = s.get('callouts') or ()
                if cs and 'callout' not in seen:
                    seen.add('callout')
                    out.append((f0 + round((cs[0]['at'] + P.CALLOUT_IN + 0.3) * self.fps), f'callout {name}'))
                m = s.get('morph')
                if m and 'morph' not in seen and any(g['new'] for g in m['segments']):
                    seen.add('morph')
                    out.append((f0 + round((m['at'] + 0.6 * m['dur']) * self.fps), f'morph {name}'))
                if m and 'ghost' not in seen and not any(g['new'] for g in m['segments']):
                    seen.add('ghost')
                    out.append((f0 + round((m['at'] + 0.3 * m['dur']) * self.fps), f'ghost {name}'))
            elif kind == 'edit':
                out += self._edit_keyframes(i, s, seen_edit)
            elif kind == 'patch':
                out.append((f0 + round((P.INTER_REVEAL + 0.3) * self.fps), f'interstitial {s["patch"]}'))
            elif kind == 'xref-push':
                out.append((f0 + round((P.XREF_CLICK + 0.12) * self.fps), f'xref {s["name"]} click'))
                out.append((f0 + round((P.XREF_SWITCH + P.XREF_REVEAL * 0.4) * self.fps), f'xref {s["name"]} dive'))
                if not graphed and s.get('edge') is not None:
                    graphed = True
                    r0, r1 = graphmod.route_span()
                    out.append((f0 + round((r0 + 0.6 * (r1 - r0)) * self.fps), f'graph {s["name"]} route'))
                out.append((f0 + round((P.XREF_SWITCH + P.XREF_REVEAL + P.XREF_SWEEP + 0.4) * self.fps),
                            f'xref {s["name"]} def'))
        out.sort(key=lambda kf: kf[0])   # stills numbered in the order they are seen
        return tuple((min(n, self.frames - 1), label) for n, label in out)

    def _edit_keyframes(self, i, s, seen):
        """An edit shot's key moments: typing (the longest typed step outside a
        speed-up, in the first three files), the first kill, speed-up and
        elision."""
        f0, path, clk = self.shot_frames[i], s['path'], s['clock']
        out = []

        def at(src):
            return f0 + round(edits.shot_time(src, clk) * self.fps)

        fast = [(f['at'], f['until']) for f in s['fast']]

        def calm(src):
            t = edits.shot_time(src, clk)
            return not any(a - 0.1 <= t <= b + 0.1 for a, b in fast)

        typed = [st for st in s['steps'] if st['kind'] == 'type' and calm(st['at']) and calm(st['at'] + st['dur'])]
        if typed and seen.get('typing', 0) < 3:
            seen['typing'] = seen.get('typing', 0) + 1
            st = max(typed, key=lambda x: (len(x['text']), -x['at']))
            out.append((at(st['at'] + st['times'][len(st['times']) * 2 // 3 - 1 if len(st['times']) > 1 else 0]
                           + 0.01), f'typing {path}'))
        kills = [st for st in s['steps'] if st['kind'] == 'kill' and calm(st['at'] + st['cut'])]
        if kills and 'kill' not in seen:
            seen['kill'] = 1
            out.append((at(kills[0]['at'] + kills[0]['cut'] - 0.08), f'kill {path}'))
        if fast and 'speedup' not in seen:
            seen['speedup'] = 1
            out.append((f0 + round(_peak_moment(s['fast'][0], clk) * self.fps), f'speedup {path}'))
        cs = s.get('callouts') or ()
        if cs and 'callout' not in seen:
            seen['callout'] = 1
            c = cs[0]
            out.append((f0 + round(min(c['at'] + P.CALLOUT_IN + 0.3, (c['at'] + c['until']) / 2) * self.fps),
                        f'callout {path}'))
        fills = [st for st in s['steps'] if st['kind'] == 'fill']
        if fills and 'elision' not in seen:
            seen['elision'] = 1
            st = fills[0]
            out.append((at(st['at'] + st['dur'] * edits.FILL_POUR * 0.45), f'elision {path}'))
        return out

    # ---- drawing ----
    def draw(self, n, cv):
        i, t = self.at(n)
        shot = self.shots[i]
        st = stage(cv, self.font)
        if shot['scene'] == 'card':
            SCENES['card'](self, cv, vb.Box.of(cv), shot, t, i, st)
            return
        alpha = 1.0
        if i > 0 and self.shots[i - 1]['scene'] == 'card':
            alpha = min(alpha, vb.clamp01(t / FADE))
        if i + 1 < len(self.shots) and self.shots[i + 1]['scene'] == 'card':
            alpha = min(alpha, vb.clamp01((shot['dur'] - t) / FADE))
        with cv.layer(opacity=alpha):
            SCENES[shot['scene']](self, cv, st.main, shot, t, i, st)
            if st.side is not None:
                path, stack = self.state(n)
                info = self.patch_info(shot.get('patch', 0))   # a --series side bar follows its patch
                kw = dict(commit=info['commit'], files=info['files'], current_path=path, xref_stack=stack,
                          ui=UI, mono=self.font)
                if st.compact:   # no room for the graph in 1:1
                    sidebar.draw_strip(cv, st.side, **kw)
                else:
                    graph = None
                    if self.graph_nodes:
                        def graph(c, box, i=i, t=t, path=path, stack=stack):
                            self.draw_graph(c, box, i, t, path, stack, st.scale)
                    sidebar.draw(cv, st.side, scale=st.scale, graph=graph, beside=st.side.w > st.side.h, **kw)

    def graph_part(self, patch):
        """The call graph the side bar shows during `patch` (its own, with --series)."""
        if patch not in self.graph_parts:
            self.graph_parts[patch] = graphmod.part(self.plan.graph, patch)
        return self.graph_parts[patch]

    def graph_layout(self, box, scale, patch=0):
        k = (tuple(box), scale, patch)
        if k not in self.graph_layouts:
            self.graph_layouts[k] = graphmod.layout(self.graph_part(patch), box, scale)
        return self.graph_layouts[k]

    def graph_current(self, i, path, stack, t=0.0):
        """The node on screen: the top of the xref stack, else the definition an
        edit shot is writing, else the last hunk's node in the file in view."""
        if stack:
            name, p, _ = stack[-1]
            nid = graphmod.node_id(p, name, self.shots[i].get('patch', 0))
            return nid if nid in self.graph_nodes else None
        written = [nid for at, nid in self.graph_written.get(i, ()) if at <= t]
        if written:
            return written[-1]
        return self.graph_home[i].get(path)

    def draw_graph(self, cv, box, i, t, path, stack, scale):
        k = self.shots[i].get('patch', 0)
        trail = [graphmod.node_id(p, name, k) for name, p, _ in stack]
        g = self.graph_part(k)
        state = graphmod.state_at(g, self.shots, i, t, self.graph_current(i, path, stack, t), trail)
        graphmod.draw(cv, self.graph_layout(box, scale, k), g, state, color=THEME.cursor,
                      accent=THEME.xref_box, bg=sidebar.Style().bg, scale=scale)


def _layout(st, buf):
    return editor.layout(st.main, st.mono, digits=max(3, len(str(len(buf.lines)))), minimap=True)


def _stats(plan):
    files = len(plan.files)
    added = sum(f['added'] for f in plan.files)
    deleted = sum(f['deleted'] for f in plan.files)
    return f'{files} file{"s" if files != 1 else ""}, +{added} {sidebar.MINUS}{deleted}'


def draw_card(tour, cv, box, shot, t, i, st):
    """The title and end cards, each fading in and out over 0.4 s."""
    alpha = min(vb.clamp01(t / 0.4), vb.clamp01((shot['dur'] - t) / 0.4))
    with cv.layer(opacity=alpha):
        CARD_KINDS[shot['kind']](tour, cv, box.pad(60), shot, t)


# ---- the title card ----
AVATAR_COLORS = ('#51afef', '#c678dd', '#98be65', '#da8548', '#46d9ff', '#ff6c6b')
MAX_AVATARS = 6


def initials(name):
    """Two letters for an avatar: the first letters of the first two words, or
    a one-word name's first two letters."""
    words = [w for w in name.split() if w[:1].isalnum()] or [name or '?']
    s = words[0][0] + words[1][0] if len(words) > 1 else words[0][:2]
    return s.upper()


def avatars(names):
    """(colour, initials) per co-author, at most MAX_AVATARS; past that the last
    circle counts the rest (`+N`)."""
    out = [(AVATAR_COLORS[k % len(AVATAR_COLORS)], initials(n)) for k, n in enumerate(names)]
    if len(out) > MAX_AVATARS:
        out = out[:MAX_AVATARS - 1] + [('#3f444a', f'+{len(names) - MAX_AVATARS + 1}')]
    return out


def card_text(plan):
    """What the title card says: {kicker, title, name, subtitle}."""
    c = plan.commit
    out = {'kicker': plan.repo.upper(), 'title': c['subject'], 'name': c['author'],
           'subtitle': ' · '.join(x for x in (c['short'], c['date'][:10]) if x)}
    if plan.is_range:
        commits = c.get('commits', [])
        a, b = plan.label.split('-', 1)
        n = len(commits)
        dates = [x['date'][:10] for x in commits if x.get('date')]
        span = dates[0] if dates and dates[0] == dates[-1] else (f'{dates[0]} → {dates[-1]}' if dates else '')
        authors = list(dict.fromkeys(x.get('author', '') for x in commits))
        out.update(title=f'{n} commit{"s" if n != 1 else ""}',
                   name=authors[0] if len(authors) == 1 else f'{len(authors)} authors',
                   subtitle=' · '.join(x for x in (f'{a}..{b}', span) if x))
    return out


def counter_value(t, n):
    """A counter rolling from 0 up to n over TITLE_ROLL, slowing as it lands."""
    p = vb.clamp01((t - P.TITLE_ROLL[0]) / (P.TITLE_ROLL[1] - P.TITLE_ROLL[0]))
    return n * (1 - (1 - p) ** 3)


def _title_lines(text, box_w, sizes, most, line_height=1.18):
    """(font, lines) of the title: the largest of `sizes` whose wrap fits in
    `most` lines, else the smallest with its last line ellipsized."""
    for size in sizes:
        font = vb.Font(UI, size, weight=700, line_height=line_height)
        lines = list(vbtext.wrap(text, font, box_w, balance=True))
        if len(lines) <= most and all(vbtext.measure(ln, font) <= box_w for ln in lines):
            return font, lines
    lines = lines[:most]
    lines[-1] = sidebar.ellipsize(' '.join(lines[most - 1:]) + ' …', font, box_w)
    return font, [ln if vbtext.measure(ln, font) <= box_w else sidebar.ellipsize(ln, font, box_w) for ln in lines]


def _name_bar(cv, text, t, shot, x, bottom, width, title_f, sub_f, pad=28):
    """The author's name bar (lower_third_slide_in_bar), its two lines cut to
    fit the bar so a long name never runs past its end."""
    room = width - 8 - 2 * pad
    return lower_third_bar(cv, sidebar.ellipsize(text['name'], title_f, room),
                           sidebar.ellipsize(text['subtitle'], sub_f, room), (t - P.TITLE_BAR_IN) / 0.5,
                           (t - (shot['dur'] - P.TITLE_BAR_OUT)) / 0.3, x=x, bottom=bottom, width=width,
                           title_font=title_f, subtitle_font=sub_f, color='#1c1f24', accent=THEME.cursor,
                           ink='#e6e9ef', muted='#9aa3b2', pad=pad)


def _bar_height(title_f, sub_f, pad=28):
    return pad * 2 + vbtext.line_height(title_f) + 8 + vbtext.line_height(sub_f)


def _coauthors(cv, names, t, cx, top, r, align='center', right=None):
    """The co-authors' initials as an avatar stack under a CO-AUTHORS caption,
    centred on `cx` (or ending at `right`), the caption's top at `top`."""
    a = vb.clamp01((t - P.TITLE_AVATARS) / 0.25)
    spread = vb.ease_out_back(vb.clamp01((t - P.TITLE_AVATARS) / 0.4))
    av = avatars(names)
    row_w = 2 * r * 1.12 + (len(av) - 1) * 2 * r * 1.3
    caption = vb.Font(UI, 17 if r < 30 else 20, weight=700, tracking=0.16)
    cw = vbtext.measure('CO-AUTHORS', caption)
    with cv.layer(opacity=a):
        if right is not None:
            vbtext.draw(cv, ['CO-AUTHORS'], caption, right - max(row_w, cw), top, '#9aa3b2')
            cx = right - row_w / 2
        else:
            vbtext.draw(cv, ['CO-AUTHORS'], caption, cx - cw / 2, top, '#9aa3b2')
        draw_avatar_stack(cv, av, cx, top + vbtext.line_height(caption) + 14 + r, spread, r=r, ring=THEME.bg)


def _title_card(tour, cv, box, shot, t):
    """Repo name over the subject (a two-tier kicker and title), `+a −d` rolling
    up on odometers, the author on a name bar sliding in, co-authors beside it.
    Portrait frames get a layout of their own (`_title_card_portrait`)."""
    if cv.height > cv.width:
        _title_card_portrait(tour, cv, box, shot, t)
        return
    plan, text = tour.plan, card_text(tour.plan)
    kicker_f = vb.Font(UI, 26, weight=700, tracking=0.2)
    meta_f = vb.Font(UI, 30)
    digits = vb.Font(tour.font, 44, weight=700)
    # The name bar first: the title stack is centred in the room above it.
    bar_w = min(640, box.w * 0.55)
    names = plan.commit.get('coauthors') or []
    bar_f, sub_f = vb.Font(UI, 36, weight=700), vb.Font(UI, 22)
    bar = _name_bar(cv, text, t, shot, box.x, box.bottom, bar_w, bar_f, sub_f)
    if names:
        _coauthors(cv, names, t, 0, bar.y - 4, 26, right=box.right)
    room = vb.Box(box.x, box.y, box.w, max(0.0, bar.y - box.y - 24))
    title_f, lines = _title_lines(text['title'], box.w * 0.8, (66, 58, 50), 3)
    cell_h = vbtext.line_height(digits) * 1.15
    heights = [vbtext.line_height(kicker_f), *[vbtext.line_height(title_f)] * len(lines), 10, cell_h]
    boxes, _ = room.stack(heights, gap=12, align='center')
    _title_stack(cv, box, t, text, kicker_f, title_f, lines, boxes[0], boxes[1:1 + len(lines)])
    _counters(cv, boxes[-1], plan, t, meta_f, digits, cell_h)


def _title_card_portrait(tour, cv, box, shot, t):
    """The title card laid out for a tall frame: one column centred top to
    bottom, the title as large as its length allows, the counters, then the
    name bar across the column and the co-authors under it."""
    plan, text = tour.plan, card_text(tour.plan)
    names = plan.commit.get('coauthors') or []
    kicker_f = vb.Font(UI, 34, weight=700, tracking=0.2)
    meta_f = vb.Font(UI, 40)
    digits = vb.Font(tour.font, 72, weight=700)
    title_f, lines = _title_lines(text['title'], box.w, (112, 100, 88, 76, 66, 58), 5)
    bar_f, sub_f = vb.Font(UI, 46, weight=700), vb.Font(UI, 28)
    cell_h = vbtext.line_height(digits) * 1.15
    r = 34
    co_h = (vbtext.line_height(vb.Font(UI, 20)) + 14 + 2 * r) if names else 0
    th = vbtext.line_height(title_f)
    heights = [vbtext.line_height(kicker_f), 24, *[th] * len(lines), 64, cell_h, 110,
               _bar_height(bar_f, sub_f), 56 if names else 0, co_h]
    boxes, _ = box.stack(heights, gap=0, align='center')
    kb, tbs = boxes[0], boxes[2:2 + len(lines)]
    cb, bb, ab = boxes[3 + len(lines)], boxes[5 + len(lines)], boxes[-1]
    _title_stack(cv, box, t, text, kicker_f, title_f, lines, kb, tbs)
    _counters(cv, cb, plan, t, meta_f, digits, cell_h)
    _name_bar(cv, text, t, shot, box.x, bb.bottom, box.w, bar_f, sub_f)
    if names:
        _coauthors(cv, names, t, box.cx, ab.y, r)


def _title_stack(cv, box, t, text, kicker_f, title_f, lines, kb, tbs):
    """The kicker sliding in over the title's lines rising into place."""
    k, rise = kicker_title_progress(t / 1.2)
    kicker = sidebar.ellipsize(text['kicker'], kicker_f, kb.w - 48)
    with cv.layer(opacity=k):
        vbtext.draw(cv, [kicker], kicker_f, kb.x - 24 * (1 - k), kb.y, THEME.cursor, align='center',
                    width=kb.w)
    kw = vbtext.measure(kicker, kicker_f)
    cv.mark('card.kicker', kb.cx - kw / 2, kb.y, kw, kb.h)
    for ln, b in zip(lines, tbs):
        if rise > 0:
            with cv.clip(b.x, b.y, b.w, b.h + 8):
                vbtext.draw(cv, [ln], title_f, b.x, b.y + b.h * (1 - rise), '#e6e9ef', align='center', width=b.w)
    if tbs:
        tw = max(vbtext.measure(ln, title_f) for ln in lines)
        cv.mark('card.title', box.cx - tw / 2, tbs[0].y, tw, tbs[-1].bottom - tbs[0].y)


def _counters(cv, b, plan, t, meta_f, digits, cell_h):
    """`N files  + 0024  − 0003`, the numbers rolling up on odometer wheels."""
    added = sum(f['added'] for f in plan.files)
    deleted = sum(f['deleted'] for f in plan.files)
    n = len(plan.files)
    head = f'{n} file{"s" if n != 1 else ""}'
    while True:   # the wheels shrink until the whole row fits its box
        cw = vbtext.measure('0', digits) * 1.5
        gap = cw * 0.12
        sign_f = vb.Font(digits.family, digits.size, weight=700)

        def wheels(v):
            p = len(str(v))
            return p * cw + (p - 1) * gap

        parts = [(head, meta_f, '#9aa3b2', None), ('  +', sign_f, THEME.fringe_added, None),
                 (None, None, THEME.fringe_added, added), ('  ' + sidebar.MINUS, sign_f, THEME.fringe_removed, None),
                 (None, None, THEME.fringe_removed, deleted)]
        widths = [vbtext.measure(s, f) + 8 if s else wheels(v) for s, f, _, v in parts]
        if sum(widths) <= b.w or digits.size <= 20:
            break
        digits = vb.Font(digits.family, digits.size * 0.9, weight=digits.weight)
        meta_f = vb.Font(meta_f.family, meta_f.size * 0.9)
    x = b.cx - sum(widths) / 2
    a = vb.clamp01((t - P.TITLE_ROLL[0] + 0.3) / 0.3)
    with cv.layer(opacity=a):
        for (s, f, color, v), w in zip(parts, widths):
            if s:
                vbtext.draw(cv, [s], f, x, b.y + (cell_h - vbtext.line_height(f)) / 2, color)
            else:
                draw_odometer(cv, counter_value(t, v), len(str(v)), digits, x, b.y, color, face='#1c1f24')
            x += w
    cv.mark('card.counters', b.cx - sum(widths) / 2, b.y, sum(widths), cell_h)


# ---- the closing card ----
def recap(plan):
    """(files visited, in order; definitions dived into, as (name, path, line)).
    For develop: the files edited, and the definitions written."""
    if plan.mode == 'develop':
        files = list(dict.fromkeys(s['path'] for s in plan.shots if s['kind'] == 'edit'))
        # A definition a later --series patch rewrites is listed once, at its
        # line in the last patch that wrote it.
        written = {}
        for n in (plan.graph or {}).get('nodes', ()):
            if n['touched']:
                written[n['name'], n['path']] = n['line']
        return files, [(name, path, line) for (name, path), line in written.items()]
    files, defs = [], []
    for s in plan.shots:
        if s['kind'] in ('dive', 'travel', 'open'):
            path = s.get('to', s.get('path'))
            if path not in files:
                files.append(path)
        elif s['kind'] == 'xref-push':
            d = (s['name'], s['definition']['path'], s['definition']['line'])
            if d not in defs:
                defs.append(d)
    return files, defs


def _end_slots(box, portrait, header, bodies, link, gap=16):
    """The closing card's boxes: header, visited, defs, and next (or None),
    the whole centred in `box`. Side by side like end_card_slots in landscape
    and square frames (`bodies` tall at the most); stacked in portrait, each
    panel its own height."""
    if portrait:
        total = header + sum(bodies) + gap + (gap + link if link else 0)
        b = box.anchor(box.w, min(total, box.h))
        hb = vb.Box(b.x, b.y, b.w, header - gap)
        v = vb.Box(b.x, b.y + header, b.w, bodies[0])
        d = vb.Box(b.x, v.bottom + gap, b.w, bodies[1])
        return hb, v, d, (vb.Box(b.x, d.bottom + gap, b.w, link) if link else None)
    body = max(bodies)
    total = header + body + (gap + link if link else 0)
    s = end_card_slots(box.anchor(box.w, min(total, box.h)), header=header, gap=gap, link=link, split=0.55)
    return s['header'], s['video'], s['subscribe'], (s['link'] if link else None)


def _panel(cv, b, style):
    cv.rect(*b, style.bg, radius=14)
    cv.outline(b.x + 0.5, b.y + 0.5, b.w - 1, b.h - 1, style.border, width=1.5, radius=14)


def _rows(cv, b, head, rows, F, style, draw_row, per=1):
    """A side-bar style panel: a header, then one row (`per` lines tall) per item
    while they fit, the rest as `+K more`."""
    _panel(cv, b, style)
    inner = b.pad(22, 26)
    vbtext.draw(cv, [head], F['head'], inner.x, inner.y, style.head)
    y = inner.y + vbtext.line_height(F['head']) + 12
    rh = vbtext.line_height(F['row'])
    lines = max(1, int((inner.bottom - y + 1) // rh))
    if not rows:
        vbtext.draw(cv, ['(none)'], F['row'], inner.x, y, style.muted)
        return
    shown = rows if len(rows) * per <= lines else rows[:max(0, (lines - 1) // per)]
    with cv.clip(*inner):
        for r in shown:
            draw_row(r, inner.x, y, inner.w)
            y += rh * per
        if len(shown) < len(rows):
            vbtext.draw(cv, [f'+{len(rows) - len(shown)} more'], F['row'], inner.x, y, style.muted)


def _end_card(tour, cv, box, shot, t):
    """The recap: what the tour visited and dived into, and what comes next."""
    plan, style = tour.plan, sidebar.Style()
    portrait = cv.height > cv.width
    files, defs = recap(plan)

    def sized(k):
        """The card's type and boxes with every size scaled by k."""
        F = {'head': vb.Font(UI, 19 * k, weight=700, tracking=0.14),
             'row': vb.Font(tour.font, 24 * k, line_height=1.5)}
        rh = vbtext.line_height(F['row'])
        chrome = 2 * 22 + vbtext.line_height(F['head']) + 12
        kicker = vb.Font(UI, 24 * k, weight=700, tracking=0.2)
        title = vb.Font(UI, (52 if not portrait else 46) * k, weight=700, line_height=1.15)
        lines = list(vbtext.wrap(plan.commit['subject'], title, box.w))
        if len(lines) > 2:
            lines = [lines[0], sidebar.ellipsize(' '.join(lines[1:]), title, box.w)]
        lines = [ln if vbtext.measure(ln, title) <= box.w else sidebar.ellipsize(ln, title, box.w) for ln in lines]
        header = vbtext.line_height(kicker) + 8 + len(lines) * vbtext.line_height(title) + 6 + 40 * k + 16 + 20
        link = 84 * k if plan.next else 0
        body_room = box.h - header - (16 + link if link else 0)
        cap = (body_room - 16) / 2 if portrait else body_room
        want = [chrome + max(1, n) * rh + 8 for n in (len(files), 2 * len(defs))]
        bodies = [min(w, cap) for w in want]
        if not portrait:
            bodies = [max(bodies)] * 2
        total = header + sum(bodies) + 16 + (16 + link if link else 0)
        return F, rh, kicker, title, lines, header, link, bodies, total, bodies == want

    # A tall frame scales the whole card up until it fills most of the column
    # (never past what its lists need), rather than leaving bands above and below.
    k = 1.0
    if portrait:
        for k in (1.6, 1.5, 1.4, 1.3, 1.2, 1.1, 1.0):
            *_, total, whole = sized(k)
            if whole and total <= box.h * 0.8:
                break
    F, rh, kicker, title, lines, header, link, bodies, _, _ = sized(k)
    hb, vbox, dbox, nbox = _end_slots(box, portrait, header, bodies, link)
    stats = {f['path']: f for f in plan.files}
    p = t / P.END_REVEAL

    def show(i):
        a = slot_reveal(p, i, count=4)
        return a, cv.layer(opacity=a, dy=(1 - a) * 24)

    a, layer = show(0)
    with layer:
        vbtext.draw(cv, ['END OF SESSION' if plan.mode == 'develop' else 'END OF TOUR'], kicker, hb.x, hb.y,
                    THEME.cursor)
        ty = hb.y + vbtext.line_height(kicker) + 8
        vbtext.draw(cv, lines, title, hb.x, ty, '#e6e9ef')
        sb = vb.Box(hb.x, ty + len(lines) * vbtext.line_height(title) + 6, hb.w, 40 * k)
        _stats_line(cv, sb, _stats(plan), vb.Font(tour.font, 28 * k), align='left')
    cv.mark('card.header', *hb)

    def file_row(path, x, y, w):
        f = stats.get(path, {})
        stat = sidebar.stat_text(f) if f else ''
        sw = vbtext.measure(stat, F['row'])
        vbtext.draw(cv, [sidebar.ellipsize_left(path, F['row'], w - sw - 24)], F['row'], x, y, style.fg)
        sx = x + w - sw
        if f.get('binary') or not stat:
            vbtext.draw(cv, [stat], F['row'], sx, y, style.muted)
            return
        plus, _, minus = stat.rpartition(' ')
        if plus:
            vbtext.draw(cv, [plus], F['row'], sx, y, style.added)
            sx += vbtext.measure(plus + ' ', F['row'])
        vbtext.draw(cv, [minus], F['row'], sx, y, style.removed)

    def def_row(d, x, y, w):   # the name, and under it where it is defined
        name, path, line = d
        bold = vb.Font(tour.font, 24 * k, weight=700, line_height=1.5)
        vbtext.draw(cv, [sidebar.ellipsize(name, bold, w)], bold, x, y, style.strong)
        vbtext.draw(cv, [sidebar.ellipsize_left(f'→ {path}:{line}', F['row'], w - 24)], F['row'], x + 24, y + rh,
                    style.muted)

    _, layer = show(1)
    with layer:
        _rows(cv, vbox, f'{"EDITED" if plan.mode == "develop" else "VISITED"}  {len(files)}', files, F, style,
              file_row)
    cv.mark('card.visited', *vbox)
    _, layer = show(2)
    with layer:
        _rows(cv, dbox, 'WRITTEN' if plan.mode == 'develop' else 'DIVED INTO', defs, F, style, def_row, per=2)
    cv.mark('card.defs', *dbox)
    if nbox is not None:
        _, layer = show(3)
        with layer:
            _panel(cv, nbox, style)
            inner = nbox.pad(0, 30)
            small = vb.Font(UI, 19 * k, weight=700, tracking=0.14)
            row = vb.Font(UI, 28 * k, weight=700)
            kb = vbtext.draw(cv, ['NEXT'], small, inner.x, inner.cy - vbtext.line_height(small) / 2, THEME.cursor)
            short = vb.Font(tour.font, 26 * k)
            sx = kb.right + 24
            s_box = vbtext.draw(cv, [plan.next['short']], short, sx, inner.cy - vbtext.line_height(short) / 2,
                                style.muted)
            room = inner.right - 40 - s_box.right - 16
            vbtext.draw(cv, [sidebar.ellipsize(plan.next['subject'], row, room)], row, s_box.right + 16,
                        inner.cy - vbtext.line_height(row) / 2, style.strong)
            ax, ay = inner.right - 8, inner.cy
            cv.line(ax - 10, ay - 11, ax + 1, ay, THEME.cursor, width=3.5)
            cv.line(ax + 1, ay, ax - 10, ay + 11, THEME.cursor, width=3.5)
        cv.mark('card.next', *nbox)


# ---- the card between two patches of a --series ----
DECK_CARD = 0.62    # the deck's cards, as a fraction of the frame (stacked_card_reveal's `card`)
DECK_GAP = 26       # px between two cards of the deck
DECK_DEPTH = 2      # the incoming card and one blank under it


def _patch_face(tour, cv, k):
    """Patch k's card, drawn full frame (the deck shrinks it): `PATCH k/N`
    over the commit's subject, its short sha, author and date, its size."""
    w, h = cv.width, cv.height
    portrait = h > w
    style = sidebar.Style()
    cv.fill(style.bg)
    cv.outline(3, 3, w - 6, h - 6, style.border, width=6)
    info = tour.patch_info(k)
    c = info['commit']
    n = len(tour.patches)
    box = vb.Box(0, 0, w, h).pad(w * 0.09, h * 0.1)
    kicker = vb.Font(UI, 44 if portrait else 40, weight=700, tracking=0.2)
    sub = ' · '.join(x for x in (c.get('short'), c.get('author'), (c.get('date') or '')[:10]) if x)
    # The meta line shrinks to fit before it is cut; the subject takes the
    # largest size whose wrapped lines fit the room the card has left.
    for size in (40, 36, 32, 28, 26) if not portrait else (36, 32, 28, 26):
        meta = vb.Font(UI, size)
        if vbtext.measure(sub, meta) <= box.w:
            break
    files = len(info.get('files') or ())
    stats = f'{files} file{"s" if files != 1 else ""}  +{info.get("added", 0)} {sidebar.MINUS}{info.get("deleted", 0)}'
    fixed = vbtext.line_height(kicker) + 18 + 24 + 2 * vbtext.line_height(meta)
    for size in range(92 if portrait else 84, 47, -4):
        title = vb.Font(UI, size, weight=700, line_height=1.16)
        lines = list(vbtext.wrap(c['subject'], title, box.w, balance=True))
        if fixed + len(lines) * vbtext.line_height(title) <= box.h:
            break
    rows = max(1, int((box.h - fixed) // vbtext.line_height(title)))
    if len(lines) > rows:
        lines = lines[:rows - 1] + [sidebar.ellipsize(' '.join(lines[rows - 1:]), title, box.w)]
    heights = [vbtext.line_height(kicker), 18, *[vbtext.line_height(title)] * len(lines), 24,
               vbtext.line_height(meta), vbtext.line_height(meta)]
    boxes, _ = box.stack(heights, gap=0, align='center')
    vbtext.draw(cv, [f'PATCH {k + 1} / {n}'], kicker, box.x, boxes[0].y, THEME.cursor, align='center', width=box.w)
    for ln, b in zip(lines, boxes[2:2 + len(lines)]):
        vbtext.draw(cv, [ln], title, box.x, b.y, '#e6e9ef', align='center', width=box.w)
    vbtext.draw(cv, [sidebar.ellipsize(sub, meta, box.w)], meta, box.x, boxes[-2].y, style.muted, align='center',
                width=box.w)
    vbtext.draw(cv, [stats], meta, box.x, boxes[-1].y, style.fg, align='center', width=box.w)


REACH = 0.895   # how far the strip's fill runs toward the next patch: short of ticking it (step_states)


def series_progress(k, n, t):
    """The series strip's progress `t` s into the card before patch k of n:
    the patches before k done, the fill running from patch k-1's marker up to
    patch k's as the card comes in, never reaching it (that would tick it
    off as done before it has played)."""
    e = vb.ease_in_out_quart(vb.clamp01((t - 0.2) / (P.INTER_REVEAL - 0.2)))
    return ((k - 1) + e * REACH) / max(1, n - 1)


def series_states(k, n):
    """What the strip shows for each of n patches on the card before patch k:
    `done` (filled, a tick), `current` (a larger ring, its number in it) or
    `pending` (grey, its number)."""
    return ['done' if j < k else 'current' if j == k else 'pending' for j in range(n)]


def _patch_card(tour, cv, box, shot, t):
    """The card between two patches: the patch just played is the top card of a
    deck and is flung off, the next one's card coming to the top
    (stacked_card_reveal, held with the card on the deck), over a strip of the
    series (progress_steps_connector_fill) with the patches played ticked, the
    fill running on to the next one, which is ringed as current."""
    k, n = shot['patch'], len(tour.patches)
    w, h = cv.width, cv.height
    portrait = h > w
    p = 0.75 * vb.clamp01(t / P.INTER_REVEAL)   # stop with the new card on top of the deck
    stacked_card_reveal(cv, lambda c: _patch_face(tour, c, k - 1), lambda c: _patch_face(tour, c, k), p,
                        card=DECK_CARD, depth=DECK_DEPTH, gap=DECK_GAP, background=THEME.bg,
                        blank=sidebar.Style().border)
    cw, ch = w * DECK_CARD, h * DECK_CARD
    cv.mark('card.deck', (w - cw) / 2, (h - ch) / 2, cw, ch)
    r = 22 if not portrait else 26
    labels = [tour.patch_info(j)['commit'].get('short', '') if n <= 6 or j == k else '' for j in range(n)]
    span = w * (0.5 if not portrait else 0.7) if n > 2 else w * 0.3
    x0, x1 = w / 2 - span / 2, w / 2 + span / 2
    deck_bottom = h / 2 + ch / 2 + DECK_DEPTH * DECK_GAP
    y = deck_bottom + (h - deck_bottom) * 0.42
    mono = vb.Font(tour.font, r * 0.95, weight=600)
    num_f = vb.Font(UI, r, weight=800)
    xs = draw_progress_steps(cv, x0, x1, y, series_progress(k, n, t), labels, r=r, track=6, done=THEME.cursor,
                             todo='#3f444a', ink='#e6e9ef', muted='#9aa3b2', font=num_f, label_font=mono)
    # The patch about to play: a ring, larger than the other markers, its
    # number inside (no tick: it has not played yet), its label lit.
    cx, a = xs[k], vb.clamp01((t - 0.2) / (P.INTER_REVEAL - 0.2))
    big = r * (1 + 0.3 * vb.ease_out_back(a))
    cv.arc(cx, y, big * 1.25, 0, 2 * math.pi, THEME.cursor, width=2, alpha=0.35 * a)
    cv.sector(cx, y, big, 0, 2 * math.pi, THEME.bg)
    cv.arc(cx, y, big - r * 0.14, 0, 2 * math.pi, THEME.cursor, width=r * 0.28)
    num = str(k + 1)
    nw, nh = vbtext.block_size([num], num_f)
    vbtext.draw(cv, [num], num_f, cx - nw / 2, y - nh / 2, '#e6e9ef')
    if labels[k]:
        lw, _ = vbtext.block_size([labels[k]], mono)
        vbtext.draw(cv, [labels[k]], mono, cx - lw / 2, y + r * 1.9, '#e6e9ef')
    cv.mark('card.series.current', cx - big, y - big, 2 * big, 2 * big)
    lh = vbtext.line_height(mono)
    cv.mark('card.series', x0 - r * 1.5, y - r * 1.5, x1 - x0 + 3 * r, r * 1.5 + r * 1.9 + lh)


CARD_KINDS = {'title': _title_card, 'end': _end_card, 'patch': _patch_card}


def _stats_line(cv, box, s, font, align='center'):
    head, _, rest = s.partition(', ')
    plus, _, minus = rest.partition(' ')
    parts = [(head + ', ', '#9aa3b2'), (plus + ' ', THEME.fringe_added), (minus, THEME.fringe_removed)]
    w = sum(vbtext.measure(p, font) for p, _ in parts)
    x = box.cx - w / 2 if align == 'center' else box.x
    for p, color in parts:
        vbtext.draw(cv, [p], font, x, box.y, color)
        x += vbtext.measure(p, font)


def _ease(t, a, b):
    return vb.ease_in_out_quart(vb.clamp01((t - a) / max(b - a, 1e-6)))


def draw_editor(tour, cv, box, shot, t, i, st):
    """The editor scene: one function per shot kind, all of the same shape."""
    before, after = tour.views[i]
    # Lay out in the box this scene was handed, which a later stage may make
    # smaller than the stage's main box.
    EDITOR_KINDS[shot['kind']](tour, cv, replace(st, main=box), shot, t, i, before, after)


def swept_lines(tour, st, i):
    """The lines hunk shot i sweeps in this format: its added lines in the
    view it lands on, but for the ones its morph brings in."""
    s = tour.shots[i]
    view = tour.views[i][1]
    rows = _layout(st, tour.buffer(view)).rows
    b = _fitted(tour, st, view)
    morphed = {seg.line + r for seg in tour.segments.get(i, ()) for r in range(seg.new)}
    return [n - 1 for x, y in s['added'] for n in range(x, y + 1) if b <= n - 1 < b + rows and n - 1 not in morphed]


def _pane(tour, cv, st, view, top=None, wash=True, **kw):
    """Draw the editor showing `view` (its scroll fitted to this format's rows),
    with the current hunk's swept lines still washed (`view.wash`) unless
    `wash` is False (the hunk shot sweeps them itself)."""
    if view.edit is not None:   # develop: an edit shot's buffer as it starts or ends
        s = tour.shots[view.edit]
        return _edit_pane(tour, cv, st, view.edit, s['natural'] if view.end else 0.0, **kw)[0]
    if wash and view.wash is not None:
        mine = list(kw.get('sweeps', ()))
        taken = {sw[0] for sw in mine}
        kw['sweeps'] = [(ln, 1.0, SETTLED) for ln in swept_lines(tour, st, view.wash) if ln not in taken] + mine
    buf = tour.buffer(view)
    lay = _layout(st, buf)
    if top is None:
        top = _fitted(tour, st, view)
    added, removed, tints = tour.marks.get((view.patch, view.path), ((), (), ()))
    kw.setdefault('cursor', view.cursor)
    return editor.draw(cv, lay, buf, top, marks_added=added, marks_removed_at=removed, tints=tints,
                       minimap=tour.minimaps.get((view.patch, view.path, view.variant)), theme=THEME, **kw)


def _fitted(tour, st, view):
    """The view's top fitted to this format's rows, moved on if long lines
    above its focus wrap it off the bottom."""
    buf = tour.buffer(view)
    lay = _layout(st, buf)
    return editor.keep_in_view(lay, buf.lines, fit_top(view.top, view.focus, lay.rows, len(buf.lines) + view.extra),
                               view.focus)


def _open(tour, cv, st, shot, t, i, before, after):
    keys, letters, ret, _ = tour.timings[i]
    if t >= ret:
        _pane(tour, cv, st, after, blink=t - ret)
        return
    if t < keys[0]:
        echo = ''
    elif t < keys[1]:
        echo = 'C-x-'
    elif t < P.OPEN_PROMPT:
        echo = 'C-x C-f-'
    else:
        typed = sum(1 for x in letters if x <= t)
        echo = 'Find file: ' + shot['path'][:typed]
    _pane(tour, cv, st, before, echo=echo, echo_cursor=t >= P.OPEN_PROMPT)


# A far scroll's speed ramps up, holds and ramps down (time_remapping's
# speed_ramp, called as is), the text leaning with its speed. No trailing
# copy: offset by whole fractions of a row, one read as double vision.
RAMP = (0.0, 0.35, 0.65, 1.0)   # where the speed keyframes sit, as fractions of the scroll
MOTION_DEGREES, MOTION_PER_DEGREE, MOTION_STRETCH = 1.5, 500.0, 0.015


def scroll_at(t, a, b, dur):
    """The view's top `t` s into a far scroll from `a` to `b` taking `dur`:
    speed 0 -> full -> 0 over RAMP, exactly at `b` once `dur` is up."""
    if t >= dur:
        return float(b)
    if t <= 0:
        return float(a)
    times = [x * dur for x in RAMP]
    full = 1 / (dur * (RAMP[2] - RAMP[1] + RAMP[1] / 2 + (1 - RAMP[2]) / 2))   # the area under the ramp is 1
    return a + (b - a) * min(1.0, speed_ramp(t, times, [0, full, full, 0]))


def scroll_motion(a, b, t, dur, line_h):
    """(degrees, stretch) the text leans and stretches by `t` s into a far
    scroll from `a` to `b` taking `dur`, following its speed."""
    v = scroll_velocity(lambda x: scroll_at(x, a, b, dur), t) * line_h
    return velocity_skew(v, max_degrees=MOTION_DEGREES, per_degree=MOTION_PER_DEGREE, max_stretch=MOTION_STRETCH)


def morph_progress(shot, t):
    """The hunk's morph progress at `t`: 0 (the old text) until the morph
    starts, 1 once it is over or when there is none."""
    m = shot.get('morph')
    if not m:
        return 1.0
    return vb.clamp01((t - m['at']) / m['dur'])


def focus_amount(shot, t, landed):
    """How far the rest of the buffer is dimmed `t` s into a hunk shot whose
    scroll lands at `landed`: easing in after it, out before the shot ends."""
    return _ease(t - landed, 0, P.FOCUS_IN) * (1 - _ease(t - (shot['dur'] - P.FOCUS_OUT), 0, P.FOCUS_OUT))


def _hunk(tour, cv, st, shot, t, i, before, after):
    """Scroll to the hunk; morph its replaced and removed lines from old to new;
    sweep a highlighter over the lines that are only added."""
    buf = tour.buffer(after)
    lay = _layout(st, buf)
    rows = lay.rows
    a = _fitted(tour, st, before) if before.path == after.path else 0.0
    b = _fitted(tour, st, after)
    landed = P.scroll_time(shot) if abs(a - b) > 1e-6 else 0.0   # already there: sweep at once
    motion = None
    if shot.get('scroll') == 'far':
        top = scroll_at(t, a, b, landed)
        if 0 < t < landed:
            motion = scroll_motion(a, b, t, landed, lay.line_h)
    else:
        top = vb.interpolate(t, [0, landed], [a, b], vb.ease_in_out_quart) if landed else b
    segs = tour.segments.get(i, ())
    shown = swept_lines(tour, st, i)
    p = vb.clamp01((t - landed) / P.HUNK_SWEEP)
    k = len(shown)
    sweeps = [(ln, vb.ease_in_out_quart(vb.clamp01(p * k - j)), settle(t - landed - P.HUNK_SWEEP))
              for j, ln in enumerate(shown)]
    if before.wash is not None and before.path == after.path and before.patch == after.patch:
        # the hunk before, in this file, hands over: its wash fades as the view moves on
        fade = SETTLED * (1 - vb.clamp01(t / max(landed, 1e-6)))
        taken = set(shown)
        sweeps = [(ln, 1.0, fade) for ln in swept_lines(tour, st, before.wash) if ln not in taken and fade > 0] + sweeps
    f = shot.get('focus')
    where = _pane(tour, cv, st, after, top=top, wash=False, sweeps=sweeps, blink=max(0.0, t - landed), morphs=segs,
                  morph_p=morph_progress(shot, t), focus=(f[0] - 1, f[1] - 1) if f else None,
                  focus_amount=focus_amount(shot, t, landed), motion=motion)
    for k, c in enumerate(shot.get('callouts') or ()):
        if c['at'] <= t < c['until']:
            _callout(tour, cv, st, where, shot, i, k, c, t)


# ---- commit-message callouts ----
CALLOUT_W = 620          # the card's widest, px
CALLOUT_LINES = 3        # its text's most lines; past that, an ellipsis at a word boundary
CALLOUT_BG = '#1c1f24'


def callout_fonts(st):
    size = 21 if st.compact else 22
    return (vb.Font(UI, size * 0.72, weight=700, tracking=0.16),
            vb.Font(UI, size, line_height=1.32))


def callout_lines(text, font, width):
    """The card's text: wrapped to `width`, at most CALLOUT_LINES lines, a cut
    ending with an ellipsis between two words."""
    lines = list(vbtext.wrap(text, font, width))
    if len(lines) > CALLOUT_LINES:
        lines = lines[:CALLOUT_LINES - 1] + [sidebar.ellipsize_words(' '.join(lines[CALLOUT_LINES - 1:]), font, width)]
    return lines


def callout_box(area, band, point, w, h, gap):
    """Where the card goes in `area` (the text area), right-aligned: never over
    the `point` row, and never over the `band` (the current hunk's rows)
    while there is room for it below the band (first) or above it. Only when
    the hunk fills the screen does it go over code, its own included, at the
    end of the area away from point."""
    x = area.right - w - gap
    top, bottom = max(band.y, area.y), min(band.bottom, area.bottom)
    if bottom <= top:   # the hunk is off screen: keep clear of point alone
        top, bottom = point.y, point.bottom
    below = area.bottom - bottom - gap
    above = top - area.y - gap
    if below >= h:
        y = bottom + gap
    elif above >= h:
        y = top - gap - h
    else:   # no free room: over code, at the end of the area away from point
        y = area.y + gap if point.cy > area.cy else area.bottom - gap - h
        if vb.Box(x, y, w, h).intersects(point):
            y = point.y - gap - h if point.y - area.y >= area.bottom - point.bottom else point.bottom + gap
        y = max(area.y, min(y, area.bottom - h))
    return vb.Box(x, y, w, h)


def _union(a, b):
    x0, y0 = min(a.x, b.x), min(a.y, b.y)
    return vb.Box(x0, y0, max(a.right, b.right) - x0, max(a.bottom, b.bottom) - y0)


def _text_area(lay):
    """The text area's whole rows."""
    return lay.text_box.pad(0, 0, max(0.0, lay.text_box.h - lay.rows * lay.line_h), 0)


def _callout(tour, cv, st, where, shot, i, k, c, t):
    """A hunk's commit sentence: its card beside the hunk's lit band, its line
    to the words it names (or, for a deletion, to the gap the lines left)."""
    lay = where.layout
    area = _text_area(lay)
    f0, f1 = shot['focus']
    a, b = where.line_box(f0 - 1), where.line_box(max(f0, f1) - 1)
    band = vb.Box(area.x, a.y, area.w, max(lay.line_h, b.bottom - a.y))
    if c['len']:
        target = where.word_box(c['line'] - 1, c['col'], c['len'])
    else:   # a deletion: the gap the lines left, where its − rule is
        gy = where.line_box(c['line'] - 1).y
        target = vb.Box(lay.text_box.x, gy - 2, lay.cell_w * 6, 4)
    point = where.line_box(tour.views[i][1].cursor[0])
    _callout_card(tour, cv, st, where, (i, k), c, t, target, band, point, outline=bool(c['len']))


def _callout_card(tour, cv, st, where, key, c, t, target, band, point, outline=True):
    """One commit sentence as a caption card beside the code, clear of `band`
    (the current hunk's rows) and the `point` row (see `callout_box`), a line
    drawn from it to `target` (callout_line_with_label's leader, called as
    is, with the label drawn as our card); fading in, then out before
    `until`. `key` names the callout for the wrapped-text cache."""
    lay = where.layout
    area = _text_area(lay)
    kicker_f, font = callout_fonts(st)
    band = _union(band, vb.Box(area.x, target.y, area.w, target.h))   # the words it points at stay clear too
    pad = 16 if st.compact else 18
    w = min(CALLOUT_W, area.w * 0.62)
    key = (*key, round(w))
    if key not in tour.callout_text:
        tour.callout_text[key] = callout_lines(c['text'], font, w - 2 * pad - 6)
    lines = tour.callout_text[key]
    h = 2 * pad + vbtext.line_height(kicker_f) + 6 + len(lines) * vbtext.line_height(font)
    card = callout_box(area, band, point, w, h, lay.line_h * 0.5)
    e = vb.ease_out_quint(vb.clamp01((t - c['at']) / 0.3))
    fade = vb.clamp01((c['until'] - t) / 0.25)
    run = vb.clamp01((t - c['at'] - 0.15) / (P.CALLOUT_IN - 0.15))
    with cv.layer(opacity=fade):
        if run > 0.7 and outline:
            a = vb.clamp01((run - 0.7) / 0.3)
            cv.outline(target.x - 3, target.y + 1, target.w + 6, target.h - 2, THEME.cursor, width=2.5, radius=4,
                       alpha=a)
        below = card.y > target.cy
        ax = min(max(target.cx, card.x + 24), card.right - 24)
        anchor = (ax, card.y if below else card.bottom)
        tip = (target.cx, target.bottom if below else target.y)
        n0 = len(cv.marks)
        callout_line(cv, tip, anchor, '', run, color=THEME.cursor, dot_color=THEME.cursor, width=2.5, dot=5,
                     shelf=0, gap=0)
        cv.marks[n0:] = [(f'callout.{name}', b) for name, b in cv.marks[n0:] if name != 'label']
        with cv.layer(opacity=e, dy=10 * (1 - e)):
            cv.rect(*card, CALLOUT_BG, radius=10, alpha=0.96)
            cv.outline(card.x + 0.5, card.y + 0.5, card.w - 1, card.h - 1, '#3f444a', width=1, radius=10)
            cv.rect(card.x, card.y + 10, 4, card.h - 20, THEME.cursor, radius=2)
            x, y = card.x + pad + 6, card.y + pad
            vbtext.draw(cv, [f'WHY  ·  {c["commit"]}'], kicker_f, x, y, THEME.cursor)
            y += vbtext.line_height(kicker_f) + 6
            vbtext.draw(cv, lines, font, x, y, '#e6e9ef')
    cv.mark('callout.card', *card)
    cv.mark('callout.target', *target)
    for name, b in (('callout.point', point), ('callout.hunk', band)):   # what it keeps clear of
        m = editor._clipped(b, area)
        if m is not None:
            cv.mark(name, *m)
    return card


def settle(t):
    """A swept band's strength `t` s after the sweep ends: full, then easing down to
    a light wash so the text under it stays easy to read."""
    return 1 - (1 - SETTLED) * _ease(t, 0.0, 0.6)


def _reveal(cv, box, a, b, p, center, r0):
    """circular_reveal kept to `box`: its circle is eased to cover the box's
    farthest corner (not the frame's) as p reaches 1."""
    cx, cy = center
    far = max(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 for x in (box.x, box.right) for y in (box.y, box.bottom))
    k = (far + 2 - r0) / (reach(cv, cx, cy) + 2 - r0)
    with cv.clip(*box):
        circular_reveal(cv, a, b, p, center=center, r0=r0, ease=lambda x: GROW(x) * k)
        if 0 < p < 1:   # a rim on the circle, so the opening reads even between two look-alike panes
            r = r0 + (far + 2 - r0) * GROW(vb.clamp01(p))
            cv.arc(cx, cy, r, 0, 2 * math.pi, THEME.xref_box, width=4, alpha=0.9 * (1 - vb.clamp01(p)) ** 0.5)


def _word_box(tour, st, view, line, col, n):
    """Where a word lands in the pane showing `view`, without drawing it."""
    buf = tour.buffer(view)
    return editor.Geometry(_layout(st, buf), buf.lines, _fitted(tour, st, view)).box(line, col, n)


def _xref_source(tour, cv, st, shot, t, before):
    """The pane the xref starts from: the pointer's word boxed once clicked,
    `M-.` in the echo area. Returns its Placement."""
    use_line, col, name = shot['line'] - 1, shot['col'], shot['name']
    moved = t >= P.XREF_CLICK
    cursor = (use_line, col) if moved else before.cursor
    view = replace(before, focus=use_line, cursor=cursor)
    boxes = [(use_line, col, len(name))] if moved else []
    echo = 'M-.' if t >= P.XREF_KEYS[0] else ''
    return _pane(tour, cv, st, view, boxes=boxes, echo=echo, blink=None if moved else t + 1.0)


def _xref_push(tour, cv, st, shot, t, i, before, after):
    """M-. as a dive: the pointer turns into a target over the word, the click
    ripples, and the definition's buffer opens out of the word in a growing
    circle (circular_clip_reveal)."""
    use_line, col, name = shot['line'] - 1, shot['col'], shot['name']
    if t < P.XREF_SWITCH:
        where = _xref_source(tour, cv, st, shot, t, before)
        wb = where.word_box(use_line, col, len(name))
        cv.mark('xref.word', *wb)
        if t >= P.XREF_CLICK:
            click_ripple(cv, wb.cx, wb.cy, (t - P.XREF_CLICK) / P.XREF_RIPPLE, color=THEME.xref_box,
                         reach=where.layout.line_h * 1.5, width=4)
        _pointer(cv, st.main, wb, where.layout.line_h, t)
        return
    d = after.cursor
    p = vb.clamp01((t - P.XREF_SWITCH - P.XREF_REVEAL) / P.XREF_SWEEP)
    band = (d[0], vb.ease_in_out_quart(p), settle(t - P.XREF_SWITCH - P.XREF_REVEAL - P.XREF_SWEEP))

    def definition(cv):
        _pane(tour, cv, st, after, sweeps=[band], boxes=[(d[0], d[1], len(name))], blink=t - P.XREF_SWITCH)

    q = (t - P.XREF_SWITCH) / P.XREF_REVEAL
    if q >= 1:
        definition(cv)
        return
    wb = _word_box(tour, st, replace(before, focus=use_line, cursor=(use_line, col)), use_line, col, len(name))
    _reveal(cv, st.main, lambda c: _xref_source(tour, c, st, shot, t, before), definition, q, (wb.cx, wb.cy),
            wb.h * 0.4)
    cv.mark('xref.word', *wb)


def pointer_at(box, target, t):
    """The pointer's tip at `t` s into an xref push: gliding in from the pane's
    lower right to `target`, resting through the click."""
    start = (box.right - box.w * 0.12, box.bottom - box.h * 0.18)
    pose = cursor_path([start, target], t, start=0.0, travel=P.XREF_TRAVEL, dwell=0.4, bend=0.18)
    return pose.x, pose.y


def hover_at(t, word, box, line_h):
    """How far the pointer has turned from an arrow into a target (0 -> 1) at
    `t`: by its distance to the word's box."""
    x, y = pointer_at(box, (word.cx, word.cy + line_h * 0.15), t)
    return hover_amount(x, y, word, reach=line_h * 1.5)


def _pointer(cv, box, word, line_h, t):
    """The mouse pointer gliding to the word, folding into a round target as it
    arrives (cursor_state_change), resting through the click, then fading."""
    fade = 1 - vb.clamp01((t - (P.XREF_CLICK + 0.45)) / 0.25)
    if fade <= 0:
        return
    x, y = pointer_at(box, (word.cx, word.cy + line_h * 0.15), t)
    with cv.layer(opacity=fade * 0.85):
        b = draw_state_cursor(cv, x, y, hover_at(t, word, box, line_h), color=THEME.xref_box, ink='#1c1f24',
                              radius=line_h * 0.3, size=1.5)
    cv.mark('xref.pointer', *b)


def _xref_pop(tour, cv, st, shot, t, i, before, after):
    """M-, as the dive in reverse: the definition shrinks back into the word it
    came from, in a closing circle, easing back as it goes (the zoom-out-reveal
    feel, drawn here inside the pane rather than over the whole frame)."""
    echo = 'M-,' if t >= P.POP_KEYS[0] else ''
    if t < P.POP_SWITCH:
        _pane(tour, cv, st, before, echo=echo)
        return
    fade = 1 - vb.clamp01((t - P.POP_SWITCH) / 0.5)
    word = (after.cursor[0], after.cursor[1], len(shot['name']))
    boxes = [word] if fade > 0.5 else []

    def back(cv):
        _pane(tour, cv, st, after, boxes=boxes, echo=echo if t < P.POP_SWITCH + 0.3 else '', blink=t - P.POP_SWITCH)

    q = (t - P.POP_SWITCH) / P.POP_REVEAL
    if q >= 1:
        back(cv)
        return
    wb = _word_box(tour, st, after, *word)
    cx, cy = wb.cx, wb.cy
    k = 1 - 0.15 * vb.ease_in_out_quart(q)

    def leaving(cv):
        with cv.layer(dx=cx * (1 - k), dy=cy * (1 - k), scale=k):
            _pane(tour, cv, st, before, echo=echo)

    _reveal(cv, st.main, back, leaving, 1 - q, (cx, cy), wb.h * 0.4)


# ---- develop: the performance ----
class _Runs:
    """A performance's lines as highlighted runs, looked up when drawn: a line
    from the old or the new file takes its colours from that file, lexed
    whole once; a line being edited is lexed on its own."""

    def __init__(self, lines, old, new, path):
        self.lines, self.old, self.new, self.path = lines, old, new, path

    def __len__(self):
        return len(self.lines) or 1   # an empty buffer still has the line point sits on

    def __getitem__(self, i):
        if not self.lines:
            return ()
        origin, text = self.lines[i]
        if origin is not None:
            runs = self.old if origin[0] == 'old' else self.new
            if origin[1] < len(runs):
                return runs[origin[1]]
        return highlight.highlight_lines(text, self.path)[0] if text else ()


def _changed_ranges(lines):
    """0-based (first, last) ranges of the lines not as the old file had them."""
    out = []
    for n, (origin, _) in enumerate(lines):
        if origin is None or origin[0] == 'new':
            if out and out[-1][1] == n - 1:
                out[-1][1] = n
            else:
                out.append([n, n])
    return [tuple(r) for r in out]


ECHO_PROMPT = 'Goto line: '


def _edit_pane(tour, cv, st, i, src, **kw):
    """The edit shot i's buffer at natural time `src`: point, the marked
    region, a fold still pouring, the window scrolled as Emacs would, the
    progress strip. Returns (Placement, edits.State)."""
    s = tour.shots[i]
    perf = tour.performance(i)
    old_runs, new_runs, digits, mode = tour.edit_texts[i]
    state = perf.state_at(src)
    buf = editor.Buffer(s['path'], _Runs(state.lines, old_runs, new_runs, s['path']), mode)
    lay = editor.layout(st.main, st.mono, digits=digits, minimap=True)
    scroll, top = perf.view(src, lay.rows)
    motion = None
    if scroll is not None:
        a, b, into, dur, far = scroll
        if far:
            top = scroll_at(into, a, b, dur)
            if 0 < into < dur:
                motion = scroll_motion(a, b, into, dur, lay.line_h)
        else:
            top = vb.interpolate(into, [0, dur], [a, b], vb.ease_in_out_quart)
    if state.cursor:   # long lines wrap (no sideways scroll): keep all of point's line in view
        top = editor.keep_in_view(lay, buf.lines, top, state.cursor[0])
    ranges = _changed_ranges(state.lines)
    bars = editor.minimap_bars(buf.lines[k] for k in range(len(state.lines)))
    done = s['done_before'] + state.done
    kw.setdefault('echo', state.echo)
    kw.setdefault('echo_cursor', state.echo.startswith(ECHO_PROMPT))
    kw.setdefault('blink', state.since)
    where = editor.draw(cv, lay, buf, top, cursor=state.cursor,
                        marks_added=[n for a, b in ranges for n in range(a, b + 1)],
                        marks_removed_at=state.removed,
                        minimap=editor.Minimap(bars, tuple(ranges), tuple(state.removed)),
                        region=state.region, folds=[state.fold] if state.fold else (),
                        progress=(math.floor(done + 1e-6), s['total_lines']), motion=motion, theme=THEME, **kw)
    return where, state


BADGE_BG = '#ecbe7b'
BADGE_INK = '#1c1f24'


def _speed_badge(cv, st, where, shot, t, avoid=None):
    """The fast-forward badge while a speed-up runs: `▶▶ ×N` in a pill at the
    text area's top right (its bottom right when a callout card is up there),
    fading in and out with the speed and bumping as it reaches the peak."""
    from videobuilder.capabilities.ui.badge_count_bump import bump_scale
    clk = shot['clock']
    for f in shot['fast']:
        if not f['at'] <= t <= f['until']:
            continue
        s0 = clk['speeds'][0]
        speed = edits.speed_at(t, clk)
        a = vb.clamp01((speed - s0) / max(1e-6, f['peak'] - s0))
        if a <= 0.02:
            return None
        lay = where.layout
        area = _text_area(lay)
        font = vb.Font(UI, 26 if not st.compact else 22, weight=800)
        label = f'▶▶ ×{round(f["peak"])}'
        tw = vbtext.measure(label, font)
        h = vbtext.line_height(font) * 1.35
        w = tw + h * 0.9
        pad = lay.line_h * 0.5
        x, y = area.right - w - pad, area.y + pad
        if avoid is not None and avoid.intersects(vb.Box(x, y, w, h)):
            y = area.bottom - h - pad
        k = bump_scale(t - f['times'][1])
        cx, cy = x + w / 2, y + h / 2
        with cv.layer(opacity=a, dx=cx * (1 - k), dy=cy * (1 - k), scale=k):
            cv.rect(x, y, w, h, BADGE_BG, radius=h / 2)
            vbtext.draw(cv, [label], font, x + (w - tw) / 2, y + (h - vbtext.line_height(font)) / 2, BADGE_INK)
        box = vb.Box(cx - w * k / 2, cy - h * k / 2, w * k, h * k)
        cv.mark('editor.badge', *box)
        return box
    return None


def _edit(tour, cv, st, shot, t, i, before, after):
    """Develop: the file edited from its old text to its new one, the script
    played on the shot's clock; the commit sentence of the hunk being
    written beside point, and the fast-forward badge through a speed-up."""
    where, state = _edit_pane(tour, cv, st, i, edits.source_time(t, shot['clock']))
    card = None
    for k, c in enumerate(shot.get('callouts') or ()):
        if c['at'] <= t < c['until']:
            lay = where.layout
            line, col = state.cursor
            point = where.line_box(line)
            # the hunk being written: the run of changed lines point is in
            run = next(((a, b) for a, b in _changed_ranges(state.lines) if a <= line <= b + 1), (line, line))
            first, last = where.line_box(min(run[0], line)), where.line_box(max(run[1], line))
            band = vb.Box(first.x, first.y, first.w, last.bottom - first.y)
            h = col - col % lay.cols   # point's row of its (maybe wrapped) line, from its start to point
            target = where.word_box(line, h, max(1, col - h))
            card = _callout_card(tour, cv, st, where, (i, k), c, t, target, band, point, outline=False)
    _speed_badge(cv, st, where, shot, t, avoid=card)


EDITOR_KINDS = {'open': _open, 'hunk': _hunk, 'xref-push': _xref_push, 'xref-pop': _xref_pop, 'edit': _edit}


# ---- the repo map ----
# The camera is (cx, cy, span): the map point at the box's centre and how many
# map px the box spans across, the map laid out at rest in the box itself.
def lit_amounts(shot, t):
    """{tile key: 0 -> 1} lit at `t` s into a `map` shot: the changed tiles
    light one after another in the plan's order."""
    keys = shot.get('lit', [])
    return {k: vb.clamp01((t - P.light_start(j, len(keys))) / P.MAP_LIGHT_FADE) for j, k in enumerate(keys)}


def _wide(box):
    return box.cx, box.cy, box.w


def _close(box, tile):
    """The camera framing `tile` with room around it."""
    b = tile.box
    return b.cx, b.cy, max(b.w * 2.2, b.h * 2.2 * box.w / max(box.h, 1), box.w * 0.25)


def _on_screen(box, cam, b):
    cx, cy, span = cam
    s = box.w / span
    return vb.Box(box.cx + (b.x - cx) * s, box.cy + (b.y - cy) * s, b.w * s, b.h * s)


def _lerp_box(a, b, e):
    return vb.Box(*(x + (y - x) * e for x, y in zip(a, b)))


def _map_under(tour, cv, box, lay, cam, lit, skip=(), focus=None):
    """The whole map seen through camera `cam` (the zoom-to-location idea, for a
    box instead of the frame); `focus`, the tile the camera dives into next,
    keeps its label on top."""
    cx, cy, span = cam
    s = box.w / span
    with cv.clip(*box):
        cv.rect(*box, THEME.bg)
        with cv.layer(dx=box.cx - cx * s, dy=box.cy - cy * s, scale=s):
            repomap.draw(cv, lay, lit, ui=UI, mono=tour.font, added=THEME.fringe_added,
                         removed=THEME.fringe_removed, outline=THEME.xref_box, skip=skip, focus=focus)


def _focus(tour, cv, st, box, cam, tile, view, q):
    """`tile` grown `q` (0 -> 1) of the way from where the camera shows it to
    the whole box, the editor showing `view` fading in inside it and filling the
    box exactly at q = 1."""
    if tile is None:
        _pane(tour, cv, st, view)
        return
    e = vb.ease_in_out_quart(vb.clamp01(q))
    r = _lerp_box(_on_screen(box, cam, tile.box), box, e)
    with cv.clip(*box):
        repomap.draw_tile(cv, tile, 1.0, box=r, label_alpha=1 - vb.clamp01(q / 0.4), ui=UI, mono=tour.font,
                          added=THEME.fringe_added, removed=THEME.fringe_removed, outline=THEME.xref_box)
        a = vb.clamp01((q - 0.15) / 0.35)
        if a <= 0:
            return
        k = r.w / box.w
        with cv.clip(*r), cv.layer(opacity=a, dx=r.cx - box.cx * k, dy=r.cy - box.cy * k, scale=k):
            _pane(tour, cv, st, view)


def draw_map(tour, cv, box, shot, t, i, st):
    """The repo map scene: the map itself, and the camera moves into and
    between files."""
    before, after = tour.views[i]
    if shot['kind'] == 'map':
        _map(tour, cv, replace(st, main=box), tour.map_layout(box), shot, t, before, after, focus=tour.dive_after(i))
        return
    MAP_KINDS[shot['kind']](tour, cv, replace(st, main=box), tour.map_layout(box), shot, t, before, after)


def _map(tour, cv, st, lay, shot, t, before, after, focus=None):
    _map_under(tour, cv, st.main, lay, _wide(st.main), lit_amounts(shot, t), focus=focus)


def _all_lit(lay):
    return {x.key: 1.0 for x in lay.tiles if x.heat is not None}


def _dive(tour, cv, st, lay, shot, t, before, after):
    box, tile = st.main, lay.tile(shot['path'])
    cam = _wide(box) if tile is None else fly_to(vb.ease_in_out_quart(vb.clamp01(t / P.DIVE_CAM)), _wide(box),
                                                   _close(box, tile))
    q = vb.clamp01((t - P.DIVE_MORPH[0]) / (P.DIVE_MORPH[1] - P.DIVE_MORPH[0]))
    _map_under(tour, cv, box, lay, cam, _all_lit(lay), skip=(shot['path'],))
    _focus(tour, cv, st, box, cam, tile, after, q)


def _travel(tour, cv, st, lay, shot, t, before, after):
    box = st.main
    a, b = lay.tile(shot['path']), lay.tile(shot['to'])
    if a is None or b is None:
        _pane(tour, cv, st, after if t >= P.TRAVEL_SWITCH else before)
        return
    p = vb.clamp01((t - P.TRAVEL_CAM[0]) / (P.TRAVEL_CAM[1] - P.TRAVEL_CAM[0]))
    cam = fly_to(vb.ease_in_out_quart(p), _close(box, a), _close(box, b))
    q_from = 1 - vb.clamp01(t / P.TRAVEL_SHRINK)
    q_to = vb.clamp01((t - P.TRAVEL_GROW[0]) / (P.TRAVEL_GROW[1] - P.TRAVEL_GROW[0]))
    _map_under(tour, cv, box, lay, cam, _all_lit(lay), skip=(a.key, b.key))
    if q_to > 0:
        _focus(tour, cv, st, box, cam, a, before, 0)
        _focus(tour, cv, st, box, cam, b, after, q_to)
    else:
        _focus(tour, cv, st, box, cam, b, after, 0)
        _focus(tour, cv, st, box, cam, a, before, q_from)


MAP_KINDS = {'map': _map, 'dive': _dive, 'travel': _travel}
SCENES = {'card': draw_card, 'editor': draw_editor, 'map': draw_map}


def build(plan, change, formats=FORMATS, font='monospace', log=None):
    """(vb.Film, shot_frames) for the plan."""
    tour = Tour(plan, change, formats, font, log)
    return tour.film, tour.shot_frames

"""The tour as a videobuilder Film, built from a plan.

The frame is a stage: `stage(cv)` names its boxes per format (the main box,
the side bar's), and each shot's `scene` picks the draw function that plays
in the main box (`SCENES`). Today the scenes are the title/end `card` and the
`editor`; a repo map or a call graph would be one more entry. `draw(n, cv)`
is pure: everything it shows is derived from the shot list and tables built
once here.
"""
import bisect
import re
from dataclasses import dataclass, replace

import videobuilder as vb
from videobuilder import text as vbtext
from videobuilder.capabilities.broadcast.click_ripple_indicator import click_ripple
from videobuilder.capabilities.broadcast.cursor_path_animation import CursorPose, cursor_path, draw_cursor

from . import editor, highlight, plan as P, sidebar, sounds

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


SCRATCH_TEXT = (';; This buffer is for text that is not saved, and for Lisp evaluation.\n'
                ';; To create a file, visit it with C-x C-f and enter text in its buffer.\n')
SCRATCH = editor.Buffer('*scratch*', highlight.highlight_lines(SCRATCH_TEXT, 'scratch.el'), 'Lisp Interaction')


def fit_top(top, focus, rows, n):
    """`top`, moved as little as needed so line `focus` is on screen in a window
    of `rows` and the window doesn't run past the end of the file."""
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


class Tour:
    """A plan made into a film. `film` is the vb.Film; `shot_frames[i]` is the
    first frame of shot i (where a later narration track would hook in)."""

    def __init__(self, plan, change, formats=FORMATS, font='monospace', log=None):
        for s in plan.shots:
            if s['scene'] not in SCENES:
                raise ValueError(f'unknown scene {s["scene"]!r} in a {s["kind"]!r} shot; '
                                 f'known: {", ".join(SCENES)}')
        self.plan, self.change, self.font = plan, change, font
        self.fps = plan.fps
        self.shots = plan.shots
        starts, acc = [], 0.0
        for s in self.shots:
            starts.append(round(acc * self.fps))
            acc += s['dur']
        self.shot_frames = starts
        self.frames = max(1, round(acc * self.fps))
        self.buffers, self.marks = {}, {}
        for f in change.files:
            if f.text is not None:
                self.marks[f.path] = (sorted({n - 1 for h in f.hunks for a, b in h.added for n in range(a, b + 1)}),
                                      sorted({r for h in f.hunks for r in h.removed_at}),
                                      [(a - 1, b - 1) for h in f.hunks for a, b in h.added])
        for s in self.shots:
            for path in (s.get('path'), (s.get('definition') or {}).get('path')):
                if path and path not in self.buffers:
                    text = change.text(path)
                    if text is None:
                        raise ValueError(f'{path} has no text at {change.target[:12]} to show')
                    self.buffers[path] = editor.Buffer(path, highlight.highlight_lines(text, path), _mode(path))
        self.views = self._views()
        self.timings = {i: P.open_timing(s['path']) for i, s in enumerate(self.shots) if s['kind'] == 'open'}
        self.film = vb.Film(self.draw, fps=self.fps, frames=self.frames, formats=formats, background=THEME.bg,
                            sounds=sounds.sounds_for(self.shots, self.shot_frames, self.fps, log=log),
                            keyframes=self.keyframes())

    def _def_col(self, path, line, name):
        """Where `name` sits on its definition line: the first whole-word match
        (a hand-written plan may lack the definition's `col`)."""
        runs = self.buffers[path].lines[line - 1] if 0 < line <= len(self.buffers[path].lines) else ()
        m = re.search(rf'(?<![\w$]){re.escape(name)}(?![\w$])', ''.join(t for _, t in runs))
        return m.start() if m else 0

    def _views(self):
        """[(before, after)] View per shot."""
        out = []
        cur = View(None, 0, 0, (0, 0), ())
        for s in self.shots:
            before, kind = cur, s['kind']
            if kind == 'open':
                cur = View(s['path'], 0, 0, (0, 0), cur.stack)
            elif kind == 'hunk':
                focus = s['focus_line'] - 1
                runs = self.buffers[s['path']].lines
                col = _first_col(runs[focus]) if focus < len(runs) else 0
                cur = View(s['path'], s['top'], focus, (focus, col), cur.stack)
            elif kind == 'xref-push':
                d = s['definition']
                dl = d['line'] - 1
                entry = (s['name'], d['path'], d['line'])
                col = d['col'] if 'col' in d else self._def_col(d['path'], d['line'], s['name'])
                cur = View(d['path'], s['def_top'], dl, (dl, col),
                           cur.stack + (entry,))
            elif kind == 'xref-pop':
                ln = s['line'] - 1
                cur = View(s['path'], s['back_top'], ln, (ln, s['col']), cur.stack[:-1])
            out.append((before, cur))
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
                  'xref-pop': P.POP_SWITCH}.get(kind, 0)
        v = after if t >= switch else before
        return v.path, v.stack

    def keyframes(self):
        out, hunks = [], 0
        for i, s in enumerate(self.shots):
            f0, kind = self.shot_frames[i], s['kind']
            if kind == 'title':
                out.append((f0 + round(s['dur'] / 2 * self.fps), 'title'))
            elif kind == 'end':
                out.append((f0 + round(s['dur'] / 2 * self.fps), 'end'))
            elif kind == 'open':
                out.append((f0 + round((self.timings[i][2] + 0.3) * self.fps), f'open {s["path"]}'))
            elif kind == 'hunk' and hunks < 12:
                hunks += 1
                t = P.HUNK_SCROLL + P.HUNK_SWEEP * 0.6
                out.append((f0 + round(t * self.fps), f'hunk {s["path"]}#{s["index"] + 1}'))
            elif kind == 'xref-push':
                out.append((f0 + round((P.XREF_CLICK + 0.12) * self.fps), f'xref {s["name"]} click'))
                out.append((f0 + round((P.XREF_SWITCH + P.XREF_SWEEP + 0.4) * self.fps), f'xref {s["name"]} def'))
        return tuple((min(n, self.frames - 1), label) for n, label in out)

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
                kw = dict(commit=self.plan.commit, files=self.plan.files, current_path=path, xref_stack=stack,
                          ui=UI, mono=self.font)
                if st.compact:
                    sidebar.draw_strip(cv, st.side, **kw)
                else:
                    sidebar.draw(cv, st.side, scale=st.scale, **kw)


def _layout(st, buf):
    return editor.layout(st.main, st.mono, digits=max(3, len(str(len(buf.lines)))))


def _stats(plan):
    files = len(plan.files)
    added = sum(f['added'] for f in plan.files)
    deleted = sum(f['deleted'] for f in plan.files)
    return f'{files} file{"s" if files != 1 else ""}, +{added} {sidebar.MINUS}{deleted}'


def draw_card(tour, cv, box, shot, t, i, st):
    """The title and end cards."""
    plan = tour.plan
    alpha = min(vb.clamp01(t / 0.4), vb.clamp01((shot['dur'] - t) / 0.4))
    portrait = cv.height > cv.width
    big = 64 if not portrait else 60
    width = box.w * (0.78 if not portrait else 0.84)
    title = vb.Font(UI, big, weight=700, line_height=1.18)
    meta = vb.Font(UI, 30)
    small = vb.Font(UI, 26, weight=700, tracking=0.2)
    mono = vb.Font(tour.font, 30)
    c = plan.commit
    with cv.layer(opacity=alpha):
        lines = list(vbtext.wrap(c['subject'], title, width, balance=True))[:4]
        info = ' · '.join(x for x in (c['short'], c['author'], c['date'][:10]) if x)
        kicker = 'git video · tour' if shot['kind'] == 'title' else 'end of tour'
        blocks = [(kicker.upper(), small, THEME.cursor), *((ln, title, '#e6e9ef') for ln in lines),
                  ('', meta, None), (info, meta, '#9aa3b2'), (_stats(plan), mono, None)]
        heights = [vbtext.line_height(f) for _, f, _ in blocks]
        boxes, _ = box.pad(60).stack(heights, gap=10, align='center')
        for (s, f, color), b in zip(blocks, boxes):
            if not s:
                continue
            if color is None:   # the stats line: + and − in colour, glyphs kept
                _stats_line(cv, b, s, f)
                continue
            vbtext.draw(cv, [s], f, b.x, b.y, color, align='center', width=b.w)
        cv.mark(f'card.{shot["kind"]}', *box.pad(60))


def _stats_line(cv, box, s, font):
    head, _, rest = s.partition(', ')
    plus, _, minus = rest.partition(' ')
    parts = [(head + ', ', '#9aa3b2'), (plus + ' ', THEME.fringe_added), (minus, THEME.fringe_removed)]
    w = sum(vbtext.measure(p, font) for p, _ in parts)
    x = box.cx - w / 2
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


def _pane(tour, cv, st, view, top=None, **kw):
    """Draw the editor showing `view` (its scroll fitted to this format's rows)."""
    buf = tour.buffers[view.path] if view.path else SCRATCH
    lay = _layout(st, buf)
    if top is None:
        top = fit_top(view.top, view.focus, lay.rows, len(buf.lines))
    added, removed, tints = tour.marks.get(view.path, ((), (), ()))
    kw.setdefault('cursor', view.cursor)
    return editor.draw(cv, lay, buf, top, marks_added=added, marks_removed_at=removed, tints=tints,
                       theme=THEME, **kw)


def _fitted(tour, st, view):
    buf = tour.buffers[view.path] if view.path else SCRATCH
    lay = _layout(st, buf)
    return fit_top(view.top, view.focus, lay.rows, len(buf.lines))


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


def _hunk(tour, cv, st, shot, t, i, before, after):
    buf = tour.buffers[after.path]
    rows = _layout(st, buf).rows
    a = _fitted(tour, st, before) if before.path == after.path else 0.0
    b = _fitted(tour, st, after)
    top = vb.interpolate(t, [0, P.HUNK_SCROLL], [a, b], vb.ease_in_out_quart)
    shown = [n - 1 for x, y in shot['added'] for n in range(x, y + 1) if b <= n - 1 < b + rows]
    p = vb.clamp01((t - P.HUNK_SCROLL) / P.HUNK_SWEEP)
    k = len(shown)
    sweeps = [(ln, vb.ease_in_out_quart(vb.clamp01(p * k - j)), settle(t - P.HUNK_SCROLL - P.HUNK_SWEEP))
              for j, ln in enumerate(shown)]
    _pane(tour, cv, st, after, top=top, sweeps=sweeps, blink=max(0.0, t - P.HUNK_SCROLL))


def settle(t):
    """A swept band's strength `t` s after the sweep ends: full, then easing down to
    a light wash so the text under it stays easy to read."""
    return 1 - (1 - SETTLED) * _ease(t, 0.0, 0.6)


def _xref_push(tour, cv, st, shot, t, i, before, after):
    use_line, col, name = shot['line'] - 1, shot['col'], shot['name']
    if t < P.XREF_SWITCH:
        moved = t >= P.XREF_CLICK
        view = before if not moved else View(before.path, before.top, before.focus, (use_line, col), before.stack)
        view = View(view.path, view.top, use_line, view.cursor, view.stack)
        boxes = [(use_line, col, len(name))] if moved else []
        echo = 'M-.' if t >= P.XREF_KEYS[0] else ''
        where = _pane(tour, cv, st, view, boxes=boxes, echo=echo, blink=None if moved else t + 1.0)
        wb = where.word_box(use_line, col, len(name))
        cv.mark('xref.word', *wb)
        cx, cy = wb.cx, wb.cy
        if moved:
            click_ripple(cv, cx, cy, (t - P.XREF_CLICK) / P.XREF_RIPPLE, color=THEME.xref_box,
                         reach=where.layout.line_h * 1.5, width=4)
        _pointer(cv, st.main, (cx, cy + where.layout.line_h * 0.15), t)
        return
    d = after.cursor
    p = vb.clamp01((t - P.XREF_SWITCH) / P.XREF_SWEEP)
    band = (d[0], vb.ease_in_out_quart(p), settle(t - P.XREF_SWITCH - P.XREF_SWEEP))
    _pane(tour, cv, st, after, sweeps=[band], boxes=[(d[0], d[1], len(name))],
          blink=t - P.XREF_SWITCH)


def _pointer(cv, box, target, t):
    """The mouse pointer gliding in from the pane's lower right to `target`,
    resting through the click, then fading."""
    start = (box.right - box.w * 0.12, box.bottom - box.h * 0.18)
    fade = 1 - vb.clamp01((t - (P.XREF_CLICK + 0.45)) / 0.25)
    if fade <= 0:
        return
    pose = cursor_path([start, target], t, start=0.0, travel=P.XREF_TRAVEL, dwell=0.4, bend=0.18)
    with cv.layer(opacity=fade):
        draw_cursor(cv, CursorPose(pose.x, pose.y, pose.stop, -1.0), scale=1.5, color='#ffffff', edge='#1c1f24')


def _xref_pop(tour, cv, st, shot, t, i, before, after):
    echo = 'M-,' if t >= P.POP_KEYS[0] else ''
    if t < P.POP_SWITCH:
        _pane(tour, cv, st, before, echo=echo)
        return
    fade = 1 - vb.clamp01((t - P.POP_SWITCH) / 0.5)
    boxes = [(after.cursor[0], after.cursor[1], len(shot['name']))] if fade > 0.5 else []
    _pane(tour, cv, st, after, boxes=boxes, echo=echo if t < P.POP_SWITCH + 0.3 else '', blink=t - P.POP_SWITCH)


EDITOR_KINDS = {'open': _open, 'hunk': _hunk, 'xref-push': _xref_push, 'xref-pop': _xref_pop}
SCENES = {'card': draw_card, 'editor': draw_editor}


def build(plan, change, formats=FORMATS, font='monospace', log=None):
    """(vb.Film, shot_frames) for the plan."""
    tour = Tour(plan, change, formats, font, log)
    return tour.film, tour.shot_frames

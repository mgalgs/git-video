"""The tour as a videobuilder Film, built from a plan.

The frame is a stage: `stage(cv)` names its boxes per format (the main box,
the side bar's), and each shot's `scene` picks the draw function that plays
in the main box (`SCENES`): the title/end `card`, the `editor`, and the
repo `map`, whose camera moves (`dive`, `travel`) carry the tour from the
map into a file and from file to file. A call graph would be one more
entry. `draw(n, cv)` is pure: everything it shows is derived from the shot
list and tables built once here (the treemap per format among them).
"""
import bisect
import math
import re
from dataclasses import dataclass, replace

import videobuilder as vb
from videobuilder import text as vbtext
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

from . import editor, highlight, plan as P, repomap, sidebar, sounds

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
            for path in (s.get('path'), s.get('to'), (s.get('definition') or {}).get('path')):
                if path and path not in self.buffers:
                    text = change.text(path)
                    if text is None:
                        raise ValueError(f'{path} has no text at {change.target[:12]} to show')
                    self.buffers[path] = editor.Buffer(path, highlight.highlight_lines(text, path), _mode(path))
        self.views = self._views()
        self.maps = {}   # the treemap per main box, laid out once
        if plan.map:
            for f in formats:
                self.map_layout(stage(vb.film.as_format(f), font).main)
        self.timings = {i: P.open_timing(s['path']) for i, s in enumerate(self.shots) if s['kind'] == 'open'}
        self.film = vb.Film(self.draw, fps=self.fps, frames=self.frames, formats=formats, background=THEME.bg,
                            sounds=sounds.sounds_for(self.shots, self.shot_frames, self.fps, log=log),
                            keyframes=self.keyframes())

    def map_layout(self, box):
        k = tuple(box)
        if k not in self.maps:
            self.maps[k] = repomap.layout(self.plan.map or {'groups': []}, box)
        return self.maps[k]

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
            if kind == 'map':
                cur = View(None, 0, 0, (0, 0), cur.stack)
            elif kind in ('open', 'dive', 'travel'):
                cur = View(s.get('to', s['path']), 0, 0, (0, 0), cur.stack)
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
                  'xref-pop': P.POP_SWITCH, 'dive': P.DIVE_SWITCH, 'travel': P.TRAVEL_SWITCH}.get(kind, 0)
        v = after if t >= switch else before
        return v.path, v.stack

    def keyframes(self):
        out, hunks, travels = [], 0, 0
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
            elif kind == 'hunk' and hunks < 12:
                hunks += 1
                t = P.HUNK_SCROLL + P.HUNK_SWEEP * 0.6
                out.append((f0 + round(t * self.fps), f'hunk {s["path"]}#{s["index"] + 1}'))
            elif kind == 'xref-push':
                out.append((f0 + round((P.XREF_CLICK + 0.12) * self.fps), f'xref {s["name"]} click'))
                out.append((f0 + round((P.XREF_SWITCH + P.XREF_REVEAL * 0.4) * self.fps), f'xref {s["name"]} dive'))
                out.append((f0 + round((P.XREF_SWITCH + P.XREF_REVEAL + P.XREF_SWEEP + 0.4) * self.fps),
                            f'xref {s["name"]} def'))
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


def _title_card(tour, cv, box, shot, t):
    """Repo name over the subject (a two-tier kicker and title), `+a −d` rolling
    up on odometers, the author on a name bar sliding in, co-authors beside it."""
    plan, text = tour.plan, card_text(tour.plan)
    portrait = cv.height > cv.width
    title_f = vb.Font(UI, 60 if portrait else 66, weight=700, line_height=1.18)
    kicker_f = vb.Font(UI, 26, weight=700, tracking=0.2)
    meta_f = vb.Font(UI, 30)
    digits = vb.Font(tour.font, 44, weight=700)
    # The name bar first: the title stack is centred in the room above it.
    bar_w = min(640, box.w * 0.55)
    bar = lower_third_bar(cv, text['name'], text['subtitle'], (t - P.TITLE_BAR_IN) / 0.5,
                          (t - (shot['dur'] - P.TITLE_BAR_OUT)) / 0.3, x=box.x, bottom=box.bottom, width=bar_w,
                          title_font=vb.Font(UI, 36, weight=700), subtitle_font=vb.Font(UI, 22), color='#1c1f24',
                          accent=THEME.cursor, ink='#e6e9ef', muted='#9aa3b2')
    names = plan.commit.get('coauthors') or []
    if names:
        a = vb.clamp01((t - P.TITLE_AVATARS) / 0.25)
        spread = vb.ease_out_back(vb.clamp01((t - P.TITLE_AVATARS) / 0.4))
        av = avatars(names)
        r = 26
        row_w = 2 * r * 1.12 + (len(av) - 1) * 2 * r * 1.3
        with cv.layer(opacity=a):
            caption = vb.Font(UI, 17, weight=700, tracking=0.16)
            cw = vbtext.measure('CO-AUTHORS', caption)
            vbtext.draw(cv, ['CO-AUTHORS'], caption, box.right - max(row_w, cw), bar.y - 4, '#9aa3b2')
            draw_avatar_stack(cv, av, box.right - row_w / 2, bar.y + bar.h / 2 + 10, spread, r=r, ring=THEME.bg)
    room = vb.Box(box.x, box.y, box.w, max(0.0, bar.y - box.y - 24))
    lines = list(vbtext.wrap(text['title'], title_f, box.w * (0.84 if portrait else 0.8), balance=True))
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = sidebar.ellipsize(lines[-1] + ' …', title_f, box.w * 0.84)
    k, rise = kicker_title_progress(t / 1.2)
    th = vbtext.line_height(title_f)
    cell_h = vbtext.line_height(digits) * 1.15
    heights = [vbtext.line_height(kicker_f), *[th] * len(lines), 10, cell_h]
    boxes, _ = room.stack(heights, gap=12, align='center')
    kb, tbs, cb = boxes[0], boxes[1:1 + len(lines)], boxes[-1]
    with cv.layer(opacity=k):
        vbtext.draw(cv, [text['kicker']], kicker_f, kb.x - 24 * (1 - k), kb.y, THEME.cursor, align='center',
                    width=kb.w)
    kw = vbtext.measure(text['kicker'], kicker_f)
    cv.mark('card.kicker', kb.cx - kw / 2, kb.y, kw, kb.h)
    for ln, b in zip(lines, tbs):
        if rise > 0:
            with cv.clip(b.x, b.y, b.w, b.h + 8):
                vbtext.draw(cv, [ln], title_f, b.x, b.y + b.h * (1 - rise), '#e6e9ef', align='center', width=b.w)
    if tbs:
        tw = max(vbtext.measure(ln, title_f) for ln in lines)
        cv.mark('card.title', box.cx - tw / 2, tbs[0].y, tw, tbs[-1].bottom - tbs[0].y)
    _counters(cv, cb, plan, t, meta_f, digits, cell_h)


def _counters(cv, b, plan, t, meta_f, digits, cell_h):
    """`N files  + 0024  − 0003`, the numbers rolling up on odometer wheels."""
    added = sum(f['added'] for f in plan.files)
    deleted = sum(f['deleted'] for f in plan.files)
    n = len(plan.files)
    head = f'{n} file{"s" if n != 1 else ""}'
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
    """(files visited, in order; definitions dived into, as (name, path, line))."""
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
    F = {'head': vb.Font(UI, 19, weight=700, tracking=0.14), 'row': vb.Font(tour.font, 24, line_height=1.5)}
    rh = vbtext.line_height(F['row'])
    chrome = 2 * 22 + vbtext.line_height(F['head']) + 12
    kicker = vb.Font(UI, 24, weight=700, tracking=0.2)
    title = vb.Font(UI, 52 if not portrait else 46, weight=700, line_height=1.15)
    lines = list(vbtext.wrap(plan.commit['subject'], title, box.w))
    if len(lines) > 2:
        lines = [lines[0], sidebar.ellipsize(' '.join(lines[1:]), title, box.w)]
    header = vbtext.line_height(kicker) + 8 + len(lines) * vbtext.line_height(title) + 6 + 40 + 16 + 20
    link = 84 if plan.next else 0
    body_room = box.h - header - (16 + link if link else 0)
    cap = (body_room - 16) / 2 if portrait else body_room
    bodies = [min(chrome + max(1, n) * rh + 8, cap) for n in (len(files), 2 * len(defs))]
    if not portrait:
        bodies = [max(bodies)] * 2
    hb, vbox, dbox, nbox = _end_slots(box, portrait, header, bodies, link)
    stats = {f['path']: f for f in plan.files}
    p = t / P.END_REVEAL

    def show(i):
        a = slot_reveal(p, i, count=4)
        return a, cv.layer(opacity=a, dy=(1 - a) * 24)

    a, layer = show(0)
    with layer:
        vbtext.draw(cv, ['END OF TOUR'], kicker, hb.x, hb.y, THEME.cursor)
        ty = hb.y + vbtext.line_height(kicker) + 8
        vbtext.draw(cv, lines, title, hb.x, ty, '#e6e9ef')
        sb = vb.Box(hb.x, ty + len(lines) * vbtext.line_height(title) + 6, hb.w, 40)
        _stats_line(cv, sb, _stats(plan), vb.Font(tour.font, 28), align='left')
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
        bold = vb.Font(tour.font, 24, weight=700, line_height=1.5)
        vbtext.draw(cv, [sidebar.ellipsize(name, bold, w)], bold, x, y, style.strong)
        vbtext.draw(cv, [sidebar.ellipsize_left(f'→ {path}:{line}', F['row'], w - 24)], F['row'], x + 24, y + rh,
                    style.muted)

    _, layer = show(1)
    with layer:
        _rows(cv, vbox, f'VISITED  {len(files)}', files, F, style, file_row)
    cv.mark('card.visited', *vbox)
    _, layer = show(2)
    with layer:
        _rows(cv, dbox, 'DIVED INTO', defs, F, style, def_row, per=2)
    cv.mark('card.defs', *dbox)
    if nbox is not None:
        _, layer = show(3)
        with layer:
            _panel(cv, nbox, style)
            inner = nbox.pad(0, 30)
            small = vb.Font(UI, 19, weight=700, tracking=0.14)
            row = vb.Font(UI, 28, weight=700)
            kb = vbtext.draw(cv, ['NEXT'], small, inner.x, inner.cy - vbtext.line_height(small) / 2, THEME.cursor)
            short = vb.Font(tour.font, 26)
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


CARD_KINDS = {'title': _title_card, 'end': _end_card}


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
    buf = tour.buffers[view.path] if view.path else SCRATCH
    return _layout(st, buf).word_box(line, col, n, _fitted(tour, st, view))


def _xref_source(tour, cv, st, shot, t, before):
    """The pane the xref starts from: the pointer's word boxed once clicked,
    `M-.` in the echo area. Returns its Placement."""
    use_line, col, name = shot['line'] - 1, shot['col'], shot['name']
    moved = t >= P.XREF_CLICK
    cursor = (use_line, col) if moved else before.cursor
    view = View(before.path, before.top, use_line, cursor, before.stack)
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
    wb = _word_box(tour, st, View(before.path, before.top, use_line, (use_line, col), before.stack),
                   use_line, col, len(name))
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


EDITOR_KINDS = {'open': _open, 'hunk': _hunk, 'xref-push': _xref_push, 'xref-pop': _xref_pop}


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


def _map_under(tour, cv, box, lay, cam, lit, skip=()):
    """The whole map seen through camera `cam` (the zoom-to-location idea, for a
    box instead of the frame)."""
    cx, cy, span = cam
    s = box.w / span
    with cv.clip(*box):
        cv.rect(*box, THEME.bg)
        with cv.layer(dx=box.cx - cx * s, dy=box.cy - cy * s, scale=s):
            repomap.draw(cv, lay, lit, ui=UI, mono=tour.font, added=THEME.fringe_added,
                         removed=THEME.fringe_removed, outline=THEME.xref_box, skip=skip)


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
    MAP_KINDS[shot['kind']](tour, cv, replace(st, main=box), tour.map_layout(box), shot, t, before, after)


def _map(tour, cv, st, lay, shot, t, before, after):
    _map_under(tour, cv, st.main, lay, _wide(st.main), lit_amounts(shot, t))


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

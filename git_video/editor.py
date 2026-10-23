"""An Emacs-looking editor pane, drawn into any box of any canvas.

Self-contained: it knows text, colours, a scroll position and marks, nothing
about git or tours. Give it pre-highlighted lines (runs of (colour, text) per
line, tabs already expanded) and a box:

    lay = editor.layout(vb.Box(40, 40, 1200, 1000), vb.Font('monospace', 26, line_height=1.35))
    buf = editor.Buffer('app.py', lines, 'Python')
    where = editor.draw(cv, lay, buf, scroll_top=10.5, cursor=(14, 4), sweeps=[(14, 0.6)])
    where.word_box(14, 4, 4)     # the canvas box of a word, for an overlay or a camera to aim at

Lines and columns are 0-based. `scroll_top` is the (fractional) index of the
first line at the top of the text area, so scrolling is smooth. Everything is
clipped to the box; the canvas size is never consulted.
"""
import contextlib
import math
from dataclasses import dataclass, field, replace
from typing import Sequence

import videobuilder as vb
from videobuilder import text as vbtext
from videobuilder.capabilities.broadcast.spotlight_dim import spotlight_dim
from videobuilder.capabilities.typography.blinking_cursor import cursor_on
from videobuilder.capabilities.typography.dither_reveal import dither_reveal
from videobuilder.capabilities.typography.scramble_decode import draw_scramble
from videobuilder.capabilities.ui.highlighter_swipe import highlighter_swipe
from videobuilder.capabilities.ui.scroll_progress_bar import scroll_progress
from videobuilder.capabilities.ui.scroll_velocity_skew import skewed

MINIMAP_CELLS = 3.5       # the minimap strip's width, in cells
MINIMAP_MAX_SCALE = 3.0   # px per line at the most (a short file stays a short strip)


@dataclass(frozen=True)
class Theme:
    bg: str = '#282c34'
    fg: str = '#bbc2cf'
    fringe_bg: str = '#21242b'
    line_no: str = '#4b5263'
    line_no_current: str = '#bbc2cf'
    current_line: str = '#2f343f'
    modeline_bg: str = '#1c1f24'
    modeline_fg: str = '#bbc2cf'
    modeline_accent: str = '#51afef'
    echo_fg: str = '#bbc2cf'
    fringe_added: str = '#98be65'
    fringe_removed: str = '#ff6c6b'
    tint_added: str = '#98be65'
    tint_alpha: float = 0.10
    sweep: str = '#ecbe7b'
    sweep_alpha: float = 0.30
    cursor: str = '#51afef'
    xref_box: str = '#ecbe7b'
    removed_fg: str = '#ff6c6b'
    removed_alpha: float = 0.10
    strike: str = '#ff6c6b'
    underline: str = '#98be65'
    ghost_alpha: float = 0.75
    dim: str = '#0f1115'
    dim_strength: float = 0.55   # never 1: the context stays readable
    minimap_bg: str = '#21242b'
    minimap_bar: str = '#4b5263'
    minimap_bar_changed: str = '#98be65'
    minimap_view: str = '#51afef'
    region: str = '#3e4451'      # the marked region about to be killed ...
    region_edge: str = '#ff6c6b' # ... outlined, so it reads without its colour
    fold_fg: str = '#7a8291'
    progress_done: str = '#98be65'
    progress_todo: str = '#3f444a'


@dataclass
class Buffer:
    name: str
    lines: Sequence      # per line: a sequence of (colour, text) runs
    major_mode: str = 'Fundamental'


@dataclass(frozen=True)
class Layout:
    """Where everything in the pane goes, in canvas px."""
    box: vb.Box
    font: vb.Font
    cell_w: float
    line_h: float
    rows: int
    fringe_w: float
    gutter_w: float
    text_box: vb.Box
    modeline_box: vb.Box
    echo_box: vb.Box
    minimap_box: vb.Box | None = None   # the strip at the text area's right edge, when asked for
    hscroll: int = 0                    # columns scrolled off the left edge

    @property
    def cols(self):
        """Whole text columns the text area shows."""
        return max(1, int(self.text_box.w // self.cell_w))

    def line_y(self, line, scroll_top):
        """Top of `line` when the view starts at `scroll_top`."""
        return self.text_box.y + (line - scroll_top) * self.line_h

    def col_x(self, col):
        return self.text_box.x + (col - self.hscroll) * self.cell_w

    def line_box(self, line, scroll_top):
        """The full-width text row of `line`."""
        return vb.Box(self.text_box.x, self.line_y(line, scroll_top), self.text_box.w, self.line_h)

    def word_box(self, line, col, length, scroll_top):
        return vb.Box(self.col_x(col), self.line_y(line, scroll_top), length * self.cell_w, self.line_h)

    def visible_range(self, scroll_top, n_lines):
        """The lines at least half on screen."""
        first = max(0, math.ceil(scroll_top - 0.5))
        last = min(n_lines - 1, math.floor(scroll_top + self.rows - 0.5))
        return range(first, max(first, last + 1))

    def hit(self, x, y, scroll_top):
        """(line, col) under the canvas point, or None outside the text area."""
        tb = self.text_box
        if not (tb.x <= x < tb.right and tb.y <= y < tb.bottom):
            return None
        return math.floor((y - tb.y) / self.line_h + scroll_top), math.floor((x - tb.x) / self.cell_w) + self.hscroll


def layout(box, font, digits=3, minimap=False):
    """The pane's layout in `box` with `font`; `digits` sets the line-number gutter's
    width; `minimap` reserves a MINIMAP_CELLS-wide strip at the right edge."""
    box = vb.Box(*box)
    cell = vbtext.measure('M', font)
    lh = vbtext.line_height(font)
    echo_h = round(lh * 1.25)
    mode_h = round(lh * 1.2)
    fringe = round(cell * 1.3)
    gutter = round(cell * (digits + 1.5))
    pad = round(cell * 0.6)
    text_top = box.y + round(lh * 0.25)
    mode_y = box.bottom - echo_h - mode_h
    strip = round(cell * MINIMAP_CELLS) if minimap else 0
    text_box = vb.Box(box.x + fringe + gutter + pad, text_top,
                      max(0.0, box.w - fringe - gutter - pad - strip), max(0.0, mode_y - text_top))
    rows = max(1, int(text_box.h // lh))
    mini = vb.Box(box.right - strip, box.y, strip, mode_y - box.y) if minimap else None
    return Layout(box, font, cell, lh, rows, fringe, gutter, text_box,
                  vb.Box(box.x, mode_y, box.w, mode_h), vb.Box(box.x, mode_y + mode_h, box.w, echo_h), mini)


@dataclass(frozen=True)
class Minimap:
    """The whole buffer in miniature. `bars`: per line, (start, end) of its text
    as fractions 0..1 of the strip's width; `added`: 0-based inclusive (first,
    last) line ranges marked `+`; `removed_at`: 0-based lines before which
    removed lines sat, marked `−`."""
    bars: Sequence
    added: Sequence = ()
    removed_at: Sequence = ()


def minimap_bars(lines, width=80):
    """Minimap bars for highlighted lines: each line's text from its first
    non-space column to its end, over `width` columns."""
    out = []
    for runs in lines:
        first, end = _text_extent(runs)
        out.append((min(1.0, first / width), min(1.0, end / width)) if end > first else (0.0, 0.0))
    return tuple(out)


@dataclass(frozen=True)
class Placement:
    """What one drawn frame showed, for overlays and cameras to target."""
    layout: Layout
    scroll_top: float
    visible: range
    cursor_box: vb.Box | None = field(default=None)

    def word_box(self, line, col, length):
        return self.layout.word_box(line, col, length, self.scroll_top)

    def line_box(self, line):
        return self.layout.line_box(line, self.scroll_top)


def _text_extent(runs):
    """(first non-space column, length) of a line."""
    s = ''.join(t for _, t in runs)
    body = s.rstrip()
    first = len(body) - len(body.lstrip())
    return first, len(body)


def modeline_text(buf, cursor):
    line, col = cursor if cursor else (0, 0)
    return f' -:---  {buf.name}', f'L{line + 1}:C{col}   ({buf.major_mode}) '


@dataclass(frozen=True)
class Segment:
    """A run of old lines morphing into the `new` buffer lines from 0-based
    `line` (with new == 0, the old lines sat just before `line`). `old`: runs
    per old line, like Buffer.lines; `ops`: [kind, old row, old col, new row,
    new col, text] tokens (kind `equal`, `delete` or `insert`; rows within the
    segment, -1 on the side a token is not on)."""
    line: int
    old: Sequence
    new: int
    ops: Sequence


# The morph's phases, as fractions of its one progress: removed words dissolve,
# rows and kept words glide old -> new, added words decode, underlined from UNDERLINE_AT.
# Added words wait until the glide has all but settled: drawn at their final rows,
# they would otherwise land on kept words still on their way there.
DISSOLVE = (0.0, 0.4)
GLIDE = (0.15, 0.5)
DECODE = (0.45, 1.0)
UNDERLINE_AT = 0.6


def _phase(p, span):
    a, b = span
    return vb.clamp01((p - a) / (b - a))


def segment_rows(seg, p):
    """How many rows the segment takes at morph progress `p`: its old line
    count at 0, its new one at 1."""
    e = vb.ease_in_out_quart(_phase(p, GLIDE))
    return len(seg.old) + (seg.new - len(seg.old)) * e


def _spans(tokens):
    """(row, first col, end col) per run of tokens on a row with at most a
    one-column gap between them: one strike or underline per phrase."""
    out = []
    for row, col, text in sorted(tokens):
        if out and out[-1][0] == row and col - out[-1][2] <= 1:
            out[-1][2] = max(out[-1][2], col + len(text))
        else:
            out.append([row, col, col + len(text)])
    return out


def _clipped(b, area):
    x0, y0 = max(b.x, area.x), max(b.y, area.y)
    x1, y1 = min(b.right, area.right), min(b.bottom, area.bottom)
    return vb.Box(x0, y0, x1 - x0, y1 - y0) if x1 >= x0 and y1 >= y0 else None


def draw(cv, lay, buf, scroll_top, *, cursor=None, blink=None, marks_added=(), marks_removed_at=(),
         tints=(), sweeps=(), boxes=(), modeline=None, echo='', echo_cursor=False, minimap=None,
         morphs=(), morph_p=1.0, focus=None, focus_amount=0.0, motion=None, region=None, folds=(),
         progress=None, hscroll=0, theme=Theme()):
    """Draw the pane and return its Placement.

    `cursor`: (line, col) of point, a block; `blink`: seconds since point last
    moved (None: solid). `marks_added`: lines with a `+` in the fringe;
    `marks_removed_at`: lines before which removed lines sat (a `−` and a rule
    on that boundary). `tints`: (first, last) line ranges washed in the added
    colour. `sweeps`: (line, progress[, strength]) highlighter strokes over a
    line's text, `strength` (default 1) scaling the band's alpha.
    `boxes`: (line, col, length) words outlined. `modeline`: (left, right)
    strings, default buffer name and position. `echo`: the echo-area text,
    with a cursor after it when `echo_cursor`. `minimap`: a Minimap drawn in
    the layout's strip (laid out with `minimap=True`), its viewport box
    following `scroll_top`.

    `morphs`: Segments shown at `morph_p` (0 -> 1): at 0 the old lines, removed
    words struck through (a segment with no new lines is a ghost, drawn
    faint); between, removed words dissolve, the rows and kept words glide to
    the new layout and added words decode in; at 1 the buffer as it is, added
    words underlined. Lines below a segment move with its height.

    `focus`: (first, last) lines to keep bright while the rest of the text
    area dims by `focus_amount` (0 -> 1) of `theme.dim_strength`; the band
    follows the morph (it covers any segment inside it).

    `motion`: (degrees, stretch, streak px) while the view scrolls fast: the
    text leans and stretches by the first two, and a faint copy trails
    `streak` px behind it.

    `region`: ((line, col), (line, col)) the marked region, shaded and
    outlined. `folds`: (line, count) rows drawn under `line` over what is
    there, `… count lines` in a dashed box: an elided run still pouring in.
    `progress`: (done, total) lines, as a count and a thin bar in the mode
    line. `hscroll`: columns scrolled off the left edge, as Emacs scrolls the
    window sideways to keep point in view.
    """
    if hscroll != lay.hscroll:
        lay = replace(lay, hscroll=hscroll)
    box, tb, lh, cw = lay.box, lay.text_box, lay.line_h, lay.cell_w
    n = len(buf.lines)
    p = vb.clamp01(morph_p)
    active = sorted(morphs, key=lambda s: s.line) if p < 1 else []
    tops, shifts, acc = [], [], 0.0
    for seg in active:
        tops.append(lay.line_y(seg.line, scroll_top) + acc * lh)
        acc += segment_rows(seg, p) - seg.new
        shifts.append((seg.line + seg.new, acc))
    inside = {}   # buffer line -> segment index, for the lines a segment draws itself
    for k, seg in enumerate(active):
        for ln in range(seg.line, seg.line + seg.new):
            inside[ln] = k

    def y_of(ln):
        k = inside.get(ln)
        if k is not None:
            return tops[k] + (ln - active[k].line) * lh
        d = 0.0
        for thr, cum in shifts:
            if ln >= thr:
                d = cum
        return lay.line_y(ln, scroll_top) + d * lh

    def shown(ln):   # how much of a line's decorations show: a segment's new lines arrive with its words
        return 1.0 if ln not in inside else _phase(p, DECODE)

    extra = sum(len(s.old) + s.new for s in active)
    first = max(0, math.floor(scroll_top) - extra)
    last = min(n - 1, math.ceil(scroll_top + lay.rows) + extra)
    lines = [ln for ln in range(first, last + 1) if tb.y - 2 * lh < y_of(ln) < tb.bottom + lh]
    lineset = set(lines)
    font = lay.font
    small = vb.Font(font.family, font.size * 0.95, weight=700)
    cur_line = cursor[0] if cursor else None
    cursor_box = None
    with cv.clip(*box):
        cv.rect(*box, theme.bg)
        cv.rect(box.x, box.y, lay.fringe_w, lay.modeline_box.y - box.y, theme.fringe_bg)
        with cv.clip(box.x, box.y, box.w, lay.modeline_box.y - box.y):
            if cur_line is not None and cur_line in lineset:
                cv.rect(box.x + lay.fringe_w, y_of(cur_line), box.w - lay.fringe_w, lh, theme.current_line)
            for a, b in tints:
                for ln in range(max(a, first), min(b, last) + 1):
                    if ln in lineset:
                        cv.rect(tb.x - cw * 0.3, y_of(ln), tb.w + cw * 0.3, lh, theme.tint_added,
                                alpha=theme.tint_alpha * shown(ln))
            for k, seg in enumerate(active):   # the old lines' wash, fading as they go
                h = segment_rows(seg, p) * lh
                if h > 0.5:
                    cv.rect(tb.x - cw * 0.3, tops[k], tb.w + cw * 0.3, h, theme.removed_fg,
                            alpha=theme.removed_alpha * (1 - _phase(p, GLIDE)))
            if region is not None:
                _region(cv, lay, buf, region, y_of, lineset, theme)
            for ln, sp, *rest in sweeps:
                fade = rest[0] if rest else 1.0
                if ln in lineset and sp > 0:
                    c0, c1 = _text_extent(buf.lines[ln])
                    if c1 > c0:
                        y = y_of(ln)
                        highlighter_swipe(cv, lay.col_x(c0) - cw * 0.25, y + lh * 0.1, (c1 - c0 + 0.5) * cw,
                                          lh * 0.8, sp, color=theme.sweep, alpha=theme.sweep_alpha * fade, seed=ln)
            with cv.clip(*tb.pad(0, 0, -lh, 0)), _moving(cv, tb, motion):
                if motion and motion[2]:
                    with cv.layer(opacity=0.16, dy=motion[2]):
                        for ln in lines:
                            if ln not in inside:
                                _draw_runs(cv, lay, buf.lines[ln], y_of(ln), font)
                for ln in lines:
                    if ln in inside:
                        continue
                    _draw_runs(cv, lay, buf.lines[ln], y_of(ln), font)
                for k, seg in enumerate(active):
                    _draw_segment(cv, lay, buf, seg, tops[k], p, font, theme)
                for seg in morphs:   # added words stay underlined once the morph is over
                    if p >= 1 or seg not in active:
                        ins = [(seg.line + o[3], o[4], o[5]) for o in seg.ops if o[0] == 'insert']
                        for row, c0, c1 in _spans(ins):
                            if row in lineset:
                                _underline(cv, lay, y_of(row), c0, c1, theme)
            gx = box.x + lay.fringe_w
            for ln in lines:
                num = str(ln + 1)
                vbtext.draw(cv, [num], font, gx, y_of(ln), theme.line_no_current if ln == cur_line else theme.line_no,
                            align='right', width=lay.gutter_w - cw * 0.5, alpha=shown(ln))
            added = set(marks_added)
            for ln in lines:
                if ln in added:
                    y = y_of(ln)
                    a = shown(ln)
                    cv.rect(box.x, y, cw * 0.22, lh, theme.fringe_added, alpha=a)
                    vbtext.draw(cv, ['+'], small, box.x + cw * 0.22, y + (lh - vbtext.line_height(small)) / 2,
                                theme.fringe_added, align='center', width=lay.fringe_w - cw * 0.22, alpha=a)
            seg_at = {seg.line: k for k, seg in enumerate(active)}
            for k, seg in enumerate(active):   # a − beside each old line while it is there
                a = 1 - _phase(p, DISSOLVE)
                rows = segment_rows(seg, p)
                sh = vbtext.line_height(small)
                for r in range(min(len(seg.old), math.ceil(rows))):
                    y = tops[k] + r * lh
                    vbtext.draw(cv, ['−'], small, box.x, y + (lh - sh) / 2, theme.fringe_removed, align='center',
                                width=lay.fringe_w, alpha=a)
            for ln in marks_removed_at:
                if first <= ln <= last + 1:
                    k = seg_at.get(ln)
                    a = 1.0 if k is None else _phase(p, GLIDE)
                    if a <= 0:
                        continue
                    y = y_of(ln) if k is None or active[k].new else tops[k]
                    cv.rect(box.x, y - 1.5, lay.fringe_w + lay.gutter_w, 3, theme.fringe_removed, alpha=a)
                    sh = vbtext.line_height(small)
                    cv.rect(box.x, y - sh / 2, lay.fringe_w, sh, theme.fringe_bg, alpha=a)
                    vbtext.draw(cv, ['−'], small, box.x, y - sh / 2, theme.fringe_removed,
                                align='center', width=lay.fringe_w, alpha=a)
            for ln, col, length in boxes:
                if ln in lineset:
                    y = y_of(ln)
                    b = vb.Box(lay.col_x(col), y, length * cw, lh)
                    cv.outline(b.x - 3, b.y + 1, b.w + 6, b.h - 2, theme.xref_box, width=2.5, radius=4)
            for ln, count in folds:
                _fold(cv, lay, y_of(ln) + lh, count, small, theme)
            if cursor is not None:
                cl, cc = cursor
                cursor_box = lay.word_box(cl, cc, 1, scroll_top)
                a = 1.0 if blink is None else cursor_on(blink, since=0.5)
                if a > 0 and cl in lineset and cl not in inside:
                    cb = vb.Box(cursor_box.x, y_of(cl), cursor_box.w, lh)
                    cv.rect(*cb, theme.cursor, alpha=a)
                    ch = _char_at(buf.lines[cl], cc)
                    if ch.strip():
                        vbtext.draw(cv, [ch], font, cb.x, cb.y, theme.bg, alpha=a)
        area = vb.Box(tb.x, box.y, tb.w, lay.modeline_box.y - box.y)
        if focus is not None and focus_amount > 0:
            a, b = focus
            y0, y1 = y_of(a), y_of(b) + lh
            for k, seg in enumerate(active):
                if a <= seg.line <= b + 1:
                    y0 = min(y0, tops[k])
                    y1 = max(y1, tops[k] + segment_rows(seg, p) * lh)
            if y1 - y0 < lh:   # a closed-up deletion: keep a row lit around the gap
                y0, y1 = (y0 + y1 - lh) / 2, (y0 + y1 + lh) / 2
            band = vb.Box(box.x, y0, tb.right - box.x, y1 - y0)
            with cv.clip(box.x, box.y, tb.right - box.x, lay.modeline_box.y - box.y):
                spotlight_dim(cv, band, focus_amount, color=theme.dim, strength=theme.dim_strength, radius=6,
                              pad=2, feather=lh * 0.6, steps=4)
            m = _clipped(band, vb.Box(box.x, box.y, tb.right - box.x, lay.modeline_box.y - box.y))
            if m is not None:
                cv.mark('editor.focus', *m)
        for k, seg in enumerate(active):
            m = _clipped(vb.Box(tb.x, tops[k], tb.w, max(0.0, segment_rows(seg, p) * lh)), area)
            if m is not None:
                cv.mark(f'editor.segment {k}', *m)
        cv.mark('editor.text', *tb.pad(0, 0, max(0.0, tb.h - lay.rows * lh), 0))
        if lay.minimap_box is not None:
            _minimap(cv, lay, minimap, n, scroll_top, theme)
        mb = lay.modeline_box
        cv.rect(*mb, theme.modeline_bg)
        cv.rect(mb.x, mb.y, mb.w, 1.5, theme.line_no)
        left, right = modeline if modeline is not None else modeline_text(buf, cursor)
        my = mb.y + (mb.h - lh) / 2
        bold = vb.Font(font.family, font.size, weight=700, line_height=font.line_height)
        rx = mb.right - vbtext.measure(right, font) - cw * 0.5
        if progress is not None:
            left_end = _progress(cv, lay, progress, left, bold, rx - cw * 2, my, theme)
        else:
            left_end = vbtext.draw(cv, [left], bold, mb.x + cw * 0.5, my, theme.modeline_fg).right
        vbtext.draw(cv, [right], font, max(left_end + cw * 2, rx), my, theme.modeline_accent)
        cv.mark('editor.modeline', *mb)
        eb = lay.echo_box
        ey = eb.y + (eb.h - lh) / 2
        if echo:
            vbtext.draw(cv, [echo], font, eb.x + cw * 0.5, ey, theme.echo_fg)
        if echo_cursor:
            cv.rect(eb.x + cw * (0.5 + len(echo)), ey, cw, lh, theme.cursor)
        cv.mark('editor.echo', *eb)
    return Placement(lay, scroll_top, lay.visible_range(scroll_top, n), cursor_box)


def _region(cv, lay, buf, region, y_of, lineset, theme):
    """The marked region: each row's stretch shaded, the whole outlined (a
    step at each row whose ends differ), so it reads without its colour.
    A row the region runs past ends one cell after its text, for the newline."""
    (l0, c0), (l1, c1) = sorted(region)
    if (l0, c0) == (l1, c1):
        return
    cw, lh = lay.cell_w, lay.line_h
    rows = []
    for ln in range(l0, l1 + 1):
        if ln not in lineset:
            continue
        n = len(''.join(t for _, t in buf.lines[ln])) if ln < len(buf.lines) else 0
        a = c0 if ln == l0 else 0
        b = c1 if ln == l1 else n + 1
        x0, x1 = max(lay.col_x(a), lay.text_box.x - cw * 0.3), lay.col_x(b)
        if x1 > x0:
            rows.append((x0, x1, y_of(ln)))
    if not rows:
        return
    for x0, x1, y in rows:
        cv.rect(x0, y, x1 - x0, lh, theme.region, alpha=0.95)
    w, e = 2.0, theme.region_edge
    for k, (x0, x1, y) in enumerate(rows):
        cv.line(x0, y, x0, y + lh, e, width=w)
        cv.line(x1, y, x1, y + lh, e, width=w)
        if k == 0:
            cv.line(x0, y, x1, y, e, width=w)
        else:
            px0, px1, _ = rows[k - 1]
            if px0 != x0:
                cv.line(min(px0, x0), y, max(px0, x0), y, e, width=w)
            if px1 != x1:
                cv.line(min(px1, x1), y, max(px1, x1), y, e, width=w)
        if k == len(rows) - 1:
            cv.line(x0, y + lh, x1, y + lh, e, width=w)
    top, bottom = rows[0][2], rows[-1][2] + lh
    left, right = min(r[0] for r in rows), max(r[1] for r in rows)
    m = _clipped(vb.Box(left - w, top - w, right - left + 2 * w, bottom - top + 2 * w),
                 vb.Box(lay.box.x, lay.box.y, lay.box.w, lay.modeline_box.y - lay.box.y))
    if m is not None:
        cv.mark('editor.region', *m)


def _fold(cv, lay, y, count, font, theme):
    """An elided run's fold row at `y`: `… count lines` in a dashed box over
    the row, the count falling as the lines pour in above it."""
    tb, cw, lh = lay.text_box, lay.cell_w, lay.line_h
    if y + lh <= tb.y or y >= tb.bottom:
        return
    label = f'\u2026 {count} line{"s" if count != 1 else ""}'
    w = (len(label) + 3) * cw
    box = vb.Box(tb.x, y + lh * 0.08, w, lh * 0.84)
    gx = lay.box.x + lay.fringe_w   # a row of its own: no line number shows through
    cv.rect(gx, y, tb.right - gx, lh, theme.bg)
    cv.rect(*box, theme.fringe_bg, radius=4)
    dash, gap = cw * 0.6, cw * 0.4
    x = box.x
    while x < box.right:
        x1 = min(box.right, x + dash)
        cv.line(x, box.y, x1, box.y, theme.fold_fg, width=1.5)
        cv.line(x, box.bottom, x1, box.bottom, theme.fold_fg, width=1.5)
        x += dash + gap
    cv.line(box.x, box.y, box.x, box.bottom, theme.fold_fg, width=1.5)
    cv.line(box.right, box.y, box.right, box.bottom, theme.fold_fg, width=1.5)
    vbtext.draw(cv, [label], lay.font, box.x + cw * 1.5, y, theme.fold_fg)
    m = _clipped(box, tb)
    if m is not None:
        cv.mark('editor.fold', *m)


def _progress(cv, lay, progress, left, bold, right_x, y, theme):
    """The mode line's left text (cut from the left to make room) and the lines
    done of the total, as `done/total` and a thin bar ending before `right_x`.
    Returns where it ends."""
    from .sidebar import ellipsize_left
    mb, cw = lay.modeline_box, lay.cell_w
    done, total = progress
    label = f'{done}/{total} lines'
    small = vb.Font(lay.font.family, lay.font.size * 0.85, line_height=lay.font.line_height)
    lw = vbtext.measure(label, small)
    bar_w = max(cw * 4, min(cw * 14, (right_x - mb.x) * 0.25))
    strip_w = lw + cw + bar_w
    x0 = mb.x + cw * 0.5
    room = right_x - x0 - strip_w - cw * 2
    if room < cw * 6:   # no room for the strip: the left text as it is
        return vbtext.draw(cv, [left], bold, x0, y, theme.modeline_fg).right
    lb = vbtext.draw(cv, [ellipsize_left(left, bold, room)], bold, x0, y, theme.modeline_fg)
    sx = max(lb.right + cw * 2, right_x - strip_w)
    vbtext.draw(cv, [label], small, sx, y + (vbtext.line_height(bold) - vbtext.line_height(small)) / 2,
                theme.modeline_fg)
    bx, by, bh = sx + lw + cw, mb.y + mb.h / 2 - 3, 6
    frac = done / total if total else 1.0
    cv.rect(bx, by, bar_w, bh, theme.progress_todo, radius=3)
    if frac > 0:
        cv.rect(bx, by, bar_w * min(1.0, frac), bh, theme.progress_done, radius=3)
    cv.mark('editor.progress', sx, mb.y, bx + bar_w - sx, mb.h)
    return bx + bar_w


def _moving(cv, tb, motion):
    """The text's lean and stretch while it scrolls (scroll_velocity_skew's
    `skewed`, called as is), about the text area's centre."""
    if not motion or (not motion[0] and not motion[1]):
        return contextlib.nullcontext()
    return skewed(cv, tb.cx, tb.cy, motion[0], motion[1])


def _draw_runs(cv, lay, runs, y, font):
    col = 0
    for color, s in runs:
        x = lay.col_x(col)
        if x > lay.text_box.right:
            break
        if s.strip():
            vbtext.draw(cv, [s], font, x, y, color)
        col += len(s)


def _underline(cv, lay, y, c0, c1, theme, alpha=1.0):
    uy = y + lay.line_h * 0.86
    cv.line(lay.col_x(c0), uy, lay.col_x(c1), uy, theme.underline, width=2, alpha=alpha)


SCRAMBLE_GLYPHS = 'abcdefghijklmnopqrstuvwxyz_=+*<>/#'


def _draw_segment(cv, lay, buf, seg, top, p, font, theme):
    """One segment's words at morph progress `p` (see `draw`), kept inside the
    rows the segment has at `p`: a ghost's rows close over it like a shutter, a
    growing segment opens like a drawer."""
    lh = lay.line_h
    rows = segment_rows(seg, p)
    if rows * lh < 0.5:
        return
    ge = vb.ease_in_out_quart(_phase(p, GLIDE))
    dq = _phase(p, DISSOLVE)
    iq = _phase(p, DECODE)
    ghost = seg.new == 0
    with cv.clip(lay.box.x, top, lay.box.w, rows * lh):
        for kind, orow, ocol, nrow, ncol, text in seg.ops:
            if kind != 'equal':
                continue
            y = top + (orow + (nrow - orow) * ge) * lh
            x = lay.col_x(ocol + (ncol - ocol) * ge)
            vbtext.draw(cv, [text], font, x, y, _color_at(buf.lines[seg.line + nrow], ncol))
        gone = [o for o in seg.ops if o[0] == 'delete']
        if dq < 1 and gone:
            for row in sorted({o[1] for o in gone}):
                y = top + row * lh
                words = [o for o in gone if o[1] == row]

                def put(c, words=words, y=y, row=row):
                    for _, _, ocol, _, _, text in words:
                        color = _color_at(seg.old[row], ocol) if ghost else theme.removed_fg
                        vbtext.draw(c, [text], font, lay.col_x(ocol), y, color,
                                    alpha=theme.ghost_alpha if ghost else 1.0)

                dither_reveal(cv, vb.Box(lay.text_box.x, y, lay.text_box.w, lh), 1 - dq, put,
                              cell=max(3.0, lh * 0.16))
                for _, c0, c1 in _spans([(row, o[2], o[5]) for o in words]):
                    sy = y + lh * 0.52
                    cv.line(lay.col_x(c0) - 1, sy, lay.col_x(c1) + 1, sy, theme.strike, width=2, alpha=1 - dq)
        if iq > 0:
            with cv.layer(opacity=vb.clamp01(iq / 0.2)):
                for kind, _, _, nrow, ncol, text in seg.ops:
                    if kind == 'insert':
                        draw_scramble(cv, text, font, lay.col_x(ncol), top + nrow * lh, iq,
                                      _color_at(buf.lines[seg.line + nrow], ncol), scrambled=theme.underline,
                                      seed=f'{seg.line}:{nrow}:{ncol}', glyphs=SCRAMBLE_GLYPHS)
            if p >= UNDERLINE_AT:
                ins = [(o[3], o[4], o[5]) for o in seg.ops if o[0] == 'insert']
                for row, c0, c1 in _spans(ins):
                    _underline(cv, lay, top + row * lh, c0, c1, theme, alpha=_phase(p, (UNDERLINE_AT, 0.75)))


def _minimap(cv, lay, mm, n, scroll_top, theme):
    """The strip: one bar per line (bucketed to whole px rows when lines are
    thinner than a pixel), `+` ticks beside added lines and `−` dashes where
    lines were removed, and a box over the lines on screen."""
    strip = lay.minimap_box
    cv.rect(*strip, theme.minimap_bg)
    cv.rect(strip.x, strip.y, 1, strip.h, theme.line_no, alpha=0.6)
    cv.mark('editor.minimap', *strip)
    inner = strip.pad(lay.cell_w * 0.55, lay.line_h * 0.25)
    if inner.w <= 0 or inner.h <= 0 or n <= 0:
        return
    scale = min(MINIMAP_MAX_SCALE, inner.h / n)
    bars = mm.bars if mm is not None else ()
    added = [(a, b) for a, b in (mm.added if mm is not None else ())]
    changed = set(ln for a, b in added for ln in range(a, b + 1))
    rows = {}   # px row -> (length, start, end, changed)
    for ln, (a, b) in enumerate(bars[:n]):
        if b <= a:
            continue
        y = int(ln * scale)
        cur = rows.get(y)
        if cur is None or b - a > cur[0] or (ln in changed and not cur[3]):
            rows[y] = (b - a, a, b, ln in changed or bool(cur and cur[3]))
    bh = max(1.0, 0.7 * scale)
    for y, (_, a, b, ch) in rows.items():
        cv.rect(inner.x + a * inner.w, inner.y + y, max(1.0, (b - a) * inner.w), bh,
                theme.minimap_bar_changed if ch else theme.minimap_bar, alpha=0.9 if ch else 0.8)
    tick_x = strip.x + 2
    for a, b in added:
        cv.rect(tick_x, inner.y + a * scale, 3, max(2.0, (b - a + 1) * scale), theme.fringe_added)
    for ln in (mm.removed_at if mm is not None else ()):
        cv.rect(tick_x - 1, inner.y + ln * scale - 1, 7, 2, theme.fringe_removed)
    view_h = max(6.0, min(lay.rows, n) * scale)
    y = inner.y + scroll_progress(scroll_top, n, lay.rows) * max(0.0, n - lay.rows) * scale
    y = min(y, inner.bottom - view_h)
    vbox = vb.Box(strip.x + 1.5, y, strip.w - 3, view_h)
    cv.rect(*vbox, theme.minimap_view, alpha=0.15)
    cv.outline(*vbox, theme.minimap_view, width=1.5, radius=2)
    cv.mark('editor.viewport', *vbox)


def _color_at(runs, col):
    """The colour of the run holding column `col` of a line."""
    c = 0
    for color, s in runs:
        if col < c + len(s):
            return color
        c += len(s)
    return runs[-1][0] if runs else Theme.fg


def _char_at(runs, col):
    c = 0
    for _, s in runs:
        if col < c + len(s):
            return s[col - c]
        c += len(s)
    return ' '

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
import math
from dataclasses import dataclass, field
from typing import Sequence

import videobuilder as vb
from videobuilder import text as vbtext
from videobuilder.capabilities.typography.blinking_cursor import cursor_on
from videobuilder.capabilities.ui.highlighter_swipe import highlighter_swipe


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

    def line_y(self, line, scroll_top):
        """Top of `line` when the view starts at `scroll_top`."""
        return self.text_box.y + (line - scroll_top) * self.line_h

    def col_x(self, col):
        return self.text_box.x + col * self.cell_w

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
        return math.floor((y - tb.y) / self.line_h + scroll_top), math.floor((x - tb.x) / self.cell_w)


def layout(box, font, digits=3):
    """The pane's layout in `box` with `font`; `digits` sets the line-number gutter's width."""
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
    text_box = vb.Box(box.x + fringe + gutter + pad, text_top,
                      max(0.0, box.w - fringe - gutter - pad), max(0.0, mode_y - text_top))
    rows = max(1, int(text_box.h // lh))
    return Layout(box, font, cell, lh, rows, fringe, gutter, text_box,
                  vb.Box(box.x, mode_y, box.w, mode_h), vb.Box(box.x, mode_y + mode_h, box.w, echo_h))


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


def draw(cv, lay, buf, scroll_top, *, cursor=None, blink=None, marks_added=(), marks_removed_at=(),
         tints=(), sweeps=(), boxes=(), modeline=None, echo='', echo_cursor=False, theme=Theme()):
    """Draw the pane and return its Placement.

    `cursor`: (line, col) of point, a block; `blink`: seconds since point last
    moved (None: solid). `marks_added`: lines with a `+` in the fringe;
    `marks_removed_at`: lines before which removed lines sat (a `−` and a rule
    on that boundary). `tints`: (first, last) line ranges washed in the added
    colour. `sweeps`: (line, progress[, strength]) highlighter strokes over a
    line's text, `strength` (default 1) scaling the band's alpha.
    `boxes`: (line, col, length) words outlined. `modeline`: (left, right)
    strings, default buffer name and position. `echo`: the echo-area text,
    with a cursor after it when `echo_cursor`.
    """
    box, tb, lh, cw = lay.box, lay.text_box, lay.line_h, lay.cell_w
    n = len(buf.lines)
    first = max(0, math.floor(scroll_top))
    last = min(n - 1, math.ceil(scroll_top + lay.rows))
    lines = range(first, last + 1)
    font = lay.font
    small = vb.Font(font.family, font.size * 0.95, weight=700)
    cur_line = cursor[0] if cursor else None
    cursor_box = None
    with cv.clip(*box):
        cv.rect(*box, theme.bg)
        cv.rect(box.x, box.y, lay.fringe_w, lay.modeline_box.y - box.y, theme.fringe_bg)
        with cv.clip(box.x, box.y, box.w, lay.modeline_box.y - box.y):
            if cur_line is not None and cur_line in lines:
                y = lay.line_y(cur_line, scroll_top)
                cv.rect(box.x + lay.fringe_w, y, box.w - lay.fringe_w, lh, theme.current_line)
            for a, b in tints:
                for ln in range(max(a, first), min(b, last) + 1):
                    cv.rect(tb.x - cw * 0.3, lay.line_y(ln, scroll_top), tb.w + cw * 0.3, lh, theme.tint_added,
                            alpha=theme.tint_alpha)
            for ln, p, *rest in sweeps:
                fade = rest[0] if rest else 1.0
                if ln in lines and p > 0:
                    c0, c1 = _text_extent(buf.lines[ln])
                    if c1 > c0:
                        y = lay.line_y(ln, scroll_top)
                        highlighter_swipe(cv, lay.col_x(c0) - cw * 0.25, y + lh * 0.1, (c1 - c0 + 0.5) * cw,
                                          lh * 0.8, p, color=theme.sweep, alpha=theme.sweep_alpha * fade, seed=ln)
            with cv.clip(*tb.pad(0, 0, -lh, 0)):
                for ln in lines:
                    y = lay.line_y(ln, scroll_top)
                    col = 0
                    for color, s in buf.lines[ln]:
                        x = lay.col_x(col)
                        if x > tb.right:
                            break
                        if s.strip():
                            vbtext.draw(cv, [s], font, x, y, color)
                        col += len(s)
            gx = box.x + lay.fringe_w
            for ln in lines:
                num = str(ln + 1)
                y = lay.line_y(ln, scroll_top)
                vbtext.draw(cv, [num], font, gx, y, theme.line_no_current if ln == cur_line else theme.line_no,
                            align='right', width=lay.gutter_w - cw * 0.5)
            added = set(marks_added)
            for ln in lines:
                if ln in added:
                    y = lay.line_y(ln, scroll_top)
                    cv.rect(box.x, y, cw * 0.22, lh, theme.fringe_added)
                    vbtext.draw(cv, ['+'], small, box.x + cw * 0.22, y + (lh - vbtext.line_height(small)) / 2,
                                theme.fringe_added, align='center', width=lay.fringe_w - cw * 0.22)
            for ln in marks_removed_at:
                if first <= ln <= last + 1:
                    y = lay.line_y(ln, scroll_top)
                    cv.rect(box.x, y - 1.5, lay.fringe_w + lay.gutter_w, 3, theme.fringe_removed)
                    sh = vbtext.line_height(small)
                    cv.rect(box.x, y - sh / 2, lay.fringe_w, sh, theme.fringe_bg)
                    vbtext.draw(cv, ['−'], small, box.x, y - sh / 2, theme.fringe_removed,
                                align='center', width=lay.fringe_w)
            for ln, col, length in boxes:
                if ln in lines:
                    b = lay.word_box(ln, col, length, scroll_top)
                    cv.outline(b.x - 3, b.y + 1, b.w + 6, b.h - 2, theme.xref_box, width=2.5, radius=4)
            if cursor is not None:
                cl, cc = cursor
                cursor_box = lay.word_box(cl, cc, 1, scroll_top)
                a = 1.0 if blink is None else cursor_on(blink, since=0.5)
                if a > 0 and cl in lines:
                    cv.rect(*cursor_box, theme.cursor, alpha=a)
                    ch = _char_at(buf.lines[cl], cc)
                    if ch.strip():
                        vbtext.draw(cv, [ch], font, cursor_box.x, cursor_box.y, theme.bg, alpha=a)
        cv.mark('editor.text', *tb.pad(0, 0, max(0.0, tb.h - lay.rows * lh), 0))
        mb = lay.modeline_box
        cv.rect(*mb, theme.modeline_bg)
        cv.rect(mb.x, mb.y, mb.w, 1.5, theme.line_no)
        left, right = modeline if modeline is not None else modeline_text(buf, cursor)
        my = mb.y + (mb.h - lh) / 2
        bold = vb.Font(font.family, font.size, weight=700, line_height=font.line_height)
        lb = vbtext.draw(cv, [left], bold, mb.x + cw * 0.5, my, theme.modeline_fg)
        rw = vbtext.measure(right, font)
        vbtext.draw(cv, [right], font, max(lb.right + cw * 2, mb.right - rw - cw * 0.5), my, theme.modeline_accent)
        cv.mark('editor.modeline', *mb)
        eb = lay.echo_box
        ey = eb.y + (eb.h - lh) / 2
        if echo:
            vbtext.draw(cv, [echo], font, eb.x + cw * 0.5, ey, theme.echo_fg)
        if echo_cursor:
            cv.rect(eb.x + cw * (0.5 + len(echo)), ey, cw, lh, theme.cursor)
        cv.mark('editor.echo', *eb)
    return Placement(lay, scroll_top, lay.visible_range(scroll_top, n), cursor_box)


def _char_at(runs, col):
    c = 0
    for _, s in runs:
        if col < c + len(s):
            return s[col - c]
        c += len(s)
    return ' '

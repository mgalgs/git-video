"""The side bar beside (or under) the editor: the commit, the changed files,
the call graph and the xref stack, for orientation. The current file is marked with `▶` and
weight, not only colour."""
from dataclasses import dataclass
import functools
import itertools

import videobuilder as vb
from videobuilder import text as vbtext

MARK = '▶'   # ▶
MINUS = '−'  # −


@dataclass(frozen=True)
class Style:
    bg: str = '#21242b'
    border: str = '#3f444a'
    head: str = '#51afef'
    fg: str = '#bbc2cf'
    muted: str = '#7a8291'
    strong: str = '#e6e9ef'
    added: str = '#98be65'
    removed: str = '#ff6c6b'
    current_bg: str = '#2f343f'


def stat_text(f):
    """`+a −d` for a file entry (a dict with added, deleted, binary, status)."""
    if f.get('binary'):
        return 'binary'
    if f.get('status') == 'D':
        return f'{MINUS}{f["deleted"]}'
    return f'+{f["added"]} {MINUS}{f["deleted"]}'


def ellipsize_left(s, font, width):
    if vbtext.measure(s, font) <= width:
        return s
    while s and vbtext.measure('…' + s, font) > width:
        s = s[1:]
    return '…' + s


def ellipsize(s, font, width):
    if vbtext.measure(s, font) <= width:
        return s
    while s and vbtext.measure(s + '…', font) > width:
        s = s[:-1]
    return s.rstrip() + '…'


def ellipsize_words(s, font, width):
    """`s` cut at a word boundary with an ellipsis so it fits `width`; never
    inside a word (a single word too wide is cut as `ellipsize` would)."""
    if vbtext.measure(s, font) <= width:
        return s
    words = s.split()
    while len(words) > 1:
        words.pop()
        out = ' '.join(words).rstrip(',;:—-') + '…'
        if vbtext.measure(out, font) <= width:
            return out
    return ellipsize(s, font, width)


def _greedy(words, font, width):
    lines, cur = [], ''
    for w in words:
        t = f'{cur} {w}' if cur else w
        if cur and vbtext.measure(t, font) > width:
            lines.append(cur)
            cur = w
        else:
            cur = t
    return lines + [cur] if cur else lines


@functools.lru_cache(maxsize=512)
def wrap_words(text, font, width, balance=False):
    """Lines of `text` no wider than `width`, broken only at spaces: never
    inside a word, so never at a hyphen and never with one added. None when a
    word alone is wider. `balance`: as many lines as greedy wrapping, at the
    narrowest width that keeps them (CSS text-wrap: balance)."""
    words = text.split()
    if not words:
        return ()
    if max(vbtext.measure(w, font) for w in words) > width:
        return None
    lines = _greedy(words, font, width)
    if balance and len(lines) > 1:
        lo, hi = 0.0, float(width)
        while hi - lo > 0.5:
            mid = (lo + hi) / 2
            if any(vbtext.measure(w, font) > mid for w in words) or len(_greedy(words, font, mid)) > len(lines):
                lo = mid
            else:
                hi = mid
        lines = _greedy(words, font, hi)
    return tuple(lines)


def fit_words(text, fonts, width, fits, balance=False):
    """(font, lines) for a card's title: `text` wrapped at spaces onto as many
    lines as `fits(font, n)` allows, in the first (largest) of `fonts` where it
    fits whole. Failing that, in the last font, cut to the most lines that fit
    (at least one), the last ending with an ellipsis after a whole word. A word
    wider than `width` ends the text before it; only a first word that wide is
    cut inside, there being no word boundary before it."""
    for font in fonts:
        lines = wrap_words(text, font, width, balance)
        if lines is not None and fits(font, len(lines)):
            return font, list(lines)
    font = fonts[-1]
    most = 1
    while most < 64 and fits(font, most + 1):
        most += 1
    words = text.split()
    kept = list(itertools.takewhile(lambda w: vbtext.measure(w, font) <= width, words))
    if not kept:
        return font, [ellipsize(text, font, width)]
    lines = _greedy(kept, font, width)
    if len(lines) <= most and len(kept) == len(words):
        return font, lines
    lines = lines[:most - 1] + [' '.join(lines[most - 1:])]
    while True:   # the ellipsis after the last whole word that leaves it room, on the last line it fits
        tail = lines[-1].split()
        while tail and vbtext.measure(' '.join(tail).rstrip(',;:—-') + '…', font) > width:
            tail.pop()
        if tail:
            return font, lines[:-1] + [' '.join(tail).rstrip(',;:—-') + '…']
        if len(lines) == 1:
            return font, [ellipsize(lines[0], font, width)]
        lines = lines[:-1]


def _window(n, current, rows):
    """The first index of a `rows`-tall window over n items that shows `current`."""
    if n <= rows or current is None:
        return 0
    return max(0, min(current - rows // 2, n - rows))


def _fonts(ui, mono, scale):
    s = scale
    return dict(head=vb.Font(ui, 17 * s, weight=700, tracking=0.14),
                subject=vb.Font(ui, 27 * s, weight=700, line_height=1.25),
                meta=vb.Font(ui, 19 * s),
                body=vb.Font(ui, 21 * s, line_height=1.35),
                row=vb.Font(mono, 19 * s, line_height=1.45),
                row_bold=vb.Font(mono, 19 * s, weight=700, line_height=1.45))


def draw(cv, box, *, commit, files, current_path, xref_stack, ui='sans-serif', mono='monospace', scale=1.0,
         style=Style(), graph=None, beside=False):
    """The full side bar in `box`. `commit`: the plan's commit dict; `files`: the
    plan's file dicts; `xref_stack`: [(name, path, line)], oldest first.
    `graph(cv, box)`: draws the call graph into its section's box, between
    the files and the xref stack, or (`beside`, for a wide, short bar) in a
    column of its own on the right.
    Returns the files rows as drawn: [(texts, weight)]."""
    F = _fonts(ui, mono, scale)
    box = vb.Box(*box)
    with cv.clip(*box):
        cv.rect(*box, style.bg, radius=10)
        cv.outline(box.x + 0.5, box.y + 0.5, box.w - 1, box.h - 1, style.border, width=1, radius=10)
        c_box, f_box, graph_box, x_box, xref_rows, files_shown = _sections(box, files, xref_stack, F, scale,
                                                                           graph is not None, beside)
        _commit(cv, c_box, commit, F, style)
        drawn = _files(cv, f_box, files, current_path, files_shown, F, style)
        _xref(cv, x_box, xref_stack, xref_rows, F, style)
        if graph_box is not None:
            cv.mark('sidebar.graph', *graph_box)
            with cv.clip(*graph_box):
                y = _header(cv, graph_box, 'CALL GRAPH', F, style)
                graph(cv, vb.Box(graph_box.x, y + 6 * scale, graph_box.w, graph_box.bottom - y - 6 * scale))
    return drawn


def _sections(box, files, xref_stack, F, scale, graph, beside):
    """(commit, files, graph or None, xref) boxes, the xref rows and the file rows shown."""
    inner = box.pad(26 * scale, 26 * scale)
    hh = vbtext.line_height(F['head'])
    rh = vbtext.line_height(F['row'])
    gap = 22 * scale
    graph_box = None
    if graph and beside:
        inner, graph_box = inner.row(vb.fr, vb.fr * 0.62, gap=gap * 1.5)
    # Reserve the xref, files and graph sections first; the commit takes what is left.
    xref_rows = max(2, min(4, len(xref_stack)))
    xref_h = hh + 8 * scale + xref_rows * rh
    max_files = max(1, int((inner.h * (0.30 if graph and not beside else 0.38) - hh) // rh))
    files_shown = min(len(files), max_files)
    files_h = hh + 8 * scale + files_shown * rh
    if graph and not beside:
        graph_h = min(240 * scale, inner.h * 0.26)
        commit_h = max(0.0, inner.h - xref_h - files_h - graph_h - 3 * gap)
        c_box, f_box, graph_box, x_box = inner.column(commit_h, files_h, graph_h, xref_h, gap=gap)
    else:
        commit_h = max(0.0, inner.h - xref_h - files_h - 2 * gap)
        c_box, f_box, x_box = inner.column(commit_h, files_h, xref_h, gap=gap)
    return c_box, f_box, graph_box, x_box, xref_rows, files_shown


def free_area(box, *, commit, files, xref_stack, ui='sans-serif', mono='monospace', scale=1.0, graph=False,
              beside=False, **_):
    """The largest empty box in the full side bar as `draw` lays it out: the
    commit section below its text (a short message leaves room), or None."""
    F = _fonts(ui, mono, scale)
    c_box = _sections(vb.Box(*box), files, xref_stack, F, scale, graph, beside)[0]
    if c_box.h <= 0:
        return None
    y = _commit_layout(c_box, commit, F)[-1] + 12
    return vb.Box(c_box.x, y, c_box.w, c_box.bottom - y) if c_box.bottom - y > 0 else None


def _header(cv, box, label, F, style):
    vbtext.draw(cv, [label], F['head'], box.x, box.y, style.head)
    return box.y + vbtext.line_height(F['head']) + 8


def _commit_layout(box, commit, F):
    """(subject lines, their y, meta, its y, body lines, their y, where the text ends)."""
    y = box.y + vbtext.line_height(F['head']) + 8
    sub = vbtext.wrap(commit['subject'], F['subject'], box.w)
    slh = vbtext.line_height(F['subject'])
    sub = list(sub[:3])
    if len(vbtext.wrap(commit['subject'], F['subject'], box.w)) > 3:
        sub[-1] = ellipsize(sub[-1] + '…', F['subject'], box.w)
    sub_y = y
    y += slh * len(sub) + 6
    date = commit.get('date', '')[:10]
    meta = ' · '.join(x for x in (commit.get('short'), commit.get('author'), date) if x)
    meta_y = y
    y += vbtext.line_height(F['meta'])
    end = y
    y += 14
    blh = vbtext.line_height(F['body'])
    room = int((box.bottom - y) // blh)
    lines = []
    if room > 0:
        if commit.get('body'):
            for para in commit['body'].split('\n\n'):
                if lines:
                    lines.append('')
                lines += vbtext.wrap(' '.join(para.split()), F['body'], box.w)
        else:   # a range: one row per commit
            lines = [ellipsize(f'{c["short"]} {c["subject"]}', F['body'], box.w)
                     for c in commit.get('commits', [])] if len(commit.get('commits', [])) > 1 else []
        lines = trim(lines, room, F['body'], box.w)
        if lines:
            end = y + len(lines) * blh
    return sub, sub_y, ellipsize(meta, F['meta'], box.w), meta_y, lines, y, end


def _commit(cv, box, commit, F, style):
    if box.h <= 0:
        return
    cv.mark('sidebar.commit', *box)
    with cv.clip(*box):
        _header(cv, box, 'COMMIT', F, style)
        sub, sub_y, meta, meta_y, lines, body_y, _ = _commit_layout(box, commit, F)
        vbtext.draw(cv, sub, F['subject'], box.x, sub_y, style.strong)
        vbtext.draw(cv, [meta], F['meta'], box.x, meta_y, style.muted)
        if lines:   # a subject-only commit, or a one-commit range, has no body to show
            vbtext.draw(cv, lines, F['body'], box.x, body_y, style.fg)


def trim(lines, room, font, width):
    """At most `room` lines; a cut ends on a line of text with an ellipsis, never
    on the blank line between paragraphs."""
    if len(lines) <= room:
        return list(lines)
    lines = list(lines[:room])
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines[-1] = ellipsize(lines[-1] + ' \u2026', font, width)
    return lines


def _files(cv, box, files, current_path, rows, F, style):
    """Returns what it drew, one (texts, weight of the path) per row, for tests."""
    cv.mark('sidebar.files', *box)
    drawn = []
    with cv.clip(*box):
        n = len(files)
        y = _header(cv, box, f'FILES  {n}', F, style)
        cur = next((i for i, f in enumerate(files) if f['path'] == current_path), None)
        start = _window(n, cur, rows)
        rh = vbtext.line_height(F['row'])
        cell = vbtext.measure('M', F['row'])
        for i in range(start, min(n, start + rows)):
            f = files[i]
            is_cur = i == cur
            font = F['row_bold'] if is_cur else F['row']
            texts = []

            def put(s, fnt, x, color):
                texts.append(s)
                vbtext.draw(cv, [s], fnt, x, y, color)

            if is_cur:
                cv.rect(box.x - 8, y, box.w + 16, rh, style.current_bg, radius=4)
                put(MARK, font, box.x, style.head)
            stat = stat_text(f)
            sw = vbtext.measure(stat, F['row'])
            path_w = box.w - 2 * cell - sw - cell * 1.5
            put(ellipsize_left(f['path'], font, path_w), font, box.x + 2 * cell, style.strong if is_cur else style.fg)
            x = box.right - sw
            if f.get('binary'):
                put(stat, F['row'], x, style.muted)
            else:
                plus, _, minus = stat.rpartition(' ')
                if plus:
                    put(plus, F['row'], x, style.added)
                    x += vbtext.measure(plus + ' ', F['row'])
                put(minus, F['row'], x, style.removed)
            drawn.append((tuple(texts), font.weight))
            y += rh
    return drawn


def _xref(cv, box, stack, rows, F, style):
    cv.mark('sidebar.xref', *box)
    with cv.clip(*box):
        y = _header(cv, box, 'XREF STACK', F, style)
        rh = vbtext.line_height(F['row'])
        if not stack:
            vbtext.draw(cv, ['(empty)'], F['row'], box.x + 2 * vbtext.measure('M', F['row']), y, style.muted)
            return
        cell = vbtext.measure('M', F['row'])
        shown = list(enumerate(stack))[-rows:]
        for depth, (name, path, line) in reversed(shown):
            top = depth == len(stack) - 1
            font = F['row_bold'] if top else F['row']
            if top:
                vbtext.draw(cv, [MARK], font, box.x, y, style.head)
            label = ellipsize(f'{name} → {path}:{line}', font, box.w - 2 * cell)
            vbtext.draw(cv, [label], font, box.x + 2 * cell, y, style.strong if top else style.fg)
            y += rh


def draw_strip(cv, box, *, commit, files, current_path, xref_stack, ui='sans-serif', mono='monospace',
               style=Style()):
    """The narrow 1:1 strip: the subject, the current file, the top of the xref stack."""
    box = vb.Box(*box)
    F = _fonts(ui, mono, 1.0)
    with cv.clip(*box):
        cv.rect(*box, style.bg, radius=10)
        inner = box.pad(14, 22)
        subject = vb.Font(ui, 24, weight=700)
        cv.mark('sidebar.commit', inner.x, inner.y, inner.w, vbtext.line_height(subject))
        vbtext.draw(cv, [ellipsize(commit['subject'], subject, inner.w)], subject, inner.x, inner.y,
                    style.strong)
        lh = vbtext.line_height(F['row'])
        y1 = inner.y + vbtext.line_height(subject) + 8
        cv.mark('sidebar.files', inner.x, y1, inner.w, lh)
        cur = next((i for i, f in enumerate(files) if f['path'] == current_path), None)
        if cur is not None:
            f = files[cur]
            tail = f'  {stat_text(f)}  ({cur + 1}/{len(files)})'
            tw = vbtext.measure(tail, F['row'])
            path = ellipsize_left(f['path'], F['row_bold'], inner.w - tw - 2 * vbtext.measure('M', F['row']))
            line = f'{MARK} {path}'
            b = vbtext.draw(cv, [line], F['row_bold'], inner.x, y1, style.strong)
            vbtext.draw(cv, [tail], F['row'], b.right, y1, style.muted)
        else:
            vbtext.draw(cv, [f'{len(files)} files'], F['row'], inner.x, y1, style.muted)
        y2 = y1 + lh
        cv.mark('sidebar.xref', inner.x, y2, inner.w, lh)
        if xref_stack:
            name, path, line = xref_stack[-1]
            label = f'xref {MARK} {name} → {path}:{line}  (depth {len(xref_stack)})'
            vbtext.draw(cv, [ellipsize(label, F['row'], inner.w)], F['row'], inner.x, y2, style.fg)

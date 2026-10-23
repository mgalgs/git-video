"""The side bar beside (or under) the editor: the commit, the changed files and
the xref stack, for orientation. The current file is marked with `▶` and
weight, not only colour."""
from dataclasses import dataclass

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


def _ellipsize_left(s, font, width):
    if vbtext.measure(s, font) <= width:
        return s
    while s and vbtext.measure('…' + s, font) > width:
        s = s[1:]
    return '…' + s


def _ellipsize(s, font, width):
    if vbtext.measure(s, font) <= width:
        return s
    while s and vbtext.measure(s + '…', font) > width:
        s = s[:-1]
    return s.rstrip() + '…'


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
         style=Style()):
    """The full side bar in `box`. `commit`: the plan's commit dict; `files`: the
    plan's file dicts; `xref_stack`: [(name, path, line)], oldest first.
    Returns the files rows as drawn: [(texts, weight)]."""
    F = _fonts(ui, mono, scale)
    box = vb.Box(*box)
    with cv.clip(*box):
        cv.rect(*box, style.bg, radius=10)
        cv.outline(box.x + 0.5, box.y + 0.5, box.w - 1, box.h - 1, style.border, width=1, radius=10)
        inner = box.pad(26 * scale, 26 * scale)
        hh = vbtext.line_height(F['head'])
        rh = vbtext.line_height(F['row'])
        gap = 22 * scale
        # Reserve the xref and files sections first; the commit takes what is left.
        xref_rows = max(2, min(4, len(xref_stack)))
        xref_h = hh + 8 * scale + xref_rows * rh
        file_rows_n = len(files)
        max_files = max(1, int((inner.h * 0.38 - hh) // rh))
        files_shown = min(file_rows_n, max_files)
        files_h = hh + 8 * scale + files_shown * rh
        commit_h = max(0.0, inner.h - xref_h - files_h - 2 * gap)
        c_box, f_box, x_box = inner.column(commit_h, files_h, xref_h, gap=gap)
        _commit(cv, c_box, commit, F, style)
        drawn = _files(cv, f_box, files, current_path, files_shown, F, style)
        _xref(cv, x_box, xref_stack, xref_rows, F, style)
    return drawn


def _header(cv, box, label, F, style):
    vbtext.draw(cv, [label], F['head'], box.x, box.y, style.head)
    return box.y + vbtext.line_height(F['head']) + 8


def _commit(cv, box, commit, F, style):
    if box.h <= 0:
        return
    cv.mark('sidebar.commit', *box)
    with cv.clip(*box):
        y = _header(cv, box, 'COMMIT', F, style)
        sub = vbtext.wrap(commit['subject'], F['subject'], box.w)
        slh = vbtext.line_height(F['subject'])
        sub = list(sub[:3])
        if len(vbtext.wrap(commit['subject'], F['subject'], box.w)) > 3:
            sub[-1] = _ellipsize(sub[-1] + '…', F['subject'], box.w)
        vbtext.draw(cv, sub, F['subject'], box.x, y, style.strong)
        y += slh * len(sub) + 6
        date = commit.get('date', '')[:10]
        meta = ' · '.join(x for x in (commit.get('short'), commit.get('author'), date) if x)
        vbtext.draw(cv, [_ellipsize(meta, F['meta'], box.w)], F['meta'], box.x, y, style.muted)
        y += vbtext.line_height(F['meta']) + 14
        blh = vbtext.line_height(F['body'])
        room = int((box.bottom - y) // blh)
        if room <= 0:
            return
        if commit.get('body'):
            lines = []
            for para in commit['body'].split('\n\n'):
                if lines:
                    lines.append('')
                lines += vbtext.wrap(' '.join(para.split()), F['body'], box.w)
        else:   # a range: one row per commit
            lines = [_ellipsize(f'{c["short"]} {c["subject"]}', F['body'], box.w)
                     for c in commit.get('commits', [])] if len(commit.get('commits', [])) > 1 else []
        lines = trim(lines, room, F['body'], box.w)
        if lines:   # a subject-only commit, or a one-commit range, has no body to show
            vbtext.draw(cv, lines, F['body'], box.x, y, style.fg)


def trim(lines, room, font, width):
    """At most `room` lines; a cut ends on a line of text with an ellipsis, never
    on the blank line between paragraphs."""
    if len(lines) <= room:
        return list(lines)
    lines = list(lines[:room])
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines[-1] = _ellipsize(lines[-1] + ' \u2026', font, width)
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
            put(_ellipsize_left(f['path'], font, path_w), font, box.x + 2 * cell, style.strong if is_cur else style.fg)
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
            label = _ellipsize(f'{name} → {path}:{line}', font, box.w - 2 * cell)
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
        vbtext.draw(cv, [_ellipsize(commit['subject'], subject, inner.w)], subject, inner.x, inner.y,
                    style.strong)
        lh = vbtext.line_height(F['row'])
        y1 = inner.y + vbtext.line_height(subject) + 8
        cv.mark('sidebar.files', inner.x, y1, inner.w, lh)
        cur = next((i for i, f in enumerate(files) if f['path'] == current_path), None)
        if cur is not None:
            f = files[cur]
            tail = f'  {stat_text(f)}  ({cur + 1}/{len(files)})'
            tw = vbtext.measure(tail, F['row'])
            path = _ellipsize_left(f['path'], F['row_bold'], inner.w - tw - 2 * vbtext.measure('M', F['row']))
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
            vbtext.draw(cv, [_ellipsize(label, F['row'], inner.w)], F['row'], inner.x, y2, style.fg)

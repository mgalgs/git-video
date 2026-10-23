import unittest

import videobuilder as vb

from git_video import editor, highlight

CODE = ''.join(f'def f{i}(x):\n\treturn x + {i}\n' for i in range(40))
FONT = vb.Font('monospace', 24, line_height=1.35)


def buffer(text=CODE, path='a.py'):
    return editor.Buffer(path, highlight.highlight_lines(text, path), 'Python')


def pixel(cv, x, y):
    cv.surface.flush()
    data, stride = cv.surface.get_data(), cv.surface.get_stride()
    i = int(y) * stride + int(x) * 4
    return tuple(data[i:i + 4])


class TestEditor(unittest.TestCase):
    def test_marks_inside_box(self):
        for box in (vb.Box(0, 0, 1256, 1032), vb.Box(300, 200, 700, 500), vb.Box(10, 10, 300, 200)):
            cv = vb.Canvas(1920, 1080, '#000')
            lay = editor.layout(box, FONT)
            editor.draw(cv, lay, buffer(), 3.5, cursor=(5, 2), marks_added=[5, 6], marks_removed_at=[8],
                        tints=[(5, 6)], sweeps=[(5, 0.5)], boxes=[(6, 1, 6)], echo='M-.', echo_cursor=True)
            names = {n for n, _ in cv.marks}
            self.assertEqual(names, {'editor.text', 'editor.modeline', 'editor.echo'})
            for name, b in cv.marks:
                self.assertTrue(box.contains(b), (box, name, b))
            cv = vb.Canvas(1920, 1080, '#000')
            lay = editor.layout(box, FONT, minimap=True)
            buf = buffer()
            seg = editor.Segment(5, highlight.highlight_lines('x = 1\ny = 2\n', 'a.py'), 1,
                                 [['delete', 0, 0, -1, -1, 'x'], ['insert', -1, -1, 0, 4, 'return']])
            editor.draw(cv, lay, buf, 3.5, cursor=(5, 2), marks_added=[5], marks_removed_at=[5], tints=[(5, 5)],
                        minimap=editor.Minimap(editor.minimap_bars(buf.lines), [(4, 4)], [5]), morphs=[seg],
                        morph_p=0.5)
            names = {n for n, _ in cv.marks}
            self.assertLessEqual({'editor.text', 'editor.modeline', 'editor.echo', 'editor.minimap',
                                  'editor.viewport'}, names)
            for name, b in cv.marks:
                self.assertTrue(box.contains(b), (box, name, b))

    def drawn_text(self, lay, buf, top, **kw):
        """[(text, x, y, font)] of every vbtext.draw call of a frame."""
        from unittest import mock
        calls = []
        real = editor.vbtext.draw

        def record(cv, lines, font, x, y, *a, **k):
            calls.append((lines[0] if lines else '', x, y, font, k))
            return real(cv, lines, font, x, y, *a, **k)

        cv = vb.Canvas(1000, 700, '#000')
        with mock.patch.object(editor.vbtext, 'draw', record):
            where = editor.draw(cv, lay, buf, top, **kw)
        return calls, where, cv

    def test_long_lines_wrap_instead_of_scrolling_sideways(self):
        long = 'x = [' + ', '.join(str(i) for i in range(60)) + ']'
        text = 'def f(verbose=3):\n' + long + '\n' + 'return 1\n'
        lay = editor.layout(vb.Box(0, 0, 900, 600), FONT, minimap=True)
        self.assertGreater(len(long), 2 * lay.cols)
        col = len(long) - 2
        buf = buffer(text)
        calls, where, cv = self.drawn_text(lay, buf, 0, cursor=(1, col), region=((1, 1), (1, col)),
                                           marks_added=[1], boxes=[(1, lay.cols - 2, 6)])
        tb = lay.text_box
        code = [(t, x, y) for t, x, y, f, _ in calls if f is lay.font and x >= tb.x - 1 and y < tb.bottom]
        # no text starts left of the text area or runs past its right edge: nothing is chopped
        for t, x, y in code:
            self.assertGreaterEqual(x, tb.x - 0.01, t)
            self.assertLessEqual(x + len(t) * lay.cell_w, tb.right + 0.01, t)
        # every line starts at its first column, on its own first row
        rows = editor.wrap_rows(len(long), lay.cols)
        self.assertEqual(rows, len(long) // lay.cols + 1)
        y0, y1, y2 = (round(lay.line_y(k, 0), 2) for k in (0, 1, 2))
        starts = {round(y, 2): min(x for _, x, yy in code if round(yy, 2) == round(y, 2)) for _, _, y in code}
        self.assertAlmostEqual(starts[y1], lay.col_x(0))
        glyphs = {}
        for t, x, y in code:   # by letter, as point redraws the one under it
            for i, ch in enumerate(t):
                glyphs[round(y, 1), round(x + i * lay.cell_w, 1)] = ch
        row_text = ''.join(ch for (y, x), ch in sorted(glyphs.items()) if y1 - 0.5 < y < y1 + rows * lay.line_h - 0.5)
        self.assertEqual(row_text.replace(' ', ''), long.replace(' ', ''))
        # the line after it sits below all its rows
        ret = [y for t, x, y in code if t == 'return']
        self.assertAlmostEqual(ret[0], lay.line_y(1 + rows, 0))
        # line numbers on first rows only: 1, 2, 3 at the rows each line starts on
        nums = {t: round(y, 2) for t, x, y, f, k in calls if t.isdigit() and k.get('align') == 'right'}
        self.assertEqual(nums, {'1': y0, '2': y1, '3': round(lay.line_y(1 + rows, 0), 2)})
        # point, the region and a word split by the wrap follow the rows
        self.assertTrue(tb.contains(where.cursor_box), (tb, where.cursor_box))
        self.assertAlmostEqual(where.cursor_box.y, lay.line_y(1 + col // lay.cols, 0))
        self.assertTrue(lay.box.contains(dict(cv.marks)['editor.region']))
        parts = where.word_boxes(1, lay.cols - 2, 6)
        self.assertEqual([round(b.w / lay.cell_w) for b in parts], [2, 4])
        self.assertEqual(where.line_box(1).h, rows * lay.line_h)

    def test_continuation_rows_have_a_glyph_and_no_number(self):
        from unittest import mock
        long = 'y = ' + 'abc + ' * 60
        lay = editor.layout(vb.Box(0, 0, 900, 600), FONT)
        rows = editor.wrap_rows(len(long), lay.cols)
        drawn = []
        with mock.patch.object(editor, '_continuation', lambda cv, lay, y, theme, a=1.0: drawn.append(round(y, 2))):
            calls, _, _ = self.drawn_text(lay, buffer('a = 1\n' + long + '\nb = 2\n'), 0, marks_added=[1])
        self.assertEqual(drawn, [round(lay.line_y(1 + r, 0), 2) for r in range(1, rows)])
        numbered = {round(y, 2) for t, x, y, f, k in calls if t.isdigit() and k.get('align') == 'right'}
        self.assertFalse(numbered & set(drawn))
        self.assertEqual(sum(t == '+' and f is not lay.font for t, x, y, f, k in calls), 1)   # on its first row only

    def test_keep_in_view_with_wrapped_lines(self):
        lay = editor.layout(vb.Box(0, 0, 900, 600), FONT)
        long = 'z' * (lay.cols * 4)
        lines = buffer('\n'.join([long] * 10) + '\n').lines
        top = editor.keep_in_view(lay, lines, 0.0, 6)
        geo = editor.Geometry(lay, lines, top)
        self.assertGreaterEqual(geo.offset(6), 0)
        self.assertLessEqual(geo.offset(6) + geo.rows(6), lay.rows)
        self.assertEqual(editor.keep_in_view(lay, lines, 0.0, 0), 0.0)

    def test_region_fold_progress_marks(self):
        for box in (vb.Box(0, 0, 1256, 1032), vb.Box(300, 200, 700, 500)):
            cv = vb.Canvas(1920, 1080, '#000')
            lay = editor.layout(box, FONT, minimap=True)
            editor.draw(cv, lay, buffer(), 3, cursor=(6, 2), region=((5, 4), (7, 2)), folds=[(9, 37)],
                        progress=(12, 225))
            marks = dict(cv.marks)
            for name in ('editor.region', 'editor.fold', 'editor.progress'):
                self.assertIn(name, marks, (box, name))
                self.assertTrue(box.contains(marks[name]), (box, name))
            self.assertTrue(marks['editor.modeline'].contains(marks['editor.progress']))
            self.assertFalse(marks['editor.region'].intersects(marks['editor.fold']))
            cv = vb.Canvas(1920, 1080, '#000')
            editor.draw(cv, lay, buffer(), 3, cursor=(6, 2))
            names = {n for n, _ in cv.marks}
            self.assertFalse(names & {'editor.region', 'editor.fold', 'editor.progress'})
        # the region is outlined, not only shaded: its edge colour is on its border
        cv = vb.Canvas(1000, 700, '#000')
        lay = editor.layout(vb.Box(0, 0, 1000, 700), FONT)
        theme = editor.Theme()
        editor.draw(cv, lay, buffer(), 0, region=((2, 0), (2, 6)), theme=theme)
        r = dict(cv.marks)['editor.region']
        edge = tuple(round(c * 255) for c in vb.rgba(theme.region_edge)[:3])
        b, g, red, _ = pixel(cv, r.x + r.w / 2, r.y + 2)
        self.assertEqual((red, g, b), edge)

    def test_minimap(self):
        box = vb.Box(0, 0, 1000, 700)
        plain = editor.layout(box, FONT)
        lay = editor.layout(box, FONT, minimap=True)
        strip = round(lay.cell_w * editor.MINIMAP_CELLS)
        self.assertEqual(lay.minimap_box.w, strip)
        self.assertAlmostEqual(plain.text_box.w - lay.text_box.w, strip)
        self.assertEqual(lay.minimap_box.right, box.right)
        theme = editor.Theme()
        buf = buffer()
        mm = editor.Minimap(editor.minimap_bars(buf.lines), added=[(30, 33)], removed_at=[60])
        ys = []
        for top in (0, 20, 50):
            cv = vb.Canvas(1000, 700, '#000')
            editor.draw(cv, lay, buf, top, minimap=mm, theme=theme)
            marks = dict(cv.marks)
            self.assertTrue(marks['editor.minimap'].contains(marks['editor.viewport']))
            ys.append(marks['editor.viewport'].y)
        self.assertEqual(ys, sorted(ys))
        self.assertLess(ys[0], ys[-1])

        def ink(color, mm):
            cv = vb.Canvas(1000, 700, '#000')
            editor.draw(cv, lay, buf, 0, minimap=mm, theme=theme)
            want = tuple(round(c * 255) for c in vb.rgba(color)[:3])
            strip = lay.minimap_box
            count = 0
            for y in range(int(strip.y), int(strip.bottom)):
                for x in range(int(strip.x), int(strip.x + 8)):
                    b, g, r, _ = pixel(cv, x, y)
                    count += (r, g, b) == want
            return count

        self.assertGreater(ink(theme.fringe_added, mm), 0)
        self.assertGreater(ink(theme.fringe_removed, mm), 0)
        bare = editor.Minimap(mm.bars)
        self.assertEqual(ink(theme.fringe_added, bare), 0)
        self.assertEqual(ink(theme.fringe_removed, bare), 0)

    def morph(self, p, seg, **kw):
        cv = vb.Canvas(1000, 700, '#000')
        lay = editor.layout(vb.Box(0, 0, 1000, 700), FONT)
        editor.draw(cv, lay, buffer(), 0, morphs=[seg], morph_p=p, **kw)
        return cv, lay

    def ghost(self):
        old = highlight.highlight_lines('def gone():\n    pass\n', 'a.py')
        ops = [['delete', 0, 0, -1, -1, 'def'], ['delete', 0, 4, -1, -1, 'gone'], ['delete', 1, 4, -1, -1, 'pass']]
        return editor.Segment(3, old, 0, ops)

    def replace(self):
        # buffer line 3 is '\treturn x + 1' (tabs expanded); it replaced two old lines
        old = highlight.highlight_lines('    return x - 7\n    # old\n', 'a.py')
        ops = [['equal', 0, 4, 0, 4, 'return'], ['equal', 0, 11, 0, 11, 'x'], ['delete', 0, 13, -1, -1, '-'],
               ['delete', 0, 15, -1, -1, '7'], ['delete', 1, 4, -1, -1, '#'], ['delete', 1, 6, -1, -1, 'old'],
               ['insert', -1, -1, 0, 13, '+'], ['insert', -1, -1, 0, 15, '1']]
        return editor.Segment(3, old, 1, ops)

    def test_morph_rows_collapse(self):
        for seg, start, end in ((self.ghost(), 2, 0), (self.replace(), 2, 1)):
            for p, rows in ((0, start), (1 - 1e-9, end)):
                cv, lay = self.morph(p, seg)
                marks = dict(cv.marks)
                if rows:
                    self.assertAlmostEqual(marks['editor.segment 0'].h, rows * lay.line_h, places=3)
                    self.assertAlmostEqual(marks['editor.segment 0'].y, lay.line_y(3, 0))
                else:
                    self.assertLess(marks.get('editor.segment 0', vb.Box(0, 0, 0, 0)).h, 1e-3)
            self.assertNotIn('editor.segment 0', dict(self.morph(1, seg)[0].marks))
        self.assertEqual(editor.segment_rows(self.ghost(), 0.5), 2 + (0 - 2) * vb.ease_in_out_quart(
            (0.5 - editor.GLIDE[0]) / (editor.GLIDE[1] - editor.GLIDE[0])))

    def test_lines_below_move_with_the_segment(self):
        seg = self.ghost()
        theme = editor.Theme()
        lay = editor.layout(vb.Box(0, 0, 1000, 700), FONT)
        # the cursor's current-line band on line 5 sits 2 rows lower at p=0 than at p=1
        def band_y(p):
            cv = vb.Canvas(1000, 700, '#000')
            editor.draw(cv, lay, buffer(), 0, morphs=[seg], morph_p=p, cursor=(5, 0), theme=theme)
            want = tuple(round(c * 255) for c in vb.rgba(theme.current_line)[:3])
            x = int(lay.text_box.right - 5)
            for y in range(int(lay.text_box.y), int(lay.text_box.bottom)):
                b, g, r, _ = pixel(cv, x, y)
                if (r, g, b) == want:
                    return y
        self.assertAlmostEqual(band_y(0) - band_y(1), 2 * lay.line_h, delta=1.5)

    def test_strike_and_underline_ink(self):
        theme = editor.Theme()

        def ink(p, color, row_y, seg=None):
            cv, lay = (self.morph(p, seg or self.replace(), theme=theme) if p is not None else
                       (vb.Canvas(1000, 700, '#000'), editor.layout(vb.Box(0, 0, 1000, 700), FONT)))
            if p is None:
                editor.draw(cv, lay, buffer(), 0, theme=theme)
            want = tuple(round(c * 255) for c in vb.rgba(color)[:3])
            y0 = lay.line_y(3, 0)
            count = 0
            for y in range(int(y0 + lay.line_h * row_y) - 2, int(y0 + lay.line_h * row_y) + 3):
                for x in range(int(lay.col_x(13)), int(lay.col_x(17))):
                    b, g, r, _ = pixel(cv, x, y)
                    count += (r, g, b) == want
            return count

        self.assertGreater(ink(0, theme.strike, 0.52), 0)
        self.assertEqual(ink(None, theme.strike, 0.52), 0)
        self.assertGreater(ink(1, theme.underline, 0.86), 0)
        self.assertEqual(ink(None, theme.underline, 0.86), 0)

    def morph_case(self, old, new):
        from git_video import worddiff
        buf = buffer(new)
        seg = editor.Segment(0, highlight.highlight_lines(old, 'a.py'), len(new.splitlines()),
                             tuple(tuple(o) for o in worddiff.ops(old.splitlines(), new.splitlines())))
        return buf, seg

    def glyphs_at(self, lay, buf, seg, p):
        """{(x, y): char} of every glyph drawn in the text area at morph progress p."""
        from unittest import mock
        drawn = {}
        real = editor.vbtext.draw

        def record(cv, lines, font, x, y, *a, **kw):
            for i, ch in enumerate(lines[0] if lines else ''):
                if ch.strip() and font is lay.font and x >= lay.text_box.x:
                    drawn[round(x + i * lay.cell_w, 2), round(y, 2)] = ch
            return real(cv, lines, font, x, y, *a, **kw)

        with mock.patch.object(editor.vbtext, 'draw', record):
            editor.draw(vb.Canvas(1000, 700, '#000'), lay, buf, 0, morphs=[seg], morph_p=p)
        return drawn

    def test_morph_keeps_unchanged_words_still(self):
        # only `foo` -> `bar` changes: every other glyph stays exactly where it is
        buf, seg = self.morph_case('x = foo(a, b)\nkeep\n', 'x = bar(a, b)\nkeep\n')
        lay = editor.layout(vb.Box(0, 0, 900, 600), FONT)
        y = round(lay.line_y(0, 0), 2)
        want = {(round(lay.col_x(c), 2), y): ch for c, ch in enumerate('x = bar(a, b)') if ch.strip() and not 4 <= c < 7}
        for k in range(21):
            got = self.glyphs_at(lay, buf, seg, k / 20)
            for pos, ch in want.items():
                self.assertEqual(got.get(pos), ch, (k / 20, pos, ch))
            # and nothing else is drawn on that row outside the changed word
            for (x, gy), ch in got.items():
                if gy == y and (x, gy) not in want:
                    col = (x - lay.text_box.x) / lay.cell_w
                    self.assertTrue(4 <= col < 7 and col == round(col), (k / 20, col, ch))

    def test_morph_follows_wrapped_lines(self):
        lay = editor.layout(vb.Box(0, 0, 900, 600), FONT)
        tail = ', '.join(f'arg{i}' for i in range(40)) + ')'
        old, new = 'x = foo(' + tail, 'x = bar(' + tail
        self.assertGreater(len(new), 2 * lay.cols)
        buf, seg = self.morph_case(old + '\nkeep\n', new + '\nkeep\n')
        want = {(round(lay.col_x(c % lay.cols), 2), round(lay.line_y(c // lay.cols, 0), 2)): ch
                for c, ch in enumerate(new) if ch.strip() and not 4 <= c < 7}
        for k in range(11):
            got = self.glyphs_at(lay, buf, seg, k / 10)
            for pos, ch in want.items():
                self.assertEqual(got.get(pos), ch, (k / 10, pos, ch))
        # the line after it starts below every row of the wrapped one
        rows = editor.wrap_rows(len(new), lay.cols)
        got = self.glyphs_at(lay, buf, seg, 1.0)
        self.assertEqual(got.get((round(lay.col_x(0), 2), round(lay.line_y(rows, 0), 2))), 'k')

    def test_new_line_wipes_in_without_scrambling(self):
        from unittest import mock
        buf, seg = self.morph_case('x = foo(a, b)\n', 'x = bar(a, b)\ntotal = compute(q)\n')
        lay = editor.layout(vb.Box(0, 0, 900, 600), FONT)
        scrambled = []
        real = editor.draw_scramble
        with mock.patch.object(editor, 'draw_scramble', lambda cv, text, *a, **kw: (scrambled.append(text),
                                                                                    real(cv, text, *a, **kw))):
            for k in range(21):
                editor.draw(vb.Canvas(1000, 700, '#000'), lay, buf, 0, morphs=[seg], morph_p=k / 20)
        self.assertEqual(set(scrambled), {'bar'})
        self.assertLessEqual(editor.DECODE[1] - editor.DECODE[0], 0.3)   # about half the old 0.55
        # the new line is there once the morph is over, and partly there midway
        done = self.glyphs_at(lay, buf, seg, 1.0)
        y1 = round(lay.line_y(1, 0), 2)
        self.assertEqual(''.join(ch for (x, y), ch in sorted(done.items()) if y == y1), 'total=compute(q)')

    def test_fringe_marks_never_overdraw(self):
        from unittest import mock
        lay = editor.layout(vb.Box(0, 0, 900, 600), FONT)
        glyphs = []
        real = editor.vbtext.draw

        def record(cv, lines, font, x, y, *a, **kw):
            if lines and lines[0] in ('+', '−') and x < lay.box.x + lay.fringe_w and kw.get('alpha', 1) > 0:
                glyphs.append((lines[0], y, y + editor.vbtext.line_height(font)))
            return real(cv, lines, font, x, y, *a, **kw)

        with mock.patch.object(editor.vbtext, 'draw', record):
            editor.draw(vb.Canvas(1000, 700, '#000'), lay, buffer(), 0, marks_added=[2, 3, 5, 6, 8, 9],
                        marks_removed_at=[3, 4, 5, 8, 12])
        self.assertEqual(sum(g == '+' for g, *_ in glyphs), 6)
        self.assertIn('−', [g for g, *_ in glyphs])   # the boundary at line 12 is clear of any +
        spans = sorted((a, b) for _, a, b in glyphs)
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            self.assertLessEqual(b0, b1)
            self.assertGreaterEqual(b0 + 1, a1, spans)

    def test_morph_midway_differs(self):
        frames = [self.morph(p, self.replace())[0].bgra() for p in (0, 0.5, 1)]
        self.assertNotEqual(frames[0], frames[1])
        self.assertNotEqual(frames[1], frames[2])
        self.assertNotEqual(frames[0], frames[2])

    def test_focus_dims_outside_band(self):
        theme = editor.Theme()
        lay = editor.layout(vb.Box(0, 0, 1000, 700), FONT, minimap=True)
        empty = editor.Buffer('e.txt', highlight.highlight_lines('\n' * 40, 'e.txt'))

        def frame(amount):
            cv = vb.Canvas(1000, 700, '#000')
            editor.draw(cv, lay, empty, 0, focus=(5, 6), focus_amount=amount, theme=theme,
                        minimap=editor.Minimap(editor.minimap_bars(empty.lines)))
            return cv

        bg = tuple(round(c * 255) for c in vb.rgba(theme.bg)[:3])
        x = int(lay.text_box.x + 40)
        out_y, in_y = int(lay.line_y(15, 0) + 5), int(lay.line_y(5, 0) + lay.line_h / 2)
        strip = lay.minimap_box
        mm = (int(strip.x + strip.w / 2), int(strip.bottom - 4))
        lit, dim = frame(0), frame(1)
        self.assertEqual(pixel(lit, x, out_y)[2::-1], bg)
        b, g, r, _ = pixel(dim, x, out_y)
        self.assertLess(r + g + b, sum(bg))
        self.assertGreaterEqual(r + g + b, 0.3 * sum(bg))
        self.assertEqual(pixel(dim, x, in_y)[2::-1], bg)
        self.assertEqual(pixel(dim, *mm), pixel(lit, *mm))   # the minimap is never dimmed
        band = dict(dim.marks)['editor.focus']
        self.assertLessEqual(band.y, lay.line_y(5, 0))
        self.assertGreaterEqual(band.bottom, lay.line_y(7, 0) - 0.5)
        self.assertNotIn('editor.focus', dict(lit.marks))

    def test_layout_geometry(self):
        box = vb.Box(300, 200, 700, 500)
        lay = editor.layout(box, FONT)
        self.assertGreaterEqual(lay.rows, 1)
        self.assertGreater(lay.cell_w, 0)
        wb = lay.word_box(4, 3, 5, 2.0)
        self.assertAlmostEqual(wb.x, lay.col_x(3))
        self.assertAlmostEqual(wb.w, 5 * lay.cell_w)
        self.assertTrue(box.contains(wb))
        lb = lay.line_box(4, 2.0)
        self.assertEqual((lb.x, lb.w), (lay.text_box.x, lay.text_box.w))
        vis = lay.visible_range(2.0, 100)
        self.assertEqual(vis.start, 2)
        self.assertLessEqual(len(vis), lay.rows)
        self.assertEqual(lay.visible_range(2.6, 100).start, 3)   # line 2 is less than half on screen
        self.assertEqual(lay.visible_range(0, 3), range(0, 3))
        self.assertEqual(lay.hit(wb.cx, wb.cy, 2.0), (4, 5))     # the middle letter of the word
        self.assertEqual(lay.hit(wb.x + 1, wb.cy, 2.0), (4, 3))
        self.assertIsNone(lay.hit(box.x - 5, wb.cy, 2.0))

    def test_clipping_and_out_of_range(self):
        box = vb.Box(300, 200, 700, 500)
        cv = vb.Canvas(1200, 900, '#000000')
        lay = editor.layout(box, FONT)
        editor.draw(cv, lay, buffer(), 500.0, cursor=(900, 3), marks_added=[1, 999], marks_removed_at=[0, 1000],
                    sweeps=[(999, 1.0)], boxes=[(-3, 0, 4)], echo='x' * 400)
        editor.draw(cv, lay, buffer(), -3.0, cursor=(0, 300), echo='x' * 400, echo_cursor=True)
        for x in range(int(box.x) - 2, int(box.right) + 2, 7):
            for y in (box.y - 2, box.y - 1, box.bottom + 1, box.bottom + 2):
                self.assertEqual(pixel(cv, x, y)[:3], (0, 0, 0), (x, y))
        for y in range(int(box.y) - 2, int(box.bottom) + 2, 7):
            for x in (box.x - 2, box.x - 1, box.right + 1, box.right + 2):
                self.assertEqual(pixel(cv, x, y)[:3], (0, 0, 0), (x, y))

    def test_highlight_cache_and_tabs(self):
        a = highlight.highlight_lines(CODE, 'a.py')
        self.assertIs(a, highlight.highlight_lines(CODE, 'a.py'))
        self.assertEqual(''.join(s for _, s in a[1]), '    return x + 0')
        self.assertEqual(len(a), 80)
        tok = highlight.tokens_by_line('a\tb\n', 'x.txt')
        self.assertEqual(''.join(s for _, s in tok[0]), 'a   b')

    def test_fringe_glyphs(self):
        box = vb.Box(0, 0, 800, 600)
        lay = editor.layout(box, FONT)
        theme = editor.Theme()

        def fringe_ink(**kw):
            cv = vb.Canvas(800, 600, '#000')
            editor.draw(cv, lay, buffer(), 0, theme=theme, **kw)
            y = lay.line_y(3, 0)
            bg = vb.rgba(theme.fringe_bg)
            bg = tuple(round(c * 255) for c in bg[:3])
            count = 0
            for yy in range(int(y - 6), int(y + lay.line_h)):
                for xx in range(int(lay.fringe_w)):
                    b, g, r, _ = pixel(cv, xx, yy)
                    count += (r, g, b) != bg
            return count

        self.assertEqual(fringe_ink(), 0)
        self.assertGreater(fringe_ink(marks_added=[3]), 20)
        self.assertGreater(fringe_ink(marks_removed_at=[3]), 20)

    def test_plain_text_fallback(self):
        self.assertEqual(highlight.lexer_for('README.unknownext').name, 'Text only')
        cv = vb.Canvas(800, 600, '#000')
        lay = editor.layout(vb.Box(0, 0, 800, 600), FONT)
        buf = editor.Buffer('notes.unknownext', highlight.highlight_lines('hello\nworld\n', 'notes.unknownext'))
        p = editor.draw(cv, lay, buf, 0, cursor=(1, 0))
        self.assertEqual(p.visible, range(0, 2))

    def test_placement(self):
        box = vb.Box(100, 50, 900, 700)
        lay = editor.layout(box, FONT)
        cv = vb.Canvas(1200, 900, '#000')
        p = editor.draw(cv, lay, buffer(), 7.25, cursor=(10, 4))
        self.assertEqual(p.visible, lay.visible_range(7.25, 80))
        self.assertEqual(p.cursor_box, lay.word_box(10, 4, 1, 7.25))
        self.assertTrue(box.contains(p.word_box(10, 4, 6)))
        self.assertIsNone(editor.draw(cv, lay, buffer(), 0).cursor_box)


if __name__ == '__main__':
    unittest.main()

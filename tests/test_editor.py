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

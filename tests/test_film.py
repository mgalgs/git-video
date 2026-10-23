import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import videobuilder as vb

from git_video import cli, collect, film, plan, sidebar
from tests import fixtures
from tests.test_cli import quiet


class TestFilm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        empty = os.path.join(cls.tmp.name, 'no-sfx')
        os.makedirs(empty)
        cls.env = mock.patch.dict(os.environ, {vb.sfx.ENV: empty})
        cls.env.start()
        cls.repo, cls.sha = fixtures.make_repo(cls.tmp.name)
        cls.change = collect.collect(cls.repo, cls.sha['change'])
        cls.plan = plan.plan(cls.change, 0)
        cls.tour = film.Tour(cls.plan, cls.change, log=io.StringIO())

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp.cleanup()

    def frame_of(self, kind, t, nth=0):
        i = [k for k, s in enumerate(self.plan.shots) if s['kind'] == kind][nth]
        return i, self.tour.shot_frames[i] + round(t * self.plan.fps)

    def test_build_frames(self):
        f, starts = film.build(self.plan, self.change, log=io.StringIO())
        self.assertEqual(f.frames, round(self.plan.total * self.plan.fps))
        self.assertEqual([x.name for x in f.formats], ['16x9', '9x16', '1x1'])
        self.assertEqual(starts[0], 0)
        self.assertTrue(all(a < b for a, b in zip(starts, starts[1:])))

    def test_total_matches_frames(self):
        self.assertLessEqual(abs(self.tour.film.frames / self.plan.fps - self.plan.total), 1 / self.plan.fps)

    def test_keyframes_all_formats(self):
        out = os.path.join(self.tmp.name, 'stills')
        code, stdout, _ = quiet(cli.main, ['-C', self.repo, self.sha['change'], '--stills', out, '--format', 'all'])
        self.assertEqual(code, 0)
        labels = [label for _, label in self.tour.film.keyframes]
        for want in ('title', 'open src/app.py', 'hunk src/app.py#1', 'xref frob click', 'xref frob def', 'end'):
            self.assertIn(want, labels)
        for fmt in ('16x9', '9x16', '1x1'):
            self.assertTrue(os.path.isfile(os.path.join(out, f'sheet-{fmt}.png')))
            pngs = [p for p in os.listdir(out) if p.endswith(f'-{fmt}.png') and not p.startswith('sheet')]
            self.assertEqual(len(pngs), len(labels))
            for n, _ in self.tour.film.keyframes:
                cv = self.tour.film.frame(n, fmt)
                f = vb.FORMATS[fmt]
                self.assertEqual((cv.surface.get_width(), cv.surface.get_height()), (f.width, f.height))

    def test_draw_is_pure(self):
        _, n = self.frame_of('xref-push', plan.XREF_CLICK + 0.1)
        for fmt in ('16x9', '1x1'):
            a = self.tour.film.frame(n, fmt).bgra()
            self.tour.film.frame(n + 7, fmt)
            self.assertEqual(a, self.tour.film.frame(n, fmt).bgra())

    def test_regions_do_not_overlap(self):
        for fmt in ('16x9', '9x16', '1x1'):
            _, n = self.frame_of('hunk', 1.0)
            cv = self.tour.film.frame(n, fmt)
            st = film.stage(cv)
            ed = [b for name, b in cv.marks if name.startswith('editor.')]
            sb = [b for name, b in cv.marks if name.startswith('sidebar.')]
            self.assertEqual(len(ed), 3)
            self.assertEqual(len(sb), 3, fmt)
            for b in ed:
                self.assertTrue(st.main.contains(b), (fmt, b))
                for c in sb:
                    self.assertFalse(b.intersects(c), (fmt, b, c))
            for c in sb:
                self.assertTrue(st.side.contains(c), (fmt, c))

    def test_planned_rows_fit_every_format(self):
        for fmt in ('16x9', '9x16', '1x1'):
            cv = self.tour.film.frame(0, format=fmt)
            st = film.stage(cv)
            self.assertGreaterEqual(film.editor.layout(st.main, st.mono).rows, plan.PLAN_ROWS, fmt)

    def test_definition_cursor_lands_on_the_name(self):
        ch = collect.collect(fixtures.make_edge_repo(self.tmp.name + '/go'), 'HEAD')
        p = plan.plan(ch, 3)
        tour = film.Tour(p, ch, log=io.StringIO())
        i = next(k for k, s in enumerate(p.shots) if s['kind'] == 'xref-push' and s['name'] == 'Read')
        self.assertEqual(tour.views[i][1].cursor, (4, len('func (r *Reader) ')))
        # a plan without the definition's col still finds the whole word
        self.assertEqual(tour._def_col('reader.go', 5, 'Read'), len('func (r *Reader) '))

    def test_sidebar_current_marker(self):
        i, n = self.frame_of('hunk', 1.0, nth=1)
        path, _ = self.tour.state(n)
        self.assertEqual(path, self.plan.shots[i]['path'])
        rows = sidebar.file_rows(self.plan.files, path)
        marked = [r for r in rows if r.startswith(sidebar.MARK)]
        self.assertEqual(marked, [f'{sidebar.MARK} {path}  +1 {sidebar.MINUS}1'])
        self.assertTrue(all(r.startswith('  ') for r in rows if r not in marked))

    def test_xref_stack_push_pop(self):
        i, n = self.frame_of('xref-push', plan.XREF_SWITCH + 0.2)
        self.assertEqual(self.tour.state(n), ('src/lib.py', (('frob', 'src/lib.py', 1),)))
        _, n = self.frame_of('xref-push', plan.XREF_SWITCH - 0.1)
        self.assertEqual(self.tour.state(n)[1], ())
        j = i + 1
        end = self.tour.shot_frames[j] + round(self.plan.shots[j]['dur'] * self.plan.fps) - 1
        self.assertEqual(self.tour.state(end), ('src/app.py', ()))

    def test_body_trim_never_ends_on_a_blank_line(self):
        font = vb.Font('sans-serif', 21)
        out = sidebar.trim(['one', 'two', '', 'three'], 3, font, 1000)
        self.assertEqual(out, ['one', 'two \u2026'])
        self.assertEqual(sidebar.trim(['a', 'b'], 3, font, 1000), ['a', 'b'])

    def test_sweep_settles_to_a_wash(self):
        self.assertEqual(film.settle(-1), 1.0)
        self.assertAlmostEqual(film.settle(5), film.SETTLED)

    def test_unknown_scene_rejected(self):
        p = plan.Plan.from_json(self.plan.to_json())
        p.shots[1] = {**p.shots[1], 'scene': 'call-graph'}
        with self.assertRaisesRegex(ValueError, 'call-graph'):
            film.Tour(p, self.change, log=io.StringIO())

    def test_xref_targets_word_box(self):
        i, n = self.frame_of('xref-push', plan.XREF_CLICK + 0.1)
        cv = self.tour.film.frame(n, '16x9')
        marks = dict(cv.marks)
        word, text = marks['xref.word'], marks['editor.text']
        self.assertTrue(text.contains(word))
        st = film.stage(cv)
        lay = film._layout(st, self.tour.buffers['src/app.py'])
        self.assertAlmostEqual(word.w, len('frob') * lay.cell_w, places=3)
        self.assertEqual(lay.hit(word.x + 1, word.cy, film.fit_top(*self._top(i, lay))), (4, 11))

    def _top(self, i, lay):
        v = self.tour.views[i][0]
        return v.top, 4, lay.rows, len(self.tour.buffers['src/app.py'].lines)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'needs ffmpeg')
    def test_render_smoke(self):
        from videobuilder.render import render
        out = os.path.join(self.tmp.name, 'smoke.mp4')
        render(self.tour.film, out, format='1x1', jobs=1, frames=(0, 15), log=io.StringIO())
        r = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', out], capture_output=True,
                           text=True, check=True)
        streams = json.loads(r.stdout)['streams']
        self.assertIn('video', [s['codec_type'] for s in streams])


if __name__ == '__main__':
    unittest.main()

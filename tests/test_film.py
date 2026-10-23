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
        for want in ('title', 'map', 'dive src/app.py', 'travel tests/test_app.py', 'open src/lib.py',
                     'hunk src/app.py#1', 'xref frob click', 'xref frob dive', 'xref frob def', 'end'):
            self.assertIn(want, labels)
        for fmt in ('16x9', '9x16', '1x1'):
            self.assertTrue(os.path.isfile(os.path.join(out, f'sheet-{fmt}.png')))
            pngs = [p for p in os.listdir(out) if p.endswith(f'-{fmt}.png') and not p.startswith('sheet')]
            self.assertEqual(len(pngs), len(labels))
            for n, _ in self.tour.film.keyframes:
                cv = self.tour.film.frame(n, fmt)
                f = vb.FORMATS[fmt]
                self.assertEqual((cv.surface.get_width(), cv.surface.get_height()), (f.width, f.height))

    def test_keyframes_in_time_order(self):
        frames = [n for n, _ in self.tour.film.keyframes]
        self.assertEqual(frames, sorted(frames))

    def test_draw_is_pure(self):
        i = next(k for k, s in enumerate(self.plan.shots) if s['kind'] == 'hunk' and s['morph'])
        s = self.plan.shots[i]
        frames = [self.tour.shot_frames[i] + round((s['morph']['at'] + 0.6) * self.plan.fps),self.frame_of('xref-push', plan.XREF_CLICK + 0.1)[1], self.frame_of('dive', 0.5)[1],
                  self.frame_of('travel', 0.7)[1], self.frame_of('map', 1.0)[1], self.frame_of('title', 1.5)[1]]
        for n in frames:
            for fmt in ('16x9', '1x1'):
                a = self.tour.film.frame(n, fmt).bgra()
                self.tour.film.frame(n + 7, fmt)
                self.assertEqual(a, self.tour.film.frame(n, fmt).bgra())

    def test_map_tiles_inside_main_and_apart(self):
        _, n = self.frame_of('map', plan.MAP_DUR - 0.1)
        for fmt in ('16x9', '9x16', '1x1'):
            cv = self.tour.film.frame(n, fmt)
            st = film.stage(cv)
            tiles = [(name, b) for name, b in cv.marks if name.startswith('map.tile ')]
            side = [b for name, b in cv.marks if name.startswith('sidebar.')]
            self.assertTrue(side)
            for k, (name, b) in enumerate(tiles):
                self.assertTrue(st.main.contains(b), (fmt, name, b))
                for _, c in tiles[k + 1:]:
                    self.assertFalse(b.intersects(c), (fmt, name))
                for c in side:
                    self.assertFalse(b.intersects(c), (fmt, name))
            names = {name.split(' ', 1)[1] for name, _ in tiles}
            self.assertLessEqual({'src/app.py', 'src/lib.py', 'tests/test_app.py', 'bin.dat'}, names)
            for name, b in cv.marks:   # every changed tile's +a −d label stays on the map
                if name.startswith('map.label '):
                    self.assertTrue(st.main.contains(b), (fmt, name))

    def test_dive_lands_on_the_pane(self):
        i, _ = self.frame_of('dive', 0)
        last = self.tour.shot_frames[i + 1] - 1
        first = self.tour.shot_frames[i + 1]
        a = dict(self.tour.film.frame(last, '16x9').marks)['editor.text']
        b = dict(self.tour.film.frame(first, '16x9').marks)['editor.text']
        for x, y in zip(a, b):
            self.assertAlmostEqual(x, y, delta=1.0)
        cv = self.tour.film.frame(self.frame_of('dive', 0.5 * plan.DIVE_DUR)[1], '16x9')
        mid = dict(cv.marks)['editor.text']
        st = film.stage(cv)
        self.assertTrue(st.main.contains(mid))
        self.assertLess(mid.w, b.w - 10)

    def test_travel_leaves_and_lands_on_panes(self):
        i, _ = self.frame_of('travel', 0)
        before = dict(self.tour.film.frame(self.tour.shot_frames[i] - 1, '16x9').marks)['editor.text']
        start = dict(self.tour.film.frame(self.tour.shot_frames[i], '16x9').marks)['editor.text']
        for x, y in zip(before, start):
            self.assertAlmostEqual(x, y, delta=1.0)
        mid = self.tour.film.frame(self.frame_of('travel', 0.6)[1], '16x9')
        self.assertNotIn('editor.text', dict(mid.marks))   # out on the map between the two
        self.assertEqual(self.tour.state(self.frame_of('travel', plan.TRAVEL_SWITCH + 0.05)[1])[0],
                         'tests/test_app.py')

    def test_pointer_hover_reaches_the_word(self):
        i, n = self.frame_of('xref-push', plan.XREF_CLICK)
        cv = self.tour.film.frame(n, '16x9')
        marks = dict(cv.marks)
        word, st = marks['xref.word'], film.stage(cv)
        lh = film._layout(st, self.tour.buffers[0, 'src/app.py', 'new']).line_h
        self.assertEqual(film.hover_at(0, word, st.main, lh), 0)
        self.assertGreaterEqual(film.hover_at(plan.XREF_CLICK, word, st.main, lh), 0.99)
        self.assertTrue(marks['xref.pointer'].intersects(word))

    def test_xref_reveal_is_between_panes(self):
        fps = self.plan.fps
        for kind, switch, reveal in (('xref-push', plan.XREF_SWITCH, plan.XREF_REVEAL),
                                     ('xref-pop', plan.POP_SWITCH, plan.POP_REVEAL)):
            frames = [self.frame_of(kind, t)[1] for t in (switch - 1 / fps, switch + reveal / 2, switch + reveal + 0.1)]
            a, mid, b = (self.tour.film.frame(n, '16x9') for n in frames)
            self.assertNotEqual(mid.bgra(), a.bgra(), kind)
            self.assertNotEqual(mid.bgra(), b.bgra(), kind)
            if kind == 'xref-push':
                self.assertIn('xref.word', dict(mid.marks))

    def test_counter_rolls(self):
        self.assertEqual(film.counter_value(plan.TITLE_ROLL[0], 11), 0)
        self.assertEqual(film.counter_value(plan.TITLE_ROLL[1], 11), 11)
        (n,) = [n for n, label in self.tour.film.keyframes if label == 'title']
        t = self.tour.at(n)[1]   # the title still's own time: counters mid-roll
        self.assertTrue(0 < film.counter_value(t, 11) < 11)
        self.assertTrue(plan.TITLE_ROLL[0] < t < plan.TITLE_ROLL[1] < plan.TITLE_DUR)

    def test_title_card_regions(self):
        delete = collect.collect(self.repo, self.sha['delete'])
        tours = {'change': self.tour, 'delete': film.Tour(plan.plan(delete), delete, log=io.StringIO())}
        for rev, tour in tours.items():
            for fmt in ('16x9', '9x16', '1x1'):
                cv = tour.film.frame(round(2.2 * self.plan.fps), fmt)
                marks = dict(cv.marks)
                names = ['card.kicker', 'card.title', 'card.counters', 'lower-third']
                if rev == 'delete':
                    names.append('avatars')
                else:
                    self.assertNotIn('avatars', marks)
                frame = vb.Box.of(cv)
                for k, a in enumerate(names):
                    self.assertIn(a, marks, (rev, fmt))
                    self.assertTrue(frame.contains(marks[a]), (rev, fmt, a))
                    for b in names[k + 1:]:
                        self.assertFalse(marks[a].intersects(marks[b]), (rev, fmt, a, b))

    def test_card_text(self):
        c = film.card_text(self.plan)
        self.assertEqual((c['kicker'], c['title'], c['name']), ('REPO', 'Use the library from an app', 'Test Person'))
        self.assertEqual(c['subtitle'], f'{self.plan.commit["short"]} · 2026-01-02')
        r = film.card_text(plan.plan(collect.collect(self.repo, f'{self.sha["root"]}..HEAD')))
        self.assertEqual(r['title'], '2 commits')
        self.assertIn('2026-01-02', r['subtitle'])
        self.assertIn('..', r['subtitle'])
        self.assertEqual(r['name'], 'Test Person')

    def test_initials(self):
        self.assertEqual(film.initials('Claude Opus 5.5'), 'CO')
        self.assertEqual(film.initials('Ada'), 'AD')
        av = film.avatars(['a'] * 8)
        self.assertEqual(len(av), film.MAX_AVATARS)
        self.assertEqual(av[-1][1], '+3')
        self.assertEqual(len(film.avatars(['a'] * 6)), 6)

    def test_recap(self):
        self.assertEqual(film.recap(self.plan), (['src/app.py', 'src/lib.py', 'tests/test_app.py'],
                                                 [('frob', 'src/lib.py', 1)]))

    def test_end_card_regions(self):
        delete = collect.collect(self.repo, self.sha['delete'])
        tours = {'change': self.tour, 'delete': film.Tour(plan.plan(delete), delete, log=io.StringIO())}
        for rev, tour in tours.items():
            n = next(n for n, label in tour.film.keyframes if label == 'end')
            for fmt in ('16x9', '9x16', '1x1'):
                cv = tour.film.frame(n, fmt)
                marks = dict(cv.marks)
                names = ['card.header', 'card.visited', 'card.defs'] + (['card.next'] if rev == 'change' else [])
                if rev == 'delete':
                    self.assertNotIn('card.next', marks)
                frame = vb.Box.of(cv)
                for k, a in enumerate(names):
                    self.assertIn(a, marks, (rev, fmt))
                    self.assertTrue(frame.contains(marks[a]), (rev, fmt, a))
                    for b in names[k + 1:]:
                        self.assertFalse(marks[a].intersects(marks[b]), (rev, fmt, a, b))

    def test_lit_order_follows_files(self):
        (m,) = [s for s in self.plan.shots if s['kind'] == 'map']
        self.assertEqual(m['lit'], ['bin.dat', 'src/app.py', 'src/lib.py', 'tests/test_app.py'])
        early = film.lit_amounts(m, plan.MAP_LIGHT_START + 0.05)
        self.assertGreater(early['bin.dat'], 0)
        self.assertEqual(early['tests/test_app.py'], 0)
        self.assertEqual(set(film.lit_amounts(m, m['dur']).values()), {1.0})
        self.assertEqual(set(film.lit_amounts(m, 0).values()), {0.0})

    def test_regions_do_not_overlap(self):
        for fmt in ('16x9', '9x16', '1x1'):
            _, n = self.frame_of('hunk', 1.0)
            cv = self.tour.film.frame(n, fmt)
            st = film.stage(cv)
            ed = [b for name, b in cv.marks if name.startswith('editor.')]
            sb = [b for name, b in cv.marks if name.startswith('sidebar.')]
            self.assertEqual(len(ed), 6)   # text, mode line, echo, minimap, viewport, focus
            self.assertEqual(len(sb), 3 if fmt == '1x1' else 4, fmt)   # commit, files, xref (+ graph)
            for k, b in enumerate(sb):
                for c in sb[k + 1:]:
                    self.assertFalse(b.intersects(c), (fmt, b, c))
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

    def test_planned_cols_fit_every_format(self):
        for fmt in ('16x9', '9x16', '1x1'):
            cv = self.tour.film.frame(0, format=fmt)
            st = film.stage(cv)
            lay = film.editor.layout(st.main, st.mono, digits=6, minimap=True)
            self.assertGreaterEqual(int(lay.text_box.w // lay.cell_w), plan.PLAN_COLS, fmt)

    def test_minimap_in_every_format(self):
        _, n = self.frame_of('hunk', 1.0)
        for fmt in ('16x9', '9x16', '1x1'):
            cv = self.tour.film.frame(n, fmt)
            st = film.stage(cv)
            marks = dict(cv.marks)
            mm, view, text = marks['editor.minimap'], marks['editor.viewport'], marks['editor.text']
            self.assertTrue(st.main.contains(mm), fmt)
            self.assertTrue(mm.contains(view), fmt)
            self.assertGreaterEqual(mm.x, text.right - 0.5, fmt)
            self.assertLessEqual(mm.y, text.y)
            for name, b in cv.marks:
                if name.startswith('sidebar.'):
                    self.assertFalse(mm.intersects(b), (fmt, name))

    def test_focus_eases_in_and_out(self):
        i, _ = self.frame_of('hunk', 0)
        s = self.plan.shots[i]
        self.assertEqual(film.focus_amount(s, plan.HUNK_SCROLL, plan.HUNK_SCROLL), 0)
        self.assertEqual(film.focus_amount(s, plan.HUNK_SCROLL + plan.FOCUS_IN, plan.HUNK_SCROLL), 1)
        self.assertAlmostEqual(film.focus_amount(s, s["dur"], plan.HUNK_SCROLL), 0)
        n = self.tour.shot_frames[i] + round(1.2 * self.plan.fps)
        band = dict(self.tour.film.frame(n, '16x9').marks)['editor.focus']
        text = dict(self.tour.film.frame(n, '16x9').marks)['editor.text']
        self.assertLess(band.h, text.h)

    def test_fringe_marks_come_from_plan(self):
        _, n = self.frame_of('hunk', 1.0)
        p = plan.Plan.from_json(self.plan.to_json())
        for f in p.files:
            f['marks'] = {'added': [], 'removed_at': []}
        bare = film.Tour(p, self.change, formats=('16x9',), log=io.StringIO())

        def fringe_ink(tour):
            cv = tour.film.frame(n, '16x9')
            text = dict(cv.marks)['editor.text']
            st = film.stage(cv)
            green = vb.rgba(film.THEME.fringe_added)
            want = tuple(round(c * 255) for c in green[:3])
            cv.surface.flush()
            data, stride = cv.surface.get_data(), cv.surface.get_stride()
            count = 0
            for y in range(int(text.y), int(text.bottom), 2):
                for x in range(int(st.main.x), int(st.main.x + 12)):
                    i = y * stride + x * 4
                    b, g, r = data[i:i + 3]
                    count += (r, g, b) == want
            return count

        self.assertGreater(fringe_ink(self.tour), 0)
        self.assertEqual(fringe_ink(bare), 0)

    def test_new_keyframes_all_formats(self):
        tours = [self.tour]
        ch = collect.collect(fixtures.make_morph_repo(self.tmp.name + '/kf'), 'HEAD')
        tours.append(film.Tour(plan.plan(ch), ch, log=io.StringIO()))
        delete = self.tmp.name + '/kfdel'
        fixtures.init(delete)
        fixtures.write(delete, 'a.py', 'def keep():\n    return 1\n\n\ndef gone():\n    return 2\n')
        fixtures.commit(delete, 'Start')
        fixtures.write(delete, 'a.py', 'def keep():\n    return 1\n')
        fixtures.commit(delete, 'Drop gone()')
        ch = collect.collect(delete, 'HEAD')
        tours.append(film.Tour(plan.plan(ch), ch, log=io.StringIO()))
        for make in (lambda d: fixtures.make_far_repo(d), lambda d: fixtures.make_graph_repo(d),
                     lambda d: fixtures.make_callout_repo(d)[0]):
            ch = collect.collect(make(self.tmp.name + f'/kf{len(tours)}'), 'HEAD')
            tours.append(film.Tour(plan.plan(ch), ch, log=io.StringIO()))
        labels = [label for t in tours for _, label in t.film.keyframes]
        for want in ('morph src/lib.py#1', 'morph m.py#2', 'ghost a.py#1', 'scroll far.py#2', 'graph helper route',
                     'callout lib.py#1'):
            self.assertIn(want, labels)
        for tour in tours:
            for n, label in tour.film.keyframes:
                kind = label.split()[0]
                if kind not in ('morph', 'ghost', 'callout', 'scroll', 'graph'):
                    continue
                i, t = tour.at(n)
                if kind in ('morph', 'ghost'):
                    self.assertTrue(0 < film.morph_progress(tour.shots[i], t) < 1, label)
                for fmt in ('16x9', '9x16', '1x1'):
                    cv = tour.film.frame(n, fmt)
                    st = film.stage(cv)
                    marks = dict(cv.marks)
                    if kind in ('morph', 'ghost'):
                        self.assertIn('editor.segment 0', marks, (label, fmt))
                    for name, b in cv.marks:
                        if name.startswith(('editor.', 'callout.')):
                            self.assertTrue(st.main.contains(b), (label, fmt, name, b))
                        if name.startswith('sidebar.'):
                            self.assertTrue(st.side.contains(b), (label, fmt, name, b))
                        if name.startswith('graph.'):
                            self.assertTrue(marks['sidebar.graph'].contains(b), (label, fmt, name, b))

    def test_ghost_at_end_of_file_is_shown(self):
        # Deleting the last lines of a long file: the window must not be
        # clamped to the new, shorter file, or the ghost lines fall below it.
        repo = self.tmp.name + '/tail'
        fixtures.init(repo)
        body = ''.join(f'def f{i}():\n    return {i}\n' for i in range(40))
        fixtures.write(repo, 'lib.py', body)
        fixtures.commit(repo, 'Start')
        fixtures.write(repo, 'lib.py', ''.join(body.splitlines(True)[:-6]))
        fixtures.commit(repo, 'Drop the last three functions')
        ch = collect.collect(repo, 'HEAD')
        tour = film.Tour(plan.plan(ch), ch, log=io.StringIO())
        i = next(k for k, s in enumerate(tour.shots) if s['kind'] == 'hunk')
        n = tour.shot_frames[i] + round((tour.shots[i]['morph']['at'] - 0.05) * tour.fps)
        for fmt in ('16x9', '9x16', '1x1'):
            cv = tour.film.frame(n, fmt)
            marks = dict(cv.marks)
            seg, text = marks['editor.segment 0'], marks['editor.text']
            lay = film._layout(film.stage(cv), tour.buffers[0, 'lib.py', 'new'])
            self.assertAlmostEqual(seg.h, 6 * lay.line_h, places=3, msg=fmt)
            self.assertLessEqual(seg.bottom, text.bottom + 1e-6, fmt)

    def test_far_scroll_ramps(self):
        dur = plan.HUNK_SCROLL_FAR
        pos = [film.scroll_at(dur * k / 100, 10, 250, dur) for k in range(101)]
        self.assertEqual(pos[0], 10)
        self.assertEqual(pos[-1], 250)
        self.assertEqual(pos, sorted(pos))
        speed = [b - a for a, b in zip(pos, pos[1:])]
        self.assertGreater(speed[50], 3 * speed[4])
        self.assertGreater(speed[50], 3 * speed[-5])
        ch = collect.collect(fixtures.make_far_repo(self.tmp.name + '/farfilm'), 'HEAD')
        tour = film.Tour(plan.plan(ch), ch, log=io.StringIO())
        (n,) = [n for n, label in tour.film.keyframes if label.startswith('scroll ')]
        i, t = tour.at(n)
        self.assertEqual(tour.shots[i]['scroll'], 'far')
        st = film.stage(tour.film.frame(n, '16x9'))
        # landed exactly where a plain draw of the hunk's view puts the text
        end = tour.shot_frames[i] + round(dur * tour.fps) + 1
        moved = dict(tour.film.frame(end, '16x9').marks)['editor.viewport']
        from videobuilder.canvas import Canvas
        from videobuilder.film import FORMATS
        cv = Canvas(1920, 1080, '#000', format=FORMATS['16x9'])
        film._pane(tour, cv, st, tour.views[i][1])
        self.assertEqual(dict(cv.marks)['editor.viewport'], moved)
        # mid-scroll the text leans and streaks: unlike the same frame without motion
        mid = tour.film.frame(n, '16x9').bgra()
        with mock.patch.object(film, 'velocity_skew', lambda v, **kw: (0.0, 0.0)), \
                mock.patch.object(film, 'STREAK', 0.0):
            still = tour.film.frame(n, '16x9').bgra()
        self.assertNotEqual(mid, still)

    def test_ellipsize_words(self):
        font = vb.Font('sans-serif', 22)
        s = 'The helper other() returns three times its input.'
        self.assertEqual(sidebar.ellipsize_words(s, font, 5000), s)
        w = vb.text.measure('The helper other() ret', font)
        out = sidebar.ellipsize_words(s, font, w)
        self.assertTrue(out.endswith('…'))
        self.assertTrue(s.startswith(out[:-1]))
        self.assertIn(s[len(out) - 1], ' ')   # cut between two words
        self.assertLessEqual(vb.text.measure(out, font), w)
        lines = film.callout_lines(' '.join(['word'] * 200), font, 300)
        self.assertEqual(len(lines), film.CALLOUT_LINES)
        self.assertTrue(lines[-1].endswith('…'))

    def test_callout_frames_all_formats(self):
        repo, sha = fixtures.make_callout_repo(self.tmp.name + '/cf')
        ch = collect.collect(repo, sha['head'])
        p = plan.plan(ch)
        tour = film.Tour(p, ch, log=io.StringIO())
        (n,) = [n for n, label in tour.film.keyframes if label.startswith('callout ')]
        i, t = tour.at(n)
        c = p.shots[i]['callouts'][0]
        self.assertTrue(c['at'] < t < c['until'])
        for fmt in ('16x9', '9x16', '1x1'):
            cv = tour.film.frame(n, fmt)
            st = film.stage(cv)
            marks = dict(cv.marks)
            self.assertEqual([m for m, _ in cv.marks].count('callout.card'), 1)
            card, target = marks['callout.card'], marks['callout.target']
            self.assertIn('callout.line', marks)
            self.assertTrue(st.main.contains(card), fmt)
            self.assertTrue(marks['editor.text'].contains(card), fmt)
            self.assertFalse(card.intersects(target), fmt)
            self.assertFalse(card.intersects(marks['editor.minimap']), fmt)
            for name, b in cv.marks:
                if name.startswith('sidebar.'):
                    self.assertFalse(card.intersects(b), (fmt, name))
            before = tour.film.frame(tour.shot_frames[i] + round((c['at'] - 0.1) * p.fps), fmt)
            self.assertNotIn('callout.card', dict(before.marks))

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
        cv = self.tour.film.frame(n, format='16x9')
        st = film.stage(cv)
        rows = sidebar.draw(cv, st.side, commit=self.plan.commit, files=self.plan.files, current_path=path,
                            xref_stack=(), scale=st.scale)
        marked = [r for r in rows if sidebar.MARK in r[0]]
        self.assertEqual(marked, [((sidebar.MARK, path, '+1', f'{sidebar.MINUS}1'), 700)])
        self.assertEqual(len(rows), len(self.plan.files))
        self.assertTrue(all(w == 400 for _, w in rows if (_, w) not in marked))

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

    def test_commit_without_a_body_draws(self):
        # A subject-only commit, and a one-commit range (whose body is always
        # empty), leave the side bar's body section with no lines.
        for spec in (self.sha['root'], f'{self.sha["root"]}..{self.sha["change"]}'):
            ch = collect.collect(self.repo, spec)
            tour = film.Tour(plan.plan(ch, 0), ch, log=io.StringIO())
            for fmt in ('16x9', '9x16', '1x1'):
                for n, _ in tour.keyframes():
                    tour.film.frame(n, fmt)

    def test_editor_scene_draws_into_the_box_it_is_given(self):
        i, n = self.frame_of('hunk', plan.HUNK_SCROLL + 0.2)
        from videobuilder.canvas import Canvas
        from videobuilder.film import FORMATS
        cv = Canvas(1920, 1080, '#000', format=FORMATS['16x9'])
        box = vb.Box(300, 200, 700, 500)
        st = film.stage(cv)
        t = (n - self.tour.shot_frames[i]) / self.plan.fps
        film.draw_editor(self.tour, cv, box, self.plan.shots[i], t, i, st)
        self.assertTrue(box.contains(dict(cv.marks)['editor.text']))

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
        lay = film._layout(st, self.tour.buffers[0, 'src/app.py', 'new'])
        self.assertAlmostEqual(word.w, len('frob') * lay.cell_w, places=3)
        self.assertEqual(lay.hit(word.x + 1, word.cy, film.fit_top(*self._top(i, lay))), (4, 11))

    def _top(self, i, lay):
        v = self.tour.views[i][0]
        return v.top, 4, lay.rows, len(self.tour.buffers[0, 'src/app.py', 'new'].lines)

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

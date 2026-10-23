import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import videobuilder as vb

from git_video import cli, collect, editor, film, plan, sidebar
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

    def test_wide_pane_shows_ninety_columns(self):
        from types import SimpleNamespace
        for voice in (False, True):
            st = film.stage(SimpleNamespace(width=1920, height=1080, format='16x9'), voice=voice)
            for digits in (3, 4):   # a file of up to 9999 lines
                lay = editor.layout(st.main, st.mono, digits=digits, minimap=True)
                self.assertGreaterEqual(lay.cols, 90, (voice, digits))
                self.assertGreaterEqual(lay.rows, plan.PLAN_ROWS, (voice, digits))
            self.assertGreaterEqual(st.mono.size, 22)   # still easily read at 1080p

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

    def test_cards_fit_any_subject_and_name(self):
        import copy
        from git_video.film import vbtext
        p = copy.deepcopy(self.plan)
        p.commit['subject'] = 'Rework the whole thing ' * 14 + 'Supercalifragilisticexpialidocious' * 3
        p.commit['author'] = 'Maximiliana Wolfeschlegelsteinhausenbergerdorff-Von Ziegenhals Esq.'
        p.repo = 'an-extremely-long-repository-name-that-goes-on-and-on-forever'
        tour = film.Tour(p, self.change, log=io.StringIO())
        bars = []
        real = film.lower_third_bar

        def bar(cv, title, subtitle, *a, width=640, title_font=None, subtitle_font=None, pad=28, **kw):
            bars.append((vbtext.measure(title, title_font), vbtext.measure(subtitle, subtitle_font), width - 8 - 2 * pad))
            return real(cv, title, subtitle, *a, width=width, title_font=title_font, subtitle_font=subtitle_font,
                        pad=pad, **kw)

        end = next(n for n, label in tour.film.keyframes if label == 'end')
        with mock.patch.object(film, 'lower_third_bar', bar):
            for fmt in ('16x9', '9x16', '1x1'):
                for n in (round(2.2 * p.fps), end):
                    cv = tour.film.frame(n, fmt)
                    frame = vb.Box.of(cv)
                    for name, b in cv.marks:
                        if name.startswith('card.') or name == 'lower-third':
                            self.assertTrue(frame.pad(20).contains(b), (fmt, name, b))
                    if n != end:
                        self.assertLess(dict(cv.marks)['card.title'].w, frame.w - 100)
        self.assertTrue(bars)
        for name_w, sub_w, room in bars:
            self.assertLessEqual(max(name_w, sub_w), room)

    def test_portrait_title_card_fills_the_column(self):
        cv = self.tour.film.frame(round(plan.TITLE_STILL * self.plan.fps), '9x16')
        marks = dict(cv.marks)
        top, bottom = marks['card.kicker'].y, marks['lower-third'].bottom
        self.assertGreater(bottom - top, 0.4 * cv.height)   # one column, no empty band down the middle
        self.assertLess(marks['card.counters'].bottom, marks['lower-third'].y)
        self.assertLess(marks['lower-third'].y - marks['card.counters'].bottom, 0.12 * cv.height)

    def washed(self, tour, n, fmt):
        """The lines' highlighter strokes drawn on frame n: [(y, alpha, progress)]."""
        strokes = []
        real = editor.highlighter_swipe

        def record(cv, x, y, w, h, p, **kw):
            strokes.append((y, kw.get('alpha', 1.0), p))
            return real(cv, x, y, w, h, p, **kw)

        with mock.patch.object(editor, 'highlighter_swipe', record):
            tour.film.frame(n, fmt)
        return strokes

    def test_wash_stays_through_travels_and_returns(self):
        # a pure addition (swept), a dive and M-, back, then a travel on
        ch = collect.collect(fixtures.make_edge_repo(self.tmp.name + '/wash'), 'HEAD')
        p = plan.plan(ch)
        tour = film.Tour(p, ch, log=io.StringIO())
        shots, fps = p.shots, p.fps
        starts = tour.shot_frames
        travel = next(i for i, s in enumerate(shots) if s['kind'] == 'travel')
        pop = next(i for i, s in enumerate(shots) if s['kind'] == 'xref-pop')
        push = pop - 1
        settled = editor.Theme().sweep_alpha * film.SETTLED
        for fmt in ('16x9', '9x16', '1x1'):
            frames = [starts[travel] - 2, starts[travel] - 1, starts[travel], starts[travel] + 1,   # leaving on a travel
                      starts[travel] + round(plan.TRAVEL_SHRINK * fps * 0.5),
                      starts[push], starts[push] + 1, starts[push] + round(plan.XREF_CLICK * fps),   # off on M-.
                      starts[pop] + round(plan.POP_SWITCH * fps) + 1,   # back on M-,
                      starts[pop] + round((plan.POP_SWITCH + plan.POP_REVEAL) * fps),
                      starts[pop + 1] - 1]
            for n in frames:
                strokes = self.washed(tour, n, fmt)
                self.assertTrue(strokes, (fmt, n, tour.at(n)))
                self.assertTrue(any(abs(a - settled) < 1e-6 and p >= 1 for _, a, p in strokes), (fmt, n, strokes))
            # the travel lands on the next hunk's lines, which sweep at once
            nxt = travel + 1
            self.assertEqual(shots[nxt]['kind'], 'hunk')
            self.assertFalse(shots[nxt].get('morph'))
            self.assertEqual(tour.views[travel][1], tour.views[nxt][0])
            self.assertTrue(self.washed(tour, starts[nxt] + 2, fmt), fmt)
            n = starts[nxt] + round(plan.HUNK_SWEEP * fps) + 1
            self.assertTrue(all(p >= 1 for _, _, p in self.washed(tour, n, fmt)), fmt)

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
        # mid-scroll the text leans: unlike the same frame without motion
        mid = tour.film.frame(n, '16x9').bgra()
        with mock.patch.object(film, 'velocity_skew', lambda v, **kw: (0.0, 0.0)):
            still = tour.film.frame(n, '16x9').bgra()
        self.assertNotEqual(mid, still)
        # and is never drawn twice: no ghost copy trails it
        cv = Canvas(1920, 1080, '#000', format=FORMATS['16x9'])
        lay = editor.layout(st.main, st.mono, minimap=True)
        buf = editor.Buffer('a.py', [[('#fff', 'x = 1')]] * 40)
        editor.draw(cv, lay, buf, 0.0, motion=(1.5, 0.015))
        ref = Canvas(1920, 1080, '#000', format=FORMATS['16x9'])
        calls = []
        with mock.patch.object(editor, '_draw_runs', lambda *a: calls.append(a[3])):
            editor.draw(ref, lay, buf, 0.0, motion=(1.5, 0.015))
        self.assertEqual(len(calls), len(set(calls)))

    def test_develop_jump_scroll_glides(self):
        # an `M-g g` jump puts point on its line at once; the view still ramps there
        ch = collect.collect(fixtures.make_far_repo(self.tmp.name + '/fardev'), 'HEAD')
        tour = film.Tour(plan.build(ch, 0, 'develop'), ch, formats=('16x9',), log=io.StringIO())
        (i,) = [k for k, s in enumerate(tour.shots) if s['kind'] == 'edit']
        tops, real = [], editor.draw

        def record(cv, lay, buf, top, **kw):
            tops.append(top)
            return real(cv, lay, buf, top, **kw)

        with mock.patch.object(editor, 'draw', record):
            for n in range(tour.shot_frames[i], tour.shot_frames[i + 1]):
                tour.film.frame(n, '16x9')
        self.assertGreater(max(tops) - min(tops), 200)
        steps = [abs(b - a) for a, b in zip(tops, tops[1:])]
        self.assertLess(max(steps), 0.2 * (max(tops) - min(tops)))

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

    def develop_tours(self):
        if not hasattr(self, '_develop'):
            out = {}
            for name, (repo, rev) in {
                    'change': (self.repo, self.sha['change']),
                    'big': (fixtures.make_big_repo(self.tmp.name + '/dbig'), 'HEAD'),
                    'callout': (fixtures.make_callout_repo(self.tmp.name + '/dcall')[0], 'HEAD'),
                    'develop': (fixtures.make_develop_repo(self.tmp.name + '/ddev'), 'HEAD')}.items():
                ch = collect.collect(repo, rev)
                p = plan.build(ch, 0, 'develop')
                out[name] = film.Tour(p, ch, log=io.StringIO())
            type(self)._develop = out
        return self._develop

    def test_speedup_still_avoids_held_fill(self):
        # a held fill in the middle of the plateau plays near natural speed:
        # the still goes to the longest stretch at the peak
        from git_video import edits
        clk = {'times': [0.0, 1.0, 1.4, 3.0, 3.0001, 4.0, 4.0001, 9.0, 9.4, 10.0],
               'speeds': [1, 1, 20, 20, 1.2, 1.2, 20, 20, 1, 1]}
        span = {'times': [1.0, 1.4, 9.0, 9.4]}
        t = film._peak_moment(span, clk)
        self.assertAlmostEqual(t, 6.50005, places=3)
        self.assertEqual(edits.speed_at(t, clk), 20)

    def test_develop_keyframes_all_formats(self):
        tours = self.develop_tours()
        labels = {name: [label for _, label in t.film.keyframes] for name, t in tours.items()}
        for want in ('typing src/lib.py', 'kill src/lib.py', 'end'):
            self.assertIn(want, labels['change'])
        self.assertTrue(any(x.startswith('speedup ') for x in labels['big']), labels['big'])
        self.assertTrue(any(x.startswith('elision ') for x in labels['develop']), labels['develop'])
        want_mark = {'kill': 'editor.region', 'speedup': 'editor.badge', 'elision': 'editor.fold'}
        for name, tour in tours.items():
            for n, label in tour.film.keyframes:
                kind = label.split()[0]
                i, t = tour.at(n)
                if kind in ('typing', 'kill', 'speedup', 'elision'):
                    self.assertEqual(tour.shots[i]['kind'], 'edit', label)
                for fmt in ('16x9', '9x16', '1x1'):
                    cv = tour.film.frame(n, fmt)
                    st = film.stage(cv)
                    marks = dict(cv.marks)
                    if kind not in ('typing', 'kill', 'speedup', 'elision'):
                        continue
                    self.assertIn('editor.progress', marks, (name, label, fmt))
                    if kind in want_mark:
                        self.assertIn(want_mark[kind], marks, (name, label, fmt))
                    cursor = tour.performance(i).state_at(film.edits.source_time(t, tour.shots[i]['clock'])).cursor
                    lay = film.editor.layout(st.main, st.mono, digits=tour.edit_texts[i][2], minimap=True)
                    _, top = tour.performance(i).view(film.edits.source_time(t, tour.shots[i]['clock']), lay.rows, lay.cols)
                    self.assertTrue(top <= cursor[0] < top + lay.rows, (name, label, fmt))
                    side = [b for m, b in cv.marks if m.startswith('sidebar.')]
                    on_side = 'callout.card' in marks and st.side is not None and st.side.contains(marks['callout.card'])
                    for m, b in cv.marks:
                        if m.startswith('callout.') and on_side:   # a card in the side bar's free area
                            continue
                        if m.startswith(('editor.', 'callout.')):
                            self.assertTrue(st.main.contains(b), (name, label, fmt, m, b))
                            for c in side:
                                self.assertFalse(b.intersects(c), (name, label, fmt, m))
                    if 'editor.badge' in marks and 'callout.card' in marks:
                        self.assertFalse(marks['editor.badge'].intersects(marks['callout.card']), (label, fmt))
                    if 'editor.badge' in marks:
                        self.assertTrue(marks['editor.text'].contains(marks['editor.badge']), (label, fmt))

    def test_develop_callout_on_screen(self):
        tour = self.develop_tours()['callout']
        (i,) = [k for k, s in enumerate(tour.shots) if s['kind'] == 'edit']
        c = tour.shots[i]['callouts'][0]
        n = tour.shot_frames[i] + round((c['at'] + 1.0) * tour.fps)
        for fmt in ('16x9', '9x16', '1x1'):
            marks = dict(tour.film.frame(n, fmt).marks)
            self.assertIn('callout.card', marks, fmt)
            self.assertTrue(marks['editor.text'].contains(marks['callout.card']), fmt)
            self.assertFalse(marks['callout.card'].intersects(marks['callout.target']), fmt)

    def test_callout_box_keeps_clear_of_point_and_hunk(self):
        area = vb.Box(100, 50, 1200, 900)
        lh, w, h = 35.0, 600, 150
        for top in range(0, 26):
            for size in (1, 3, 8, 20, 26):
                band = vb.Box(area.x, area.y + top * lh, area.w, size * lh)
                for prow in range(top, min(26, top + size)):
                    point = vb.Box(area.x, area.y + prow * lh, area.w, lh)
                    card = film.callout_box(area, band, point, w, h, lh / 2)
                    self.assertTrue(area.contains(card), (top, size, prow))
                    self.assertFalse(card.intersects(point), (top, size, prow))
                    visible = max(0, min(band.bottom, area.bottom) - max(band.y, area.y))
                    if area.h - visible - lh >= h + lh:   # room above or below: the hunk stays clear
                        room = max(band.y - area.y, area.bottom - band.bottom)
                        if room >= h + lh / 2:
                            self.assertFalse(card.intersects(band), (top, size, prow))

    def test_develop_callout_beside_point(self):
        tour = self.develop_tours()['callout']
        (i,) = [k for k, s in enumerate(tour.shots) if s['kind'] == 'edit']
        s = tour.shots[i]
        for c in s['callouts']:
            for dt in (0.4, 1.0, (c['until'] - c['at']) * 0.8):
                n = tour.shot_frames[i] + round((c['at'] + dt) * tour.fps)
                for fmt in ('16x9', '9x16', '1x1'):
                    marks = dict(tour.film.frame(n, fmt).marks)
                    if 'callout.card' not in marks:
                        continue
                    card = marks['callout.card']
                    self.assertFalse(card.intersects(marks['callout.point']), (fmt, n))
                    area = marks['editor.text']
                    hunk = marks['callout.hunk']
                    if max(hunk.y - area.y, area.bottom - hunk.bottom) >= card.h + 20:
                        self.assertFalse(card.intersects(hunk), (fmt, n))

    def test_free_callout_box_keeps_three_rows_from_point(self):
        area = vb.Box(100, 50, 1200, 26 * 35.0)
        lh, w, h = 35.0, 600, 150
        ends = [area.x + 1100.0] * 26   # long lines everywhere ...
        for k in (2, 3, 4, 5, 6, 7, 14, 15, 16, 17, 18):   # ... but for two runs of short ones
            ends[k] = area.x + 200.0
        for prow in range(26):
            point = vb.Box(area.x, area.y + prow * lh, area.w, lh)
            card = film.free_callout_box(area, ends, [point], point, w, h, lh, lh / 2)
            if card is None:
                continue
            self.assertTrue(area.contains(card), prow)
            self.assertGreaterEqual(max(point.y - card.bottom, card.y - point.bottom), 3 * lh - 1e-6, prow)
            rows = range(int((card.y - area.y) // lh), int((card.bottom - 1 - area.y) // lh) + 1)
            self.assertTrue(all(ends[r] < card.x for r in rows), (prow, list(rows)))
        point = vb.Box(area.x, area.y + 10 * lh, area.w, lh)   # free space on both sides: the nearer
        card = film.free_callout_box(area, ends, [point], point, w, h, lh, lh / 2)
        self.assertLess(card.y, point.y)
        self.assertIsNone(film.free_callout_box(area, [area.x + 1100.0] * 26, [point], point, w, h, lh, lh / 2))
        card = film.far_callout_box(area, [point], [point], w, h, lh, lh / 2)   # none free: over code, far away
        self.assertGreater(card.bottom, area.bottom - lh)

    def test_side_bar_free_area(self):
        box = vb.Box(1376, 24, 520, 1032)
        files = [{'path': 'a.py', 'added': 1, 'deleted': 0}]
        short = {'subject': 'Fix it', 'body': 'One line.', 'short': 'abc', 'author': 'A', 'date': '2026-01-01'}
        free = sidebar.free_area(box, commit=short, files=files, xref_stack=(), graph=True)
        self.assertTrue(box.contains(free))
        self.assertGreater(free.h, 200)
        cv = vb.Canvas(1920, 1080, '#000')
        sidebar.draw(cv, box, commit=short, files=files, current_path='a.py', xref_stack=(), graph=lambda c, b: None)
        commit = dict(cv.marks)['sidebar.commit']
        self.assertTrue(commit.contains(free))
        long = dict(short, body=' '.join(['word'] * 400))
        free = sidebar.free_area(box, commit=long, files=files, xref_stack=(), graph=True)
        self.assertTrue(free is None or free.h < 40)

    def test_develop_callout_far_from_point(self):
        tours = self.develop_tours()
        seen = 0
        for name in ('callout', 'develop', 'big'):
            tour = tours[name]
            for i, s in enumerate(tour.shots):
                for k, c in enumerate(s.get('callouts') or ()):
                    for dt in (0.1, 0.5, 1.0, (c['until'] - c['at']) * 0.9):
                        n = tour.shot_frames[i] + round((c['at'] + dt) * tour.fps)
                        cv = tour.film.frame(n, '16x9')
                        marks = dict(cv.marks)
                        kind, box = film.develop_callout_at(tour, film.stage(cv), i, k, tour.at(n)[1])
                        if 'callout.card' not in marks:
                            continue
                        card, point = marks['callout.card'], marks['callout.point']
                        self.assertFalse(card.intersects(point), (name, n))
                        if kind == 'free':
                            seen += 1
                            lh = film.stage(cv).mono.size * 1.35
                            gap = max(point.y - card.bottom, card.y - point.bottom)
                            self.assertGreaterEqual(gap, 3 * lh - 2, (name, n))
        self.assertTrue(seen)

    def test_develop_draw_is_pure_and_flat(self):
        import time
        ch = collect.collect(fixtures.make_budget_repo(self.tmp.name + '/flat'), 'HEAD')
        tour = film.Tour(plan.build(ch, 2, 'develop'), ch, formats=('16x9',), log=io.StringIO())
        (i,) = [k for k, s in enumerate(tour.shots) if s['kind'] == 'edit']
        first, last = tour.shot_frames[i] + 3, tour.shot_frames[i + 1] - 3
        mid = (first + last) // 2
        for n in (first, mid, last, mid + 1):
            a = tour.film.frame(n, '16x9').bgra()
            tour.film.frame(n + 11, '16x9')
            self.assertEqual(a, tour.film.frame(n, '16x9').bgra(), n)

        def cost(n):
            best = None
            for _ in range(3):
                t0 = time.perf_counter()
                tour.film.frame(n, '16x9')
                dt = time.perf_counter() - t0
                best = dt if best is None else min(best, dt)
            return best

        self.assertLessEqual(cost(last), 4 * cost(first))

    def test_develop_recap(self):
        tour = self.develop_tours()['change']
        files, written = film.recap(tour.plan)
        self.assertEqual(files, ['src/lib.py', 'src/app.py', 'tests/test_app.py'])
        self.assertIn(('frob', 'src/lib.py', 1), written)
        n = next(n for n, label in tour.film.keyframes if label == 'end')
        for fmt in ('16x9', '9x16', '1x1'):
            marks = dict(tour.film.frame(n, fmt).marks)
            for name in ('card.header', 'card.visited', 'card.defs'):
                self.assertIn(name, marks, fmt)

    def test_develop_recap_lists_a_rewritten_definition_once(self):
        nodes = [{'name': 'frob', 'path': 'lib.py', 'line': 3, 'touched': True},
                 {'name': 'main', 'path': 'lib.py', 'line': 9, 'touched': True},
                 {'name': 'helper', 'path': 'util.py', 'line': 1, 'touched': False},
                 {'name': 'frob', 'path': 'lib.py', 'line': 5, 'touched': True}]
        p = plan.Plan(version=2, fps=30, base='a', target='c', label='a..c', is_range=True, commit={}, files=[], verbose=0, mode='develop',
                      graph={'nodes': nodes, 'edges': []})
        self.assertEqual(film.recap(p)[1], [('frob', 'lib.py', 5), ('main', 'lib.py', 9)])

    def test_develop_views_meet_at_the_cuts(self):
        tour = self.develop_tours()['change']
        for i, s in enumerate(tour.shots):
            if s['kind'] != 'edit' or i + 1 >= len(tour.shots) or tour.shots[i + 1]['scene'] == 'card':
                continue
            end = tour.film.frame(tour.shot_frames[i + 1] - 1, '16x9')
            start = tour.film.frame(tour.shot_frames[i + 1], '16x9')
            a, b = dict(end.marks)['editor.text'], dict(start.marks)['editor.text']
            for x, y in zip(a, b):
                self.assertAlmostEqual(x, y, delta=1.0)

    def test_interstitial_all_formats(self):
        repo, sha = fixtures.make_rewrite_repo(self.tmp.name + '/inter')
        combined, patches = collect.series(repo, f'{sha["a"]}..{sha["c"]}')
        for mode in ('tour', 'develop'):
            tour = film.Tour(plan.build(combined, 0, mode, patches), combined, patches=patches, log=io.StringIO())
            (n,) = [n for n, label in tour.film.keyframes if label == 'interstitial 1']
            i, t = tour.at(n)
            self.assertEqual(tour.shots[i]['kind'], 'patch')
            for fmt in ('16x9', '9x16', '1x1'):
                cv = tour.film.frame(n, fmt)
                marks = dict(cv.marks)
                frame = vb.Box.of(cv)
                for name in ('card.deck', 'card.series'):
                    self.assertTrue(frame.contains(marks[name]), (mode, fmt, name))
                self.assertFalse(marks['card.deck'].intersects(marks['card.series']), (mode, fmt))
            # the side bar follows the patch: the shot after the card shows commit 2
            j = i + 1
            after = tour.shot_frames[j] + 20
            self.assertEqual(tour.shots[j]['patch'], 1)
            cv = tour.film.frame(after, '16x9')
            self.assertEqual(tour.patch_info(1)['commit']['short'], patches[1].commits[0].short)
            self.assertIn('sidebar.commit', dict(cv.marks))
        self.assertEqual(film.series_progress(1, 2, 0), 0)
        self.assertEqual(film.series_states(2, 4), ['done', 'done', 'current', 'pending'])

    def test_series_strip_never_marks_the_next_patch_done(self):
        from videobuilder.capabilities.ui.progress_steps_connector_fill import step_states
        for n in (2, 3, 7):
            for k in range(1, n):
                for t in (0, 0.3, plan.INTER_REVEAL, plan.INTER_DUR):
                    _, nodes = step_states(n, film.series_progress(k, n, t))
                    self.assertEqual(nodes[:k], [1.0] * k, (n, k, t))   # the patches played are ticked
                    self.assertEqual(nodes[k:], [0.0] * (n - k), (n, k, t))   # the next one and on are not
                self.assertGreater(film.series_progress(k, n, plan.INTER_DUR), (k - 1 + 0.8) / (n - 1))
        # drawn: patch 2's marker (of 2) is a ring larger than a marker, at the strip's right end
        repo, sha = fixtures.make_rewrite_repo(self.tmp.name + '/strip')
        combined, patches = collect.series(repo, f'{sha["a"]}..{sha["c"]}')
        tour = film.Tour(plan.build(combined, 0, 'tour', patches), combined, patches=patches, log=io.StringIO())
        (n,) = [n for n, label in tour.film.keyframes if label == 'interstitial 1']
        for fmt in ('16x9', '9x16', '1x1'):
            cv = tour.film.frame(n, fmt)
            marks = dict(cv.marks)
            ring, strip = marks['card.series.current'], marks['card.series']
            self.assertTrue(strip.contains(ring), fmt)
            self.assertGreater(ring.cx, strip.cx)
            self.assertGreater(ring.w, 2 * (22 if fmt != '9x16' else 26))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'needs ffmpeg')
    def test_render_develop_smoke(self):
        from videobuilder.render import render
        tour = self.develop_tours()['change']
        (i,) = [k for k, s in enumerate(tour.shots) if s['kind'] == 'edit'][:1]
        out = os.path.join(self.tmp.name, 'develop.mp4')
        f0 = tour.shot_frames[i]
        render(tour.film, out, format='16x9', jobs=1, frames=(f0, f0 + 10), log=io.StringIO())
        r = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', out], capture_output=True,
                           text=True, check=True)
        self.assertIn('video', [s['codec_type'] for s in json.loads(r.stdout)['streams']])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'needs ffmpeg')
    def test_render_smoke(self):
        from videobuilder.render import render
        out = os.path.join(self.tmp.name, 'smoke.mp4')
        render(self.tour.film, out, format='1x1', jobs=1, frames=(0, 15), log=io.StringIO())
        r = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', out], capture_output=True,
                           text=True, check=True)
        streams = json.loads(r.stdout)['streams']
        self.assertIn('video', [s['codec_type'] for s in streams])


class TestVoiceFilm(unittest.TestCase):
    """Captions with the voice (a fake engine): where they sit in every format."""

    @classmethod
    def setUpClass(cls):
        from git_video import voice
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {vb.sfx.ENV: os.path.join(cls.tmp.name, 'no-sfx'),
                                               'XDG_CACHE_HOME': os.path.join(cls.tmp.name, 'cache')})
        cls.env.start()
        repo, sha = fixtures.make_callout_repo(cls.tmp.name)
        cls.change = collect.collect(repo, sha['head'])
        repo, sha = fixtures.make_range_repo(cls.tmp.name)
        cls.series = collect.series(repo, f'{sha["a"]}..{sha["c"]}')
        cls.tours = {}
        for mode in ('tour', 'develop'):
            p = voice.narrate(cls.change, [cls.change], 0, mode, fixtures.FakeEngine(), 'bf_isabella')
            cls.tours[mode] = film.Tour(p, cls.change, log=io.StringIO())
        change, patches = cls.series
        p = voice.narrate(change, patches, 0, 'tour', fixtures.FakeEngine(), 'bf_isabella')
        cls.tours['series'] = film.Tour(p, change, log=io.StringIO(), patches=patches)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp.cleanup()

    def mid(self, tour, q):
        return round((q['start'] + q['end']) / 2 * tour.fps)

    def test_captions_in_every_format(self):
        seen = set()
        for name, tour in self.tours.items():
            for q in tour.voice['phrases']:
                n = self.mid(tour, q)
                shot = tour.shots[q['shot']]
                for fmt in ('16x9', '9x16', '1x1'):
                    cv = tour.film.frame(n, fmt)
                    caps = [b for m, b in cv.marks if m == 'caption']
                    self.assertEqual(len(caps), 1, (name, fmt, q['text']))
                    cap = caps[0]
                    why = (name, fmt, shot['kind'], q['text'])
                    self.assertTrue(vb.captions.safe_area(cv).contains(cap), why)
                    st = film.stage(cv, voice=True)
                    self.assertLess(cap.h, 2 * vb.text.line_height(st.caption_font), why)   # one line, never three
                    words = [b for m, b in cv.marks if m == 'caption.word']
                    self.assertEqual(len(words), 1, why)
                    self.assertTrue(cap.contains(words[0]), why)
                    marks = dict(cv.marks)
                    others = [(m, b) for m, b in cv.marks if m.split('.')[0] in ('callout', 'editor', 'sidebar', 'card')
                              or m in ('lower-third', 'avatars', 'xref.word', 'xref.pointer')]
                    for m, b in others:
                        self.assertFalse(cap.intersects(b), (*why, m))
                    if shot['scene'] == 'editor':
                        seen.add((fmt, 'editor'))
                        self.assertTrue(st.caption.contains(cap), why)
                        self.assertGreaterEqual(cap.y, marks['editor.echo'].bottom, why)
                        if fmt == '16x9':   # under the pane
                            self.assertLess(cap.x, marks['editor.text'].right)
                            self.assertLessEqual(cap.right, st.side.x)
                        else:   # in the band between the code and the side bar
                            self.assertLessEqual(cap.bottom, st.side.y, why)
                    elif shot['scene'] == 'card':
                        seen.add((fmt, shot['kind']))
        for fmt in ('16x9', '9x16', '1x1'):
            self.assertLessEqual({(fmt, 'editor'), (fmt, 'title'), (fmt, 'end'), (fmt, 'patch')}, seen)

    def test_caption_off_point_and_the_hunk(self):
        tour = self.tours['develop']
        for q in tour.voice['phrases']:
            if tour.shots[q['shot']]['kind'] != 'edit':
                continue
            for fmt in ('16x9', '9x16', '1x1'):
                cv = tour.film.frame(self.mid(tour, q), fmt)
                marks = dict(cv.marks)
                text = marks['editor.text']
                self.assertGreaterEqual(marks['caption'].y, text.bottom)   # point and the hunk are in the text area

    def test_rows_and_cols_with_the_voice(self):
        for fmt in ('16x9', '9x16', '1x1'):
            cv = vb.film.as_format(fmt)
            st = film.stage(cv, voice=True)
            self.assertGreaterEqual(film.editor.layout(st.main, st.mono).rows, plan.PLAN_ROWS, fmt)
            lay = film.editor.layout(st.main, st.mono, digits=6, minimap=True)
            self.assertGreaterEqual(int(lay.text_box.w // lay.cell_w), plan.PLAN_COLS, fmt)
            self.assertEqual(film.stage(cv), film.stage(cv, voice=False))
            self.assertIsNone(film.stage(cv).caption)

    def test_draw_is_pure(self):
        tour = self.tours['tour']
        for q in tour.voice['phrases'][:4]:
            n = self.mid(tour, q)
            for fmt in ('16x9', '9x16'):
                a = tour.film.frame(n, fmt).bgra()
                tour.film.frame(n + 11, fmt)
                self.assertEqual(a, tour.film.frame(n, fmt).bgra())

    def test_caption_keyframes(self):
        tour = self.tours['tour']
        caps = [(n, label) for n, label in tour.film.keyframes if label.startswith('caption ')]
        self.assertEqual([label for _, label in caps], ['caption title', 'caption hunk'])
        for n, _ in caps:
            self.assertTrue(any(round(q['start'] * tour.fps) <= n < round(q['end'] * tour.fps)
                                for q in tour.voice['phrases']))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'needs ffmpeg')
    def test_render_voiced(self):
        from videobuilder.render import render
        tour = self.tours['tour']
        q = tour.voice['phrases'][0]
        n = round(q['start'] * tour.fps)
        out = os.path.join(self.tmp.name, 'voiced.mp4')
        render(tour.film, out, format='16x9', jobs=1, frames=(n - 5, n + 10), log=io.StringIO())
        r = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', out], capture_output=True,
                           text=True, check=True)
        self.assertEqual(sorted(s['codec_type'] for s in json.loads(r.stdout)['streams']), ['audio', 'video'])

    def test_no_voice_no_captions(self):
        p = plan.build(self.change)
        tour = film.Tour(p, self.change, log=io.StringIO())
        for n, _ in tour.film.keyframes:
            for fmt in ('16x9', '9x16', '1x1'):
                self.assertNotIn('caption', dict(tour.film.frame(n, fmt).marks))


if __name__ == '__main__':
    unittest.main()

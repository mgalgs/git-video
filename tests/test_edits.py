import bisect
import tempfile
import unittest

from git_video import collect, editor, edits, plan, worddiff
from tests import fixtures


def script_of(change, path, elide=None):
    f = next(f for f in change.files if f.path == path)
    old = edits.split_lines(change.old_text(path) or '')
    new = edits.split_lines(f.text or '')
    return old, new, edits.build(old, new, f.hunks, elide=elide, seed=path)


def expected_at(old, steps, t):
    """The buffer at natural time t, worked out from the steps alone: every
    step before t applied whole, the one running applied as far as it has got."""
    lines = list(old)
    for st in steps:
        tau = t - st['at']
        if tau < 0:
            break
        if tau >= st['dur']:
            lines = edits.apply(lines, [st])
            continue
        if st['kind'] == 'kill' and tau >= st['cut']:
            lines = edits.apply(lines, [st])
        elif st['kind'] == 'type':
            body = st['text'][:-1] if st['open'] else st['text']
            n = bisect.bisect_right(st['times'][:-1] if st['open'] else st['times'], tau)
            text = body[:n] + ('\n' if st['open'] and tau >= st['times'][-1] else '')
            lines = edits.apply(lines, [{**st, 'text': text}])
        elif st['kind'] == 'fill':
            rows = st['text'][1:].split('\n')
            m = edits._poured(len(rows), tau, st['dur'])
            lines = edits.apply(lines, [{**st, 'text': '\n' + '\n'.join(rows[:m]) if m else ''}])
        break
    return lines


class TestEdits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo, cls.sha = fixtures.make_repo(cls.tmp.name)
        cls.develop = collect.collect(fixtures.make_develop_repo(cls.tmp.name), 'HEAD')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_apply_reproduces_new_text(self):
        changes = [self.develop, collect.collect(self.repo, self.sha['change']),
                   collect.collect(self.repo, self.sha['root'])]
        statuses = set()
        for ch in changes:
            for f in plan.order_files(ch):
                for elide in (None, 30):
                    old, new, sc = script_of(ch, f.path, elide)
                    self.assertEqual(edits.apply(old, sc['steps']), new, (f.path, elide))
                    final = edits.Performance(old, sc['steps']).state_at(sc['natural'])
                    self.assertEqual([x for _, x in final.lines], new, f.path)
                    statuses.add(f.status)
        self.assertEqual(statuses, {'A', 'M', 'D', 'R'})
        old, new, sc = script_of(self.develop, 'old.py')   # a deleted file: C-x h, C-w
        self.assertEqual(new, [])
        self.assertEqual([(st['kind'], st.get('keys')) for st in sc['steps']], [('kill', 'C-x h')])
        old, new, sc = script_of(self.develop, 'tabs.py')
        self.assertEqual(old[1], '    if True:')   # tabs expanded, as the editor draws them

    def test_apply_steps_reproduce_new_text(self):
        big = collect.collect(fixtures.make_big_repo(self.tmp.name), 'HEAD')
        for ch, path in ((self.develop, 'core.py'), (big, 'big.py')):
            old, new, _ = script_of(ch, path)
            f = next(f for f in ch.files if f.path == path)
            n = len(f.hunks)
            self.assertGreaterEqual(n, 2)
            for shown in ({0}, set(), {n - 1}, set(range(1, n, 2)), {1, n - 2}):
                with self.subTest(path=path, shown=shown):
                    self.check_shown(old, new, f.hunks, path, shown)

    def check_shown(self, old, new, hunks, path, shown):
        n = len(hunks)
        sc = edits.build(old, new, hunks, seed=path, shown=shown)
        steps = sc['steps']
        self.assertEqual(edits.apply(old, steps), new)
        perf = edits.Performance(old, steps)
        self.assertEqual([x for _, x in perf.state_at(sc['natural']).lines], new)
        # one apply step per run of hunks not shown, in order
        runs = sum(1 for hi in range(n) if hi not in shown and (hi == 0 or hi - 1 in shown))
        applied = [st for st in steps if st['kind'] == 'apply']
        self.assertEqual(len(applied), runs)
        self.assertEqual(sorted(h for st in applied for h in st['hunks']), [h for h in range(n) if h not in shown])
        settled = {j for st in applied for *_, settle in st['parts'] for _, j in settle}
        self.assertFalse(settled & set(sc['written']))
        for st in applied:   # the marker shows while the step runs, and the lines between hunks keep their origin
            state = perf.state_at(st['at'] + st['dur'] / 2)
            self.assertEqual(state.fold[1:], (len(st['hunks']), 'applied'))
            self.assertEqual([x for _, x in state.lines], edits.apply(old, steps[:steps.index(st) + 1]))
            for (a, _, ta, _), (b, _, _, _) in zip(st['parts'], st['parts'][1:]):
                between = state.lines[a + ta.count('\n'):b]
                self.assertTrue(all(o and o[0] == 'old' for o, _ in between), (a, b))

    def test_lines_are_coloured_from_their_file(self):
        for f in plan.order_files(self.develop):
            old, new, sc = script_of(self.develop, f.path, 30)
            for origin, text in edits.Performance(old, sc['steps']).state_at(sc['natural']).lines:
                self.assertIsNotNone(origin, (f.path, text))
                self.assertEqual(text, (old if origin[0] == 'old' else new)[origin[1]], f.path)

    def test_modified_line_edited_in_place(self):
        ch = collect.collect(self.repo, self.sha['change'])
        old, new, sc = script_of(ch, 'src/lib.py')
        on_line = [st for st in sc['steps'] if st['kind'] in ('kill', 'type')]
        self.assertEqual([st['kind'] for st in on_line], ['kill', 'type', 'kill', 'type'])
        for st in on_line:
            if st['kind'] == 'kill':
                self.assertEqual(st['from'][0], st['to'][0])
                self.assertEqual(st['from'][0], 1)
            else:
                self.assertEqual(st['line'], 1)
                self.assertNotIn('\n', st['text'])
        self.assertEqual([st['text'] for st in on_line], ['+', '*', '1', '2'])

    def test_line_edits(self):
        for a, b in (('    return x + 1', '    return x * 2'), ('a  = 1', 'a = 1'), ('', 'new'), ('gone', ''),
                     ('f(a, b)', 'g(a, b, c)')):
            out, delta = a, 0
            for col, dl, ins in worddiff.line_edits(a, b):
                c = col + delta
                out = out[:c] + ins + out[c + dl:]
                delta += len(ins) - dl
            self.assertEqual(out, b)

    def test_typing_keeps_an_uneven_cadence(self):
        old, new, sc = script_of(self.develop, 'new50.py')
        (typed,) = [st for st in sc['steps'] if st['kind'] == 'type']
        gaps = [b - a for a, b in zip(typed['times'], typed['times'][1:]) if b > a]
        self.assertGreater(max(gaps), 1.5 * min(gaps))
        self.assertEqual(typed['times'], sorted(typed['times'][:-1]) + [typed['times'][-1]])

    def test_state_matches_reference(self):
        ch = collect.collect(fixtures.make_budget_repo(self.tmp.name), 'HEAD')
        for change, path, elide, snap in ((ch, 'work.py', None, edits.SNAP_EVERY), (ch, 'work.py', None, 4),
                                          (self.develop, 'core.py', 30, edits.SNAP_EVERY),
                                          (self.develop, 'new100.py', 30, 1)):
            old, new, sc = script_of(change, path, elide)
            steps = sc['steps']
            perf = edits.Performance(old, steps, snap_every=snap)
            times = [0.0, sc['natural']]
            for st in steps:
                times += [st['at'] - 0.001, st['at'] + 0.001]
                if st['kind'] == 'kill':
                    times += [st['at'] + st['cut'] - 0.001, st['at'] + st['cut'] + 0.001]
            times += [sc['natural'] * k / 60 for k in range(60)]
            for t in times:
                state = perf.state_at(t)
                self.assertEqual([x for _, x in state.lines], expected_at(old, steps, t), (path, t))
                self.assertLess(perf.replays, snap)
            self.assertGreater(len(steps), snap, path)   # past a snapshot at least

    def test_view_keeps_point_in_the_window(self):
        ch = collect.collect(fixtures.make_budget_repo(self.tmp.name + '/v'), 'HEAD')
        old, new, sc = script_of(ch, 'work.py')
        perf = edits.Performance(old, sc['steps'])
        for rows in (26, 40):
            for k in range(200):
                t = sc['natural'] * k / 200
                scroll, top = perf.view(t, rows)
                if scroll is None:
                    line = perf.state_at(t).cursor[0]
                    self.assertTrue(top <= line < top + rows, (rows, t, top, line))

    def test_view_counts_wrapped_rows_and_never_bobs(self):
        ch = collect.collect(fixtures.make_wrap_repo(self.tmp.name), 'HEAD')
        old, new, sc = script_of(ch, 'wrap.py')
        self.assertIn('kill', [st['kind'] for st in sc['steps']])
        for rows, cols in ((20, 40), (26, 67)):
            perf = edits.Performance(old, sc['steps'])
            m = min(edits.MARGIN, (rows - 1) // 4)
            self.assertNotEqual(perf.tops(rows, cols), perf.tops(rows), (rows, cols))   # the wrap is counted
            prev, scrolling = None, False
            for k in range(int(sc['natural'] * 30)):
                t = k / 30
                scroll, top = perf.view(t, rows, cols)
                state = perf.state_at(t)
                if scroll is not None:
                    a, b, into, dur, far = scroll
                    if not scrolling and prev is not None:
                        self.assertEqual(a, prev, (rows, cols, t))   # a jump glides from where the view was
                    top, scrolling = a + (b - a) * into / dur, True
                    prev = top
                    continue
                scrolling = False
                line = state.cursor[0]
                if prev is not None and top < prev:
                    self.assertLess(line, prev + m, (rows, cols, t, prev, top))   # only point near the top scrolls back
                wrapped = [editor.wrap_rows(x, cols) for _, x in state.lines]
                self.assertLessEqual(top, line, (rows, cols, t))
                self.assertLessEqual(sum(wrapped[top:line + 1]), rows, (rows, cols, t))   # all of point's line shows
                prev = top

    def test_jumps_to_far_hunks(self):
        ch = collect.collect(fixtures.make_far_repo(self.tmp.name), 'HEAD')
        old, new, sc = script_of(ch, 'far.py')
        jumps = [st for st in sc['steps'] if st['kind'] == 'jump']
        self.assertEqual(len(jumps), 1)
        self.assertEqual(jumps[0]['keys'], f'M-g g {jumps[0]["line"] + 1}')
        self.assertEqual(jumps[0]['scroll'], 'far')
        self.assertEqual(jumps[0]['times'], sorted(jumps[0]['times']))


if __name__ == '__main__':
    unittest.main()

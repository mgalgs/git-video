import json
import os
import re
import subprocess
import tempfile
import unittest
from unittest import mock

from git_video import cli, collect, condense, film, plan
from tests import fixtures
from tests.test_cli import quiet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = (15, 30, 60, 120)


def plan_json(repo, rev, *extra):
    """cli.main --plan-json: (code, the plan as a dict or None, stderr)."""
    code, out, err = quiet(cli.main, ['-C', repo, '--plan-json', *extra, rev])
    return code, (json.loads(out) if code == 0 else None), err


class TestScore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_called_new_definition_outranks_a_test_edit(self):
        repo = os.path.join(self.tmp.name, 'called')
        fixtures.init(repo)
        fixtures.write(repo, 'app.py', 'def run():\n    return 1\n')
        fixtures.write(repo, 'tests/test_app.py', 'def test_run():\n    assert True\n')
        fixtures.commit(repo, 'Start')
        fixtures.write(repo, 'util.py', 'def helper(x):\n    y = x * 2\n    return y\n')
        fixtures.write(repo, 'app.py', 'from util import helper\n\n\ndef run():\n    return helper(1)\n')
        fixtures.write(repo, 'tests/test_app.py',
                       'def test_run():\n    assert True\n    a = 1\n    b = 2\n    assert a < b\n')
        fixtures.commit(repo, 'Add a helper')
        scores = condense.score_hunks(collect.collect(repo, 'HEAD'))
        helper, test = scores['util.py', 0], scores['tests/test_app.py', 0]
        self.assertGreater(helper.value, test.value)
        self.assertIn('new def helper', helper.why)
        self.assertIn('called from 1 hunk', helper.why)
        self.assertTrue(test.why.startswith('test, 3 lines'), test.why)

    def test_named_hunk_outranks_an_unnamed_one(self):
        repo = os.path.join(self.tmp.name, 'named')
        fixtures.init(repo)
        gap = '\n' * 12
        old = f'def alpha(x):\n    return x + 1\n{gap}def beta(x):\n    return x + 2\n'
        fixtures.write(repo, 'lib.py', old)
        fixtures.commit(repo, 'Start')
        fixtures.write(repo, 'lib.py', old.replace('x + 1', 'x * 3').replace('x + 2', 'x * 4'))
        fixtures.commit(repo, 'Rework both\n\nThe alpha() function now triples its input.')
        scores = condense.score_hunks(collect.collect(repo, 'HEAD'))
        a, b = scores['lib.py', 0], scores['lib.py', 1]
        self.assertGreater(a.value, b.value)
        self.assertIn('named in the message', a.why)
        self.assertNotIn('named in the message', b.why)


class TestSelection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.big = collect.collect(fixtures.make_big_repo(cls.tmp.name), 'HEAD')
        cls.repo, cls.sha = fixtures.make_repo(cls.tmp.name)
        cls.change = collect.collect(cls.repo, cls.sha['change'])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_left_out_is_everything_not_visited(self):
        dev = collect.collect(fixtures.make_develop_repo(self.tmp.name), 'HEAD')
        for mode, ch in (('tour', self.big), ('develop', dev)):
            p = plan.build(ch, 0, mode, duration=15)
            visited = {s.get('to', s['path']) for s in p.shots if s['kind'] in ('dive', 'travel', 'open', 'edit')}
            left = set(p.patches[0]['left_out'])
            visitable = ({f.path for f in ch.files if f.text is not None} if mode == 'tour'
                         else {f.path for f in plan.order_files(ch)})
            self.assertTrue(left, mode)
            self.assertFalse(visited & left, mode)
            self.assertEqual(visited | left, visitable, mode)
            self.assertEqual(film.recap(p)[2], p.patches[0]['left_out'], mode)

    def test_map_and_totals_cover_the_whole_change(self):
        p = plan.build(self.big, 0, 'tour', duration=15)
        self.assertTrue(p.patches[0]['left_out'])
        (m,) = [s for s in p.shots if s['kind'] == 'map']
        tiled = {t['path'] for g in p.map['groups'] for t in g['tiles'] if 'path' in t}
        self.assertEqual(set(m['lit']), {f.path for f in self.big.files} & tiled)
        self.assertEqual(sum(f['added'] for f in p.files), self.big.added)
        self.assertEqual(len(p.files), len(self.big.files))

    def test_xref_caps(self):
        for target, most in ((20, 0), (45, 1), (120, plan.XREF_CAP_TOTAL)):
            p = plan.build(self.change, 3, 'tour', duration=target)
            n = sum(1 for s in p.shots if s['kind'] == 'xref-push')
            self.assertLessEqual(n, most, target)
            if target == 20:
                self.assertEqual(n, 0)

    def test_kept_hunks_keep_story_order_and_nominal_reads(self):
        repo, shas = fixtures.make_series_repo(self.tmp.name)
        ch = collect.collect(repo, f'{shas[0]}..{shas[-1]}')
        p = plan.build(ch, 0, 'tour', duration=60)
        order = [f.path for f in ch.files]
        hunks = [s for s in p.shots if s['kind'] == 'hunk']
        self.assertEqual([s['path'] for s in hunks], sorted((s['path'] for s in hunks), key=order.index))
        nominal = {(s['path'], s['index']): s['dur'] for s in plan.build(ch, 0, 'tour').shots if s['kind'] == 'hunk'}
        for s in hunks:   # selection first: what is kept is read at least as long as it would be anyway
            self.assertGreaterEqual(s['dur'], nominal[s['path'], s['index']] - 0.01, s['path'])
        self.assertLess(len(hunks), len(nominal))


class TestResize(unittest.TestCase):
    def test_floors_and_hunk_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = collect.collect(fixtures.make_big_repo(tmp), 'HEAD')
            repo, sha = fixtures.make_repo(tmp)
            change = collect.collect(repo, sha['change'])
            for ch in (big, change):
                p = plan.build(ch, 3, 'tour', duration=15)
                for s in p.shots:
                    if s['kind'] in condense.LIMITS:
                        self.assertGreaterEqual(s['dur'], condense.LIMITS[s['kind']][0] - 1e-3, s['kind'])
                    if s['kind'] == 'hunk':
                        scroll, hold, md, read = condense._hunk_parts(s)
                        self.assertGreaterEqual(read, condense.READ_MIN - 1e-3)
                        if s.get('morph'):
                            self.assertGreaterEqual(hold, condense.MORPH_MIN[0] - 1e-3)
                            self.assertGreaterEqual(md, condense.MORPH_MIN[1] - 1e-3)
                        for c in s['callouts']:
                            self.assertGreaterEqual(c['until'] - c['at'], condense.CALLOUT_MIN_EACH - 0.2 - 1e-3)
                            self.assertLessEqual(c['until'], s['dur'])
                self.assertLessEqual(abs(p.total - 15), condense.tolerance(15))

    def test_cards_give_first_and_edits_never(self):
        shots = [{'kind': 'title', 'dur': 3.4}, {'kind': 'edit', 'dur': 9.0}, {'kind': 'end', 'dur': 3.0}]
        condense.resize(shots, 15.4)
        self.assertEqual([s['dur'] for s in shots], [3.4, 9.0, 3.0])
        condense.resize(shots, 14.0)
        self.assertEqual(shots[1]['dur'], 9.0)
        self.assertAlmostEqual(sum(s['dur'] for s in shots), 14.0, places=2)
        condense.resize(shots, 10.0)   # past the floors: as near as they allow
        self.assertEqual([s['dur'] for s in shots], [2.6, 9.0, 2.0])
        shots = [{'kind': 'title', 'dur': 3.4}, {'kind': 'xref-push', 'dur': 4.4}, {'kind': 'end', 'dur': 3.0}]
        condense.resize(shots, 9.6)   # the cards shrink before the content does
        self.assertEqual(shots[1]['dur'], 4.4)
        self.assertAlmostEqual(sum(s['dur'] for s in shots), 9.6, places=2)


class TestSummary(unittest.TestCase):
    def test_summary_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = fixtures.make_big_repo(tmp)
            p = plan.build(collect.collect(repo, 'HEAD'), 0, 'tour', duration=20)
            lines = condense.summary(p).split('\n')
            self.assertRegex(lines[0], r'^target 20s, plan \d+\.\ds: kept \d+ of \d+ hunks in \d+ of 2 files$')
            kept = [x for x in lines if re.match(r'^\S+#\d+: score \d+\.\d: ', x)]
            left = [x for x in lines if re.match(r'^\S+: left out \(\+\d+ −\d+\)$', x)]
            self.assertEqual(len(kept), sum(len(v) for v in p.patches[0]['kept'].values()))
            self.assertEqual(len(left), len(p.patches[0]['left_out']))
            self.assertEqual(len(lines), 1 + len(kept) + len(left))
            from unittest import mock as m
            with m.patch('git_video.film.Tour'), m.patch('videobuilder.render.render'):
                code, _, err = quiet(cli.main, ['-C', repo, '--duration', '20', '-o', os.path.join(tmp, 'v.mp4'),
                                                'HEAD'])
            self.assertEqual(code, 0, err)
            self.assertIn('target 20s, plan ', err)
            code, _, err = quiet(cli.main, ['-C', repo, '--duration', '20', '--plan-json', 'HEAD'])
            self.assertEqual(code, 0, err)
            self.assertNotIn('target 20s', err)


class TestTolerance(unittest.TestCase):
    """Every mode and --series and --voice combination lands within the tolerance."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo, cls.sha = fixtures.make_repo(cls.tmp.name)
        cls.series, cls.shas = fixtures.make_series_repo(cls.tmp.name)
        cls.env = mock.patch.dict(os.environ, {'XDG_CACHE_HOME': os.path.join(cls.tmp.name, 'cache')})
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp.cleanup()

    def check(self, repo, rev, mode, series, voice, target):
        extra = ['--mode', mode, '--duration', str(target)] + (['--series'] if series else [])
        if voice:
            extra += ['--voice', '--narrator=heuristic']
        with mock.patch('git_video.voice.make_engine', return_value=fixtures.FakeEngine()):
            code, p, err = plan_json(repo, rev, *extra)
        self.assertEqual(code, 0, err)
        self.assertEqual(p['duration'], target)
        self.assertLessEqual(abs(p['total'] - target), condense.tolerance(target),
                             (mode, series, voice, target, p['total']))
        return p

    def matrix(self, cases, voices, serieses):
        for mode in ('tour', 'develop'):
            for series in serieses:
                for voice in voices:
                    for target in TARGETS:
                        repo, rev = cases(series, target)
                        with self.subTest(mode=mode, series=series, voice=voice, target=target):
                            self.check(repo, rev, mode, series, voice, target)

    def test_fixture_matrix(self):
        whole = f'{self.shas[0]}..{self.shas[-1]}'

        def cases(series, target):
            if not series and target <= 30:
                return self.repo, self.sha['change']
            return self.series, whole
        self.matrix(cases, (False,), (False,))

    def test_this_repos_history(self):
        # straight through plan.build, one collected change for every target (the scores are computed once)
        revs = subprocess.run(['git', '-C', ROOT, 'rev-list', '--max-parents=0', 'HEAD'], capture_output=True,
                              text=True).stdout.split()
        count = subprocess.run(['git', '-C', ROOT, 'rev-list', '--count', 'HEAD'], capture_output=True,
                               text=True).stdout.strip()
        if not revs or not count.isdigit() or int(count) < 13:
            self.skipTest('not a git checkout with history')
        whole = collect.collect(ROOT, f'{revs[-1]}..HEAD')
        combined, patches = collect.series(ROOT, 'HEAD~12..HEAD')
        for mode in ('tour', 'develop'):
            for series in (False,):
                for voice in (False,):
                    ch, ps = (combined, patches) if series else (whole, [whole])
                    for target in TARGETS:
                        with self.subTest(mode=mode, series=series, voice=voice, target=target):
                            p = self.build(ch, ps, mode, voice, target)
                            self.assertLessEqual(abs(p.total - target), condense.tolerance(target), p.total)

    def build(self, change, patches, mode, voice, target):
        return plan.build(change, 0, mode, patches, duration=target)


if __name__ == '__main__':
    unittest.main()

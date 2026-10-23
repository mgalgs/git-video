import json
import tempfile
import unittest

from git_video import collect, plan
from tests import fixtures
from tests.test_cli import quiet
from git_video import cli


def kinds(p, kind):
    return [s for s in p.shots if s['kind'] == kind]


class TestPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo, cls.sha = fixtures.make_repo(cls.tmp.name)
        cls.change = collect.collect(cls.repo, cls.sha['change'])
        cls.plans = {v: plan.plan(cls.change, v) for v in (0, 1, 2, 3)}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_plan_json_cli(self):
        code, out, _ = quiet(cli.main, ['-C', self.repo, '--plan-json', self.sha['change']])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        for key in ('version', 'fps', 'target', 'shots', 'total'):
            self.assertIn(key, doc)
        self.assertEqual(doc['target'], self.sha['change'])

    def test_bad_rev_exit_code(self):
        code, _, err = quiet(cli.main, ['-C', self.repo, 'nope'])
        self.assertEqual(code, 1)
        self.assertIn('nope', err)

    def test_json_roundtrip(self):
        p = self.plans[3]
        self.assertEqual(plan.Plan.from_json(p.to_json()), p)

    def test_shot_order(self):
        p = self.plans[0]
        self.assertEqual(p.shots[0]['kind'], 'title')
        self.assertEqual(p.shots[-1]['kind'], 'end')
        visits = [(s['kind'], s.get('path')) for s in p.shots if s['kind'] in ('open', 'hunk')]
        self.assertEqual(visits, [('open', 'src/app.py'), ('hunk', 'src/app.py'),
                                  ('open', 'src/lib.py'), ('hunk', 'src/lib.py'),
                                  ('open', 'tests/test_app.py'), ('hunk', 'tests/test_app.py')])
        self.assertNotIn('bin.dat', [s.get('path') for s in p.shots])
        self.assertIn('bin.dat', [f['path'] for f in p.files])

    def test_hunks_in_order(self):
        repo = fixtures.make_big_repo(self.tmp.name + '/order')
        p = plan.plan(collect.collect(repo, 'HEAD'))
        starts = [s['focus_line'] for s in kinds(p, 'hunk') if s['path'] == 'big.py']
        self.assertEqual(starts, sorted(starts))

    def test_xref_cross_file(self):
        p = self.plans[0]
        i = next(i for i, s in enumerate(p.shots) if s['kind'] == 'xref-push')
        push, pop = p.shots[i], p.shots[i + 1]
        self.assertEqual((push['path'], push['name']), ('src/app.py', 'frob'))
        self.assertEqual(push['definition'], {'path': 'src/lib.py', 'line': 1, 'kind': 'def', 'col': 4})
        self.assertEqual((push['line'], push['col']), (5, 11))
        self.assertEqual(pop['kind'], 'xref-pop')
        self.assertEqual((pop['path'], pop['line'], pop['col']), ('src/app.py', 5, 11))

    def test_verbose_budget(self):
        counts = [len(kinds(self.plans[v], 'xref-push')) for v in (0, 1, 2, 3)]
        self.assertEqual(counts[0], 1)
        per_file, per_hunk, hunk = {}, {}, None
        for s in self.plans[1].shots:
            if s['kind'] == 'xref-push':
                per_file[s['path']] = per_file.get(s['path'], 0) + 1
        self.assertEqual(max(per_file.values()), 1)
        for i, s in enumerate(self.plans[2].shots):
            if s['kind'] == 'hunk':
                hunk = i
            elif s['kind'] == 'xref-push':
                per_hunk[hunk] = per_hunk.get(hunk, 0) + 1
        self.assertEqual(max(per_hunk.values()), 1)
        self.assertLessEqual(counts[3], plan.XREF_CAP_TOTAL)
        self.assertEqual(counts, sorted(counts))
        self.assertGreater(counts[3], counts[0])
        names = {s['name'] for s in kinds(self.plans[3], 'xref-push')}
        self.assertEqual(names, {'frob', 'Thing', 'main'})
        # every push is answered by a pop right after it
        for p in self.plans.values():
            for i, s in enumerate(p.shots):
                if s['kind'] == 'xref-push':
                    self.assertEqual(p.shots[i + 1]['kind'], 'xref-pop')

    def test_large_hunk_dwell(self):
        repo = fixtures.make_big_repo(self.tmp.name + '/large')
        p = plan.plan(collect.collect(repo, 'HEAD'))
        h, = [s for s in kinds(p, 'hunk') if s['path'] == 'long.txt']
        shown = sum(b - a + 1 for a, b in h['added'])
        self.assertEqual(shown, plan.PLAN_ROWS)
        self.assertEqual(h['dur'], round(plan.hunk_dwell(plan.PLAN_ROWS), 3))
        self.assertLessEqual(h['dur'], plan.HUNK_SCROLL + plan.HUNK_READ_MAX)

    def test_hunk_cap(self):
        repo = fixtures.make_big_repo(self.tmp.name + '/cap')
        ch = collect.collect(repo, 'HEAD')
        self.assertEqual(len(ch.files[0].hunks), 70)
        p = plan.plan(ch)
        self.assertEqual(len([s for s in kinds(p, 'hunk') if s['path'] == 'big.py']), plan.MAX_HUNK_SHOTS)
        self.assertEqual(len([s for s in kinds(p, 'hunk') if s['path'] == 'long.txt']), 1)

    def test_skip_same_line_definition(self):
        for p in self.plans.values():
            for s in kinds(p, 'xref-push'):
                d = s['definition']
                self.assertNotEqual((d['path'], d['line']), (s['path'], s['line']))
        # main() is defined on app.py line 4 and only visited from the test file
        mains = [s for s in kinds(self.plans[3], 'xref-push') if s['name'] == 'main']
        self.assertEqual([s['path'] for s in mains], ['tests/test_app.py'])

    def test_xref_only_from_rows_the_film_keeps_in_view(self):
        ch = collect.collect(fixtures.make_edge_repo(self.tmp.name + '/rows'), 'HEAD')
        names = {s['name']: s['line'] for s in kinds(plan.plan(ch, 3), 'xref-push') if s['path'] == 'app.py'}
        self.assertEqual(names, {'near_call': plan.XREF_ROWS})

    def test_definition_column_is_the_name(self):
        from git_video import xref
        ch = collect.collect(fixtures.make_edge_repo(self.tmp.name + '/col'), 'HEAD')
        (d,) = xref.index(ch.repo, ch.target, 'Go')['Read']
        self.assertEqual((d.line, d.col), (5, len('func (r *Reader) ')))

    def test_definition_sites_are_not_candidates(self):
        # `def tearDown` names a definition, not a use: never an xref from there.
        from git_video import xref
        text = 'def tearDownClass(cls):\n    helper_call(cls)\n'
        names = [c.name for c in xref.candidates(text, 't.py', [1, 2])]
        self.assertEqual(names, ['helper_call'])

    def test_total_duration(self):
        for p in self.plans.values():
            self.assertAlmostEqual(p.total, sum(s['dur'] for s in p.shots), places=6)
        self.assertTrue(15 <= self.plans[0].total <= 60, self.plans[0].total)

    def test_no_private_paths(self):
        for p in self.plans.values():
            self.assertNotIn(self.tmp.name, p.to_json())

    def test_every_shot_has_scene(self):
        for p in self.plans.values():
            for s in p.shots:
                self.assertEqual(s['scene'], 'card' if s['kind'] in ('title', 'end') else 'editor')
        self.assertIn('"scene"', self.plans[0].to_json())

    def test_range_title(self):
        ch = collect.collect(self.repo, f'{self.sha["root"]}..HEAD')
        p = plan.plan(ch)
        self.assertTrue(p.commit['subject'].endswith(': 2 commits'))
        self.assertEqual(len(p.commit['commits']), 2)


if __name__ == '__main__':
    unittest.main()

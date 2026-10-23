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
        self.assertEqual(doc['repo'], 'repo')
        self.assertIn('next', doc)
        self.assertLessEqual({'map', 'dive', 'travel'}, {s['kind'] for s in doc['shots']})
        self.assertEqual([g['label'] for g in doc['map']['groups']], ['repo', 'src/', 'tests/'])

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
        self.assertEqual(p.shots[1]['kind'], 'map')
        visits = [(s['kind'], s.get('to', s.get('path'))) for s in p.shots
                  if s['kind'] in ('open', 'hunk', 'dive', 'travel')]
        # src/lib.py was opened by the xref into frob, so it is entered the editor's way
        self.assertEqual(visits, [('dive', 'src/app.py'), ('hunk', 'src/app.py'),
                                  ('open', 'src/lib.py'), ('hunk', 'src/lib.py'),
                                  ('travel', 'tests/test_app.py'), ('hunk', 'tests/test_app.py')])
        (travel,) = kinds(p, 'travel')
        self.assertEqual(travel['path'], 'src/lib.py')
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

    def test_a_name_defined_on_its_line_is_not_a_use(self):
        # JS/TS and Go definition sites lex as Name.Other: resolve() must not
        # redirect `function handle(` on b.js:1 to a.js's handle.
        from git_video import xref
        defs = {'handle': [xref.Definition('a.js', 1, 'function'), xref.Definition('b.js', 1, 'function')]}
        self.assertIsNone(xref.resolve(xref.Candidate(1, 16, 'handle', True), defs, 'b.js'))
        self.assertEqual(xref.resolve(xref.Candidate(3, 4, 'handle', True), defs, 'b.js').path, 'a.js')

    def test_xref_only_from_columns_every_format_shows(self):
        repo = self.tmp.name + '/cols'
        fixtures.init(repo)
        fixtures.write(repo, 'lib.py', 'def frobnicate(x):\n    return x\n\n\ndef nearby(x):\n    return x\n')
        fixtures.commit(repo, 'Start')
        fixtures.write(repo, 'app.py', f'y = {"0 + " * 22}frobnicate(2)\nz = nearby(1)\n')
        fixtures.commit(repo, 'Call far to the right')
        names = [s['name'] for s in kinds(plan.plan(collect.collect(repo, 'HEAD'), 3), 'xref-push')]
        self.assertEqual(names, ['nearby'])

    def test_shell_lisp_and_indented_c_definitions(self):
        from git_video import xref
        repo = self.tmp.name + '/langs'
        fixtures.init(repo)
        fixtures.write(repo, 'lib.sh', 'compute_total() {\n  echo 3\n}\n')
        fixtures.write(repo, 'lib.el', '(defun compute-total (x)\n  (+ x 1))\n')
        fixtures.write(repo, 'Lib.java', 'public class Lib {\n    public static int computeTotal(int x) {\n'
                       '        return helper(x);\n    }\n}\n')
        fixtures.write(repo, 'point.h', 'struct point {\n    int x;\n};\n')
        fixtures.commit(repo, 'Start')
        fixtures.write(repo, 'use.sh', '. ./lib.sh\ncompute_total 3\n')
        fixtures.write(repo, 'use.el', '(compute-total 3)\n')
        fixtures.commit(repo, 'Use them')
        head = collect.collect(repo, 'HEAD').target
        java = xref.index(repo, head, 'Java')
        self.assertEqual([(d.path, d.line) for d in java['computeTotal']], [('Lib.java', 2)])
        self.assertEqual([d.kind for d in java['Lib']], ['class'])
        self.assertNotIn('helper', java)   # a `return f(` statement is a use
        self.assertEqual([d.line for d in xref.index(repo, head, 'C')['point']], [1])
        pushes = {s['path']: s['definition']['path'] for s in kinds(plan.plan(collect.collect(repo, 'HEAD'), 3),
                                                                   'xref-push')}
        self.assertEqual(pushes, {'use.sh': 'lib.sh', 'use.el': 'lib.el'})

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
                want = {'title': 'card', 'end': 'card', 'map': 'map', 'dive': 'map', 'travel': 'map'}
                self.assertEqual(s['scene'], want.get(s['kind'], 'editor'))
        self.assertIn('"scene"', self.plans[0].to_json())

    def test_one_file_tour_has_no_travel(self):
        p = plan.plan(collect.collect(self.repo, self.sha['root']))
        self.assertEqual([s['kind'] for s in p.shots], ['title', 'map', 'dive', 'hunk', 'end'])

    def test_range_coauthors_union(self):
        p = plan.plan(collect.collect(self.repo, f'{self.sha["root"]}..HEAD'))
        self.assertEqual(p.commit['coauthors'], ['Ada Lovelace', 'Grace Hopper'])
        self.assertTrue(all(c['date'].startswith('2026-01-02') for c in p.commit['commits']))
        self.assertEqual(self.plans[0].commit['coauthors'], [])

    def test_next_in_plan_json(self):
        for rev, want in (('change', 'Drop the binary'), ('delete', None)):
            code, out, _ = quiet(cli.main, ['-C', self.repo, '--plan-json', self.sha[rev]])
            doc = json.loads(out)
            self.assertEqual(doc['next'] and doc['next']['subject'], want)
            self.assertEqual(doc['repo'], 'repo')

    def test_range_title(self):
        ch = collect.collect(self.repo, f'{self.sha["root"]}..HEAD')
        p = plan.plan(ch)
        self.assertTrue(p.commit['subject'].endswith(': 2 commits'))
        self.assertEqual(len(p.commit['commits']), 2)


if __name__ == '__main__':
    unittest.main()

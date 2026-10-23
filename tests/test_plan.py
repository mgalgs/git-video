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

    def test_morph_in_plan(self):
        hunks = {s['path']: s for s in kinds(self.plans[0], 'hunk')}
        m = hunks['src/lib.py']['morph']
        (seg,) = m['segments']
        self.assertEqual((seg['at'], seg['old'], seg['new']), (1, ['    return x + 1'], 1))
        self.assertEqual([o[5] for o in seg['ops'] if o[0] == 'delete'], ['+', '1'])
        self.assertEqual([o[5] for o in seg['ops'] if o[0] == 'insert'], ['*', '2'])
        self.assertIn(['equal', 0, 4, 0, 4, 'return'], seg['ops'])
        self.assertEqual(m['at'], plan.HUNK_SCROLL + plan.MORPH_HOLD)
        self.assertIsNone(hunks['src/app.py']['morph'])
        self.assertEqual(hunks['src/lib.py']['focus'], [2, 2])
        self.assertEqual(hunks['src/app.py']['focus'], [1, 5])

    def test_large_hunk_skips_morph(self):
        p = plan.plan(collect.collect(fixtures.make_morph_repo(self.tmp.name + '/m1'), 'HEAD'))
        big, small = kinds(p, 'hunk')
        self.assertIsNone(big['morph'])
        self.assertEqual([g['new'] for g in small['morph']['segments']], [3])

    def test_morph_dwell(self):
        hunks = {s['path']: s for s in kinds(self.plans[0], 'hunk')}
        self.assertEqual(hunks['src/lib.py']['dur'], round(plan.hunk_dwell(1, morph=True), 3))
        self.assertEqual(hunks['src/app.py']['dur'], round(plan.hunk_dwell(5), 3))
        self.assertAlmostEqual(plan.hunk_dwell(1, morph=True) - plan.hunk_dwell(1), plan.MORPH_HOLD + plan.MORPH_DUR)

    def test_morph_window_fits_segment(self):
        ch = collect.collect(fixtures.make_morph_repo(self.tmp.name + '/m2'), 'HEAD')
        p = plan.plan(ch)
        _, small = kinds(p, 'hunk')
        h = ch.files[0].hunks[1]
        top = small['top']
        self.assertLessEqual(top, h.new_start - 1)
        self.assertLessEqual(h.new_start - 1 + max(h.old_count, h.new_count), top + plan.PLAN_ROWS)
        self.assertLessEqual(top, small['focus_line'] - 1)

    def test_callouts_in_plan(self):
        repo, sha = fixtures.make_callout_repo(self.tmp.name + '/c1')
        p = plan.plan(collect.collect(repo, sha['head']))
        one, two = kinds(p, 'hunk')
        self.assertEqual([c['text'] for c in one['callouts']], ['frob() now doubles its input.'])
        self.assertEqual([c['text'] for c in two['callouts']], ['The helper other() returns three times its input.'])
        c = one['callouts'][0]
        self.assertEqual((c['line'], c['col'], c['len']), (1, 4, 4))   # `frob` on `def frob(x):`, in view
        self.assertEqual(c['commit'], p.commit['short'])
        start = plan.HUNK_SCROLL + plan.MORPH_HOLD + plan.MORPH_DUR
        self.assertAlmostEqual(c['at'], start)
        self.assertLessEqual(c['until'], one['dur'])
        self.assertGreaterEqual(c['until'] - c['at'], plan.CALLOUT_EACH - 0.2 - 1e-6)
        self.assertEqual(one['dur'], round(plan.hunk_dwell(1, morph=True, callouts=1), 3))
        self.assertNotIn('Signed-off-by', p.commit['body'])
        root = plan.plan(collect.collect(repo, sha['root']))
        (h,) = kinds(root, 'hunk')
        self.assertEqual(h['callouts'], [])
        self.assertEqual(h['dur'], round(plan.hunk_dwell(len(fixtures.RANGE_LIB_A.splitlines())), 3))

    def test_range_callouts_use_each_commit(self):
        repo, sha = fixtures.make_range_repo(self.tmp.name + '/c2')
        p = plan.plan(collect.collect(repo, f'{sha["a"]}..{sha["c"]}'))
        one, two = kinds(p, 'hunk')
        self.assertEqual([c['text'] for c in one['callouts']], ['frob() now doubles its input.'])
        self.assertEqual([c['text'] for c in two['callouts']], ['The helper other() now triples.'])
        shorts = [c['short'] for c in p.commit['commits']]
        self.assertEqual([one['callouts'][0]['commit'], two['callouts'][0]['commit']], shorts)

    def test_hunk_info(self):
        info = plan.hunk_info(self.change, self.change.files[2], self.change.files[2].hunks[0])
        self.assertEqual(info['path'], 'src/lib.py')
        self.assertEqual(info['defs'], {'frob'})
        self.assertLessEqual({'return', 'x'}, info['words'])

    def test_far_scroll_flag(self):
        p = plan.plan(collect.collect(fixtures.make_far_repo(self.tmp.name + '/far'), 'HEAD'))
        near, far = kinds(p, 'hunk')
        self.assertEqual((near['scroll'], far['scroll']), ('near', 'far'))
        self.assertEqual(far['dur'], round(plan.hunk_dwell(1, far=True), 3))
        self.assertEqual(near['dur'], round(plan.hunk_dwell(1), 3))
        big = plan.plan(collect.collect(fixtures.make_big_repo(self.tmp.name + '/farbig'), 'HEAD'))
        steps = [s for s in kinds(big, 'hunk') if s['path'] == 'big.py'][1:]
        self.assertEqual({s['scroll'] for s in steps}, {'near'})

    def test_graph_shot_fields(self):
        p = self.plans[3]
        ids = {n['id'] for n in p.graph['nodes']}
        for s in p.shots:
            if s['kind'] == 'hunk':
                self.assertIn('node', s)
                self.assertTrue(s['node'] is None or s['node'] in ids)
            if s['kind'] == 'xref-push':
                self.assertIn(s['node'], ids)
                self.assertEqual(p.graph['edges'][s['edge']]['b'], s['node'])
        code, out, _ = quiet(cli.main, ['-C', self.repo, '--plan-json', self.sha['change']])
        doc = json.loads(out)
        self.assertEqual(set(doc['graph']), {'nodes', 'edges'})

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

    def test_file_marks(self):
        files = {f['path']: f for f in self.plans[0].files}
        self.assertEqual(files['src/lib.py']['marks'], {'added': [[2, 2]], 'removed_at': [1]})
        self.assertEqual(files['src/app.py']['marks'], {'added': [[1, 5]], 'removed_at': []})

    def test_total_duration(self):
        for p in self.plans.values():
            self.assertAlmostEqual(p.total, sum(s['dur'] for s in p.shots), places=6)
        self.assertTrue(15 <= self.plans[0].total <= 75, self.plans[0].total)

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



class TestDevelopPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo, cls.sha = fixtures.make_repo(cls.tmp.name)
        cls.change = collect.collect(cls.repo, cls.sha['change'])
        cls.budget = collect.collect(fixtures.make_budget_repo(cls.tmp.name), 'HEAD')
        cls.develop = collect.collect(fixtures.make_develop_repo(cls.tmp.name), 'HEAD')
        cls.big = collect.collect(fixtures.make_big_repo(cls.tmp.name), 'HEAD')
        cls.plans = {}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def plan_of(self, name, verbose=0):
        if (name, verbose) not in self.plans:
            self.plans[name, verbose] = plan.build(getattr(self, name), verbose, 'develop')
        return self.plans[name, verbose]

    def test_develop_file_order(self):
        order = plan.order_files(collect.collect(fixtures.make_order_repo(self.tmp.name), 'HEAD'))
        self.assertEqual([f.path for f in order], ['util.py', 'app.py', 'tests/test_app.py'])
        cycle = collect.collect(fixtures.make_cycle_repo(self.tmp.name), 'HEAD')
        self.assertEqual([f.path for f in plan.order_files(cycle)], [f.path for f in cycle.files])
        # frob's body changes in src/lib.py and src/app.py's new lines call it
        self.assertEqual([f.path for f in plan.order_files(self.change)],
                         ['src/lib.py', 'src/app.py', 'tests/test_app.py'])
        p = self.plan_of('change')
        self.assertEqual([s['path'] for s in kinds(p, 'edit')], ['src/lib.py', 'src/app.py', 'tests/test_app.py'])

    def test_develop_budget(self):
        p = self.plan_of('budget')
        (patch,) = p.patches
        self.assertEqual(patch['lines'], 500)
        self.assertAlmostEqual(patch['budget'], 60.0)
        perf = plan.perf_time(p)
        self.assertTrue(patch['budget'] - 1 <= perf <= patch['budget'] + 0.05, perf)
        self.assertAlmostEqual(perf, patch['perf'], places=2)
        small = plan.build(collect.collect(self.repo, self.sha['root']), 0, 'develop')
        (patch,) = small.patches
        self.assertEqual(patch['budget'], plan.PERF_FLOOR)
        self.assertLess(patch['natural'], plan.PERF_FLOOR)
        self.assertAlmostEqual(plan.perf_time(small), patch['natural'], delta=0.01)   # never slowed to fill it
        self.assertEqual([s['fast'] for s in kinds(small, 'edit')], [[]])
        # a change between the floor and NATURAL_MAX times it plays faster, without a speed-up
        mid = self.plan_of('change')
        (patch,) = mid.patches
        if patch['natural'] > patch['budget']:
            self.assertAlmostEqual(plan.perf_time(mid), patch['budget'], delta=0.05)

    def test_develop_verbose_monotonic(self):
        for name in ('budget', 'develop'):
            perfs = [plan.perf_time(self.plan_of(name, v)) for v in (0, 1, 2, 3)]
            for a, b in zip(perfs, perfs[1:]):   # shot lengths are rounded to the millisecond
                self.assertLessEqual(a, b + 0.005, (name, perfs))
            p3 = self.plan_of(name, 3)
            self.assertAlmostEqual(perfs[3], p3.patches[0]['natural'], delta=0.05)
            for s in kinds(p3, 'edit'):
                self.assertEqual(s['fast'], [])
                self.assertFalse([st for st in s['steps'] if st['kind'] == 'fill'])

    def test_speedup_spans(self):
        from git_video import edits
        p = self.plan_of('big')
        fast = [(s, f) for s in kinds(p, 'edit') for f in s['fast']]
        self.assertTrue(fast)
        for s, f in fast:
            self.assertTrue(plan.FAST_PEAK_MIN <= f['peak'] <= plan.FAST_PEAK_MAX, f)
            self.assertGreaterEqual(edits.source_time(f['at'], s['clock']), plan.FAST_LEAD - 1e-3)
            keys = [edits.shot_time(st['at'] + x, s['clock']) for st in s['steps'] if st['kind'] == 'type'
                    for x in st['times'][:1]]
            self.assertEqual(keys, sorted(keys))
            src = edits.source_time(f['until'], s['clock']) - edits.source_time(f['at'], s['clock'])
            span = s['steps'][-1]['at'] + s['steps'][-1]['dur'] - plan.FAST_LEAD
            self.assertLessEqual(src, span + 0.01)
            self.assertEqual(edits.speed_ramp(f['until'], s['clock']['times'], s['clock']['speeds']),
                             edits.source_time(f['until'], s['clock']))
            self.assertAlmostEqual(edits.source_time(s['dur'], s['clock']), s['natural'], delta=0.01)
        for s in kinds(plan.build(collect.collect(self.repo, self.sha['root']), 0, 'develop'), 'edit'):
            self.assertEqual(s['fast'], [])

    def test_elision_thresholds(self):
        def filled(v):
            p = self.plan_of('develop', v)
            return {s['path']: [e['count'] for e in s['elisions']] for s in kinds(p, 'edit') if s['elisions']}
        self.assertEqual(filled(0), {'new50.py': [50 - 4], 'new100.py': [100 - 4]})
        self.assertEqual(filled(1), {'new100.py': [100 - 4]})
        self.assertEqual(filled(2), {})
        self.assertEqual(filled(3), {})

    def test_develop_plan_json(self):
        code, out, _ = quiet(cli.main, ['-C', self.repo, '--mode', 'develop', '--plan-json', self.sha['change']])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual((doc['version'], doc['mode'], len(doc['patches'])), (plan.VERSION, 'develop', 1))
        self.assertEqual(plan.VERSION, 2)
        edits_ = [s for s in doc['shots'] if s['kind'] == 'edit']
        self.assertTrue(edits_)
        for s in edits_:
            self.assertEqual(s['scene'], 'editor')
            for key in ('steps', 'fast', 'elisions', 'callouts', 'done_before', 'clock'):
                self.assertIn(key, s)
        self.assertNotIn('bin.dat', [s['path'] for s in edits_])
        p = plan.Plan.from_json(out)
        self.assertEqual(json.loads(p.to_json()), doc)
        self.assertEqual(sum(s['lines'] for s in edits_), doc['patches'][0]['lines'])
        self.assertEqual([s['done_before'] for s in edits_],
                         [sum(x['lines'] for x in edits_[:k]) for k in range(len(edits_))])

    def test_develop_callouts(self):
        from git_video import edits
        repo, sha = fixtures.make_callout_repo(self.tmp.name)
        p = plan.build(collect.collect(repo, sha['head']), 0, 'develop')
        (s,) = kinds(p, 'edit')
        texts = [c['text'] for c in s['callouts']]
        self.assertEqual(texts, ['frob() now doubles its input.', 'The helper other() returns three times its input.'])
        for c in s['callouts']:
            first = next(st for st in s['steps'] if st['hunk'] == c['hunk'])
            self.assertAlmostEqual(c['at'], edits.shot_time(first['at'], s['clock']), delta=0.01)
            self.assertLessEqual(c['until'], s['dur'])
            self.assertGreater(c['until'], c['at'])
        a, b = s['callouts']
        self.assertLessEqual(a['until'], b['at'])



class TestSeries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.range_repo, cls.range_sha = fixtures.make_range_repo(cls.tmp.name)
        cls.rewrite_repo, cls.rewrite_sha = fixtures.make_rewrite_repo(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def series(self, repo, a, b, mode):
        combined, patches = collect.series(repo, f'{a}..{b}')
        return plan.build(combined, 0, mode, patches), combined, patches

    def test_series_assembly(self):
        for repo, sha in ((self.range_repo, self.range_sha), (self.rewrite_repo, self.rewrite_sha)):
            for mode in ('tour', 'develop'):
                p, combined, patches = self.series(repo, sha['a'], sha['c'], mode)
                n = len(patches)
                self.assertEqual(n, 2)
                kinds_ = [s['kind'] for s in p.shots]
                self.assertEqual((kinds_[0], kinds_[1], kinds_[-1]), ('title', 'map', 'end'))
                self.assertEqual(kinds_.count('title') + kinds_.count('end') + kinds_.count('map'), 3)
                self.assertEqual(kinds_.count('patch'), n - 1)
                stamps = [s['patch'] for s in p.shots]
                self.assertEqual(stamps, sorted(stamps))
                cards = [i for i, s in enumerate(p.shots) if s['kind'] == 'patch']
                self.assertEqual([p.shots[i]['patch'] for i in cards], list(range(1, n)))
                self.assertEqual([e['commit']['short'] for e in p.patches], [c.short for c in combined.commits])
                self.assertEqual([e['first_shot'] for e in p.patches[1:]], cards)
                body = 'edit' if mode == 'develop' else 'hunk'
                for k in range(n):
                    self.assertTrue([s for s in p.shots if s['kind'] == body and s['patch'] == k], (mode, k))
        # a single rev: --series changes nothing
        repo, sha = self.rewrite_repo, self.rewrite_sha
        for mode in ('tour', 'develop'):
            combined, patches = collect.series(repo, sha['c'])
            self.assertEqual(plan.build(combined, 0, mode, patches).to_json(),
                             plan.build(collect.collect(repo, sha['c']), 0, mode).to_json())

    def test_series_tour_per_commit(self):
        p, combined, patches = self.series(self.range_repo, self.range_sha['a'], self.range_sha['c'], 'tour')
        seq = [(s['kind'], s['patch']) for s in p.shots if s['kind'] in ('hunk', 'patch')]
        self.assertEqual(seq, [('hunk', 0), ('patch', 1), ('hunk', 1)])
        b, c = kinds(p, 'hunk')
        self.assertEqual([x['text'] for x in b['callouts']], ['frob() now doubles its input.'])
        self.assertEqual([x['commit'] for x in b['callouts']], [patches[0].commits[0].short])
        self.assertEqual([x['commit'] for x in c['callouts']], [patches[1].commits[0].short])
        self.assertEqual(b['index'], 0)
        self.assertEqual(c['index'], 0)
        self.assertEqual(b['focus_line'], 2)   # each hunk against its own commit
        self.assertEqual(c['focus_line'], 13)

    def test_series_scripts_chain(self):
        from git_video import edits
        repo, sha = self.rewrite_repo, self.rewrite_sha
        p, combined, patches = self.series(repo, sha['a'], sha['c'], 'develop')
        texts = [edits.split_lines(collect.blob_text(repo, sha[x], 'lib.py')) for x in 'abc']
        (eb,), (ec,) = ([s for s in p.shots if s['kind'] == 'edit' and s['patch'] == k] for k in (0, 1))
        self.assertEqual(edits.apply(texts[0], eb['steps']), texts[1])
        self.assertEqual(edits.apply(texts[1], ec['steps']), texts[2])
        typed = ''.join(st['text'] for st in eb['steps'] if st['kind'] in ('type', 'fill'))
        killed = [st['text'] for st in ec['steps'] if st['kind'] == 'kill']
        self.assertTrue(any(k.strip() and k.strip() in typed for k in killed), killed)
        self.assertIn('total += k', ''.join(killed))   # B's loop, rewritten by C


if __name__ == '__main__':
    unittest.main()

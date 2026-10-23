import io
import os
import tempfile
import unittest
from unittest import mock

import videobuilder as vb

from git_video import collect, film, graph, plan, xref
from tests import fixtures


class TestGraph(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        empty = os.path.join(cls.tmp.name, 'no-sfx')
        os.makedirs(empty)
        cls.env = mock.patch.dict(os.environ, {vb.sfx.ENV: empty})
        cls.env.start()
        cls.change = collect.collect(fixtures.make_graph_repo(cls.tmp.name), 'HEAD')
        cls.plan = plan.plan(cls.change, 0)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp.cleanup()

    def test_fixture_nodes_and_edges(self):
        g = self.plan.graph
        nodes = {n['id']: n for n in g['nodes']}
        self.assertEqual({k for k, n in nodes.items() if n['touched']}, {'lib.py:frob', 'lib.py:main'})
        self.assertIn('util.py:helper', nodes)
        self.assertFalse(nodes['util.py:helper']['touched'])
        kinds = {(e['a'], e['b']): e['kind'] for e in g['edges']}
        self.assertEqual(kinds[('lib.py:main', 'lib.py:frob')], 'call')
        pushes = [(i, s) for i, s in enumerate(self.plan.shots) if s['kind'] == 'xref-push']
        self.assertEqual(len(pushes), 1)
        i, push = pushes[0]
        e = g['edges'][push['edge']]
        self.assertEqual((e['a'], e['b'], e['kind'], e['first']), ('lib.py:frob', 'util.py:helper', 'xref', i))
        self.assertEqual(push['node'], 'util.py:helper')
        firsts = [n['first'] for n in g['nodes']]
        self.assertEqual(firsts, sorted(firsts))
        for edge in g['edges']:
            self.assertGreaterEqual(edge['first'], max(nodes[edge['a']]['first'], nodes[edge['b']]['first']))

    def test_develop_graph(self):
        p = plan.build(self.change, 0, 'develop')
        (i, s), = [(i, s) for i, s in enumerate(p.shots) if s['kind'] == 'edit']
        nodes = {n['id']: n for n in p.graph['nodes']}
        frob, main = nodes['lib.py:frob'], nodes['lib.py:main']
        self.assertTrue(frob['touched'] and main['touched'])
        self.assertEqual((frob['first'], main['first']), (i, i))
        self.assertTrue(0 < frob['at'] < main['at'] <= s['dur'], (frob['at'], main['at']))
        calls = {(e['a'], e['b']): e for e in p.graph['edges'] if e['kind'] == 'call'}
        e = calls[('lib.py:main', 'lib.py:frob')]
        self.assertEqual(e['first'], i)
        self.assertGreaterEqual(e['at'], main['at'])
        self.assertFalse(nodes['util.py:helper']['touched'])   # called from a written line
        st = graph.state_at(p.graph, p.shots, i, frob['at'] - 0.01, None, ())
        self.assertEqual(st.nodes['lib.py:frob'], 0)
        st = graph.state_at(p.graph, p.shots, i, s['dur'], None, ())
        self.assertEqual(st.nodes['lib.py:main'], 1)

    def test_dive_edge_survives_sorting(self):
        # a call edge found before the dive sorts ahead of the dive's own edge
        ch = collect.collect(fixtures.make_call_first_repo(self.tmp.name), 'HEAD')
        p = plan.plan(ch, 0)
        pushes = [(i, s) for i, s in enumerate(p.shots) if s['kind'] == 'xref-push']
        self.assertEqual(len(pushes), 1)
        i, push = pushes[0]
        call = [e for e in p.graph['edges'] if e['kind'] == 'call']
        self.assertTrue(call and call[0]['first'] < i, p.graph['edges'])
        e = p.graph['edges'][push['edge']]
        self.assertEqual((e['a'], e['b'], e['kind'], e['first']), ('lib.py:main', 'util.py:helper', 'xref', i))

    def test_enclosures(self):
        lines = ['class A:', '    def f(self):', '        x = 1', '', '        return x', '    y = 2',
                 'z = 3', '\tdef g():', '\t\tpass']
        d = lambda name, line: (name, xref.Definition('a.py', line, 'function'))
        defs = [d('A', 1), d('f', 2), d('g', 8)]
        got = [e[0] if e else None for e in xref.enclosures(lines, defs)]
        self.assertEqual(got, ['A', 'f', 'f', None, 'f', 'A', None, 'g', 'g'])
        # one pass: a 20,000-line file of small functions is quick
        big = [s for i in range(5000) for s in (f'def f{i}():', '    return 1', '', f'v{i} = {i}')]
        bdefs = [d(f'f{i}', 4 * i + 1) for i in range(5000)]
        out = xref.enclosures(big, bdefs)
        self.assertEqual((out[1][0], out[3], out[-3][0]), ('f0', None, 'f4999'))

    def test_hunk_nodes(self):
        hunks = [s for s in self.plan.shots if s['kind'] == 'hunk']
        self.assertEqual([s['node'] for s in hunks], ['lib.py:frob', 'lib.py:main'])

    def test_node_cap(self):
        ch = collect.collect(fixtures.make_many_defs_repo(self.tmp.name), 'HEAD')
        p = plan.plan(ch, 0)
        self.assertEqual(len(p.graph['nodes']), graph.MAX_NODES)
        ids = {n['id'] for n in p.graph['nodes']}
        nodes = [s['node'] for s in p.shots if s['kind'] == 'hunk']
        self.assertEqual(nodes[:graph.MAX_NODES], [f'lib.py:f{i}' for i in range(graph.MAX_NODES)])
        self.assertEqual(set(nodes[graph.MAX_NODES:]), {None})
        for e in p.graph['edges']:
            self.assertIn(e['a'], ids)
            self.assertIn(e['b'], ids)

    def test_layout_inside_box(self):
        ch = collect.collect(fixtures.make_many_defs_repo(self.tmp.name + '/l'), 'HEAD')
        g = plan.plan(ch, 0).graph
        for box in (vb.Box(100, 50, 548, 240), vb.Box(700, 1200, 380, 580), vb.Box(0, 0, 200, 120)):
            for gr in (g, self.plan.graph):
                lay = graph.layout(gr, box)
                for nid, (x, y) in lay.pos.items():
                    hr = lay.r * graph.HUB
                    self.assertTrue(box.contains(vb.Box(x - hr, y - hr, 2 * hr, 2 * hr)), (box, nid))
                    text, lx, ly = lay.labels[nid]
                    w = vb.text.measure(text, graph.fonts()[0])
                    self.assertLessEqual(lx + w, box.right + 0.5, (box, nid, text))
                    self.assertGreaterEqual(ly, box.y)

    def test_labels_ellipsize_inside_their_column(self):
        import itertools
        nodes = [{'id': f'big.py:{name}', 'path': 'big.py', 'name': name, 'line': k + 1, 'touched': True, 'first': 0}
                 for k, name in enumerate(f'a_rather_long_function_name_number_{k}' for k in range(30))]
        nodes += [{'id': 'b.py:short', 'path': 'b.py', 'name': 'short', 'line': 1, 'touched': True, 'first': 0}]
        g = {'nodes': nodes, 'edges': []}
        for box, scale in ((vb.Box(40, 40, 548, 300), 1.0), (vb.Box(40, 40, 380, 580), 1.15),
                           (vb.Box(40, 40, 260, 160), 1.0)):
            lay = graph.layout(g, box, scale)
            for current in (None, nodes[3]['id']):
                cv = vb.Canvas(1000, 800, '#000')
                st = graph.State({n['id']: 1.0 for n in nodes}, {}, current, (), None)
                graph.draw(cv, lay, g, st, scale=scale)
                labels = [(name, b) for name, b in cv.marks if name.startswith('graph.label ')]
                self.assertTrue(labels, box)
                for name, b in labels:
                    self.assertTrue(box.contains(b), (box, name, b))
                for (na, a), (nb, b) in itertools.combinations(labels, 2):
                    self.assertFalse(a.intersects(b), (box, na, nb))
                for name, b in labels:   # never over another node's ring
                    for nid, (x, y) in lay.pos.items():
                        if nid != name.split(' ', 1)[1]:
                            hr = lay.r * graph.HUB
                            self.assertFalse(b.intersects(vb.Box(x - hr, y - hr, 2 * hr, 2 * hr)), (box, name, nid))
                cut = [lay.labels[n['id']][0] for n in nodes[:30]]
                self.assertTrue(all(t == '' or t.endswith('…') or t in {n['name'] for n in nodes} for t in cut))

    def test_edges_clear_every_label(self):
        import itertools
        files = {'film.py': ['draw', 'stage', '_hunk', 'draw_graph', 'callout_box'],
                 'graph.py': ['layout', 'state_at', 'draw', 'edge_path'], 'plan.py': ['build', 'scroll_top'],
                 'sidebar.py': ['free_area']}
        nodes = [{'id': f'{p}:{n}', 'path': p, 'name': n, 'line': k + 1, 'touched': True, 'first': 0}
                 for p, names in files.items() for k, n in enumerate(names)]
        edges = [{'a': a['id'], 'b': b['id'], 'kind': 'call' if k % 2 else 'xref', 'first': 0}
                 for k, (a, b) in enumerate(itertools.permutations(nodes, 2)) if k % 3 == 0]
        g = {'nodes': nodes, 'edges': edges}
        for gr in (g, self.plan.graph):
            for box, scale in ((vb.Box(40, 40, 468, 240), 1.0), (vb.Box(40, 40, 380, 580), 1.15),
                               (vb.Box(40, 40, 548, 300), 1.0)):
                lay = graph.layout(gr, box, scale)
                label_f = graph.fonts(scale)[0]
                bold = vb.Font(label_f.family, label_f.size, weight=700)
                boxes = [(nid, vb.Box(lx, ly, vb.text.measure(t, bold), vb.text.line_height(bold)))
                         for nid, (t, lx, ly) in lay.labels.items() if t]
                boxes += [(f'head {t}', vb.Box(x, y, vb.text.measure(t, graph.fonts(scale)[1]),
                                               vb.text.line_height(graph.fonts(scale)[1]))) for t, x, y, w, _ in lay.heads]
                half = 1.25 * scale   # half the widest edge's stroke
                for k, e in enumerate(gr['edges']):
                    pts = graph.edge_path(lay, e['a'], e['b'], k, scale)
                    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                        steps = max(1, int(max(abs(x1 - x0), abs(y1 - y0)) / 0.5))
                        for j in range(steps + 1):
                            x, y = x0 + (x1 - x0) * j / steps, y0 + (y1 - y0) * j / steps
                            for name, b in boxes:
                                inside = b.x - half < x < b.right + half and b.y - half < y < b.bottom + half
                                self.assertFalse(inside, (box, e['a'], e['b'], name, (x, y), b))

    def test_graph_grows_with_the_tour(self):
        tour = film.Tour(self.plan, self.change, log=io.StringIO())
        labels = dict((label, n) for n, label in tour.film.keyframes)
        first_hunk = next(n for label, n in labels.items() if label.startswith('hunk '))
        route = next(n for label, n in labels.items() if label.startswith('graph '))

        def nodes(n, fmt='16x9'):
            return {name for name, _ in tour.film.frame(n, fmt).marks if name.startswith('graph.node ')}

        early, late = nodes(first_hunk), nodes(route + round(1.0 * tour.fps))
        self.assertTrue(early)
        self.assertLess(early, late)
        self.assertIn('graph.node util.py:helper', late)
        self.assertNotEqual(tour.film.frame(route, '16x9').bgra(), tour.film.frame(route - 6, '16x9').bgra())
        i, t = tour.at(route)
        cur = tour.graph_current(i, *tour.state(route))
        st = graph.state_at(self.plan.graph, tour.shots, i, t, cur, [])
        self.assertIsNotNone(st.route)
        self.assertTrue(0 < st.route[1] < 1)
        for fmt in ('16x9', '9x16'):
            marks = dict(tour.film.frame(route, fmt).marks)
            self.assertIn('sidebar.graph', marks)
            for name, b in tour.film.frame(route, fmt).marks:
                if name.startswith('graph.'):
                    self.assertTrue(marks['sidebar.graph'].contains(b), (fmt, name))
        self.assertNotIn('sidebar.graph', dict(tour.film.frame(route, '1x1').marks))

    def test_current_node_is_a_hub_and_the_stack_is_numbered(self):
        tour = film.Tour(self.plan, self.change, log=io.StringIO())
        i = next(k for k, s in enumerate(tour.shots) if s['kind'] == 'xref-push')
        n = tour.shot_frames[i] + round((plan.XREF_SWITCH + 1.0) * tour.fps)
        path, stack = tour.state(n)
        self.assertEqual(tour.graph_current(i, path, stack), 'util.py:helper')
        st = film.stage(tour.film.frame(n, '16x9'))
        cv = tour.film.frame(n, '16x9')
        hub = dict(cv.marks)['graph.node util.py:helper']
        other = dict(cv.marks)['graph.node lib.py:frob']
        self.assertGreater(hub.w, other.w)   # weight, not only colour


if __name__ == '__main__':
    unittest.main()

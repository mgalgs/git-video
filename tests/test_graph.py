import io
import os
import tempfile
import unittest
from unittest import mock

import videobuilder as vb

from git_video import collect, film, graph, plan
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

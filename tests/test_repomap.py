import os
import tempfile
import unittest

from git_video import collect, plan, repomap
from tests import fixtures


def all_tiles(m):
    return [(g, t) for g in m['groups'] for t in g['tiles']]


class TestRepoMap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo, cls.sha = fixtures.make_repo(cls.tmp.name)
        cls.small = plan.plan(collect.collect(cls.repo, cls.sha['change'])).map
        cls.big = plan.plan(collect.collect(fixtures.make_tree_repo(cls.tmp.name), 'HEAD')).map

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_changed_files_always_get_tiles(self):
        m = self.big
        tiles = all_tiles(m)
        changed = [t for _, t in tiles if 'added' in t]
        self.assertEqual(sorted(t['path'] for t in changed), sorted(fixtures.TREE_CHANGED))
        self.assertTrue(all(t['added'] > 0 for t in changed))
        quiet = [t for _, t in tiles if 'path' in t and 'added' not in t]
        more = [t for _, t in tiles if 'more' in t]
        self.assertLessEqual(len(quiet), repomap.MAX_TILES)
        self.assertTrue(more)
        self.assertEqual(sum(t['more'] for t in more) + len(quiet) + len(changed), fixtures.TREE_FILES)
        self.assertEqual(m['files'], fixtures.TREE_FILES)
        lit = [any('added' in t for t in g['tiles']) for g in m['groups']]
        self.assertEqual(lit, sorted(lit, reverse=True))
        self.assertEqual(sum(lit), 3)
        self.assertLessEqual(len(m['groups']), repomap.MAX_GROUPS)
        self.assertTrue(m['groups'][-1]['dir'].endswith('more directories'))
        # changed tiles lead their group, in tour order; quiet ones by size; "more" last
        for g in m['groups']:
            kinds = ['c' if 'added' in t else 'm' if 'more' in t else 'q' for t in g['tiles']]
            self.assertEqual(kinds, sorted(kinds, key='cqm'.index))
            sizes = [t['size'] for t in g['tiles'] if 'path' in t and 'added' not in t]
            self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_small_tree_has_no_folding(self):
        m = self.small
        paths = sorted(t['path'] for _, t in all_tiles(m))
        self.assertEqual(paths, ['bin.dat', 'src/app.py', 'src/lib.py', 'tests/test_app.py'])
        self.assertFalse([t for _, t in all_tiles(m) if 'more' in t])
        self.assertEqual([g['dir'] for g in m['groups']], ['repo', 'src', 'tests'])  # bin.dat leads the tour's files
        self.assertEqual(repomap.group_of('src/app.py', 'repo'), ('src', 'app.py'))
        self.assertEqual(repomap.group_of('bin.dat', 'repo'), ('repo', 'bin.dat'))

    def test_single_leftover_is_shown_not_folded(self):
        n = repomap.MAX_TILES + 1
        tree = [(f'a/q{i:02d}', 100 + i) for i in range(n)] + [('a/c', 5), ('b/one', 1)]
        m = repomap.tiles(tree, [{'path': 'a/c', 'added': 1, 'deleted': 0}], 'r')
        more = [t for _, t in all_tiles(m) if 'more' in t]
        self.assertTrue(all(t['more'] >= 2 for t in more), more)
        self.assertEqual(sum(t['more'] for t in more) + sum('path' in t for _, t in all_tiles(m)), len(tree))

    def test_many_directories_fold(self):
        tree = [(f'd{i:02d}/f', 10) for i in range(30)]
        m = repomap.tiles(tree, [{'path': 'd29/f', 'added': 1, 'deleted': 1}], 'r')
        self.assertEqual(len(m['groups']), repomap.MAX_GROUPS)
        self.assertEqual(m['groups'][0]['dir'], 'd29')
        self.assertEqual(m['groups'][-1], {'dir': '21 more directories', 'tiles': [{'more': 21, 'size': 210}]})


if __name__ == '__main__':
    unittest.main()

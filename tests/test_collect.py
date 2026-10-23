import os
import subprocess
import tempfile
import unittest

from git_video import collect
from tests import fixtures


class TestCollect(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo, cls.sha = fixtures.make_repo(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_resolve_single(self):
        base, target, label, is_range = collect.resolve(self.repo, 'HEAD~1')
        self.assertEqual((base, target, is_range), (self.sha['root'], self.sha['change'], False))
        self.assertEqual(label, collect.short(self.repo, self.sha['change']))

    def test_resolve_root_commit(self):
        base, target, _, _ = collect.resolve(self.repo, self.sha['root'])
        self.assertEqual((base, target), ('4b825dc642cb6eb9a060e54bf8d69288fbee4904', self.sha['root']))
        ch = collect.collect(self.repo, self.sha['root'])
        self.assertEqual([(f.path, f.status) for f in ch.files], [('src/lib.py', 'A')])
        self.assertEqual(ch.files[0].hunks[0].added, [[1, 6]])

    def test_root_commit_in_a_sha256_repo(self):
        repo = self.tmp.name + '/sha256'
        try:
            fixtures.git(self.tmp.name, 'init', '-q', '--object-format=sha256', repo)
        except Exception:
            self.skipTest('git without SHA-256 support')
        fixtures.git(repo, 'config', 'user.name', 'Test Person')
        fixtures.git(repo, 'config', 'user.email', 'test@example.com')
        fixtures.write(repo, 'a.py', 'x = 1\n')
        fixtures.commit(repo, 'Start')
        ch = collect.collect(repo, 'HEAD')
        self.assertEqual(len(ch.base), 64)
        self.assertEqual([f.path for f in ch.files], ['a.py'])

    def test_resolve_range(self):
        base, target, label, is_range = collect.resolve(self.repo, f'{self.sha["root"]}..HEAD')
        self.assertEqual((base, target, is_range), (self.sha['root'], self.sha['delete'], True))
        a, b = (collect.short(self.repo, self.sha[k]) for k in ('root', 'delete'))
        self.assertEqual(label, f'{a}-{b}')
        ch = collect.collect(self.repo, f'{self.sha["root"]}..HEAD')
        self.assertEqual([c.subject for c in ch.commits], ['Use the library from an app', 'Drop the binary'])
        self.assertNotIn('bin.dat', [f.path for f in ch.files])   # added then deleted: not in the combined diff

    def test_three_dots_rejected(self):
        with self.assertRaisesRegex(collect.GitError, 'A..B'):
            collect.resolve(self.repo, 'HEAD~2...HEAD')

    def test_bad_rev(self):
        with self.assertRaisesRegex(collect.GitError, 'nope'):
            collect.collect(self.repo, 'nope')
        with self.assertRaises(collect.GitError):
            collect.collect(self.tmp.name, 'HEAD')   # not a repository

    def test_files_and_hunks(self):
        ch = collect.collect(self.repo, self.sha['change'])
        self.assertEqual([f.path for f in ch.files], ['bin.dat', 'src/app.py', 'src/lib.py', 'tests/test_app.py'])
        files = {f.path: f for f in ch.files}
        self.assertTrue(files['bin.dat'].binary)
        self.assertIsNone(files['bin.dat'].text)
        self.assertEqual((files['src/app.py'].added, files['src/app.py'].deleted), (5, 0))
        lib = files['src/lib.py']
        self.assertEqual((lib.added, lib.deleted, lib.status), (1, 1, 'M'))
        self.assertEqual(len(lib.hunks), 1)
        self.assertEqual((lib.hunks[0].added, lib.hunks[0].removed_at), ([[2, 2]], [1]))
        self.assertEqual(lib.text, fixtures.LIB_2)
        self.assertEqual(lib.lines, 6)
        self.assertEqual((ch.added, ch.deleted), (11, 1))
        self.assertEqual(ch.commits[0].body, 'The app doubles instead of incrementing.')
        self.assertEqual(ch.commits[0].author, 'Test Person')

    def test_deleted_file(self):
        ch = collect.collect(self.repo, self.sha['delete'])
        self.assertEqual([(f.path, f.status, f.text) for f in ch.files], [('bin.dat', 'D', None)])

    def test_tests_go_last(self):
        self.assertTrue(collect.is_test('tests/test_app.py'))
        self.assertTrue(collect.is_test('pkg/foo_test.go'))
        self.assertTrue(collect.is_test('web/a.spec.ts'))
        self.assertFalse(collect.is_test('src/contest.py'))

    def test_ls_tree(self):
        tree = collect.ls_tree(self.repo, self.sha['change'])
        self.assertIn(('src/lib.py', len(fixtures.LIB_2)), tree)
        self.assertIn(('bin.dat', 10), tree)
        self.assertFalse([p for p, _ in tree if p.endswith('/')])
        self.assertEqual(collect.collect(self.repo, self.sha['change']).tree, tree)

    def test_coauthor_trailers(self):
        (d,) = collect.commit_meta(self.repo, self.sha['change'], self.sha['delete'], False)
        self.assertEqual(d.coauthors, ['Ada Lovelace', 'Grace Hopper'])
        (c,) = collect.commit_meta(self.repo, self.sha['root'], self.sha['change'], False)
        self.assertEqual(c.coauthors, [])
        commits = collect.commit_meta(self.repo, self.sha['root'], self.sha['delete'], True)
        self.assertEqual([c.coauthors for c in commits], [[], ['Ada Lovelace', 'Grace Hopper']])
        self.assertEqual(collect.coauthor_names(['Solo', ' A B <a@b> ', 'A B']), ['Solo', 'A B'])

    def test_next_commit(self):
        self.assertEqual(collect.next_commit(self.repo, self.sha['change']).short,
                         collect.short(self.repo, self.sha['delete']))
        self.assertIsNone(collect.next_commit(self.repo, self.sha['delete']))
        # off the current branch's line: no next
        side = os.path.join(self.tmp.name, 'side')
        subprocess.run(['git', 'clone', '-q', self.repo, side], check=True, capture_output=True)
        fixtures.git(side, 'config', 'user.name', 'Test Person')
        fixtures.git(side, 'config', 'user.email', 'test@example.com')
        fixtures.git(side, 'checkout', '-q', '-b', 'side', self.sha['root'])
        fixtures.write(side, 'other.txt', 'x\n')
        fixtures.commit(side, 'Go another way')
        self.assertIsNone(collect.next_commit(side, self.sha['change']))
        self.assertIsNotNone(collect.next_commit(side, self.sha['root']))

    def test_parse_hunks_pure_deletion(self):
        patch = '@@ -3,2 +2,0 @@\n-a\n-b\n'
        h, = collect.parse_hunks(patch)
        self.assertEqual((h.added, h.removed_at), ([], [2]))


if __name__ == '__main__':
    unittest.main()

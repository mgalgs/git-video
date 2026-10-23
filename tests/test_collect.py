import os
import subprocess
import tempfile
import unittest
from unittest import mock

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

    def test_old_text_and_series(self):
        ch = collect.collect(self.repo, self.sha['change'])
        self.assertEqual(ch.old_text('src/lib.py'), fixtures.LIB_1)
        self.assertEqual(ch.old_text('src/app.py'), '')
        self.assertIsNone(ch.old_text('bin.dat'))
        delete = collect.collect(self.repo, self.sha['delete'])
        self.assertIsNone(delete.old_text('bin.dat'))   # binary at the base too
        repo = os.path.join(self.tmp.name, 'rename')
        fixtures.init(repo)
        body = ''.join(f'line {i}\n' for i in range(20))
        fixtures.write(repo, 'a.py', body)
        fixtures.commit(repo, 'Start')
        os.remove(os.path.join(repo, 'a.py'))
        fixtures.write(repo, 'b.py', body.replace('line 3', 'line three'))
        fixtures.commit(repo, 'Rename')
        ren = collect.collect(repo, 'HEAD')
        (f,) = ren.files
        self.assertEqual((f.status, f.old_path, f.path), ('R', 'a.py', 'b.py'))
        self.assertEqual(ren.old_text('b.py'), body)
        # series: one change per commit of a range, oldest first; one for a single rev
        combined, patches = collect.series(self.repo, f'{self.sha["root"]}..{self.sha["delete"]}')
        self.assertTrue(combined.is_range)
        self.assertEqual([p.target for p in patches], [self.sha['change'], self.sha['delete']])
        self.assertEqual([p.is_range for p in patches], [False, False])
        self.assertEqual(patches[1].old_text('bin.dat'), None)
        for spec in (self.sha['change'], f'{self.sha["root"]}..{self.sha["change"]}'):
            combined, patches = collect.series(self.repo, spec)
            self.assertEqual(patches, [combined])

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

    def test_range_commits_carry_stripped_bodies(self):
        commits = collect.commit_meta(self.repo, self.sha['root'], self.sha['delete'], True)
        self.assertEqual([c.body for c in commits], ['The app doubles instead of incrementing.', ''])
        self.assertEqual(collect.strip_trailers('Why.\n\nToken: v\nOther-Key: w', 'Token: v\nOther-Key: w'), 'Why.')
        self.assertEqual(collect.strip_trailers('Just prose: here.', ''), 'Just prose: here.')

    def test_parse_hunks_segments(self):
        h, = collect.parse_hunks('@@ -3,2 +2,0 @@\n-a\n-b\n')
        self.assertEqual(h.segments, [[2, ['a', 'b'], 0]])
        h, = collect.parse_hunks('@@ -1,3 +1,3 @@\n x\n-old\n+new\n y\n')
        self.assertEqual(h.segments, [[1, ['old'], 1]])
        h, = collect.parse_hunks('@@ -1,2 +1,3 @@\n x\n+new\n y\n')
        self.assertEqual(h.segments, [])
        h, = collect.parse_hunks('@@ -1,6 +1,5 @@\n-a\n+A\n+B\n x\n-c\n-\td\n y\n')
        self.assertEqual(h.segments, [[0, ['a'], 2], [3, ['c', '\td'], 0]])

    def test_range_attribution(self):
        repo, sha = fixtures.make_range_repo(self.tmp.name)
        ch = collect.collect(repo, f'{sha["a"]}..{sha["c"]}')
        hunks = [h for f in ch.files for h in f.hunks]
        self.assertEqual(len(hunks), 2)
        self.assertEqual([h.commit for h in hunks], [sha['b'], sha['c']])
        self.assertEqual([c.body for c in ch.commits], ['frob() now doubles its input.', 'The helper other() now triples.'])
        ch = collect.collect(repo, sha['b'])
        self.assertEqual({h.commit for f in ch.files for h in f.hunks}, {sha['b']})
        # a hunk with no added line in the range gets no commit
        self.assertEqual(collect.blame_owners(f'{sha["a"]} 1 1 1\nboundary\n\tx\n'), {})

    def test_attribution_survives_blame_failure(self):
        repo, sha = fixtures.make_range_repo(self.tmp.name + '/fail')
        real = collect.git

        def git(repo, *args, **kw):
            if args[0] == 'blame':
                raise collect.GitError('blame broke')
            return real(repo, *args, **kw)

        with mock.patch.object(collect, 'git', git):
            ch = collect.collect(repo, f'{sha["a"]}..{sha["c"]}')
        self.assertEqual([h.commit for f in ch.files for h in f.hunks], [None, None])

    def test_attribution_is_synchronous(self):
        # One blame per changed text file, through collect.git (subprocess.run),
        # done before collect returns: nothing is left running.
        repo, sha = fixtures.make_range_repo(self.tmp.name + '/sync')
        calls = []
        real = collect.git

        def git(repo, *args, **kw):
            calls.append(args[0])
            return real(repo, *args, **kw)

        with mock.patch.object(collect, 'git', git):
            collect.collect(repo, f'{sha["a"]}..{sha["c"]}')
        self.assertEqual(calls.count('blame'), 1)
        self.assertLess(calls.index('blame'), calls.index('ls-tree'))

    def test_parse_hunks_pure_deletion(self):
        patch = '@@ -3,2 +2,0 @@\n-a\n-b\n'
        h, = collect.parse_hunks(patch)
        self.assertEqual((h.added, h.removed_at), ([], [2]))


if __name__ == '__main__':
    unittest.main()

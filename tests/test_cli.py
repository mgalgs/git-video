import contextlib
import io
import os
import subprocess
import sys
import unittest

from git_video import cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def quiet(fn, *a):
    """Run fn(*a) with stdout/stderr captured: (result or SystemExit code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            r = fn(*a)
        except SystemExit as e:
            r = e.code
    return r, out.getvalue(), err.getvalue()


class TestCli(unittest.TestCase):
    def test_verbose_spellings(self):
        self.assertEqual(cli.parse(['--verbose']).verbose, 1)
        self.assertEqual(cli.parse(['--verbose=2']).verbose, 2)
        self.assertEqual(cli.parse([]).verbose, 0)
        a = cli.parse(['--verbose', 'HEAD~1'])
        self.assertEqual((a.verbose, a.rev), (1, 'HEAD~1'))
        a = cli.parse(['--verbose', '2'])
        self.assertEqual((a.verbose, a.rev), (2, 'HEAD'))
        a = cli.parse(['HEAD~1', '--verbose', '3'])
        self.assertEqual((a.verbose, a.rev), (3, 'HEAD~1'))
        code, _, err = quiet(cli.parse, ['--verbose=4'])
        self.assertEqual(code, 2)
        self.assertIn('invalid choice', err)

    def test_develop_plan_json(self):
        import tempfile
        from tests import fixtures
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha = fixtures.make_repo(tmp)
            code, out, err = quiet(cli.main, ['-C', repo, '--mode', 'develop', '--plan-json', sha['change']])
            self.assertEqual(code, 0, err)
            self.assertIn('"mode": "develop"', out)

    def test_series_help(self):
        a = cli.parse(['--series', 'A..B'])
        self.assertTrue(a.series)
        self.assertEqual(a.mode, 'tour')
        h = ' '.join(cli.parser().format_help().split())
        self.assertNotIn('ignores it', h)
        self.assertNotIn('not implemented yet). ', h)
        self.assertIn('one tour or one performance per commit', h)
        self.assertIn('Programming as a performance art', h)

    def test_help_lists_options(self):
        h = cli.parser().format_help()
        for opt in ('--format', '-o', '--out', '--jobs', '--plan-json', '-C', '--font', '--stills',
                    '--mode', '--series', '--verbose', '--voice'):
            self.assertIn(opt, h)

    def test_module_entry(self):
        r = subprocess.run([sys.executable, '-m', 'git_video', '--help'], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('git video <rev|range>', r.stdout)

    def test_output_names(self):
        self.assertEqual(cli.output_paths('abc1234', None, ['16x9']), [('16x9', 'git-video-abc1234.mp4')])
        self.assertEqual(cli.output_paths('a1-b2', 'x/out.mp4', ['16x9']), [('16x9', 'x/out.mp4')])
        self.assertEqual(cli.output_paths('a', 'out.mp4', ['16x9', '9x16', '1x1']),
                         [('16x9', 'out-16x9.mp4'), ('9x16', 'out-9x16.mp4'), ('1x1', 'out-1x1.mp4')])
        self.assertEqual(cli.output_paths('abc', None, ['16x9', '1x1']),
                         [('16x9', 'git-video-abc-16x9.mp4'), ('1x1', 'git-video-abc-1x1.mp4')])

    def test_C_chaining(self):
        self.assertEqual(cli.repo_path(['a', 'b']), os.path.join('a', 'b'))
        self.assertEqual(cli.repo_path(['a', '/abs']), '/abs')
        self.assertEqual(cli.repo_path(None), os.getcwd())


if __name__ == '__main__':
    unittest.main()


class TestOutputPath(unittest.TestCase):
    def setUp(self):
        import tempfile
        from tests import fixtures
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.repo, self.sha = fixtures.make_repo(os.path.join(self.tmp, 'r'))

    def tearDown(self):
        self._tmp.cleanup()

    def run_main(self, out):
        """cli.main with -o out, the film and the render stubbed: (code, err, rendered paths, films built)."""
        from unittest import mock
        rendered, built = [], []
        with mock.patch('git_video.film.Tour', side_effect=lambda *a, **k: built.append(1) or mock.Mock()), \
                mock.patch('videobuilder.render.render', side_effect=lambda film, path, **k: rendered.append(path)):
            code, _, err = quiet(cli.main, ['-C', self.repo, '-o', out, self.sha['change']])
        return code, err, rendered, built

    def test_missing_directory_is_created(self):
        out = os.path.join(self.tmp, 'new', 'deeper', 'v.mp4')
        code, err, rendered, _ = self.run_main(out)
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isdir(os.path.dirname(out)))
        self.assertFalse(os.path.exists(out))   # the probe leaves nothing behind
        self.assertEqual(rendered, [out])

    def test_unwritable_path_fails_before_rendering(self):
        blocker = os.path.join(self.tmp, 'file')
        open(blocker, 'w').close()
        bad = [os.path.join(blocker, 'v.mp4'), self.tmp]   # parent is a file; the path is a directory
        if os.geteuid() != 0:
            ro = os.path.join(self.tmp, 'ro')
            os.mkdir(ro, 0o500)
            bad.append(os.path.join(ro, 'v.mp4'))
        for out in bad:
            with self.subTest(out=out):
                code, err, rendered, built = self.run_main(out)
                self.assertEqual(code, 1)
                self.assertIn(out, err)
                self.assertEqual((rendered, built), ([], []))

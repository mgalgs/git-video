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

    def test_not_implemented_modes(self):
        for argv in (['--mode', 'develop'], ['--voice'], ['--voice=bf_isabella']):
            code, _, err = quiet(cli.main, argv)
            self.assertNotEqual(code, 0, argv)
            self.assertIn('not implemented yet', err, argv)

    def test_series_ignored_by_tour(self):
        a = cli.parse(['--series', 'A..B'])
        self.assertTrue(a.series)
        self.assertEqual(a.mode, 'tour')
        self.assertIn('does not apply to tour', ' '.join(cli.parser().format_help().split()))

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

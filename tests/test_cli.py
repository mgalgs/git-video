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
                    '--mode', '--series', '--verbose', '--voice', '--quality', '--duration'):
            self.assertIn(opt, h)

    def test_duration_spellings(self):
        for text, secs in (('60', 60), ('60s', 60), ('1m30s', 90), ('2m', 120), ('90.5', 90.5)):
            with self.subTest(text=text):
                self.assertEqual(cli.parse(['--duration', text]).duration, secs)
        self.assertIsNone(cli.parse([]).duration)
        for text in ('abc', '1h', '30x', 'm', ''):
            with self.subTest(text=text):
                code, _, err = quiet(cli.parse, ['--duration', text])
                self.assertEqual(code, 2)
                self.assertIn('invalid duration', err)
                self.assertIn('1m30s', err)

    def test_duration_too_short(self):
        from unittest import mock
        for text in ('14.9', '5', '10s'):
            with self.subTest(text=text), mock.patch('git_video.collect.collect') as c, \
                    mock.patch('git_video.collect.series') as s:
                code, _, err = quiet(cli.main, ['--duration', text, '--plan-json', 'HEAD'])
                self.assertEqual(code, 2)
                self.assertIn('the shortest usable target is 15s', err)
                c.assert_not_called()
                s.assert_not_called()
        import tempfile
        from tests import fixtures
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha = fixtures.make_repo(tmp)
            code, out, err = quiet(cli.main, ['-C', repo, '--duration', '15', '--plan-json', sha['change']])
            self.assertEqual(code, 0, err)

    def test_small_change_stays_short_with_a_note(self):
        import json
        import tempfile
        from unittest import mock
        from git_video import collect, plan
        from tests import fixtures
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha = fixtures.make_repo(tmp)
            code, out, err = quiet(cli.main, ['-C', repo, '--duration', '120', '--plan-json', sha['root']])
            self.assertEqual(code, 0, err)
            total = json.loads(out)['total']
            nominal = plan.build(collect.collect(repo, sha['root'])).total
            self.assertLess(total, 120)
            self.assertGreater(total, nominal)
            with mock.patch('git_video.film.Tour'), mock.patch('videobuilder.render.render'):
                code, _, err = quiet(cli.main, ['-C', repo, '--duration', '120', '-o', os.path.join(tmp, 'v.mp4'),
                                                sha['root']])
            self.assertEqual(code, 0, err)
            self.assertIn(f'git video: the change fills only {total:.1f}s of the 120s target', err)

    def test_release_workflow_renders_condensed(self):
        with open(os.path.join(ROOT, '.github', 'workflows', 'release-video.yml')) as f:
            text = f.read()
        (line,) = [x for x in text.splitlines() if 'python -m git_video' in x]
        self.assertIn('--format all --duration 60 --quality small', line)
        self.assertIn('container: debian:trixie', text)

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



class TestOutputPath(unittest.TestCase):
    def setUp(self):
        import tempfile
        from tests import fixtures
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.repo, self.sha = fixtures.make_repo(os.path.join(self.tmp, 'r'))

    def tearDown(self):
        self._tmp.cleanup()

    def run_main(self, out, *extra, calls=None):
        """cli.main with -o out, the film and the render stubbed: (code, err, rendered paths, films built).
        `calls` collects each render's keyword arguments."""
        from unittest import mock
        rendered, built = [], []

        def render(film, path, **k):
            rendered.append(path)
            if calls is not None:
                calls.append(k)
        with mock.patch('git_video.film.Tour', side_effect=lambda *a, **k: built.append(1) or mock.Mock()), \
                mock.patch('videobuilder.render.render', side_effect=render):
            code, _, err = quiet(cli.main, ['-C', self.repo, '-o', out, *extra, self.sha['change']])
        return code, err, rendered, built

    def test_quality_sets_the_encoder_crf(self):
        out = os.path.join(self.tmp, 'v.mp4')
        for flag, crf in ((None, 18), ('high', 18), ('medium', 23), ('small', 28)):
            with self.subTest(flag=flag):
                calls = []
                code, err, _, _ = self.run_main(out, *(['--quality', flag] if flag else []), calls=calls)
                self.assertEqual(code, 0, err)
                self.assertEqual([k['crf'] for k in calls], [crf])

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


    def test_unwritable_path_fails_before_narration(self):
        from unittest import mock
        from git_video import voice
        blocker = os.path.join(self.tmp, 'file')
        open(blocker, 'w').close()
        out = os.path.join(blocker, 'v.mp4')
        with mock.patch.object(voice, 'make_engine'), mock.patch.object(voice, 'check_voice'), \
                mock.patch.object(voice, 'narrate') as narrate:
            code, _, err = quiet(cli.main, ['-C', self.repo, '--voice', '-o', out, self.sha['change']])
        self.assertEqual(code, 1)
        self.assertIn(out, err)
        narrate.assert_not_called()

if __name__ == '__main__':
    unittest.main()

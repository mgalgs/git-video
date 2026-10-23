import os
import re
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, 'man', 'git-video.1')
sys.path.insert(0, os.path.join(ROOT, 'man'))
import mkman  # noqa: E402


class TestManPage(unittest.TestCase):
    def test_up_to_date(self):
        with open(PAGE) as f:
            self.assertEqual(f.read(), mkman.generate(),
                             'man/git-video.1 is out of date with README.md or the parser: run python man/mkman.py')

    def test_structure(self):
        with open(PAGE) as f:
            text = f.read()
        self.assertRegex(text, r'(?m)^\.TH GIT\\-VIDEO 1 ')
        heads = re.findall(r'(?m)^\.SH (.*)$', text)
        self.assertEqual(heads, ['NAME', 'SYNOPSIS', 'DESCRIPTION', 'OPTIONS', 'MODES', 'SEE ALSO'])
        self.assertIn('git\\-video \\- ', text)
        # every option the parser knows, and the README's modes and closing paragraph
        for flag in ('mode', 'series', 'verbose', 'voice', 'narrator', 'format', 'out', 'jobs', 'plan\\-json',
                     'font', 'stills'):
            self.assertIn(f'\\fB\\-\\-{flag}', text)
        self.assertIn('\\fB\\-C\\fR', text)
        self.assertIn('\\fBdevelop\\fR', text)
        self.assertIn('Programming as a performance art', text)
        self.assertIn('Keyboard clicking noises, mouse clicking', text)
        # no text line that roff would take for a request
        for line in text.splitlines():
            if line[:1] in ('.', "'"):
                self.assertRegex(line, r"^\.(\\\"|[A-Za-z]{1,2}\b)")

    def test_tracks_its_sources(self):
        with open(os.path.join(ROOT, 'README.md')) as f:
            readme = f.read()
        from git_video import cli
        p = cli.parser()
        changed = mkman.roff(readme.replace('A solid xref session', 'A sturdy xref session'), p, '0')
        self.assertIn('A sturdy xref session', changed)
        p.add_argument('--brand-new', help='a flag added later')
        self.assertIn('a flag added later', mkman.roff(readme, p, '0'))

    @unittest.skipUnless(shutil.which('groff'), 'groff not installed')
    def test_groff_clean(self):
        r = subprocess.run(['groff', '-man', '-Tutf8', '-ww', '-z', PAGE], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stderr), (0, ''))

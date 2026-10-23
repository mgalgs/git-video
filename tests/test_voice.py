import json
import os
import tempfile
import tomllib
import unittest

from git_video import cli, collect, plan, voice
from tests import fixtures
from tests.test_cli import ROOT, quiet


def texts(phrases, kind=None):
    return [p.text for p in phrases if kind is None or p.key[0] == kind]


class TestScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo, cls.sha = fixtures.make_callout_repo(cls.tmp.name)
        cls.change = collect.collect(cls.repo, cls.sha['head'])
        cls.tour = plan.build(cls.change, 0, 'tour')
        cls.develop = plan.build(cls.change, 0, 'develop')
        cls.vrepo, cls.vsha = fixtures.make_voice_repo(cls.tmp.name)
        cls.vchange = collect.collect(cls.vrepo, cls.vsha['head'])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_deterministic(self):
        for p in (self.tour, self.develop):
            a = voice.script(p, [self.change])
            self.assertEqual(a, voice.script(p, [self.change]))
            self.assertEqual(a, voice.script(plan.Plan.from_json(p.to_json()), [self.change]))
            self.assertTrue(a)

    def test_tour_script(self):
        got = voice.script(self.tour, [self.change])
        self.assertEqual(texts(got), [
            'This change changes both.', 'One file, four lines.', 'This is all in lib.py.',
            'frob() now doubles its input.', 'The helper other() returns three times its input.',
            "That's the change."])
        keys = [p.key for p in got]
        self.assertEqual(keys[:3], [('title',), ('title',), ('enter', 0, 'lib.py')])
        self.assertEqual(keys[3][0], 'hunk')
        self.assertEqual(keys[-1], ('end',))
        index = voice.shot_index(self.tour.shots)
        self.assertTrue(all(p.key in index for p in got))
        # the same sentences over the develop mode's hunks
        dev = voice.script(self.develop, [self.change])
        self.assertEqual(texts(dev, 'edit'), texts(got, 'hunk'))
        self.assertTrue(all(p.key in voice.shot_index(self.develop.shots) for p in dev))

    def test_next_commit(self):
        repo, sha = fixtures.make_repo(os.path.join(self.tmp.name, 'next'))
        ch = collect.collect(repo, sha['change'])
        p = plan.build(ch)
        end = texts(voice.script(p, [ch]), 'end')
        self.assertEqual(end, ["That's the change.", 'The next commit drops the binary.'])

    def test_series(self):
        repo, sha = fixtures.make_range_repo(self.tmp.name)
        change, patches = collect.series(repo, f'{sha["a"]}..{sha["c"]}')
        p = plan.build(change, 0, 'develop', patches)
        got = voice.script(p, patches)
        self.assertEqual(texts(got, 'title')[:2], ['This is a series of two patches.',
                                                   'The first patch doubles in frob.'])
        self.assertEqual([(x.key, x.text) for x in got if x.key[0] == 'patch'],
                         [(('patch', 1), 'The next patch triples in other.')])
        self.assertEqual(texts(got, 'end'), ["That's the series."])

    def test_dive_line(self):
        repo, sha = fixtures.make_repo(os.path.join(self.tmp.name, 'dive'))
        ch = collect.collect(repo, sha['change'])
        got = voice.script(plan.build(ch), [ch])
        dives = [(x.key, x.text) for x in got if x.key[0] == 'xref']
        self.assertEqual(dives[0], (('xref', 0, 'src/app.py', 5, 11, 'frob'), 'That calls frob, defined in lib.py.'))
        self.assertIn('First, app.py.', texts(got, 'enter'))

    def test_new_definition_line(self):
        got = voice.script(plan.build(self.vchange), [self.vchange])
        self.assertIn('A new function, draw_card.', texts(got, 'hunk'))
        dev = voice.script(plan.build(self.vchange, 0, 'develop'), [self.vchange])
        self.assertIn('A new function, draw_card.', texts(dev, 'edit'))

    def test_never_read_out(self):
        for mode in ('tour', 'develop'):
            got = voice.script(plan.build(self.vchange, 0, mode), [self.vchange])
            for t in texts(got):
                for bad in ('http', 'example.com', 'deadbeef', '```', '==', 'Signed-off', '<', '@'):
                    self.assertNotIn(bad, t, (mode, t))
                self.assertLessEqual(len(t.split()), voice.MAX_WORDS, t)
                self.assertTrue(t.endswith('.'), t)
            said = ' '.join(texts(got))
            self.assertIn('frob() now doubles.', said)
            long = next(t for t in texts(got) if t.startswith('The rework of other()'))
            self.assertEqual(long, 'The rework of other() keeps every caller working, because it still returns a number of '
                                   'the same kind.')   # cut at its last clause boundary in 20 words

    def test_clean_and_shorten(self):
        self.assertIsNone(voice.clean('--- -> ==='))
        self.assertIsNone(voice.clean('Signed-off-by: A <a@example.com>'))
        self.assertIsNone(voice.clean('x = f(a, b); y = g(c) | h[d]'))
        self.assertIsNone(voice.clean('```\nprint(1)\n```'))
        self.assertEqual(voice.clean('Use `draw_card` (see #12) -> faster'), 'Use draw_card faster')
        self.assertEqual(voice.clean('Xref: a name - bound'), 'a name, bound')
        words = ' '.join(f'w{k}' for k in range(30))
        self.assertEqual(len(voice.shorten(words).split()), voice.MAX_WORDS)
        s = voice.shorten('one two three four five six seven, eight nine ten eleven twelve thirteen fourteen '
                          'fifteen sixteen seventeen eighteen nineteen twenty twentyone twentytwo')
        self.assertEqual(s, 'One two three four five six seven.')
        self.assertEqual(voice.shorten('Short one. ' + words), 'Short one.')
        self.assertEqual(voice.shorten('draw_card moves'), 'draw_card moves.')   # an identifier keeps its case

    def test_spoken_forms(self):
        for token, said in (('draw_card', 'draw card'), ('kickerTitle', 'kicker title'),
                            ('git_video/film.py', 'film dot pie'), ('vb.speech', 'v b dot speech'),
                            ('HUNK_SCROLL', 'hunk scroll'), ('--voice', 'dash dash voice'),
                            ('README.md', 'readme dot M D'), ('_stats', 'stats'), ('main', 'main'),
                            ('frob()', 'frob'), ('URL', 'u r l'), ('view/tops', 'view or tops')):
            self.assertEqual(voice.spoken(token), said, token)
        lex = voice.lexicon_for([voice.Phrase('A new function, draw_card, in film.py; main calls frob().', ('t',))])
        self.assertEqual(lex, {'draw_card': 'draw card', 'film.py': 'film dot pie', 'frob()': 'frob'})

    def test_sentences(self):
        for subject, want in (('Count wrapped rows', 'This change counts wrapped rows.'),
                              ('Fix the thing', 'This change fixes the thing.'),
                              ('Do it', 'This change does it.'),
                              ('Never overlap labels', 'This change never overlaps labels.'),
                              ('fix(cli): add x', 'This change adds x.'),
                              ('[docs] carry the plan', 'This change carries the plan.'),
                              ('Land a move, and sweep it', 'This change lands a move, and sweeps it.'),
                              ('Added a thing', 'This change added a thing.'),
                              ('v0.1.0', 'This change is titled: v0.1.0.')):
            self.assertEqual(voice.sentence_from_subject(subject), want, subject)
        for n, want in ((12, 'twelve'), (47, 'about forty-five'), (213, 'about two hundred ten'),
                        (1234, 'about one thousand two hundred'), (100, 'one hundred')):
            self.assertEqual(voice.rounded(n), want, n)
        self.assertEqual(voice.size_sentence(1, 1), 'One file, one line.')
        self.assertEqual(voice.size_sentence(3, 198), 'Three files, about two hundred lines.')


class TestVoiceFlag(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(cli.parse(['--voice']).voice, 'bf_isabella')
        self.assertEqual(cli.parse(['--voice=af_heart']).voice, 'af_heart')
        a = cli.parse(['--mode', 'develop', '--voice'])
        self.assertEqual((a.mode, a.voice, a.rev), ('develop', 'bf_isabella', 'HEAD'))
        self.assertIsNone(cli.parse([]).voice)

    def test_plan_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha = fixtures.make_callout_repo(tmp)
            code, out, err = quiet(cli.main, ['-C', repo, '--plan-json', sha['head']])
            self.assertEqual(code, 0, err)
            self.assertNotIn('voice', json.loads(out))

    def test_extra_and_man_page(self):
        with open(os.path.join(ROOT, 'pyproject.toml'), 'rb') as fh:
            meta = tomllib.load(fh)
        self.assertEqual(meta['project']['optional-dependencies']['voice'], ['videobuilder[speech]'])
        with open(os.path.join(ROOT, 'README.md')) as fh:
            man = fh.read().split('```')[1]
        entry = man[man.index('--voice'):man.index('--format')]
        self.assertIn("git-video[voice]", entry)
        self.assertIn('bf_isabella', entry)


if __name__ == '__main__':
    unittest.main()

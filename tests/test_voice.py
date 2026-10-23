import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

import videobuilder as vb
from videobuilder import speech

from git_video import cli, collect, plan, voice
from tests import fixtures
from tests.test_cli import ROOT, quiet


def have_model():
    """Whether the real Kokoro voice can run here: the speech extra and both model files."""
    try:
        e = speech.Kokoro()
    except speech.SpeechUnavailable:
        return False
    return bool(e.model_path and e.voices_path and e.model_path.is_file() and e.voices_path.is_file())


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


class TestSynth(unittest.TestCase):
    """The voice said into the cache, with a fake engine."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.tmp.name, 'cache')
        self.env = mock.patch.dict(os.environ, {'XDG_CACHE_HOME': self.cache})
        self.env.start()
        self.repo, self.sha = fixtures.make_callout_repo(self.tmp.name)
        self.change = collect.collect(self.repo, self.sha['head'])

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def narrate(self, engine, mode='tour'):
        return voice.narrate(self.change, [self.change], 0, mode, engine, 'bf_isabella')

    def test_cache(self):
        e = fixtures.FakeEngine()
        a = self.narrate(e)
        n = len(a.voice['phrases'])
        self.assertEqual(e.calls, n)
        self.assertTrue(a.voice['dir'].startswith(os.path.join(self.cache, 'git-video', 'voice') + os.sep))
        e2 = fixtures.FakeEngine()
        b = self.narrate(e2)
        self.assertEqual(e2.calls, 0)   # the second render runs no model
        self.assertEqual(a, b)
        os.remove(os.path.join(a.voice['dir'], '001.wav'))   # a run killed mid-way
        e3 = fixtures.FakeEngine()
        self.assertEqual(self.narrate(e3), a)
        self.assertEqual(e3.calls, 1)
        self.assertEqual(fixtures.git(self.repo, 'status', '--porcelain', '--ignored'), '')
        other = voice.narrate(self.change, [self.change], 0, 'tour', fixtures.FakeEngine(), 'af_heart')
        self.assertNotEqual(other.voice['dir'], a.voice['dir'])

    def test_interrupted(self):
        phrases = voice.script(plan.build(self.change), [self.change])
        lex = voice.lexicon_for(phrases)
        d = voice.script_dir(phrases, lex, 'bf_isabella')
        with self.assertRaises(RuntimeError):
            voice.synth(phrases, lex, 'bf_isabella', fixtures.FakeEngine(fail_after=2), d)
        names = sorted(os.listdir(d))
        self.assertEqual(names, ['000.json', '000.vtt', '000.wav', '001.json', '001.vtt', '001.wav'])
        e = fixtures.FakeEngine()
        voice.synth(phrases, lex, 'bf_isabella', e, d)
        self.assertEqual(e.calls, len(phrases) - 2)
        self.assertFalse([x for x in os.listdir(d) if x.endswith('.tmp')])

    def test_caption_keeps_the_spelling(self):
        phrases = [voice.Phrase('A new function, draw_card, in film.py.', ('title',))]
        lex = voice.lexicon_for(phrases)
        d = os.path.join(self.tmp.name, 'one')
        voice.synth(phrases, lex, 'bf_isabella', fixtures.FakeEngine(), d)
        narr = vb.speech.load(os.path.join(d, '000.wav'))
        self.assertEqual(narr.phrases[0].text, 'A new function, draw_card, in film.py.')
        self.assertIn('draw_card', narr.track.text)
        with open(os.path.join(d, '000.json')) as fh:
            self.assertEqual(json.load(fh)['inputs']['lexicon'], {'draw_card': 'draw card', 'film.py': 'film dot pie'})

    def test_film_plays_each_phrase(self):
        import io
        from git_video import film
        p = self.narrate(fixtures.FakeEngine())
        with mock.patch.dict(os.environ, {vb.sfx.ENV: os.path.join(self.tmp.name, 'no-sfx')}):
            tour = film.Tour(p, self.change, log=io.StringIO())
        said = [x for x in tour.film.sounds if x.path.startswith(p.voice['dir'])]
        self.assertEqual([x.at for x in said], [round(q['start'] * p.fps) for q in p.voice['phrases']])
        self.assertTrue(all(x.ducks for x in said))

    def test_bed_is_silent(self):
        import wave
        p = self.narrate(fixtures.FakeEngine())
        with wave.open(os.path.join(p.voice['dir'], p.voice['bed'])) as w:
            self.assertGreater(w.getnframes(), 0)
            self.assertEqual(set(w.readframes(w.getnframes())), {0})

    def test_plan_json_voice(self):
        with mock.patch('git_video.voice.make_engine', return_value=fixtures.FakeEngine()):
            code, out, err = quiet(cli.main, ['-C', self.repo, '--voice', '--plan-json', self.sha['head']])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        v = doc['voice']
        self.assertEqual((v['voice'], v['speed']), ('bf_isabella', voice.SPEED))
        self.assertIn('lexicon', v)
        for p in v['phrases']:
            self.assertEqual(set(p), {'text', 'shot', 'start', 'end', 'audio'})
            self.assertLess(p['start'], p['end'])
            self.assertTrue(os.path.isfile(os.path.join(v['dir'], p['audio'])))
        self.assertEqual(doc['shots'][v['phrases'][0]['shot']]['kind'], 'title')

    @unittest.skipUnless(have_model(), 'no Kokoro model files (set VIDEOBUILDER_KOKORO_DIR)')
    def test_real_model(self):
        engine = voice.make_engine()
        voice.check_voice(engine, 'bf_isabella')
        phrases = [voice.Phrase('A new function, draw_card.', ('title',))]
        d = os.path.join(self.tmp.name, 'real')
        secs = voice.synth(phrases, voice.lexicon_for(phrases), 'bf_isabella', engine, d)
        self.assertTrue(0.8 <= secs[0] <= 4, secs)
        self.assertIn('draw_card', vb.speech.load(os.path.join(d, '000.wav')).track.text)


class TestVoiceFlag(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(cli.parse(['--voice']).voice, 'bf_isabella')
        self.assertEqual(cli.parse(['--voice=af_heart']).voice, 'af_heart')
        a = cli.parse(['--mode', 'develop', '--voice'])
        self.assertEqual((a.mode, a.voice, a.rev), ('develop', 'bf_isabella', 'HEAD'))
        self.assertIsNone(cli.parse([]).voice)

    def test_without_voice_no_engine(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch('git_video.voice.make_engine', side_effect=AssertionError('no engine without --voice')):
            repo, sha = fixtures.make_callout_repo(tmp)
            code, out, err = quiet(cli.main, ['-C', repo, '--plan-json', sha['head']])
            self.assertEqual(code, 0, err)
            self.assertNotIn('voice', json.loads(out))

    def test_missing_extra_or_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha = fixtures.make_callout_repo(tmp)
            out = os.path.join(tmp, 'out.mp4')

            class NoModel(fixtures.FakeEngine):
                def voices(self):
                    raise speech.SpeechUnavailable(f'Kokoro model files not found. {speech.INSTALL_HINT}')

            for make in (mock.Mock(side_effect=speech.SpeechUnavailable(speech.INSTALL_HINT)),
                         mock.Mock(return_value=NoModel())):
                with mock.patch.object(speech, 'Kokoro', make), \
                        mock.patch.dict(os.environ, {'XDG_CACHE_HOME': os.path.join(tmp, 'cache')}):
                    code, _, err = quiet(cli.main, ['-C', repo, '--voice', sha['head'], '-o', out])
                self.assertEqual(code, 2)
                self.assertIn("pip install 'videobuilder[speech]'", err)
                self.assertTrue(err.startswith('git video: '))
                self.assertFalse(os.path.exists(out))
                self.assertFalse(os.path.exists(os.path.join(tmp, 'cache')))

    def test_unknown_voice(self):
        with mock.patch('git_video.voice.make_engine', return_value=fixtures.FakeEngine()):
            code, _, err = quiet(cli.main, ['--voice=nope', 'HEAD'])
        self.assertEqual(code, 2)
        for name in ('nope', 'af_heart', 'bf_isabella'):
            self.assertIn(name, err)

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

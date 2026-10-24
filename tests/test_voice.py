import json
import os
import re
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

import videobuilder as vb
from videobuilder import speech

from git_video import cli, collect, narrator, plan, voice
from tests import fixtures
from tests.test_cli import ROOT, quiet
from tests.test_condense import SLOW


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

    def test_silence_is_allowed(self):
        # a file with hunks no sentence explains stays silent, however long it plays
        ch = collect.collect(fixtures.make_quiet_repo(self.tmp.name), 'HEAD')
        for mode in ('tour', 'develop'):
            with mock.patch.dict(os.environ, {'XDG_CACHE_HOME': os.path.join(self.tmp.name, 'cache')}):
                p = voice.narrate(ch, [ch], 0, mode, fixtures.FakeEngine(), 'bf_isabella')
            said = [(x['start'], x['end']) for x in p.voice['phrases']]
            ts = [0.0] + [t for span in said for t in span] + [p.total]
            gaps = [y - x for x, y in zip(ts[::2], ts[1::2])]
            self.assertGreater(max(gaps), 8.0, (mode, gaps))
            self.assertEqual({p.shots[x['shot']]['kind'] for x in p.voice['phrases']} - {'title', 'map', 'end'}, set(),
                             mode)

    def test_sparse_on_this_repos_history(self):
        # nothing that only says what the editor shows: no lead-ins, no "a change to X",
        # no "a new function, X", no filler over quiet files
        revs = subprocess.run(['git', '-C', ROOT, 'log', '--format=%H', '-25'], capture_output=True,
                              text=True).stdout.split()
        if len(revs) < 25:
            self.skipTest('not a git checkout with history')
        editor_state = re.compile(r"^(?:First|Next|Then|Finally), |^This is all in |^A change to |^Changes to |"
                                  r"^A new (?:test|function|method|class|setting)\b|new tests\b|^It now imports |"
                                  r"opening comment changes")
        for rev in revs if SLOW else revs[::5]:   # all 25 with GIT_VIDEO_SLOW_TESTS=1
            ch = collect.collect(ROOT, rev)
            for mode in ('tour', 'develop'):
                p = plan.build(ch, 0, mode)
                got = voice.script(p, [ch])
                for t in texts(got):
                    self.assertNotRegex(t, editor_state, (rev, mode))
                self.assertEqual(got[0].key, ('title',))
                self.assertEqual(got[-1].key, ('end',))
                self.assertEqual(len([x for x in got if x.key == ('title',)]), 1)
                for x in got:
                    if x.key[0] in ('hunk', 'edit'):
                        self.assertTrue(voice.reason(x.text), (rev, x.text))
                cut = narrator.trim(got, p)   # as narrate holds it to the limits: never rejected
                self.assertEqual(narrator.check(cut, p), [], (rev, mode))
                self.assertEqual((cut[0], cut[-1]), (got[0], got[-1]))

    def test_wording_from_this_repos_history(self):
        # test and implementation jargon is not read out; prose around it is
        self.assertIsNone(voice.say_sentence("A keyframe at the first dive's route mid-way (`graph <name> route`) "
                                             "is the graph-mid-dive still."))
        self.assertIsNone(voice.say_sentence('whoosh_soft/air and hover_tick/detent are not in vb-sfx-1, so both '
                                             'fell back to other sounds.'))
        self.assertEqual(voice.say_sentence('Edges were gentle arcs drawn straight from node to node, so in a graph '
                                            'of several files they crossed the names in the columns between.'),
                         'Edges were gentle arcs drawn straight from node to node.')
        self.assertEqual(voice.say_sentence('A shot now lasts at least as long as what is said over it plus a short '
                                            'breath.'),
                         'A shot now lasts at least as long as what is said over it plus a short breath.')
        # every verb of a coordinated predicate is conjugated, and nothing else
        for subject, said in (
                ('Ramp and streak long scrolls', 'This change ramps and streaks long scrolls.'),
                ('Collect git facts and plan the tour shots', 'This change collects git facts and plans the tour shots.'),
                ("Never overlap map labels; keep the dive target's on top",
                 "This change never overlaps map labels; keeps the dive target's on top."),
                ('Read `--verbose N`, test the side bar as drawn, drop the license claim',
                 'This change reads --verbose N, tests the side bar as drawn, drops the license claim.'),
                ('Wrap long lines at word boundaries and widen the 16:9 code pane',
                 'This change wraps long lines at word boundaries and widens the 16:9 code pane.'),
                ('Collect old texts and one change per commit of a series',
                 'This change collects old texts and one change per commit of a series.'),
                ('Name one changed definition per file, and the tests as tests',
                 'This change names one changed definition per file, and the tests as tests.'),
                ("Treat a parameter on the def line itself as bound, not a default's value",
                 "This change treats a parameter on the def line itself as bound, not a default's value."),
                ('Land a camera move on the hunk it leads to, and sweep it at once',
                 'This change lands a camera move on the hunk it leads to, and sweeps it at once.'),
                # a subject that is not an imperative
                ('Initial commit', 'This is the first commit.'),
                ('Minor fixes', 'This change brings minor fixes.')):
            self.assertEqual(voice.sentence_from_subject(subject), said)
        self.assertEqual(voice.sentence_from_subject('Initial commit', 'The next commit'), 'The next commit is the first commit.')
        # symbols stripped never leave dangling words: their clause goes
        self.assertEqual(voice.say_sentence("The def line's own parameters now count, and a parameter is matched as a "
                                            "name after `(` or `,`, so a default (`key=frobnicate`) stays a use."),
                         "The def line's own parameters now count, so a default stays a use.")
        self.assertEqual(voice.say_sentence('The identifier check (`str.isidentifier`) rejected hyphens, which most '
                                            'Lisp names have. It now accepts `[A-Za-z_][\\w-]*\\w`.'),
                         'The identifier check rejected hyphens, which most Lisp names have.')
        self.assertIsNone(voice.say_sentence('pyproject.toml declared `license = MIT`, but the repo has no LICENSE file.'))
        # a sentence about the code itself (two code names, or a part "called as is") is not read out ...
        for about_code in ('The kicker and title use kicker_title_progress for timing but are drawn here: '
                           'draw_kicker_title draws one left-aligned line, and the subject is wrapped and centred.',
                           'The per-frame keep_in_view calls in _edit_pane are gone.',
                           "The box's travel is scroll_progress_bar's scroll_progress, called as is; "
                           "the strip itself is ours.",
                           'The dim is spotlight_dim, called as is, clipped to the text area.'):
            self.assertIsNone(voice.say_sentence(about_code), about_code)
        # ... but one naming a single definition, a file or flags is
        self.assertEqual(voice.say_sentence('The title card now reads as a broadcast opener.'),
                         'The title card now reads as a broadcast opener.')
        for kept in ('--mode develop is accepted for --plan-json; rendering it lands with the film in the next commit.',
                     'The film.py title card now draws_kicker over the subject.'):
            self.assertIsNotNone(voice.say_sentence(kept), kept)

    def test_develop_slot_is_the_callouts(self):
        # an unsayable first sentence leaves its slot empty: the phrase waits for the second card
        hunk = next(k for k in voice.shot_index(self.develop.shots) if k[0] == 'edit')
        said = {(hunk[2], hunk[3]): ['frob(x) == x[0] * {2}; y = [z]', 'Now frob doubles its input, so callers see more.']}
        with mock.patch.object(plan, 'develop_sentences', return_value=said):
            got = [p for p in voice.script(self.develop, [self.change]) if p.key == hunk]
        self.assertEqual([(p.text, p.slot) for p in got], [('Now frob doubles its input, so callers see more.', 1)])

    def test_deterministic(self):
        for p in (self.tour, self.develop):
            a = voice.script(p, [self.change])
            self.assertEqual(a, voice.script(p, [self.change]))
            self.assertEqual(a, voice.script(plan.Plan.from_json(p.to_json()), [self.change]))
            self.assertTrue(a)

    def test_tour_script(self):
        got = voice.script(self.tour, [self.change])
        self.assertEqual(texts(got), [
            'This change changes both. One file, four lines.',
            'The helper other() returns three times its input, so callers keep their scale.',
            "That's the change."])   # a sentence that only says what the code does is not read out
        keys = [p.key for p in got]
        self.assertEqual(keys[0], ('title',))
        self.assertEqual(keys[1][0], 'hunk')
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
        self.assertEqual(end, ["That's the change. The next commit drops the binary."])

    def test_why(self):
        repo, sha = fixtures.make_repo(os.path.join(self.tmp.name, 'why'))
        ch = collect.collect(repo, sha['change'])
        got = voice.script(plan.build(ch), [ch])
        self.assertEqual([(p.key, p.text) for p in got][1], (('map',), 'The app doubles instead of incrementing.'))
        self.assertEqual(voice.why_line('A card was placed by the band alone and could sit over the row being '
                                        'typed in develop mode, or over the rows.\n\nMore.'),
                         'Before, a card was placed by the band alone and could sit over the row being typed in '
                         'develop mode.')
        # a past tense in a subordinate clause, or a clause that says "now", is not how things were
        self.assertEqual(voice.why_line('Develop mode edits each file from what it was into what it is, so a '
                                        'change has to say what a file was at its base.'),
                         'Develop mode edits each file from what it was into what it is.')
        self.assertEqual(voice.why_line("A fill held there now plays at speed; the midpoint could land there."),
                         'A fill held there now plays at speed; the midpoint could land there.')
        self.assertIsNone(voice.why_line('- one thing\n- another'))
        self.assertIsNone(voice.why_line('Used here.', used={'Used here.'}))

    def test_reasons_over_the_map(self):
        # a body sentence giving a reason that no hunk's callout took is said over the map,
        # kept long enough to keep its reason; one that only says what happens is not
        repo, sha = fixtures.make_repo(os.path.join(self.tmp.name, 'reasons'))
        ch = collect.collect(repo, sha['change'])
        p = plan.build(ch)
        p.commit['body'] = ('The app moves its arithmetic into one place. The card is longer now. '
                            'The helper stays private for the moment, and the public surface of the library '
                            'stays the same for every single caller, so nothing outside has to change.')
        got = [(x.key, x.text) for x in voice.script(p, [ch]) if x.key == ('map',)]
        self.assertEqual(got, [
            (('map',), 'The app moves its arithmetic into one place.'),
            (('map',), 'The helper stays private for the moment, and the public surface of the library stays the '
                       'same for every single caller, so nothing outside has to change.')])

    def test_series(self):
        repo, sha = fixtures.make_range_repo(self.tmp.name)
        change, patches = collect.series(repo, f'{sha["a"]}..{sha["c"]}')
        p = plan.build(change, 0, 'develop', patches)
        got = voice.script(p, patches)
        self.assertEqual(texts(got, 'title'), ['This is a series of two patches. The first patch doubles in frob. '
                                               'One file, four lines.'])
        self.assertEqual([(x.key, x.text) for x in got if x.key[0] == 'patch'],
                         [(('patch', 1), 'The next patch triples in other.')])
        self.assertEqual(texts(got, 'end'), ["That's the series."])

    def test_dive_line(self):
        repo, sha = fixtures.make_repo(os.path.join(self.tmp.name, 'dive'))
        ch = collect.collect(repo, sha['change'])
        p = plan.build(ch)
        self.assertEqual(texts(voice.script(p, [ch]), 'xref'), [])   # the body does not name frob
        for c in [p.commit] + p.commit['commits']:
            c['body'] = 'The app now calls frob, so the library does the work.'
        dives = [(x.key, x.text) for x in voice.script(p, [ch]) if x.key[0] == 'xref']
        self.assertEqual(dives, [(('xref', 0, 'src/app.py', 5, 11, 'frob'), 'That calls frob, defined in lib.py.')])

    def test_never_read_out(self):
        for mode in ('tour', 'develop'):
            got = voice.script(plan.build(self.vchange, 0, mode), [self.vchange])
            for t in texts(got):
                for bad in ('http', 'example.com', 'deadbeef', '```', '==', 'Signed-off', '<', '@'):
                    self.assertNotIn(bad, t, (mode, t))
                for sentence in re.split(r'(?<=\.) ', t):
                    self.assertLessEqual(len(sentence.split()), voice.MAX_WORDS, t)
                self.assertTrue(t.endswith('.'), t)
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
        # a whole sentence keeps its last word; only a hard cut drops a dangling one
        self.assertEqual(voice.shorten('Keep the file it lives in'), 'Keep the file it lives in.')
        self.assertEqual(voice.shorten(words + ' to the'), voice.shorten(words))
        self.assertEqual(voice.shorten(' '.join(f'w{k}' for k in range(19)) + ' to the end'),
                         'W0 ' + ' '.join(f'w{k}' for k in range(1, 19)) + '.')
        # a cut at a clause boundary drops a conjunction left before its comma, but not a verb
        self.assertEqual(voice.shorten('A fill was set in natural seconds and the clock divided it by the patch '
                                       'speed and, so the fold was up for one frame and then it was gone again'),
                         'A fill was set in natural seconds and the clock divided it by the patch speed.')
        self.assertEqual(voice.shorten('Develop mode edits each file from what it was into what it is, so a change '
                                       'has to say what a file was at its base by its old path'),
                         'Develop mode edits each file from what it was into what it is.')
        # an aside goes before the clauses split, so no half of it is left
        self.assertEqual(voice.clean('Plays at speed with the badge hidden; it lands there (26d4929964, README.md: '
                                     'speed 1.0 against a peak).'),
                         'Plays at speed with the badge hidden; it lands there.')
        # a piece of a quotation is silent
        for frag in ('Read aloud, it went "A change to wrap_rows.', 'And _edit." for a body: filler.',
                     'and then it stops.', '...'):
            self.assertIsNone(voice.say_sentence(frag), frag)
        self.assertEqual(voice.say_sentence('It says "draw it" now.'), 'It says "draw it" now.')

    def test_spoken_forms(self):
        for token, said in (('draw_card', 'draw card'), ('kickerTitle', 'kicker title'),
                            ('git_video/film.py', 'film dot pie'), ('vb.speech', 'v b dot speech'),
                            ('HUNK_SCROLL', 'hunk scroll'), ('--voice', 'voice'),
                            ('README.md', 'readme dot M D'), ('_stats', 'stats'), ('main', 'main'),
                            ('frob()', 'frob'), ('URL', 'u r l'), ('view/tops', 'view or tops')):
            self.assertEqual(voice.spoken(token), said, token)
        lex = voice.lexicon_for([voice.Phrase('A new function, draw_card, in film.py; main calls frob().', ('t',))])
        self.assertEqual(lex, {'draw_card': 'draw card', 'film.py': 'film dot pie', 'frob()': 'frob'})

    def test_number_forms(self):
        for text, want in (
                ('0.4', {'0.4': 'zero point four'}),
                ('It took 12.05.', {'12.05': 'twelve point zero five'}),
                ('a 0.4 s fade', {'a 0.4 s': 'a zero point four second'}),
                ('an 8 s pause, after 8 s', {'an 8 s': 'an eight second', '8 s': 'eight seconds'}),
                ('1 s', {'1 s': 'one second'}),
                ('in 3.14s', {'3.14s': 'three point one four seconds'}),
                ('200 ms', {'200 ms': 'two hundred milliseconds'}),
                ('5 min', {'5 min': 'five minutes'}),
                ('3 MB, 64 KB and 2 GB', {'3 MB': 'three megabytes', '64 KB': 'sixty-four kilobytes',
                                          '2 GB': 'two gigabytes'}),
                ('12px', {'12px': 'twelve pixels'}),
                ('45%', {'45%': 'forty-five percent'}),
                ('2x faster, 2× smaller, 1.5x', {'2x': 'two times', '2×': 'two times', '1.5x': 'one point five times'}),
                ('16:9 and 9:16', {'16:9': 'sixteen by nine', '9:16': 'nine by sixteen'}),
                ('1:1', {'1:1': 'one by one'}),
                ('at 10:30', {'10:30': 'ten thirty'}),
                ('4:05 or 9:00', {'4:05': 'four oh five', '9:00': "nine o'clock"}),
                ('v0.1.0', {'v0.1.0': 'version zero point one point zero'}),
                ('v2', {'v2': 'version two'}),
                ('1.2.3', {'1.2.3': 'one point two point three'}),
                ('3 steps, 60 seconds, 2 files', {}),   # Kokoro says these well
                ('step_2x and mod_1.py', {}),
                ('10000000.5', {}),
                ('the 1990s, 100s of shots, 10 s', {'10 s': 'ten seconds'}),   # a decade, a plural
                ("the '80s, 2000s", {}),
                ('a 60s video, cut to 30s, 120s', {'a 60s': 'a sixty second', '30s': 'thirty seconds',
                                                  '120s': 'one hundred twenty seconds'}),
                ('1,000 px and 12,000.5', {})):
            self.assertEqual(voice.number_forms(text), want, text)
        lex = voice.lexicon_for([voice.Phrase('With --duration 60, a 0.4 s fade at 1:1.', ('t',))])
        self.assertEqual(lex, {'--duration': 'duration', 'a 0.4 s': 'a zero point four second', '1:1': 'one by one'})

    def test_sentences(self):
        for subject, want in (('Count wrapped rows', 'This change counts wrapped rows.'),
                              ('Fix the thing', 'This change fixes the thing.'),
                              ('Do it', 'This change does it.'),
                              ('Never overlap labels', 'This change never overlaps labels.'),
                              ('fix(cli): add x', 'This change adds x.'),
                              ('[docs] carry the plan', 'This change carries the plan.'),
                              ('Land a move, and sweep it', 'This change lands a move, and sweeps it.'),
                              ('Added a thing', 'This change added a thing.'),
                              ('Ring the next patch', 'This change rings the next patch.'),
                              ('Bring back the map', 'This change brings back the map.'),
                              ('Speed up rendering', 'This change speeds up rendering.'),
                              ('Embed fonts', 'This change embeds fonts.'),
                              ('Need fewer frames', 'This change needs fewer frames.'),
                              ('Feed the cache', 'This change feeds the cache.'),
                              ('Focus the pane', 'This change focuses the pane.'),
                              ('Alias the flag', 'This change aliases the flag.'),
                              ('Making it work', 'This change is making it work.'),
                              ('Counts rows', 'This change counts rows.'),
                              ('Put the speed-up where the typing is', 'This change puts the speed-up where the typing is.'),
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

    def test_spoken_forms_are_heard_not_captioned(self):
        heard = []

        class Engine(fixtures.FakeEngine):
            def phonemize(self, text, lang):
                heard.append(text)
                return text

        phrases = [voice.Phrase('A 0.4 s fade at 16:9, in v0.1.0.', ('title',))]
        lex = voice.lexicon_for(phrases)
        d = voice.script_dir(phrases, lex, 'bf_isabella')
        voice.synth(phrases, lex, 'bf_isabella', Engine(), d)
        self.assertIn('zero point four second ', ''.join(heard))
        self.assertIn('sixteen by nine', ''.join(heard))
        self.assertIn('version zero point one point zero', ''.join(heard))
        self.assertEqual(vb.speech.load(os.path.join(d, '000.wav')).track.text, phrases[0].text)
        # how it is said is part of the clip's key: said otherwise, a new clip
        with mock.patch.dict(voice.UNITS, {'s': ('sec', 'secs')}):
            lex2 = voice.lexicon_for(phrases)
        self.assertEqual(lex2['A 0.4 s'], 'A zero point four sec')
        self.assertNotEqual(voice.script_dir(phrases, lex2, 'bf_isabella'), d)

    def test_cache(self):
        e = fixtures.FakeEngine()
        a = self.narrate(e)
        n = len(a.voice['phrases'])
        self.assertGreaterEqual(e.calls, n)   # a call per sentence
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
            voice.synth(phrases, lex, 'bf_isabella', fixtures.FakeEngine(fail_after=3), d)   # the opening's two sentences, then one
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
            code, out, err = quiet(cli.main, ['-C', self.repo, '--voice', '--narrator=heuristic', '--plan-json',
                                              self.sha['head']])
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
        self.assertIn('--quality', man)
        self.assertIn('--duration <time>', man)


class TestPacing(unittest.TestCase):
    """Shots stretched to the voice: never overlapping, never crossing a shot's end."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {'XDG_CACHE_HOME': os.path.join(cls.tmp.name, 'cache')})
        cls.env.start()
        cls.cases = {}
        repo, sha = fixtures.make_callout_repo(cls.tmp.name)
        ch = collect.collect(repo, sha['head'])
        for mode in ('tour', 'develop'):
            cls.cases['callout', mode] = (ch, [ch], mode)
        repo, sha = fixtures.make_repo(os.path.join(cls.tmp.name, 'r'))
        ch = collect.collect(repo, sha['change'])
        cls.cases['repo', 'tour'] = (ch, [ch], 'tour')
        cls.cases['repo', 'develop'] = (ch, [ch], 'develop')
        ch = collect.collect(fixtures.make_budget_repo(
            cls.tmp.name, 'Grow every function\n\nThe new grown_0() and grown_12() step x seventeen times over, '
                          'each step adding its own index.'), 'HEAD')
        cls.cases['budget', 'develop'] = (ch, [ch], 'develop')
        repo, sha = fixtures.make_range_repo(cls.tmp.name)
        change, patches = collect.series(repo, f'{sha["a"]}..{sha["c"]}')
        for mode in ('tour', 'develop'):
            cls.cases['series', mode] = (change, patches, mode)
        vrepo, vsha = fixtures.make_voice_repo(cls.tmp.name)
        ch = collect.collect(vrepo, vsha['head'])
        for mode in ('tour', 'develop'):
            cls.cases['voice', mode] = (ch, [ch], mode)
        cls.plans = {}
        for name, (ch, patches, mode) in cls.cases.items():
            plain = plan.build(ch, 0, mode, patches)
            voiced = voice.narrate(ch, patches, 0, mode, fixtures.FakeEngine(), 'bf_isabella')
            cls.plans[name] = (plain, voiced)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.tmp.cleanup()

    def test_voice_trims_to_the_target(self):
        repo, shas = fixtures.make_series_repo(self.tmp.name)
        ch = collect.collect(repo, f'{shas[0]}..{shas[-1]}')
        unvoiced = plan.build(ch, 0, 'tour', duration=30)
        hunks = [k for s in unvoiced.shots for k in voice.keys_of(s) if k[0] == 'hunk']
        self.assertGreaterEqual(len(hunks), 2)
        long = ' '.join(['said'] * 19) + '.'
        script = ([voice.Phrase('This change builds a pipeline.', ('title',))]
                  + [voice.Phrase(long, k, n) for k in hunks for n in range(2)]
                  + [voice.Phrase("That's the change.", ('end',))])
        p = voice.narrate(ch, [ch], 0, 'tour', fixtures.FakeEngine(), 'bf_isabella', lambda _: script, duration=30)
        from git_video import condense
        self.assertLessEqual(abs(p.total - 30), condense.tolerance(30), p.total)
        said = [q['text'] for q in p.voice['phrases']]
        self.assertLess(len(said), len(script))
        self.assertEqual(said[0], script[0].text)
        self.assertEqual(p.shots[p.voice['phrases'][0]['shot']]['kind'], 'title')
        self.assertEqual(said[-1], script[-1].text)
        self.assertEqual(p.shots[p.voice['phrases'][-1]['shot']]['kind'], 'end')

    def test_on_target_narration_is_not_trimmed(self):
        from git_video import condense
        repo, sha = fixtures.make_repo(os.path.join(self.tmp.name, 'ontarget'))
        ch = collect.collect(repo, sha['change'])
        full = voice.script(plan.build(ch, 0, 'develop', duration=15), [ch])
        p = voice.narrate(ch, [ch], 0, 'develop', fixtures.FakeEngine(), 'bf_isabella', duration=15)
        self.assertLessEqual(abs(p.total - 15), condense.tolerance(15), p.total)
        self.assertEqual([q['text'] for q in p.voice['phrases']], [x.text for x in full])

    def test_narrated_shots_take_time_from_the_rest(self):
        from git_video import condense
        repo, sha = fixtures.make_repo(os.path.join(self.tmp.name, 'take'))
        ch = collect.collect(repo, sha['change'])
        for mode in ('tour', 'develop'):
            plain = plan.build(ch, 0, mode, duration=30)
            p = voice.narrate(ch, [ch], 0, mode, fixtures.FakeEngine(), 'bf_isabella', duration=30)
            self.assertLessEqual(abs(p.total - 30), condense.tolerance(30), (mode, p.total))
            self.assertEqual([s['kind'] for s in plain.shots], [s['kind'] for s in p.shots])
            spoken = {q['shot'] for q in p.voice['phrases']}
            self.assertTrue(spoken)
            for i, (a, b) in enumerate(zip(plain.shots, p.shots)):
                mine = [q for q in p.voice['phrases'] if q['shot'] == i]
                if mine:
                    starts = plan.frame_starts(p.shots, p.fps)[0]
                    self.assertGreaterEqual(starts[i] / p.fps + b['dur'] + 0.05, mine[-1]['end'], (mode, i))
                else:
                    self.assertLessEqual(b['dur'], a['dur'] + 1e-3, (mode, i, b['kind']))

    def test_voice_off_is_todays_plan(self):
        # the shots and their lengths as the tree before --voice (cff1eeecb1) planned them
        with open(os.path.join(os.path.dirname(__file__), 'voice_off_plans.json')) as f:
            before = json.load(f)
        for name, (plain, _) in self.plans.items():
            self.assertIsNone(plain.voice)
            if ' '.join(name) in before:
                want = before[' '.join(name)]
                self.assertEqual({'total': round(plain.total, 6),
                                  'shots': [[s['kind'], s.get('path'), round(s['dur'], 6)] for s in plain.shots]},
                                 want, name)
        self.assertEqual(len(before), 7)

    def test_phrases_fit_their_shots(self):
        for name, (plain, p) in self.plans.items():
            phrases = p.voice['phrases']
            self.assertTrue(phrases, name)
            starts, frames = plan.frame_starts(p.shots, p.fps)
            bounds = [x / p.fps for x in starts] + [frames / p.fps]
            prev = None
            for q in phrases:
                i = q['shot']
                self.assertGreaterEqual(q['start'], bounds[i] - 1e-3, (name, q))
                self.assertLessEqual(q['end'] + voice.BREATH - 0.05, bounds[i + 1], (name, q))
                if prev is not None:
                    self.assertGreaterEqual(q['start'], prev['end'] + voice.GAP - 0.05, (name, prev, q))
                prev = q
            for i, s in enumerate(p.shots):
                mine = [q for q in phrases if q['shot'] == i]
                if mine:
                    said = sum(q['end'] - q['start'] for q in mine) + voice.GAP * (len(mine) - 1)
                    self.assertGreaterEqual(s['dur'], said + voice.BREATH - 0.01, (name, i, s['kind']))

    def test_same_shots_only_longer(self):
        for name, (plain, p) in self.plans.items():
            self.assertEqual([(s['kind'], s.get('path'), s.get('to')) for s in plain.shots],
                             [(s['kind'], s.get('path'), s.get('to')) for s in p.shots], name)
            spoken = {q['shot'] for q in p.voice['phrases']}
            for i, (a, b) in enumerate(zip(plain.shots, p.shots)):
                self.assertGreaterEqual(b['dur'], a['dur'] - 1e-6, (name, i))
                if i not in spoken:
                    self.assertEqual(a['dur'], b['dur'], (name, i, a['kind']))
            self.assertGreater(p.total, plain.total)
            self.assertEqual(round(sum(s['dur'] for s in p.shots), 3), p.total)

    def test_callouts_follow_the_voice(self):
        for name, (plain, p) in self.plans.items():
            starts, _ = plan.frame_starts(p.shots, p.fps)
            for q in p.voice['phrases']:
                s = p.shots[q['shot']]
                if s['kind'] != 'hunk' or not s['callouts']:
                    continue
                t = q['start'] - starts[q['shot']] / p.fps
                shown = [c for c in s['callouts'] if c['at'] - 0.05 <= t < c['until']]
                self.assertTrue(shown, (name, q['text']))
                self.assertGreaterEqual(shown[0]['until'], q['end'] - starts[q['shot']] / p.fps - 0.05)
                self.assertLessEqual(s['callouts'][-1]['until'], s['dur'])

    def test_develop_hunks_wait_for_their_phrases(self):
        from git_video import edits
        for name, (plain, p) in self.plans.items():
            if p.mode != 'develop':
                continue
            timed = voice.Timed([voice.Phrase(q['text'], k) for q, k in zip(p.voice['phrases'], self.keys(p))],
                                [q['end'] - q['start'] for q in p.voice['phrases']])
            for k, s in enumerate(p.shots):
                if s['kind'] != 'edit':
                    continue
                clk, steps = s['clock'], s['steps']
                hunks = list(plan._hunk_steps(steps).items())
                for n, (hi, (i0, i1)) in enumerate(hunks):
                    need = timed.fit(('edit', s['patch'], s['path'], hi), 0.0)[1]
                    if not need:
                        continue
                    a, b = steps[i0]['at'], steps[i1]['at'] + steps[i1]['dur']
                    nxt = steps[hunks[n + 1][1][0]]['at'] if n + 1 < len(hunks) else s['natural']
                    played = edits.shot_time(b, clk) - edits.shot_time(a, clk)
                    self.assertGreaterEqual(played, min(need, b - a) - 0.02, (name, hi))   # no faster than its phrase
                    self.assertGreaterEqual(edits.shot_time(nxt, clk) - edits.shot_time(a, clk), need - 0.02,
                                            (name, hi))
                    for c in s['callouts']:
                        if c['hunk'] == hi and c is s['callouts'][-1]:
                            self.assertGreaterEqual(c['until'], min(s['dur'] - 0.2, need - voice.BREATH) - 0.05)
            for k, entry in enumerate(p.patches):
                self.assertAlmostEqual(entry['perf'], plan.perf_time(p, k), places=2)

    def test_a_speedup_slows_to_the_phrase(self):
        from git_video import edits
        ch = self.cases['budget', 'develop'][0]
        plain = plan.build(ch, 0, 'develop')
        key = ('edit', 0, 'work.py', 0)   # the whole file is one hunk, sped up to fit the budget
        p = plan.build(ch, 0, 'develop', voice=voice.Timed([voice.Phrase('Long.', key)], [80.0]))
        a, b = plain.shots[-2], p.shots[-2]
        self.assertTrue(a['fast'])
        self.assertLess(a['dur'], 80)
        self.assertGreaterEqual(b['dur'], 80 + voice.BREATH - 0.01)
        self.assertLess(b['dur'], 80 + voice.BREATH + 2)   # aimed at the phrase, not slowed to natural pace
        self.assertEqual(p.patches[0]['perf'], plan.perf_time(p))
        self.assertGreater(b['fast'][0]['until'], a['fast'][0]['until'])   # the speed-up moved with it

    def test_a_short_hunk_rests_for_its_phrase(self):
        from git_video import edits
        ch = self.cases['callout', 'develop'][0]
        plain = plan.build(ch, 0, 'develop')
        e = next(s for s in plain.shots if s['kind'] == 'edit')
        hunks = list(plan._hunk_steps(e['steps']).items())
        (h0, (i0, i1)), (h1, (j0, _)) = hunks[:2]
        key = ('edit', 0, 'lib.py', h0)
        p = plan.build(ch, 0, 'develop', voice=voice.Timed([voice.Phrase('Long.', key)], [6.0]))
        s = next(x for x in p.shots if x['kind'] == 'edit')
        clk, st = s['clock'], s['steps']
        a, b, nxt = st[i0]['at'], st[i1]['at'] + st[i1]['dur'], st[j0]['at']
        self.assertLess(b - a, 6.0)
        self.assertAlmostEqual(edits.shot_time(b, clk) - edits.shot_time(a, clk), b - a, delta=0.02)   # natural pace
        self.assertGreaterEqual(edits.shot_time(nxt, clk) - edits.shot_time(a, clk), 6.0 + voice.BREATH - 0.02)
        mid = (edits.shot_time(b, clk) + edits.shot_time(nxt, clk)) / 2
        self.assertLess(edits.speed_at(mid, clk), 1.0)   # point rests while the sentence finishes
        c = next(c for c in s['callouts'] if c['hunk'] == h0)
        self.assertGreaterEqual(c['until'], edits.shot_time(a, clk) + 6.0 - 0.05)

    def keys(self, p):
        """Each placed phrase's key, from its shot (an edit shot's: the hunk it starts at)."""
        from git_video import edits
        out = []
        starts, _ = plan.frame_starts(p.shots, p.fps)
        for q in p.voice['phrases']:
            s = p.shots[q['shot']]
            ks = voice.keys_of(s)
            if s['kind'] == 'edit':
                t = q['start'] - starts[q['shot']] / p.fps
                at = voice.anchors(s)
                ks = [max((k for k in ks if at[k] <= t + 1e-3), key=lambda k: at[k])]
            out.append(ks[0])
        return out


if __name__ == '__main__':
    unittest.main()

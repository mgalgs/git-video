import io
import json
import os
import re
import tempfile
import unittest
from unittest import mock

from git_video import collect, narrator, plan, voice
from tests import fixtures


def P(text, key, slot=0):
    return voice.Phrase(text, key, slot)


class TestFormat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo, sha = fixtures.make_repo(cls.tmp.name)
        cls.change = collect.collect(repo, sha['change'])
        cls.tour = plan.build(cls.change, 0, 'tour')
        cls.develop = plan.build(cls.change, 0, 'develop')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_applied_hunks_have_no_rows(self):
        dev = collect.collect(fixtures.make_develop_repo(self.tmp.name), 'HEAD')
        p = plan.build(dev, 0, 'develop', duration=20)
        applied = {(s['path'], h) for s in p.shots if s['kind'] == 'edit' for st in s['steps']
                   if st['kind'] == 'apply' for h in st['hunks']}
        self.assertTrue(applied)
        rows = narrator.shot_rows(p, [dev])
        self.assertFalse({(r.key[2], r.key[3]) for r in rows if r.key[0] == 'edit'} & applied)
        self.assertEqual([r.key for r in rows], [k for s in p.shots for k in voice.keys_of(s)])

    def test_rows_name_every_key(self):
        for p in (self.tour, self.develop):
            rows = narrator.shot_rows(p, [self.change])
            self.assertEqual([r.key for r in rows], [k for s in p.shots for k in voice.keys_of(s)])
            self.assertEqual(len({r.id for r in rows}), len(rows))
            self.assertEqual(rows[0].id, 's0')
            self.assertEqual(rows[-1].key, ('end',))
            self.assertEqual([r.start for r in rows], sorted(r.start for r in rows))
        edit = [r for r in narrator.shot_rows(self.develop, [self.change]) if r.key[0] == 'edit']
        self.assertTrue(edit and all('h' in r.id for r in edit))
        table = narrator.shot_table(narrator.shot_rows(self.tour, [self.change]))
        self.assertRegex(table, r'(?m)^s\d+ +\d+\.\ds +\d+\.\ds  hunk 1 of \d .*\[src/app\.py lines \d+-\d+\]$')
        self.assertIn('clicks frob and dives into its definition in src/lib.py', table)

    def test_ids_stable_when_voiced(self):
        with mock.patch.dict(os.environ, {'XDG_CACHE_HOME': os.path.join(self.tmp.name, 'cache')}):
            voiced = voice.narrate(self.change, [self.change], 0, 'tour', fixtures.FakeEngine(), 'bf_isabella')
        a = {r.id: r.key for r in narrator.shot_rows(self.tour, [self.change])}
        b = {r.id: r.key for r in narrator.shot_rows(voiced, [self.change])}
        self.assertEqual(a, b)

    def test_parse_and_dump(self):
        rows = narrator.shot_rows(self.tour, [self.change])
        hunk = next(r for r in rows if r.key[0] == 'hunk')
        doc = {'phrases': [{'shot': 's0', 'text': 'This change moves the work into a library.'},
                           {'shot': hunk.id, 'text': '  The app now asks the library,\n so both agree'},
                           {'shot': hunk.id, 'text': 'That is the whole idea.'},
                           {'shot': rows[-1].id, 'text': 'That is all.'}]}
        got = narrator.parse(json.dumps(doc), self.tour, [self.change])
        self.assertEqual([(p.text, p.key, p.slot) for p in got], [
            ('This change moves the work into a library.', ('title',), 0),
            ('The app now asks the library, so both agree.', hunk.key, 0),
            ('That is the whole idea.', hunk.key, 1),
            ('That is all.', ('end',), 0)])
        self.assertEqual(narrator.parse(narrator.dump(got, self.tour, [self.change]), self.tour, [self.change]), got)

    def test_parse_rejects(self):
        rows = narrator.shot_rows(self.tour, [self.change])
        hunk = next(r for r in rows if r.key[0] == 'hunk')
        bad = {
            'not json': '{"phrases": [',
            'no list': '{"lines": []}',
            'empty': '{"phrases": []}',
            'no text': json.dumps({'phrases': [{'shot': 's0'}]}),
            'unknown shot': json.dumps({'phrases': [{'shot': 's0', 'text': 'Hi.'}, {'shot': 's999', 'text': 'Hi.'}]}),
            'not opening': json.dumps({'phrases': [{'shot': hunk.id, 'text': 'Hi.'}]}),
            'out of order': json.dumps({'phrases': [{'shot': 's0', 'text': 'Hi.'}, {'shot': rows[-1].id, 'text': 'A.'},
                                                    {'shot': hunk.id, 'text': 'B.'}]}),
            'symbols only': json.dumps({'phrases': [{'shot': 's0', 'text': '-> {}'}]}),
            'long opening': json.dumps({'phrases': [{'shot': 's0', 'text': 'word ' * 40}]}),
        }
        for name, text in bad.items():
            with self.assertRaises(narrator.NarratorError, msg=name):
                narrator.parse(text, self.tour, [self.change])


class TestCallouts(unittest.TestCase):
    """The narrator's callout cards: `read` takes them from the answer, and
    the plan shows them in place of the commit sentences."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo, cls.sha = fixtures.make_callout_repo(cls.tmp.name)
        cls.change = collect.collect(repo, cls.sha['head'])
        cls.tour = plan.build(cls.change, 0, 'tour')
        cls.rows = narrator.shot_rows(cls.tour, [cls.change])
        cls.hunks = [r for r in cls.rows if r.key[0] == 'hunk']

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def answer(self, callouts=None):
        doc = {'phrases': [{'shot': 's0', 'text': 'Both helpers now scale.'}]}
        if callouts is not None:
            doc['callouts'] = callouts
        return json.dumps(doc)

    def test_absent_keeps_the_commit_sentences(self):
        phrases, callouts = narrator.read(self.answer(), self.tour, [self.change])
        self.assertIsNone(callouts)
        self.assertEqual(narrator.parse(self.answer(), self.tour, [self.change]), phrases)
        p = plan.build(self.change, 0, 'tour', callouts=callouts)
        self.assertEqual([[c['text'] for c in s['callouts']] for s in p.shots if s['kind'] == 'hunk'],
                         [['frob() now doubles its input.'],
                          ['The helper other() returns three times its input, so callers keep their scale.']])

    def test_empty_shows_none(self):
        _, callouts = narrator.read(self.answer([]), self.tour, [self.change])
        self.assertEqual(callouts, {})
        p = plan.build(self.change, 0, 'tour', callouts=callouts)
        hunks = [s for s in p.shots if s['kind'] == 'hunk']
        self.assertEqual([s['callouts'] for s in hunks], [[], []])
        self.assertEqual(hunks[0]['dur'], round(plan.hunk_dwell(1, morph=True), 3))

    def test_used_in_place_of_the_commit_sentences(self):
        one, two = self.hunks
        log = io.StringIO()
        _, callouts = narrator.read(self.answer([{'shot': one.id, 'text': '  Doubling keeps the scale\n even.'},
                                                 {'shot': one.id, 'text': 'A second card.'}]),
                                    self.tour, [self.change], log)
        self.assertEqual(callouts, {one.key: ['Doubling keeps the scale even.', 'A second card.']})
        self.assertEqual(log.getvalue(), '')
        p = plan.build(self.change, 0, 'tour', callouts=callouts)
        a, b = [s for s in p.shots if s['kind'] == 'hunk']
        self.assertEqual([c['text'] for c in a['callouts']], callouts[one.key])
        self.assertEqual(b['callouts'], [])
        c = a['callouts'][0]
        self.assertEqual(c['commit'], p.commit['short'])
        f = self.change.files[0]
        line = f.hunks[0].added[0][0]   # no word to point at: the first changed line's text
        text = f.text.split('\n')[line - 1]
        self.assertEqual((c['line'], c['col'], c['len']), (line, 4, len(text.strip())))
        self.assertEqual(a['dur'], round(plan.hunk_dwell(1, morph=True, callouts=2), 3))
        again = narrator.dump(narrator.parse(self.answer(), self.tour, [self.change]), self.tour, [self.change],
                              callouts)
        self.assertEqual(narrator.read(again, self.tour, [self.change])[1], callouts)

    def test_bad_cards_are_dropped(self):
        one, _ = self.hunks
        log = io.StringIO()
        _, callouts = narrator.read(self.answer([
            {'shot': 's0', 'text': 'On the title card.'},
            {'shot': 's999', 'text': 'No such shot.'},
            {'shot': one.id, 'text': ' '},
            {'shot': one.id, 'text': ' '.join(['word'] * (narrator.CALLOUT_WORDS + 1))},
            {'shot': one.id, 'text': 'Kept.'}, {'shot': one.id, 'text': 'Kept too.'},
            {'shot': one.id, 'text': 'One too many.'}, 'not a card',
            {'shot': [one.id], 'text': 'A list.'}, {'shot': {}, 'text': 'A dict.'}]), self.tour, [self.change], log)
        self.assertEqual(callouts, {one.key: ['Kept.', 'Kept too.']})
        warnings = log.getvalue().splitlines()
        self.assertEqual(len(warnings), 8)
        self.assertTrue(all(w.startswith('git video: warning: narrator callout') for w in warnings))
        self.assertIn("'s0' is not a hunk shot", warnings[0])
        self.assertIn("'s999' is not a hunk shot", warnings[1])

    def test_develop(self):
        dev = plan.build(self.change, 0, 'develop')
        rows = [r for r in narrator.shot_rows(dev, [self.change]) if r.key[0] == 'edit']
        _, callouts = narrator.read(self.answer([{'shot': rows[1].id, 'text': 'Tripling keeps callers in scale.'}]),
                                    dev, [self.change])
        p = plan.build(self.change, 0, 'develop', callouts=callouts)
        (s,) = [s for s in p.shots if s['kind'] == 'edit']
        self.assertEqual([(c['text'], c['hunk']) for c in s['callouts']],
                         [('Tripling keeps callers in scale.', rows[1].key[3])])

    def test_voiced_plan_shows_them(self):
        one, _ = self.hunks

        class Writer:
            callouts = {one.key: ['Doubling keeps the scale even.']}

            def __call__(self, p):
                return [P('Both helpers now scale.', ('title',)), P('Doubling, that is.', one.key)]

        with mock.patch.dict(os.environ, {'XDG_CACHE_HOME': os.path.join(self.tmp.name, 'cache')}):
            p = voice.narrate(self.change, [self.change], 0, 'tour', fixtures.FakeEngine(), 'bf_isabella', Writer())
        hunks = [s for s in p.shots if s['kind'] == 'hunk']
        self.assertEqual([[c['text'] for c in s['callouts']] for s in hunks], [['Doubling keeps the scale even.'], []])
        said = next(q for q in p.voice['phrases'] if q['text'] == 'Doubling, that is.')
        self.assertEqual(p.shots[said['shot']], hunks[0])
        self.assertIn('Doubling keeps the scale even.', json.loads(p.to_json())['shots'][said['shot']]['callouts'][0]['text'])


class TestAlign(unittest.TestCase):
    """A phrase goes on the shot that shows what it names."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo, sha = fixtures.make_callout_repo(cls.tmp.name)
        cls.change = collect.collect(repo, sha['head'])
        cls.tour = plan.build(cls.change, 0, 'tour')
        cls.rows = narrator.shot_rows(cls.tour, [cls.change])
        cls.frob, cls.other = [r for r in cls.rows if r.key[0] == 'hunk']   # frob's hunk, then other's

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def read(self, *phrases):
        log = io.StringIO()
        doc = {'phrases': [{'shot': shot, 'text': text} for shot, text in phrases]}
        got = narrator.parse(json.dumps(doc), self.tour, [self.change], log)
        return [(p.key, p.slot) for p in got], log.getvalue()

    def test_rows_say_what_they_show(self):
        self.assertEqual((self.frob.defs, self.frob.files), (('frob',), ('lib.py',)))
        self.assertEqual((self.other.defs, self.other.files), (('other',), ('lib.py',)))
        self.assertEqual(self.rows[0].defs + self.rows[0].files, ())
        enter = next(r for r in self.rows if r.key[0] == 'enter')
        self.assertEqual(enter.files, ('lib.py',))
        repo, sha = fixtures.make_repo(self.tmp.name)
        ch = collect.collect(repo, sha['change'])
        (dive,) = [r for r in narrator.shot_rows(plan.build(ch, 0, 'tour'), [ch]) if r.key[0] == 'xref']
        self.assertEqual((dive.defs, dive.files), (('frob',), ('src/app.py', 'src/lib.py')))

    def test_moved_to_the_shot_that_shows_it(self):
        got, log = self.read(('s0', 'Both helpers now scale.'), (self.frob.id, 'other() keeps callers in scale.'),
                             (self.rows[-1].id, 'That is all.'))
        self.assertEqual(got, [(('title',), 0), (self.other.key, 0), (('end',), 0)])
        self.assertIn(f'git video: warning: narrator phrase 2 moved from {self.frob.id} to {self.other.id}', log)
        # its own shot shows it: it stays, and so does a phrase naming nothing shown
        got, log = self.read(('s0', 'Both.'), (self.frob.id, 'frob() and other() both scale.'),
                             (self.frob.id, 'Nothing named here.'))
        self.assertEqual(got, [(('title',), 0), (self.frob.key, 0), (self.frob.key, 1)])
        self.assertEqual(log, '')
        # a plain word that is a definition's name does not move it
        got, log = self.read(('s0', 'Both.'), (self.frob.id, 'Callers want other numbers.'))
        self.assertEqual(got, [(('title',), 0), (self.frob.key, 0)])
        self.assertEqual(log, '')

    def test_spoken_forms_count(self):
        rows = [narrator.Row('s1', ('hunk', 0, 'film.py', 0), 0, 1, '', defs=('draw_card',), files=('film.py',)),
                narrator.Row('s2', ('hunk', 0, 'sidebar.py', 0), 1, 1, '', defs=('layout',), files=('sidebar.py',))]
        self.assertEqual(narrator._subjects('The draw card helper now waits.', rows), ({'draw_card'}, set()))
        self.assertEqual(narrator._subjects('The layout() is computed once, in sidebar.py.', rows)[0], {'layout'})
        # a plain word is not a definition's name: "layout" here is English
        self.assertEqual(narrator._subjects('The layout is new.', rows), (set(), set()))
        self.assertIn('sidebar', narrator._subjects('The layout is computed once, in sidebar.py.', rows)[1])
        self.assertEqual(narrator._subjects('Nothing about code.', rows), (set(), set()))

    def test_kept_when_moving_breaks_order(self):
        phrases = (('s0', 'Both.'), (self.frob.id, 'other() keeps callers in scale.'),
                   (self.frob.id, 'Doubling is enough.'))
        got, log = self.read(*phrases)
        self.assertEqual(got, [(('title',), 0), (self.frob.key, 0), (self.frob.key, 1)])
        self.assertEqual(log, '')
        # nor onto a shot that already has its PER_SHOT phrases
        got, _ = self.read(('s0', 'Both.'), (self.frob.id, 'other() keeps callers in scale.'),
                           (self.other.id, 'One.'), (self.other.id, 'Two.'))
        self.assertEqual(got[1], (self.frob.key, 0))

    def test_opening_and_closing_never_move(self):
        got, log = self.read(('s0', 'frob() now doubles its input.'), (self.rows[-1].id, 'other() triples it.'))
        self.assertEqual(got, [(('title',), 0), (('end',), 0)])
        self.assertEqual(log, '')


class TestHeuristicCutOff(unittest.TestCase):
    """No commit-sentence callouts on a beat of more than three commits."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo, cls.shas = fixtures.make_series_repo(cls.tmp.name, n=5)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def cards(self, p):
        return [c['text'] for s in p.shots for c in s.get('callouts') or ()]

    def test_range(self):
        for mode in ('tour', 'develop'):
            three = collect.collect(self.repo, f'{self.shas[1]}..{self.shas[4]}')
            four = collect.collect(self.repo, f'{self.shas[1]}..{self.shas[5]}')
            self.assertEqual(len(three.commits), 3)
            self.assertTrue(self.cards(plan.build(three, 0, mode)), mode)
            self.assertEqual(self.cards(plan.build(four, 0, mode)), [], mode)
            self.assertEqual(self.cards(plan.build(four, 0, mode, duration=30)), [], mode)
            self.assertEqual(plan.develop_sentences(four), {})

    def test_series(self):
        from git_video import condense
        change, patches = collect.series(self.repo, f'{self.shas[0]}..{self.shas[5]}')
        p = plan.build(change, 0, 'tour', patches)
        per = [[c['text'] for s in p.shots if s.get('patch') == k for c in s.get('callouts') or ()]
               for k in range(len(patches))]
        self.assertTrue(all(per), per)   # a single-commit beat keeps its callouts
        merged = condense.merge_series(patches, 1)
        self.assertEqual(merged[0].merged, 5)
        self.assertFalse(plan.callouts_matched(merged[0]))
        merged = condense.merge_series(patches[:3], 1)
        self.assertEqual(merged[0].merged, 3)
        self.assertTrue(plan.callouts_matched(merged[0]))


class TestLimits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo, sha = fixtures.make_repo(cls.tmp.name)
        cls.change = collect.collect(repo, sha['change'])
        cls.plan = plan.build(cls.change, 0, 'tour')
        cls.hunks = [k for s in cls.plan.shots for k in voice.keys_of(s) if k[0] in ('hunk', 'xref', 'enter')]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_limits_scale_with_the_duration(self):
        for d, n in ((None, 35), (120, 35), (60, 20), (30, 12), (15, 12)):
            self.assertEqual(narrator.max_words(d), n, d)
        fifteen = [P('Opening.', ('title',)), P(' '.join(['word'] * 15) + '.', self.hunks[0]), P('Closing.', ('end',))]
        self.assertEqual(narrator.check(fifteen, self.plan), [])
        short = plan.build(self.change, 0, 'tour', duration=30)
        self.assertTrue(any('more than 12' in x for x in narrator.check(fifteen, short)))
        self.assertNotIn(fifteen[1], narrator.trim(fifteen, short))
        self.assertEqual(narrator.limits(plan.build(self.change, 0, 'tour', duration=60)), (60 // 8 + 2, 27))

    def test_drop_to_fit(self):
        s = self.script(3)
        secs = [2.0, 1.0, 1.0, 1.0, 1.0]
        self.assertEqual(narrator.drop_to_fit(s, secs, 0.1), s[:3] + s[4:])   # one, from the end of the middle
        self.assertEqual(narrator.drop_to_fit(s, secs, 2.5), [s[0], s[1], s[4]])
        self.assertEqual(narrator.drop_to_fit(s, secs, 99), [s[0], s[4]])     # never the opening or the closing
        self.assertEqual(narrator.drop_to_fit([s[0], s[4]], [2.0, 1.0], 99), [s[0], s[4]])

    def test_limits(self):
        most, speech = narrator.limits(self.plan)
        self.assertEqual(most, int(self.plan.total // 8) + 2)
        self.assertEqual(speech, round(0.45 * self.plan.total))

    def script(self, n_middle, words=6):
        mid = [P(' '.join(['word'] * words) + '.', k) for k in self.hunks[:n_middle]]
        return [P('Opening.', ('title',))] + mid + [P('Closing.', ('end',))]

    def test_trim_drops_from_the_end(self):
        most, _ = narrator.limits(self.plan)
        self.assertGreater(len(self.hunks), most - 2)
        long = self.script(len(self.hunks))
        self.assertTrue(narrator.check(long, self.plan))
        got = narrator.trim(long, self.plan)
        self.assertEqual(len(got), most)
        self.assertEqual(got[0], long[0])
        self.assertEqual(got[-1], long[-1])                    # the closing is kept ...
        self.assertEqual(got[1:-1], long[1:most - 1])          # ... and the middle cut from its end
        self.assertEqual(narrator.check(got, self.plan), [])

    def test_trim_drops_long_and_crowded_phrases(self):
        s = self.script(2)
        s.insert(2, P('word ' * 40, s[1].key, 1))           # over MAX_WORDS
        s.insert(2, P('One more.', s[1].key, 1))
        s.insert(2, P('And more.', s[1].key, 1))              # a third over one shot
        got = narrator.trim(s, self.plan)
        self.assertEqual([p.text for p in got if p.key == s[1].key], [s[1].text, 'And more.'])
        self.assertNotIn(s[4], got)

    def test_speech_share(self):
        s = self.script(2)
        total = 20.0
        seconds = [2.0, 4.0, 4.0, 1.0]   # 11 s of 20: over 45 %
        self.assertTrue(narrator.check(s, self.plan, seconds, total))
        got = narrator.trim(s, self.plan, seconds, total)
        self.assertEqual(got, [s[0], s[1], s[3]])             # 7 s
        self.assertEqual(narrator.check(got, self.plan, [2.0, 4.0, 1.0], total), [])
        with self.assertRaises(narrator.NarratorError):       # the opening alone is too much
            narrator.trim(s, self.plan, [10.0, 1, 1, 1], total)
        self.assertEqual(narrator.trim(s, self.plan, [10.0, 1, 1, 1], total, strict=False), [s[0]])

    def test_heuristic_within_limits(self):
        # the heuristic is held to the same limits: narrate cuts it to them
        repo, sha = fixtures.make_callout_repo(os.path.join(self.tmp.name, 'c'))
        ch = collect.collect(repo, sha['head'])
        for mode in ('tour', 'develop'):
            with mock.patch.dict(os.environ, {'XDG_CACHE_HOME': os.path.join(self.tmp.name, 'cache')}):
                p = voice.narrate(ch, [ch], 0, mode, fixtures.FakeEngine(), 'bf_isabella')
            said = sum(q['end'] - q['start'] for q in p.voice['phrases'])
            self.assertLessEqual(said, 0.45 * p.total + 0.01, mode)
            self.assertLessEqual(len(p.voice['phrases']), narrator.limits(plan.build(ch, 0, mode))[0])


class TestBrief(unittest.TestCase):
    def test_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = collect.collect(fixtures.make_big_repo(tmp), 'HEAD')
            for mode in ('tour', 'develop'):
                p = plan.build(big, 0, mode, duration=30)
                text = narrator.brief(big, p, [big])
                self.assertIn('about 30 seconds long', text, mode)
                self.assertIn(f'each at most {narrator.max_words(30)} words', text, mode)
                most, _ = narrator.limits(p)
                self.assertEqual(most, 30 // 8 + 2)
                self.assertIn(f'at most\n{most} phrases', text)
                kept = {(path, h['index']) for e in p.patches for path, hs in e['kept'].items() for h in hs}
                rows = [r for r in narrator.shot_rows(p, [big]) if r.key[0] in ('hunk', 'edit')]
                self.assertEqual({(r.key[2], r.key[3]) for r in rows}, kept, mode)
                self.assertNotRegex(text, r'\{(length|max_words)\}')

    def test_filled_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha = fixtures.make_callout_repo(tmp)   # its body ends in a Signed-off-by: trailer
            ch = collect.collect(repo, sha['head'])
            p = plan.build(ch, 0, 'tour')
            text = narrator.brief(ch, p, [ch])
        self.assertIn(f'commit {ch.target} Change both', text)
        self.assertIn('frob() now doubles its input.', text)
        self.assertNotIn('Signed-off-by', text)
        self.assertNotIn('t@example.com', text)
        self.assertIn(f'git diff {ch.base}..{ch.target}', text)
        most, speech = narrator.limits(p)
        self.assertIn(f'at most\n{most} phrases', text)
        self.assertIn(f'about {speech}\nseconds', text)
        self.assertIn(narrator.shot_table(narrator.shot_rows(p, [ch])), text)
        self.assertNotRegex(text, r'\{(range|commits|base|tip|max_phrases|max_speech_s|max_words|length|shot_table|'
                                  r'max_callouts|callout_words)\}')
        self.assertIn(f'about {round(p.total)} seconds long', text)
        self.assertIn('each at most 35 words', text)
        self.assertIn('{"phrases": [{"shot": "<shot id>", "text": "<what to say>"}],\n'
                      ' "callouts": [{"shot": "<hunk shot id>", "text": "<card text>"}]}', text)
        self.assertIn(f'at most {plan.CALLOUT_MAX_PER_HUNK} cards', text)
        self.assertIn(f'at most\n{narrator.CALLOUT_WORDS} words', text)
        self.assertIn('"callouts": []', text)
        self.assertIn('Do not change or\ncommit anything', text)

    def test_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, sha = fixtures.make_range_repo(tmp)
            change, patches = collect.series(repo, f'{sha["a"]}..{sha["c"]}')
            p = plan.build(change, 0, 'tour', patches)
            text = narrator.brief(change, p, patches)
        self.assertIn(f'2 commits {change.base}..{change.target}', text)
        self.assertEqual(len(re.findall(r'(?m)^commit [0-9a-f]{40}$', text)), 2)
        self.assertIn('card: patch 2 of 2', text)

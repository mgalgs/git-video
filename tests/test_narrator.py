import json
import os
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

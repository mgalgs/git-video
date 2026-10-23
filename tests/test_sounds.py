import io
import math
import os
import tempfile
import unittest
from unittest import mock

import videobuilder as vb

from git_video import collect, film, plan, sounds
from tests import fixtures

FULL = [('pro_typing_key/tick', 'key'), ('pro_click/mouse', 'click'), ('pro_hover_tick/soft', 'tick'),
        ('whoosh_fast/tight', 'whoosh'), ('whoosh_fast/sleeve', 'whoosh'), ('pro_hover_tick/detent', 'tick'),
        ('pro_typing_run/standard', 'typing')]


class TestSounds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo, sha = fixtures.make_repo(cls.tmp.name)
        cls.change = collect.collect(repo, sha['change'])
        cls.plan = plan.plan(cls.change, 3)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def pack(self, name, entries):
        return mock.patch.dict(os.environ, {vb.sfx.ENV: fixtures.fake_pack(os.path.join(self.tmp.name, name),
                                                                            entries)})

    def test_voice_mix(self):
        from git_video import voice
        with mock.patch.dict(os.environ, {'XDG_CACHE_HOME': os.path.join(self.tmp.name, 'cache')}):
            p = voice.narrate(self.change, [self.change], 0, 'develop', fixtures.FakeEngine(), 'bf_isabella')
        with self.pack('full-voice', FULL):
            f, _ = film.build(p, self.change, log=io.StringIO())
        self.assertEqual(f.audio.duck_db, voice.DUCK_DB)
        self.assertTrue(os.path.isfile(f.audio.path))
        said = [x for x in f.sounds if x.path.startswith(p.voice['dir'])]
        self.assertEqual(len(said), len(p.voice['phrases']))
        self.assertTrue(all(x.ducks for x in said))
        spans = [(x.at, x.at + x.frames - 1) for x in said]
        under = lambda x: any(a <= x.at <= b for a, b in spans)
        gains = {}
        for x in f.sounds:
            if x.ducks:
                continue
            gains.setdefault((x.effect, under(x)), set()).add(x.gain_db)
        key, run = sounds.KEY[0][0], sounds.RUN[0][0]
        self.assertEqual(gains[key, False], {sounds.KEY[2]})
        self.assertEqual(gains[key, True], {sounds.KEY[2] + voice.TYPING_UNDER_VOICE})
        for (effect, _), g in gains.items():
            if effect not in (key, run):
                self.assertEqual(len(g), 1, effect)   # the rest only duck with the bed
        self.assertIsNone(film.build(self.plan, self.change, log=io.StringIO())[0].audio)

    def test_no_pack_is_silent_with_note(self):
        empty = os.path.join(self.tmp.name, 'empty')
        os.makedirs(empty, exist_ok=True)
        log = io.StringIO()
        with mock.patch.dict(os.environ, {vb.sfx.ENV: empty}):
            f, _ = film.build(self.plan, self.change, log=log)
        self.assertEqual(f.sounds, ())
        lines = log.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn('without sound', lines[0])

    def test_counts_with_full_pack(self):
        with self.pack('full', FULL):
            f, frames = film.build(self.plan, self.change, log=io.StringIO())
        by = {}
        for s in f.sounds:
            by[s.effect] = by.get(s.effect, 0) + 1
            self.assertTrue(0 <= s.at < f.frames)
        shots = self.plan.shots
        opens = [s for s in shots if s['kind'] == 'open']
        pushes = sum(s['kind'] == 'xref-push' for s in shots)
        pops = sum(s['kind'] == 'xref-pop' for s in shots)
        keys = sum(len(s['path']) + 3 for s in opens) + 2 * pushes + 2 * pops   # C-x, C-f, letters, RET
        self.assertEqual(by['pro_typing_key/tick'], keys)
        self.assertEqual(by['pro_click/mouse'], pushes)
        self.assertEqual(by['pro_hover_tick/soft'], sum(s['kind'] == 'hunk' for s in shots) +
                         sum(len(s.get('callouts') or ()) for s in shots))
        self.assertEqual(by['whoosh_fast/tight'], len(opens) + pushes + pops)
        moves = sum(s['kind'] in ('dive', 'travel') for s in shots) + sum(s.get('scroll') == 'far' for s in shots)
        self.assertEqual(by['whoosh_fast/sleeve'], moves)
        self.assertGreater(moves, 0)
        (m,) = [s for s in shots if s['kind'] == 'map']
        self.assertEqual(by['pro_hover_tick/detent'], min(len(m['lit']), plan.MAP_MAX_TICKS))

    def test_role_fallback(self):
        with self.pack('clickonly', [('other/clicky', 'click')]):
            bank = vb.sfx.bank()
            self.assertIsNone(sounds.pick(bank, *sounds.KEY[:2]))
            self.assertEqual(sounds.pick(bank, *sounds.CLICK[:2]), 'other/clicky')
            self.assertIsNone(sounds.pick(bank, *sounds.TICK[:2]))
            self.assertIsNone(sounds.pick(bank, *sounds.WHOOSH[:2]))
            f, _ = film.build(self.plan, self.change, log=io.StringIO())
            self.assertEqual({s.effect for s in f.sounds}, {'other/clicky'})
        with self.pack('roles', [('a/key', 'key'), ('b/tick', 'tick'), ('c/whoosh', 'whoosh'), ('d/c', 'click')]):
            bank = vb.sfx.bank()
            got = [sounds.pick(bank, *t[:2]) for t in (sounds.KEY, sounds.CLICK, sounds.TICK, sounds.WHOOSH,
                                                       sounds.WHOOSH_SOFT, sounds.TILE)]
            self.assertEqual(got, ['a/key', 'd/c', 'b/tick', 'c/whoosh', 'c/whoosh', 'b/tick'])

    def test_tile_ticks_capped(self):
        shots = [plan.shot('map', 'map', plan.MAP_DUR, lit=[f'f{i}' for i in range(20)])]
        cues = sounds.cues(shots, [0], 30)
        self.assertEqual([c for _, c in cues], ['tile'] * plan.MAP_MAX_TICKS)
        frames = [f for f, _ in cues]
        self.assertEqual(frames, sorted(frames))
        self.assertLess(frames[-1], plan.MAP_DUR * 30)

    def test_new_cues(self):
        h = plan.shot('editor', 'hunk', 6.0, path='a.py', callouts=[{'at': 2.0}, {'at': 4.0}])
        cues = sounds.cues([h], [0], 30)
        self.assertEqual(cues, [(sounds.frame_at(0, plan.HUNK_SCROLL, 30), 'tick'),
                                (sounds.frame_at(0, 2.0 + 0.75 * plan.CALLOUT_IN, 30), 'tick'),
                                (sounds.frame_at(0, 4.0 + 0.75 * plan.CALLOUT_IN, 30), 'tick')])

    def test_far_scroll_whooshes(self):
        h = plan.shot('editor', 'hunk', 4.0, path='a.py', scroll='far', callouts=[])
        self.assertEqual(sounds.cues([h], [0], 30), [(sounds.frame_at(0, 0.1, 30), 'whoosh-soft'),
                                                     (sounds.frame_at(0, plan.HUNK_SCROLL_FAR, 30), 'tick')])

    def test_edit_cues(self):
        from git_video import edits
        steps = [{'kind': 'jump', 'at': 0.0, 'dur': 1.0, 'times': [0.0, 0.18, 0.4, 0.5], 'land': 0.5,
                  'scroll': 'far', 'line': 80, 'col': 0},
                 {'kind': 'kill', 'at': 1.0, 'dur': 0.6, 'mark': 0.1, 'cut': 0.4},
                 {'kind': 'type', 'at': 1.6, 'dur': 1.0, 'times': [0.0, 0.01, 0.2, 0.4], 'open': False},
                 {'kind': 'fill', 'at': 2.6, 'dur': 1.0}]
        clk, dur, _ = edits.clock(3.6, 1.0)
        shot = plan.shot('editor', 'edit', dur, steps=steps, clock=clk, fast=[], callouts=[{'at': 2.0}])
        cues = sounds.cues([shot], [0], 30)
        keys = [f for f, c in cues if c == 'key']
        # jump keys, the kill's mark and cut, typed keys (0.01 s after the one before is not heard alone)
        want = [0.0, 0.18, 0.4, 0.5, 1.1, 1.4, 1.6, 1.8, 2.0]
        self.assertEqual(keys, [sounds.frame_at(0, t, 30) for t in want])
        self.assertIn((sounds.frame_at(0, 0.5, 30), 'whoosh-soft'), cues)
        self.assertIn((sounds.frame_at(0, 2.6, 30), 'typing-run'), cues)
        self.assertIn((sounds.frame_at(0, 2.0 + 0.75 * plan.CALLOUT_IN, 30), 'tick'), cues)
        # a speed-up: no keys inside it, a typing run at its start and every RUN_EVERY s
        steps = [{'kind': 'type', 'at': 0.0, 'dur': 30.0, 'times': [k * 0.1 for k in range(300)], 'open': False}]
        clk, dur, span = edits.clock(30.0, 1.0, (2.0, 29.0, 8.0), 0.4)
        shot = plan.shot('editor', 'edit', dur, steps=steps, clock=clk, fast=[span], callouts=[])
        cues = sounds.cues([shot], [0], 30)
        inside = [f for f, c in cues if c == 'key' and span['at'] * 30 <= f <= span['until'] * 30]
        self.assertEqual(inside, [])
        runs = [f for f, c in cues if c == 'typing-run']
        self.assertEqual(runs[0], sounds.frame_at(0, span['at'], 30))
        self.assertEqual(len(runs), math.ceil((span['until'] - span['at']) / sounds.RUN_EVERY))
        self.assertTrue([f for f, c in cues if c == 'key'])

    def test_develop_counts_with_full_pack(self):
        ch = collect.collect(fixtures.make_big_repo(self.tmp.name), 'HEAD')
        p = plan.build(ch, 0, 'develop')
        with self.pack('fulldev', FULL):
            f, frames = film.build(p, ch, log=io.StringIO())
        by = {}
        for s in f.sounds:
            by[s.effect] = by.get(s.effect, 0) + 1
        self.assertGreater(by.get('pro_typing_run/standard', 0), 0)
        self.assertGreater(by.get('pro_typing_key/tick', 0), 0)
        fps = p.fps
        for i, s in enumerate(p.shots):
            for span in s.get('fast') or ():
                a, b = frames[i] + span['at'] * fps, frames[i] + span['until'] * fps
                keys = [x for x in f.sounds if x.effect == 'pro_typing_key/tick' and a < x.at < b - 1]
                self.assertEqual(keys, [])

    def test_keystroke_frames_deterministic(self):
        a = sounds.cues(self.plan.shots, [0] * len(self.plan.shots), 30)
        b = sounds.cues(self.plan.shots, [0] * len(self.plan.shots), 30)
        self.assertEqual(a, b)
        keys, letters, ret, _ = plan.open_timing('src/app.py')
        times = [*keys, *letters, ret]
        self.assertEqual(times, sorted(times))
        self.assertEqual(plan.open_timing('src/app.py'), plan.open_timing('src/app.py'))
        self.assertEqual(len(letters), len('src/app.py'))


if __name__ == '__main__':
    unittest.main()

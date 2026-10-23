import io
import os
import tempfile
import unittest
from unittest import mock

import videobuilder as vb

from git_video import collect, film, plan, sounds
from tests import fixtures

FULL = [('pro_typing_key/tick', 'key'), ('pro_click/mouse', 'click'), ('pro_hover_tick/soft', 'tick'),
        ('whoosh_fast/tight', 'whoosh'), ('whoosh_fast/sleeve', 'whoosh'), ('pro_hover_tick/detent', 'tick')]


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
        self.assertEqual(by['pro_hover_tick/soft'], sum(s['kind'] == 'hunk' for s in shots))
        self.assertEqual(by['whoosh_fast/tight'], len(opens) + pushes + pops)
        moves = sum(s['kind'] in ('dive', 'travel') for s in shots)
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

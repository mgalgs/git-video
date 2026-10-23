"""The fork-sandbox narrator, against a fake `fork-sandbox` first on PATH: it
records its argv, prints launcher-like output with a `  run dir:  <dir>`
line, and writes a canned outbox/narration.json (FAKE_NARRATION) or, per
FAKE_MODE, a bad one, none, one over the limits, a failure, or no run dir."""
import io
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from git_video import cli, collect, narrator, plan, voice
from tests import fixtures
from tests.test_cli import quiet

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
state = os.environ['FAKE_STATE']
n = len([x for x in os.listdir(state) if x.startswith('argv-')])
with open(os.path.join(state, f'argv-{n}.json'), 'w') as f:
    json.dump(sys.argv[1:], f)
with open(sys.argv[-1]) as f, open(os.path.join(state, f'handoff-{n}.md'), 'w') as g:
    g.write(f.read())
mode = os.environ.get('FAKE_MODE', 'ok')
run = os.path.join(state, f'run-{n}')
os.makedirs(os.path.join(run, 'outbox'))
print('fork-sandbox: launched in a sandbox')
print('  harness:  claude')
if mode != 'no-run-dir':
    print(f'  run dir:  {run}')
print(f'  log:      {run}/events.jsonl')
if mode == 'no-run-dir':
    print('fork-sandbox: error: something went wrong before launch', file=sys.stderr)
    sys.exit(3)
if mode == 'fail':
    sys.exit(2)
if mode == 'none':
    sys.exit(0)
text = {'bad': '{"phrases": [', 'unknown': json.dumps({'phrases': [{'shot': 's999', 'text': 'Hi.'}]})}.get(
    mode, os.environ.get('FAKE_NARRATION', ''))
with open(os.path.join(run, 'outbox', 'narration.json'), 'w') as f:
    f.write(text)
'''


class FakeForkSandbox:
    """A fake fork-sandbox on PATH (only it: `which` finds nothing else by that name)."""

    def __init__(self, tmp):
        self.bin = os.path.join(tmp, 'bin')
        self.state = os.path.join(tmp, 'state')
        os.makedirs(self.bin)
        os.makedirs(self.state)
        path = os.path.join(self.bin, 'fork-sandbox')
        with open(path, 'w') as f:
            f.write(FAKE)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def env(self, **extra):
        path = os.pathsep.join([self.bin] + [d for d in os.environ.get('PATH', '').split(os.pathsep)
                                             if not os.path.exists(os.path.join(d, 'fork-sandbox'))])
        return {'PATH': path, 'FAKE_STATE': self.state, **extra}

    def calls(self):
        return sorted(x for x in os.listdir(self.state) if x.startswith('argv-'))

    def argv(self, n=0):
        with open(os.path.join(self.state, f'argv-{n}.json')) as f:
            return json.load(f)

    def handoff(self, n=0):
        with open(os.path.join(self.state, f'handoff-{n}.md')) as f:
            return f.read()


class TestForkSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeForkSandbox(self.tmp.name)
        self.repo, self.sha = fixtures.make_repo(os.path.join(self.tmp.name, 'repo'))
        self.change = collect.collect(self.repo, self.sha['change'])
        self.plan = plan.build(self.change, 0, 'tour')
        rows = narrator.shot_rows(self.plan, [self.change])
        self.hunk = next(r for r in rows if r.key[0] == 'hunk')
        self.good = {'phrases': [{'shot': 's0', 'text': 'This change moves the arithmetic into a library.'},
                                 {'shot': self.hunk.id, 'text': 'The app now asks the library, so both agree.'},
                                 {'shot': rows[-1].id, 'text': 'That is the whole idea.'}]}
        self.cache = os.path.join(self.tmp.name, 'cache')
        self.env = mock.patch.dict(os.environ, self.fake.env(XDG_CACHE_HOME=self.cache,
                                                             FAKE_NARRATION=json.dumps(self.good)))
        self.env.start()
        self.scratch = mock.patch.object(narrator, 'SCRATCH_ROOT', os.path.join(self.tmp.name, 'claude-scratch'))
        self.scratch.start()
        os.environ.pop('GIT_VIDEO_NARRATOR_MODEL', None)
        os.environ.pop('GIT_VIDEO_NARRATOR_ARGS', None)

    def tearDown(self):
        self.scratch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def writer(self, fallback=False, log=None):
        return narrator.Narrator('fork-sandbox', self.change, [self.change], 'tour', False, 0, fallback,
                                 io.StringIO() if log is None else log)

    def test_resolve(self):
        self.assertEqual(narrator.resolve('auto'), ('fork-sandbox', True))
        self.assertEqual(narrator.resolve('auto', which=lambda _: None), ('heuristic', False))
        with mock.patch.dict(os.environ, {'PATH': self.tmp.name}):
            self.assertEqual(narrator.resolve('auto'), ('heuristic', False))
        self.assertEqual(narrator.resolve('fork-sandbox', which=lambda _: None), ('fork-sandbox', False))
        self.assertEqual(narrator.resolve('heuristic'), ('heuristic', False))

    def test_invocation(self):
        w = self.writer()
        got = w(self.plan)
        self.assertEqual([p.text for p in got], [x['text'] for x in self.good['phrases']])
        self.assertEqual([p.key for p in got], [('title',), self.hunk.key, ('end',)])
        argv = self.fake.argv()
        key = narrator.cache_key(self.change, self.plan, [self.change], 'tour', False, 0, 'opus')
        self.assertEqual(argv[:-1], [
            'run', '--foreground', '--harness', 'claude', '--model', 'opus', '--checkout', self.change.target,
            '--branch', f'git-video-narrate-{key[:12]}',
            '--task-meta', '{"kind":"investigate","prompt_template_id":"git-video-narrate-v1"}',
            self.change.repo])
        self.assertEqual(self.change.target, self.sha['change'])
        self.assertEqual(self.fake.handoff(), narrator.brief(self.change, self.plan, [self.change]))
        self.assertFalse(os.path.exists(argv[-1]))                     # our temp handoff is gone ...
        self.assertEqual(w.run_dir, os.path.join(self.fake.state, 'run-0'))
        self.assertTrue(os.path.isdir(w.run_dir))                      # ... the run dir is left
        self.assertEqual(fixtures.git(self.repo, 'status', '--porcelain'), '')

    def test_handoff_in_scratch_root(self):
        # fork-sandbox refuses a handoff outside /var/tmp/claude-scratch/, or in its forks/ dir
        self.scratch.stop()
        self.assertEqual(narrator.SCRATCH_ROOT, '/var/tmp/claude-scratch')
        try:
            self.writer()(self.plan)
        finally:
            self.scratch.start()
        handoff = os.path.realpath(self.fake.argv()[-1])
        root = os.path.realpath('/var/tmp') + '/claude-scratch'
        self.assertTrue(handoff.startswith(root + '/git-video-narrate-'), handoff)
        self.assertNotIn('/forks/', handoff[len(root):])
        self.assertFalse(os.path.exists(os.path.dirname(handoff)))      # staged, then removed

    def test_model_and_args_from_env(self):
        creds = os.path.join(self.tmp.name, 'my creds.json')
        with mock.patch.dict(os.environ, {'GIT_VIDEO_NARRATOR_MODEL': 'sonnet',
                                          'GIT_VIDEO_NARRATOR_ARGS': f"--claude-credentials '{creds}' --quiet"}):
            self.writer()(self.plan)
        argv = self.fake.argv()
        self.assertEqual(argv[argv.index('--model') + 1], 'sonnet')
        self.assertEqual(argv[-5:-2], ['--claude-credentials', creds, '--quiet'])
        self.assertEqual(argv[-2], self.change.repo)

    def test_cache(self):
        a = self.writer()(self.plan)
        self.assertEqual(len(self.fake.calls()), 1)
        w = self.writer()
        self.assertEqual(w(self.plan), a)
        self.assertEqual(len(self.fake.calls()), 1)          # a second run does not call fork-sandbox
        self.assertIsNone(w.run_dir)
        entries = os.listdir(os.path.join(self.cache, 'git-video', 'narration'))
        self.assertEqual(len(entries), 1)
        # another model, mode or verbosity is another narration
        with mock.patch.dict(os.environ, {'GIT_VIDEO_NARRATOR_MODEL': 'sonnet'}):
            self.writer()(self.plan)
        dev = plan.build(self.change, 0, 'develop')
        opening = json.dumps({'phrases': self.good['phrases'][:1]})
        with mock.patch.dict(os.environ, {'FAKE_NARRATION': opening}):
            narrator.Narrator('fork-sandbox', self.change, [self.change], 'develop', False, 0, False, io.StringIO())(dev)
        self.assertEqual(len(self.fake.calls()), 3)

    def test_cache_key_includes_duration(self):
        key = lambda d: narrator.cache_key(self.change, self.plan, [self.change], 'tour', False, 0, 'opus', d)
        self.assertEqual(key(None), key(None))
        self.assertEqual(key(60.0), key(60.0))
        self.assertEqual(len({key(None), key(60.0), key(30.0)}), 3)
        for target in (60.0, 30.0):   # the same change at two targets: two narrations
            p = plan.build(self.change, 0, 'tour', duration=target)
            narrator.Narrator('fork-sandbox', self.change, [self.change], 'tour', False, 0, False, io.StringIO(),
                              duration=target)(p)
        self.assertEqual(len(self.fake.calls()), 2)
        p = plan.build(self.change, 0, 'tour', duration=60.0)
        narrator.Narrator('fork-sandbox', self.change, [self.change], 'tour', False, 0, False, io.StringIO(),
                          duration=60.0)(p)
        self.assertEqual(len(self.fake.calls()), 2)   # found in the cache

    def test_over_the_limits_is_trimmed(self):
        most, _ = narrator.limits(self.plan)
        rows = narrator.shot_rows(self.plan, [self.change])
        mid = [{'shot': r.id, 'text': f'Reason number {n}.'} for n, r in enumerate(rows[1:-1])]
        doc = {'phrases': [{'shot': 's0', 'text': 'Opening.'}] + mid + [{'shot': rows[-1].id, 'text': 'Closing.'}]}
        self.assertGreater(len(doc['phrases']), most)
        with mock.patch.dict(os.environ, {'FAKE_NARRATION': json.dumps(doc)}):
            got = self.writer()(self.plan)
        self.assertEqual(len(got), most)
        self.assertEqual((got[0].text, got[-1].text), ('Opening.', 'Closing.'))
        self.assertEqual([p.text for p in got[1:-1]], [x['text'] for x in mid[:most - 2]])

    def test_failures(self):
        for mode, want in (('bad', 'does not pass'), ('unknown', 'no shot'), ('none', 'no outbox/narration.json'),
                           ('fail', 'exited 2'), ('no-run-dir', 'printed no run dir')):
            with self.subTest(mode), mock.patch.dict(os.environ, {'FAKE_MODE': mode}):
                with self.assertRaises(narrator.NarratorError) as cm:
                    self.writer()(self.plan)
                self.assertIn(want, str(cm.exception))
                if mode == 'no-run-dir':   # fork-sandbox's own output goes into the error
                    self.assertIn('something went wrong before launch', str(cm.exception))
                    self.assertIsNone(cm.exception.run_dir)
                else:
                    self.assertTrue(cm.exception.run_dir.startswith(self.fake.state))
                    self.assertIn(cm.exception.run_dir, str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'git-video', 'narration')))   # no failure cached

    def test_fallback(self):
        with mock.patch.dict(os.environ, {'FAKE_MODE': 'bad'}):
            log = io.StringIO()
            got = self.writer(fallback=True, log=log)(self.plan)
        self.assertEqual(got, voice.script(self.plan, [self.change]))
        lines = log.getvalue().splitlines()
        self.assertEqual(len([ln for ln in lines if 'warning' in ln]), 1)
        warn = next(ln for ln in lines if 'warning' in ln)
        self.assertIn(os.path.join(self.fake.state, 'run-0'), warn)
        self.assertIn('heuristic', warn)

    def test_fallback_says_why_launcher_refused(self):
        with mock.patch.dict(os.environ, {'FAKE_MODE': 'no-run-dir'}):
            log = io.StringIO()
            self.writer(fallback=True, log=log)(self.plan)
        warn = [ln for ln in log.getvalue().splitlines() if 'warning' in ln]
        self.assertEqual(len(warn), 1)
        self.assertIn('error: something went wrong before launch', warn[0])
        self.assertEqual(narrator._last_word('a\nError: no handoff\n  more detail\n'), 'Error: no handoff')
        self.assertEqual(narrator._last_word('a\n b \n\n'), 'b')


class TestNarratorFlag(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeForkSandbox(self.tmp.name)
        self.repo, self.sha = fixtures.make_callout_repo(self.tmp.name)
        self.env = mock.patch.dict(os.environ, self.fake.env(XDG_CACHE_HOME=os.path.join(self.tmp.name, 'cache')))
        self.env.start()
        scratch = mock.patch.object(narrator, 'SCRATCH_ROOT', os.path.join(self.tmp.name, 'claude-scratch'))
        scratch.start()
        self.addCleanup(scratch.stop)
        self.engine = mock.patch('git_video.voice.make_engine', return_value=fixtures.FakeEngine())
        self.engine.start()

    def tearDown(self):
        self.engine.stop()
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, *args):
        return quiet(cli.main, ['-C', self.repo, '--plan-json', *args, self.sha['head']])

    def narration(self):
        ch = collect.collect(self.repo, self.sha['head'])
        rows = narrator.shot_rows(plan.build(ch, 0, 'tour'), [ch])
        return json.dumps({'phrases': [{'shot': 's0', 'text': 'Both helpers now scale their input.'},
                                       {'shot': rows[-1].id, 'text': 'That keeps them in step.'}]})

    def test_parse(self):
        self.assertIsNone(cli.parse(['--voice']).narrator)
        self.assertEqual(cli.parse(['--voice', '--narrator', 'heuristic']).narrator, 'heuristic')
        self.assertEqual(cli.parse(['--voice', '--narrator=fork-sandbox']).narrator, 'fork-sandbox')
        with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
            cli.parse(['--narrator', 'gpt'])

    def test_needs_voice(self):
        for choice in ('auto', 'heuristic', 'fork-sandbox'):
            code, _, err = self.run_cli('--narrator', choice)
            self.assertEqual(code, 2)
            self.assertIn('--narrator needs --voice', err)
        self.assertEqual(self.fake.calls(), [])

    def test_auto_uses_fork_sandbox(self):
        with mock.patch.dict(os.environ, {'FAKE_NARRATION': self.narration()}):
            code, out, err = self.run_cli('--voice')
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.fake.calls()), 1)
        said = [q['text'] for q in json.loads(out)['voice']['phrases']]
        self.assertEqual(said, ['Both helpers now scale their input.', 'That keeps them in step.'])
        self.assertIn('narrating with fork-sandbox', err)
        self.assertIn(os.path.join(self.fake.state, 'run-0'), err.splitlines()[-1])   # named at the end
        # --plan-json --voice takes the same cached path: no second run
        code, out2, _ = self.run_cli('--voice')
        self.assertEqual((code, len(self.fake.calls())), (0, 1))
        self.assertEqual(json.loads(out2)['voice']['phrases'], json.loads(out)['voice']['phrases'])

    def test_auto_without_fork_sandbox(self):
        path = os.pathsep.join(d for d in os.environ['PATH'].split(os.pathsep) if d != self.fake.bin)
        with mock.patch.dict(os.environ, {'PATH': path}):   # no fork-sandbox anywhere on it
            code, out, err = self.run_cli('--voice')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.calls(), [])
        self.assertNotIn('fork-sandbox', err)
        self.assertTrue(json.loads(out)['voice']['phrases'])

    def test_explicit_heuristic(self):
        code, out, err = self.run_cli('--voice', '--narrator', 'heuristic')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.calls(), [])

    def test_auto_falls_back(self):
        with mock.patch.dict(os.environ, {'FAKE_MODE': 'bad'}):
            code, out, err = self.run_cli('--voice')
        self.assertEqual(code, 0, err)
        warn = [ln for ln in err.splitlines() if 'warning' in ln]
        self.assertEqual(len(warn), 1)
        self.assertIn('run-0', warn[0])
        ch = collect.collect(self.repo, self.sha['head'])
        self.assertEqual(json.loads(out)['voice']['phrases'][0]['text'],
                         voice.script(plan.build(ch, 0, 'tour'), [ch])[0].text)

    def test_explicit_fails(self):
        out_mp4 = os.path.join(self.tmp.name, 'out.mp4')
        for mode in ('bad', 'no-run-dir'):
            with mock.patch.dict(os.environ, {'FAKE_MODE': mode}):
                code, out, err = quiet(cli.main, ['-C', self.repo, '--voice', '--narrator=fork-sandbox',
                                                  '-o', out_mp4, self.sha['head']])
            self.assertEqual(code, 1, (mode, err))
            self.assertIn('fork-sandbox narrator', err)
            self.assertEqual(out, '')
            self.assertFalse(os.path.exists(out_mp4))
        self.assertIn('something went wrong before launch', err)


if __name__ == '__main__':
    unittest.main()

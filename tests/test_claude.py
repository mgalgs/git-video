"""The claude narrator, against a fake `claude` first on PATH: it records its
argv, its working directory (and the commit checked out there) and the
brief on its stdin, and prints `claude -p --output-format json`'s result
object around a canned answer (FAKE_NARRATION) or, per FAKE_MODE, a fenced
one, a bad one, a failure, output that is not JSON, an error result, or a
hang."""
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from git_video import cli, collect, narrator, plan, voice
from tests import fixtures
from tests.test_cli import quiet
from tests.test_fork_sandbox import FakeForkSandbox

FAKE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time
state = os.environ['FAKE_STATE']
n = len([x for x in os.listdir(state) if x.startswith('argv-')])
head = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
changes = open('.git-video-changes.diff').read() if os.path.exists('.git-video-changes.diff') else None
with open(os.path.join(state, f'argv-{n}.json'), 'w') as f:
    json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'head': head, 'changes': changes}, f)
with open(os.path.join(state, f'brief-{n}.md'), 'w') as f:
    f.write(sys.stdin.read())
mode = os.environ.get('FAKE_MODE', 'ok')
text = os.environ.get('FAKE_NARRATION', '')
if mode == 'fail':
    print('Error: Invalid API key', file=sys.stderr)
    sys.exit(1)
if mode == 'garbage':
    print('this is not json')
    sys.exit(0)
if mode == 'hang':
    time.sleep(30)
result = {'fenced': f'Here is the narration.\n\n```json\n{text}\n```\n', 'bad': '{"phrases": [',
          'unknown': json.dumps({'phrases': [{'shot': 's999', 'text': 'Hi.'}]}),
          'unbalanced': 'I could not read shot {s3 so I stopped.', 'error': 'Credit balance is too low'}.get(mode, text)
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': mode == 'error', 'result': result}))
'''


class FakeClaude:
    """A fake claude on PATH (only it: `which` finds nothing else by that name)."""

    def __init__(self, tmp):
        self.bin = os.path.join(tmp, 'claude-bin')
        self.state = os.path.join(tmp, 'claude-state')
        os.makedirs(self.bin)
        os.makedirs(self.state)
        path = os.path.join(self.bin, 'claude')
        with open(path, 'w') as f:
            f.write(FAKE)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def env(self, **extra):
        path = os.pathsep.join([self.bin] + [d for d in os.environ.get('PATH', '').split(os.pathsep)
                                             if not os.path.exists(os.path.join(d, 'claude'))])
        return {'PATH': path, 'FAKE_STATE': self.state, **extra}

    def calls(self):
        return sorted(x for x in os.listdir(self.state) if x.startswith('argv-'))

    def call(self, n=0):
        with open(os.path.join(self.state, f'argv-{n}.json')) as f:
            return json.load(f)

    def brief(self, n=0):
        with open(os.path.join(self.state, f'brief-{n}.md')) as f:
            return f.read()


class TestClaude(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeClaude(self.tmp.name)
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
        os.environ.pop('GIT_VIDEO_NARRATOR_MODEL', None)
        os.environ.pop('GIT_VIDEO_NARRATOR_ARGS', None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def writer(self, fallback=False, log=None):
        return narrator.Narrator('claude', self.change, [self.change], 'tour', False, 0, fallback,
                                 io.StringIO() if log is None else log)

    def test_success(self):
        w = self.writer()
        got = w(self.plan)
        self.assertEqual([p.text for p in got], [x['text'] for x in self.good['phrases']])
        self.assertEqual([p.key for p in got], [('title',), self.hunk.key, ('end',)])
        self.assertEqual((w.ran, w.run_dir), ('claude', None))
        call = self.fake.call()
        self.assertEqual(call['head'], self.sha['change'])                # a checkout of the change's tip ...
        changes = call['changes']   # ... with the diffstat and diff beside it
        self.assertRegex(changes, r'^ ?bin\.dat +\| Bin')
        self.assertEqual(changes.count('diff --git '), len(self.change.files))
        self.assertNotEqual(os.path.realpath(call['cwd']), os.path.realpath(self.repo))
        self.assertFalse(os.path.exists(call['cwd']))                     # ... removed after
        self.assertEqual(fixtures.git(self.repo, 'worktree', 'list').count('\n'), 0)
        self.assertEqual(fixtures.git(self.repo, 'status', '--porcelain'), '')
        self.assertEqual(self.fake.brief(), narrator.brief(self.change, self.plan, [self.change], 'claude'))

    def test_brief_output_section(self):
        mine = narrator.brief(self.change, self.plan, [self.change], 'claude')
        self.assertNotIn('outbox', mine)
        self.assertIn('Your final message is\nthe narration, one JSON object', mine)
        theirs = narrator.brief(self.change, self.plan, [self.change])
        self.assertIn('Your only output is one file:\n`outbox/narration.json`.', theirs)
        self.assertIn('Write `outbox/narration.json`, in your artifact outbox (not in the\nrepository):', theirs)
        self.assertIn('Write the file, check that it parses as JSON, and end your turn.', theirs)
        self.assertNotRegex(mine + theirs, r'\{(inspect|output|output_how|output_end)\}')
        self.assertIn(f'`{narrator.CHANGES}`', mine)
        self.assertNotIn('git diff', mine)
        self.assertIn(f'git diff {self.change.base}..{self.change.target}', theirs)

    def test_read_only_flags(self):
        self.writer()(self.plan)
        argv = self.fake.call()['argv']
        self.assertEqual(argv[:6], ['-p', '--output-format', 'json', '--model', 'opus', '--restricted'])
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'dontAsk')
        self.assertEqual(argv[argv.index('--tools') + 1], 'Read,Grep,Glob')   # no shell: git can write and read env
        allowed = argv[argv.index('--allowedTools') + 1:argv.index('--disallowedTools')]
        self.assertEqual(allowed, ['Read', 'Grep', 'Glob'])
        denied = argv[argv.index('--disallowedTools') + 1:]
        self.assertTrue({'Bash', 'Write', 'Edit', 'WebFetch', 'WebSearch'} <= set(denied), denied)
        self.assertIn('--strict-mcp-config', argv)
        self.assertNotIn('--dangerously-skip-permissions', argv)

    def test_model_from_env_not_args(self):
        with mock.patch.dict(os.environ, {'GIT_VIDEO_NARRATOR_MODEL': 'sonnet',
                                          'GIT_VIDEO_NARRATOR_ARGS': '--claude-credentials x'}):
            self.writer()(self.plan)
        argv = self.fake.call()['argv']
        self.assertEqual(argv[argv.index('--model') + 1], 'sonnet')
        self.assertNotIn('--claude-credentials', argv)

    def test_fenced_answer(self):
        with mock.patch.dict(os.environ, {'FAKE_MODE': 'fenced'}):
            got = self.writer()(self.plan)
        self.assertEqual([p.text for p in got], [x['text'] for x in self.good['phrases']])
        self.assertEqual(narrator.answer(json.dumps({'result': 'Sure: {"phrases": []} done.'})), '{"phrases": []}')
        self.assertEqual(narrator.answer(json.dumps({'result': '```\n{"a": 1}\n```'})), '{"a": 1}')

    def test_cache(self):
        a = self.writer()(self.plan)
        self.assertEqual(len(self.fake.calls()), 1)
        self.assertEqual(self.writer()(self.plan), a)
        self.assertEqual(len(self.fake.calls()), 1)          # a second run does not call claude
        with mock.patch.dict(os.environ, {'GIT_VIDEO_NARRATOR_MODEL': 'sonnet'}):
            self.writer()(self.plan)
        self.assertEqual(len(self.fake.calls()), 2)
        key = lambda name: narrator.cache_key(self.change, self.plan, [self.change], 'tour', False, 0, 'opus',
                                              None, name)
        self.assertNotEqual(key('claude'), key('fork-sandbox'))   # the two agents never share a script
        self.assertEqual(key('fork-sandbox'),
                         narrator.cache_key(self.change, self.plan, [self.change], 'tour', False, 0, 'opus'))
        self.assertTrue(os.path.isfile(narrator.cache_path(key('claude'))))
        self.assertFalse(os.path.isfile(narrator.cache_path(key('fork-sandbox'))))

    def test_failures(self):
        for mode, want in (('fail', 'claude exited 1'), ('garbage', "claude's output is not JSON"),
                           ('error', 'claude failed: Credit balance is too low'), ('bad', 'does not pass'),
                           ('unknown', 'no shot'), ('unbalanced', 'does not pass')):
            with self.subTest(mode), mock.patch.dict(os.environ, {'FAKE_MODE': mode}):
                with self.assertRaises(narrator.NarratorError) as cm:
                    self.writer()(self.plan)
                self.assertIn(want, str(cm.exception))
                self.assertIsNone(cm.exception.run_dir)
        self.assertIn('Invalid API key', self._fail_output())   # claude's own stderr goes into the error
        self.assertFalse(os.path.exists(os.path.join(self.cache, 'git-video', 'narration')))   # no failure cached
        self.assertEqual(fixtures.git(self.repo, 'worktree', 'list').count('\n'), 0)

    def test_other_worktrees_kept(self):
        away = os.path.join(self.tmp.name, 'away')
        fixtures.git(self.repo, 'worktree', 'add', '--detach', away)
        shutil.rmtree(away)                      # say, on a drive that is not mounted now
        self.writer()(self.plan)
        self.assertIn(away, fixtures.git(self.repo, 'worktree', 'list'))

    def _fail_output(self):
        with mock.patch.dict(os.environ, {'FAKE_MODE': 'fail'}):
            with self.assertRaises(narrator.NarratorError) as cm:
                self.writer()(self.plan)
        return str(cm.exception)

    def test_missing(self):
        path = os.pathsep.join(d for d in os.environ['PATH'].split(os.pathsep) if d != self.fake.bin)
        with mock.patch.dict(os.environ, {'PATH': path}):
            with self.assertRaises(narrator.NarratorError) as cm:
                self.writer()(self.plan)
        self.assertIn('cannot run claude', cm.exception.reason)

    def test_timeout(self):
        with mock.patch.dict(os.environ, {'FAKE_MODE': 'hang'}), mock.patch.object(narrator, 'CLAUDE_TIMEOUT', 1):
            with self.assertRaises(narrator.NarratorError) as cm:
                self.writer()(self.plan)
        self.assertEqual(cm.exception.reason, 'claude timed out after 1s')
        self.assertEqual(fixtures.git(self.repo, 'worktree', 'list').count('\n'), 0)

    def test_fallback(self):
        with mock.patch.dict(os.environ, {'FAKE_MODE': 'fail'}):
            log = io.StringIO()
            w = self.writer(fallback=True, log=log)
            got = w(self.plan)
        self.assertEqual(got, voice.script(self.plan, [self.change]))
        self.assertEqual(w.ran, 'heuristic')
        warn = [ln for ln in log.getvalue().splitlines() if 'warning' in ln]
        self.assertEqual(warn, ['git video: warning: claude narrator failed (claude exited 1; it said: Error: '
                                'Invalid API key); using the heuristic narrator'])


class TestResolve(unittest.TestCase):
    def which(self, *found):
        return lambda name: f'/bin/{name}' if name in found else None

    def test_auto(self):
        token = {'CLAUDE_CODE_OAUTH_TOKEN': 'x'}
        self.assertEqual(narrator.resolve('auto', self.which('fork-sandbox', 'claude'), token), ('fork-sandbox', True))
        self.assertEqual(narrator.resolve('auto', self.which('claude'), token), ('claude', True))
        self.assertEqual(narrator.resolve('auto', self.which('claude'), {'ANTHROPIC_API_KEY': 'x'}), ('claude', True))
        # claude on PATH is not consent to spend: no credential variable, no claude
        self.assertEqual(narrator.resolve('auto', self.which('claude'), {}), ('heuristic', False))
        self.assertEqual(narrator.resolve('auto', self.which('claude'), {'CLAUDE_CODE_OAUTH_TOKEN': ''}),
                         ('heuristic', False))
        self.assertEqual(narrator.resolve('auto', self.which(), token), ('heuristic', False))
        # explicit choices never fall back
        self.assertEqual(narrator.resolve('claude', self.which(), {}), ('claude', False))

    def test_auto_reads_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeClaude(tmp)
            with mock.patch.dict(os.environ, fake.env()):
                os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
                os.environ.pop('ANTHROPIC_API_KEY', None)
                self.assertEqual(narrator.resolve('auto'), ('heuristic', False))
                with mock.patch.dict(os.environ, {'CLAUDE_CODE_OAUTH_TOKEN': 'x'}):
                    self.assertEqual(narrator.resolve('auto'), ('claude', True))


class TestClaudeFlag(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeClaude(self.tmp.name)
        self.forks = FakeForkSandbox(self.tmp.name)
        self.repo, self.sha = fixtures.make_callout_repo(os.path.join(self.tmp.name, 'repo'))
        no_forks = os.pathsep.join(d for d in self.fake.env()['PATH'].split(os.pathsep) if d != self.forks.bin)
        env = mock.patch.dict(os.environ, {**self.fake.env(), 'PATH': no_forks, 'CLAUDE_CODE_OAUTH_TOKEN': 'secret',
                                           'XDG_CACHE_HOME': os.path.join(self.tmp.name, 'cache')})
        env.start()
        self.addCleanup(env.stop)
        engine = mock.patch('git_video.voice.make_engine', return_value=fixtures.FakeEngine())
        engine.start()
        self.addCleanup(engine.stop)
        ch = collect.collect(self.repo, self.sha['head'])
        rows = narrator.shot_rows(plan.build(ch, 0, 'tour'), [ch])
        os.environ['FAKE_NARRATION'] = json.dumps({'phrases': [
            {'shot': 's0', 'text': 'Both helpers now scale their input.'},
            {'shot': rows[-1].id, 'text': 'That keeps them in step.'}]})

    def test_parse(self):
        self.assertEqual(cli.parse(['--voice', '--narrator', 'claude']).narrator, 'claude')
        self.assertIn('claude', cli.parser().format_help())

    def test_auto_uses_claude(self):
        code, out, err = quiet(cli.main, ['-C', self.repo, '--plan-json', '--voice', self.sha['head']])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.fake.calls()), 1)
        said = [q['text'] for q in json.loads(out)['voice']['phrases']]
        self.assertEqual(said, ['Both helpers now scale their input.', 'That keeps them in step.'])
        self.assertIn('narrating with claude', err)
        self.assertIn('git video: narrated by claude:', err)
        self.assertRegex(err, r'(?m)^ +0\.\d\d- +\d+\.\d\d  \[title\]  Both helpers now scale their input\.$')
        self.assertNotIn('secret', out + err)

    def test_auto_without_credentials(self):
        os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN')
        code, out, err = quiet(cli.main, ['-C', self.repo, '--plan-json', '--voice', self.sha['head']])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.calls(), [])
        self.assertIn('git video: narrated by heuristic:', err)

    def test_auto_falls_back(self):
        with mock.patch.dict(os.environ, {'FAKE_MODE': 'fail'}):
            code, out, err = quiet(cli.main, ['-C', self.repo, '--plan-json', '--voice', self.sha['head']])
        self.assertEqual(code, 0, err)
        self.assertIn('warning: claude narrator failed', err)
        self.assertIn('git video: narrated by heuristic:', err)

    def test_explicit_fails(self):
        out_mp4 = os.path.join(self.tmp.name, 'out.mp4')
        with mock.patch.dict(os.environ, {'FAKE_MODE': 'fail'}):
            code, out, err = quiet(cli.main, ['-C', self.repo, '--voice', '--narrator=claude', '-o', out_mp4,
                                              self.sha['head']])
        self.assertEqual(code, 1, err)
        self.assertIn('git video: claude narrator: claude exited 1', err)
        self.assertFalse(os.path.exists(out_mp4))

    def test_format_all_narrates_once(self):
        with mock.patch('git_video.film.Tour'), mock.patch('videobuilder.render.render') as render:
            code, out, err = quiet(cli.main, ['-C', self.repo, '--voice', '--format', 'all', '--duration', '60',
                                              '-o', os.path.join(self.tmp.name, 'v.mp4'), self.sha['head']])
        self.assertEqual(code, 0, err)
        self.assertEqual(render.call_count, 3)
        self.assertEqual(len(self.fake.calls()), 1)
        self.assertEqual(err.count('git video: narrated by claude:'), 1)


if __name__ == '__main__':
    unittest.main()

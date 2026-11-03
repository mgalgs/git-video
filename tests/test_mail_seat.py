"""The mail-seat narrator, against a fake mail API in place of `narrator.post`:
it records each /v1/exec request and answers `mail send` with a thread id and
`mail export --json` with the thread, which holds the seat's reply once it has
"replied" (after `polls` looks), whose body is a canned answer or, per `mode`,
prose with no JSON or JSON that does not pass."""
import base64
import http.server
import io
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from git_video import collect, narrator, plan
from tests import fixtures

THREAD = '0f3c2a1e-5b6d-4e7f-8a9b-0c1d2e3f4a5b'
REPLY = '9a8b7c6d-5e4f-4a3b-2c1d-0e9f8a7b6c5d'


def b64(text):
    return base64.b64encode(text.encode()).decode()


class FakeMail:
    def __init__(self, narration, polls=1, mode='ok', status=200):
        self.narration, self.polls, self.mode, self.status = narration, polls, mode, status
        self.requests = []

    def __call__(self, url, token, body, key=None):
        self.requests.append({'url': url, 'token': token, 'body': body, 'key': key})
        if self.status != 200:
            return self.status, {'error': 'this token may not do that'}
        verb = body['argv'][0]
        if verb == 'send':
            return 200, {'rc': 0, 'stdout_b64': b64(f'{THREAD}\n'),
                         'stderr_b64': b64(f'fork-sandbox mail: sent {THREAD} as a new thread\n')}
        if verb == 'export':
            messages = [{'seq': 1, 'id': THREAD, 'from': '@git-video-narrate', 'to': ['@narrator'],
                         'body': 'The brief.'}]
            if sum(r['body']['argv'][0] == 'export' for r in self.requests) > self.polls:
                text = {'prose': 'I read the change but have nothing to say about it.',
                        'unknown': json.dumps({'phrases': [{'shot': 's999', 'text': 'Hi.'}]})}.get(
                            self.mode, f'Here is the narration.\n\n```json\n{json.dumps(self.narration)}\n```\n')
                messages.append({'seq': 2, 'id': REPLY, 'from': '@narrator', 'to': ['@git-video-narrate'],
                                 'in_reply_to': THREAD, 'body': text})
            doc = {'thread': THREAD, 'messages': messages, 'senders': {}, 'addressed': []}
            return 200, {'rc': 0, 'stdout_b64': b64(json.dumps(doc)), 'stderr_b64': ''}
        raise AssertionError(f'unexpected verb {verb}')

    def verbs(self):
        return [r['body']['argv'][0] for r in self.requests]


class TestMailSeat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo, self.sha = fixtures.make_repo(os.path.join(self.tmp.name, 'repo'))
        self.change = collect.collect(self.repo, self.sha['change'])
        self.plan = plan.build(self.change, 0, 'tour')
        rows = narrator.shot_rows(self.plan, [self.change])
        self.hunk = next(r for r in rows if r.key[0] == 'hunk')
        self.good = {'phrases': [{'shot': 's0', 'text': 'This change moves the arithmetic into a library.'},
                                 {'shot': self.hunk.id, 'text': 'The app now asks the library, so both agree.'},
                                 {'shot': rows[-1].id, 'text': 'That is the whole idea.'}]}
        token = os.path.join(self.tmp.name, 'token')
        with open(token, 'w') as f:
            f.write('s3cret\n')
        self.cache = os.path.join(self.tmp.name, 'cache')
        self.env = mock.patch.dict(os.environ, {
            'XDG_CACHE_HOME': self.cache, 'GIT_VIDEO_NARRATOR_MAIL_URL': 'http://mail.example:8080/',
            'GIT_VIDEO_NARRATOR_MAIL_TOKEN_FILE': token, 'GIT_VIDEO_NARRATOR_FROM': '@git-video-narrate',
            'GIT_VIDEO_NARRATOR_SEAT': '@narrator', 'GIT_VIDEO_NARRATOR_MAIL_ARGS': '--budget 2'})
        self.env.start()
        for var in ('GIT_VIDEO_NARRATOR_POLL_TIMEOUT', 'GIT_VIDEO_NARRATOR_MODEL', 'GIT_VIDEO_NARRATOR_BRANCH'):
            os.environ.pop(var, None)
        self.poll = mock.patch.object(narrator, 'MAIL_POLL', 0)
        self.poll.start()

    def tearDown(self):
        self.poll.stop()
        self.env.stop()
        self.tmp.cleanup()

    def run_with(self, fake, fallback=False, log=None):
        w = narrator.Narrator('mail-seat', self.change, [self.change], 'tour', False, 0, fallback,
                              io.StringIO() if log is None else log)
        with mock.patch.object(narrator, 'post', fake):
            return w, w(self.plan)

    def test_success(self):
        fake = FakeMail(self.good, polls=2)
        w, got = self.run_with(fake)
        self.assertEqual([p.text for p in got], [x['text'] for x in self.good['phrases']])
        self.assertEqual([p.key for p in got], [('title',), self.hunk.key, ('end',)])
        self.assertEqual(w.ran, 'mail-seat')
        self.assertEqual(fake.verbs(), ['send', 'export', 'export', 'export'])
        send = fake.requests[0]
        self.assertEqual((send['url'], send['token']), ('http://mail.example:8080/', 's3cret'))
        self.assertEqual(send['body']['tool'], 'mail')
        argv = send['body']['argv']
        self.assertEqual(argv[:3], ['send', '--from', '@git-video-narrate'])
        self.assertEqual(argv[argv.index('--to') + 1], '@narrator')
        self.assertEqual(argv[argv.index('--body') + 1], '-')   # the brief travels as stdin, never in argv
        self.assertEqual(argv[argv.index('--review-target') + 1], f'git-video-narrate:{self.change.target}')
        self.assertEqual(argv[-2:], ['--budget', '2'])
        self.assertEqual(set(send['body']), {'tool', 'argv', 'stdin_b64'})
        self.assertEqual(base64.b64decode(send['body']['stdin_b64']).decode(),
                         narrator.brief(self.change, self.plan, [self.change], 'mail-seat'))
        self.assertRegex(send['key'], r'^[A-Za-z0-9_-]{16,128}$')
        for r in fake.requests[1:]:
            self.assertIsNone(r['key'])   # read-only verbs carry no key
        for r in fake.requests[1:]:
            self.assertEqual(r['body'], {'tool': 'mail', 'argv': ['export', THREAD, '--json']})

    def test_branch_names_the_review_target(self):
        with mock.patch.dict(os.environ, {'GIT_VIDEO_NARRATOR_BRANCH': 'feature/x'}):
            fake = FakeMail(self.good)
            self.run_with(fake)
        argv = fake.requests[0]['body']['argv']
        self.assertEqual(argv[argv.index('--review-target') + 1], f'feature/x:{self.change.target}')

    def test_cached_by_seat(self):
        first = FakeMail(self.good)
        self.run_with(first)
        again = FakeMail(self.good)
        _, got = self.run_with(again)
        self.assertEqual(again.requests, [])
        self.assertEqual(len(got), 3)
        with mock.patch.dict(os.environ, {'GIT_VIDEO_NARRATOR_SEAT': '@other-narrator'}):
            other = FakeMail(self.good, polls=10 ** 6)
            os.environ['GIT_VIDEO_NARRATOR_POLL_TIMEOUT'] = '0'
            with self.assertRaises(narrator.NarratorError):
                self.run_with(other)
        self.assertEqual(other.verbs()[0], 'send')   # another seat is another key: asked afresh
        self.assertNotEqual(other.requests[0]['key'], first.requests[0]['key'])

    def test_brief(self):
        mine = narrator.brief(self.change, self.plan, [self.change], 'mail-seat')
        self.assertNotIn('outbox', mine)
        self.assertIn('Your reply to this thread is the narration, one JSON object', mine)
        self.assertIn(f'git diff {self.change.base}..{self.change.target}', mine)
        self.assertNotRegex(mine, r'\{(inspect|output|output_how|output_end)\}')

    def test_missing_env(self):
        for var in narrator.MAIL_ENV.values():
            with self.subTest(var=var), mock.patch.dict(os.environ, {var: ''}):
                fake = FakeMail(self.good)
                with self.assertRaisesRegex(narrator.NarratorError, f'^{var} is not set'):
                    self.run_with(fake)
                self.assertEqual(fake.requests, [])

    def test_http_error(self):
        fake = FakeMail(self.good, status=403)
        with self.assertRaisesRegex(narrator.NarratorError, r'mail API mail send: HTTP 403: this token may not'):
            self.run_with(fake)
        self.assertEqual(fake.verbs(), ['send'])   # a real answer: not retried

    def test_transient_retried(self):
        fake = FakeMail(self.good)
        real, answers = fake.__call__, [(503, {'error': 'restarting'})]
        wrapped = lambda *a, **k: answers.pop() if answers else real(*a, **k)
        w, got = self.run_with(wrapped)
        self.assertEqual(len(got), 3)

    def test_timeout(self):
        os.environ['GIT_VIDEO_NARRATOR_POLL_TIMEOUT'] = '0'
        fake = FakeMail(self.good, polls=10 ** 6)
        with self.assertRaisesRegex(narrator.NarratorError, f'no reply from @narrator in thread {THREAD} after 0s'):
            self.run_with(fake)
        self.assertEqual(set(fake.verbs()), {'send', 'export'})

    def test_reply_without_json(self):
        with self.assertRaisesRegex(narrator.NarratorError, r'the reply from @narrator .* holds no JSON'):
            self.run_with(FakeMail(self.good, mode='prose'))

    def test_reply_that_does_not_pass(self):
        with self.assertRaisesRegex(narrator.NarratorError, r"its reply does not pass: phrase 1: no shot 's999'"):
            self.run_with(FakeMail(self.good, mode='unknown'))

    def test_fallback(self):
        log = io.StringIO()
        w, got = self.run_with(FakeMail(self.good, mode='prose'), fallback=True, log=log)
        self.assertEqual(w.ran, 'heuristic')
        self.assertIn('mail-seat narrator failed', log.getvalue())

    def test_not_chosen_by_auto(self):
        self.assertNotEqual(narrator.resolve('auto', which=lambda _: None, environ={})[0], 'mail-seat')
        self.assertEqual(narrator.resolve('mail-seat'), ('mail-seat', False))


class TestPost(unittest.TestCase):
    """The real transport, against a server on the loopback."""

    def setUp(self):
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append((self.path, dict(self.headers), body))
                if body['argv'] == ['redirect']:
                    self.send_response(307)
                    self.send_header('Location', 'http://elsewhere.example/v1/exec')
                    self.end_headers()
                    return
                ok = self.headers['Authorization'] == 'Bearer tok'
                out = json.dumps({'rc': 0, 'stdout_b64': b64('hi')} if ok else {'error': 'bad token'}).encode()
                self.send_response(200 if ok else 401)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_post(self):
        self.assertEqual(narrator.post(self.url, 'tok', {'tool': 'mail', 'argv': ['send']}, 'k' * 20),
                         (200, {'rc': 0, 'stdout_b64': b64('hi')}))
        path, headers, body = self.seen[0]
        self.assertEqual((path, headers['Idempotency-Key'], body), ('/v1/exec', 'k' * 20,
                                                                    {'tool': 'mail', 'argv': ['send']}))
        self.assertEqual(narrator.post(self.url, 'wrong', {'tool': 'mail', 'argv': ['show']}),
                         (401, {'error': 'bad token'}))
        self.assertNotIn('Idempotency-Key', self.seen[1][1])
        self.assertEqual(narrator.post(self.url, 'tok', {'tool': 'mail', 'argv': ['redirect']})[0], 307)
        self.assertEqual(len(self.seen), 3)   # the token was not sent on


if __name__ == '__main__':
    unittest.main()

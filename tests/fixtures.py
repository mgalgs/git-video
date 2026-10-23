"""Throwaway git repositories for the tests."""
import os
import subprocess

LIB_1 = '''def frob(x):
    return x + 1


class Thing:
    size = 3
'''

LIB_2 = LIB_1.replace('x + 1', 'x * 2')

APP = '''from src.lib import frob, Thing


def main():
    return frob(2) + Thing.size
'''

TEST_APP = '''from src.app import main


def test_main():
    assert main() == 7
'''


def git(repo, *args):
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_AUTHOR_DATE': '2026-01-02T03:04:05+00:00', 'GIT_COMMITTER_DATE': '2026-01-02T03:04:05+00:00'}
    return subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def write(repo, path, data):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'wb' if isinstance(data, bytes) else 'w') as fh:
        fh.write(data)


def init(repo):
    os.makedirs(repo, exist_ok=True)
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'config', 'user.name', 'Test Person')
    git(repo, 'config', 'user.email', 'test@example.com')


def commit(repo, message):
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', message)
    return git(repo, 'rev-parse', 'HEAD')


def make_repo(tmp):
    """A repo of three commits; returns {'root', 'change', 'delete'} shas.

    root:   src/lib.py defines frob() and Thing.
    change: src/app.py calls frob() in a new main(); src/lib.py changes line 2;
            tests/test_app.py calls main(); bin.dat is binary.
    delete: bin.dat is deleted; two co-authors (one named twice).
    """
    repo = os.path.join(tmp, 'repo')
    init(repo)
    write(repo, 'src/lib.py', LIB_1)
    root = commit(repo, 'Add the library')
    write(repo, 'src/lib.py', LIB_2)
    write(repo, 'src/app.py', APP)
    write(repo, 'tests/test_app.py', TEST_APP)
    write(repo, 'bin.dat', b'\x00\x01\x02binary\x00')
    change = commit(repo, 'Use the library from an app\n\nThe app doubles instead of incrementing.\n\n'
                          'Signed-off-by: Test Person <test@example.com>\nReviewed-on: https://example.com/r/1')
    os.remove(os.path.join(repo, 'bin.dat'))
    delete = commit(repo, 'Drop the binary\n\nCo-Authored-By: Ada Lovelace <ada@example.com>\n'
                          'Co-Authored-By: Grace Hopper <grace@example.com>\n'
                          'co-authored-by: Ada Lovelace <ada@example.org>')
    return repo, {'root': root, 'change': change, 'delete': delete}


RANGE_LIB_A = '''def frob(x):
    return x + 1


SPACER = [
    1,
    2,
    3,
]


def other(x):
    return x - 1
'''


def make_range_repo(tmp):
    """A, B, C on one line; returns (repo, {'a', 'b', 'c'}).

    A: lib.py defines frob() and other().
    B: frob() changes ("frob() now doubles.").
    C: other() changes ("other() now triples.").
    """
    repo = os.path.join(tmp, 'range')
    init(repo)
    write(repo, 'lib.py', RANGE_LIB_A)
    a = commit(repo, 'Add the library')
    write(repo, 'lib.py', RANGE_LIB_A.replace('x + 1', 'x * 2'))
    b = commit(repo, 'Double in frob\n\nfrob() now doubles its input.\n\nSigned-off-by: Test Person <t@example.com>')
    write(repo, 'lib.py', RANGE_LIB_A.replace('x + 1', 'x * 2').replace('x - 1', 'x * 3'))
    c = commit(repo, 'Triple in other\n\nThe helper other() now triples.')
    return repo, {'a': a, 'b': b, 'c': c}


def make_callout_repo(tmp):
    """lib.py defines frob() and other(); HEAD changes both, with a body of
    three sentences: one naming frob(), one naming nothing, one naming other().
    Returns (repo, {'root', 'head'})."""
    repo = os.path.join(tmp, 'callout')
    init(repo)
    write(repo, 'lib.py', RANGE_LIB_A)
    root = commit(repo, 'Add the library')
    write(repo, 'lib.py', RANGE_LIB_A.replace('x + 1', 'x * 2').replace('x - 1', 'x * 3'))
    head = commit(repo, 'Change both\n\nfrob() now doubles its input. Nothing else changed here.\n'
                        'The helper other() returns three times its input.\n\nSigned-off-by: T <t@example.com>')
    return repo, {'root': root, 'head': head}


GRAPH_LIB = '''from util import helper


def frob(x):
    return x * 2


SPACER = [
    1,
    2,
    3,
]


def main():
    return 1
'''


def make_graph_repo(tmp):
    """util.py defines helper(); lib.py defines frob() and main(). HEAD makes
    frob() call helper() and main() call frob()."""
    repo = os.path.join(tmp, 'graph')
    init(repo)
    write(repo, 'util.py', 'def helper(x):\n    return x + 1\n')
    write(repo, 'lib.py', GRAPH_LIB)
    commit(repo, 'Start')
    write(repo, 'lib.py', GRAPH_LIB.replace('return x * 2', 'return helper(x) * 2').replace('return 1', 'return frob(3)'))
    commit(repo, 'Wire them up')
    return repo


def make_call_first_repo(tmp):
    """GRAPH_LIB again, but HEAD changes frob()'s body without a call and makes
    main() call frob() and helper(): the call edge main -> frob is found at
    main's hunk, before the dive into helper() that follows it."""
    repo = os.path.join(tmp, 'callfirst')
    init(repo)
    write(repo, 'util.py', 'def helper(x):\n    return x + 1\n')
    write(repo, 'lib.py', GRAPH_LIB)
    commit(repo, 'Start')
    write(repo, 'lib.py', GRAPH_LIB.replace('return x * 2', 'return x * 3').replace('return 1', 'return helper(frob(3))'))
    commit(repo, 'Call both')
    return repo


def make_many_defs_repo(tmp, n=15):
    """lib.py with n functions far apart; HEAD changes every one's body."""
    repo = os.path.join(tmp, 'many')
    init(repo)
    body = ''.join(f'def f{i}(x):\n    return x + {i}\n' + '\n' * 8 for i in range(n))
    write(repo, 'lib.py', body)
    commit(repo, 'Start')
    write(repo, 'lib.py', body.replace('return x + ', 'return x - '))
    commit(repo, 'Flip them all')
    return repo


FAR_LINES = 300


def make_far_repo(tmp):
    """A repo whose HEAD adds a line after line 5 and after line 250 of a
    300-line file: two hunks far more than FAR_ROWS apart."""
    repo = os.path.join(tmp, 'far')
    init(repo)
    rows = [f'row_{i} = {i}\n' for i in range(FAR_LINES)]
    write(repo, 'far.py', ''.join(rows))
    commit(repo, 'Add far.py')
    rows.insert(250, 'late = True\n')
    rows.insert(5, 'early = True\n')
    write(repo, 'far.py', ''.join(rows))
    commit(repo, 'Add two flags')
    return repo


MORPH_LINES = 60


def make_morph_repo(tmp):
    """A repo whose HEAD, in one 60-line file, replaces lines 5-34 (one hunk
    taller than a screen) and lines 55-57 (a short replace near the end)."""
    repo = os.path.join(tmp, 'morph')
    init(repo)
    rows = [f'value_{i} = {i}\n' for i in range(MORPH_LINES)]
    write(repo, 'm.py', ''.join(rows))
    commit(repo, 'Add m.py')
    for i in list(range(4, 34)) + [54, 55, 56]:
        rows[i] = f'value_{i} = {i} * 10\n'
    write(repo, 'm.py', ''.join(rows))
    commit(repo, 'Scale the values')
    return repo


BIG_LINES = 560
BIG_EVERY = 8


def make_big_repo(tmp):
    """A repo whose HEAD touches 70 separate lines of one file (70 hunks) and
    adds a 50-line file (one hunk taller than a screen)."""
    repo = os.path.join(tmp, 'big')
    init(repo)
    write(repo, 'big.py', ''.join(f'v{i} = {i}\n' for i in range(BIG_LINES)))
    commit(repo, 'Add big.py')
    write(repo, 'big.py', ''.join(f'v{i} = {i * 2 if i % BIG_EVERY == 4 else i}\n' for i in range(BIG_LINES)))
    write(repo, 'long.txt', ''.join(f'line {i}\n' for i in range(50)))
    commit(repo, 'Touch many lines')
    return repo


def fake_pack(root, sounds):
    """A sound-effects pack videobuilder can read: `sounds` is [(id, role)], each
    a 0.1 s silent WAV. Returns the directory, for $VIDEOBUILDER_SFX_DIR."""
    import json
    import wave
    os.makedirs(root, exist_ok=True)
    entries = []
    for i, (sid, role) in enumerate(sounds):
        name = f's{i}.wav'
        with wave.open(os.path.join(root, name), 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(b'\0\0' * 4800)
        entries.append({'id': sid, 'category': role, 'role': role, 'family': 'test', 'about': '',
                        'variants': [{'variant': None, 'file': name, 'dur_s': 0.1, 'onset_s': 0.0,
                                      'peak_s': 0.02, 'end_s': 0.1, 'peak_db': -1.0}]})
    with open(os.path.join(root, 'index.json'), 'w') as fh:
        json.dump({'schema': 1, 'pack': 'test', 'sounds': entries}, fh)
    return root


def make_edge_repo(tmp):
    """A repo whose HEAD adds:
    lib.py   two functions;
    app.py   calls near_call() on the last row the film keeps in view (line
             plan.XREF_ROWS) and far_call() two rows below it;
    reader.go  a Go method `func (r *Reader) Read(...)`, and use.go calling it.
    """
    from git_video import plan
    repo = os.path.join(tmp, 'edge')
    init(repo)
    write(repo, 'README', 'edge\n')
    commit(repo, 'Start')
    write(repo, 'lib.py', 'def near_call():\n    return 1\n\n\ndef far_call():\n    return 2\n')
    rows = [f'x{i} = {i}\n' for i in range(40)]
    rows[plan.XREF_ROWS - 1] = 'y = near_call()\n'
    rows[plan.XREF_ROWS + 1] = 'z = far_call()\n'
    write(repo, 'app.py', ''.join(rows))
    write(repo, 'reader.go', 'package r\n\ntype Reader struct{}\n\nfunc (r *Reader) Read(p []byte) int {\n'
                             '\treturn 0\n}\n')
    write(repo, 'use.go', 'package r\n\nfunc use(r *Reader) int {\n\treturn r.Read(nil)\n}\n')
    commit(repo, 'Add the edge cases')
    return repo


TREE_DIRS = 20
TREE_FILES = 400
TREE_CHANGED = ('d00/f000.py', 'd00/f020.py', 'd07/f007.py', 'd13/f013.py', 'd13/f033.py')


def make_tree_repo(tmp):
    """A repo of TREE_FILES files spread over TREE_DIRS top-level directories,
    sizes from 0 to about 40 kB; HEAD changes TREE_CHANGED (in 3 directories)."""
    repo = os.path.join(tmp, 'tree')
    init(repo)
    for i in range(TREE_FILES):
        write(repo, f'd{i % TREE_DIRS:02d}/f{i:03d}.py', 'x' * ((i * 7919) % 40000))
    commit(repo, 'Add a lot of files')
    for path in TREE_CHANGED:
        with open(os.path.join(repo, path), 'a') as fh:
            fh.write('\nchanged = True\n')
    commit(repo, 'Touch five files')
    return repo


def _core(edited=False):
    """core.py for make_develop_repo: 80 lines; `edited` gives 15 lines a word
    edit, drops two lines and inserts 12."""
    rows = []
    for i in range(20):
        rows += [f'def step_{i}(value):\n', f'    return value + {i}\n', '\n', '\n']
    if edited:
        for i in range(15):
            rows[4 * i + 1] = f'    return value * {i}\n'
        del rows[4 * 16 + 2:4 * 16 + 4]   # two blank lines after step_16
        rows[4 * 18:4 * 18] = [f'GROWN_{k} = {k}  # inserted\n' for k in range(12)]
    return ''.join(rows)


def make_develop_repo(tmp):
    """Every kind of file develop mode meets. Base: core.py (80 lines),
    old.py, a.py, tabs.py (tab-indented). HEAD: core.py edited (15 word
    edits, two lines dropped, 12 inserted); old.py deleted; a.py renamed to
    b.py with one edit; tabs.py one edit; new50.py and new100.py added (50 and
    100 lines); bin.dat added (binary)."""
    repo = os.path.join(tmp, 'develop')
    init(repo)
    write(repo, 'core.py', _core())
    write(repo, 'old.py', 'def gone():\n    return 0\n')
    a = ''.join(f'line_{i} = {i}\n' for i in range(30))
    write(repo, 'a.py', a)
    write(repo, 'tabs.py', 'def tabbed():\n\tif True:\n\t\treturn 1\n\treturn 2\n')
    commit(repo, 'Start')
    write(repo, 'core.py', _core(edited=True))
    os.remove(os.path.join(repo, 'old.py'))
    os.remove(os.path.join(repo, 'a.py'))
    write(repo, 'b.py', a.replace('line_7 = 7', 'line_7 = 70'))
    write(repo, 'tabs.py', 'def tabbed():\n\tif False:\n\t\treturn 1\n\treturn 2\n')
    write(repo, 'new50.py', ''.join(f'fifty_{i} = {i}\n' for i in range(50)))
    write(repo, 'new100.py', ''.join(f'hundred_{i} = {i}\n' for i in range(100)))
    write(repo, 'bin.dat', b'\x00\x01binary\x00')
    commit(repo, 'Rework everything')
    return repo


def make_budget_repo(tmp):
    """work.py with 25 functions; HEAD adds a 20-line function after each: 500
    added lines in 25 hunks, no run of 30."""
    repo = os.path.join(tmp, 'budget')
    init(repo)
    funcs = [f'def base_{i}(x):\n    return x + {i}\n\n\n' for i in range(25)]
    write(repo, 'work.py', ''.join(funcs))
    commit(repo, 'Start')
    out = []
    for i, f in enumerate(funcs):
        body = [f'def grown_{i}(x):\n'] + [f'    x = x * {k} + {i}  # step {k}\n' for k in range(17)] + \
               ['    return x\n', '\n']
        out += [f, *body]
    write(repo, 'work.py', ''.join(out))
    commit(repo, 'Grow every function')
    return repo


def make_order_repo(tmp):
    """HEAD adds util.py defining helper(), makes app.py call it and adds
    tests/test_app.py: diff order app.py, tests/test_app.py, util.py."""
    repo = os.path.join(tmp, 'order')
    init(repo)
    write(repo, 'app.py', 'def main():\n    return 1\n')
    commit(repo, 'Start')
    write(repo, 'util.py', 'def helper(x):\n    return x + 1\n')
    write(repo, 'app.py', 'from util import helper\n\n\ndef main():\n    return helper(1)\n')
    write(repo, 'tests/test_app.py', 'from app import main\n\n\ndef test_main():\n    assert main() == 2\n')
    commit(repo, 'Add a helper')
    return repo


def make_cycle_repo(tmp):
    """HEAD adds a.py and b.py, each defining a function the other calls."""
    repo = os.path.join(tmp, 'cycle')
    init(repo)
    write(repo, 'README', 'cycle\n')
    commit(repo, 'Start')
    write(repo, 'b.py', 'from a import ping\n\n\ndef pong(n):\n    return ping(n - 1)\n')
    write(repo, 'a.py', 'from b import pong\n\n\ndef ping(n):\n    return pong(n) if n else 0\n')
    commit(repo, 'Ping and pong')
    return repo


REWRITE_LIB = '''def main():
    return 1
'''


def make_rewrite_repo(tmp):
    """A, B, C on one line; returns (repo, {'a', 'b', 'c'}).

    A: lib.py defines main().
    B: lib.py gains helper() (6 lines).
    C: helper()'s body is rewritten (3 of B's lines) and main() calls it.
    """
    repo = os.path.join(tmp, 'rewrite')
    init(repo)
    write(repo, 'lib.py', REWRITE_LIB)
    a = commit(repo, 'Start the library')
    helper = ('\n\ndef helper(x):\n    total = 0\n    for k in range(x):\n        total += k\n'
              '    return total\n')
    write(repo, 'lib.py', REWRITE_LIB + helper)
    b = commit(repo, 'Add helper\n\nhelper() sums the numbers below x.')
    helper2 = ('\n\ndef helper(x):\n    total = x * (x - 1) // 2\n    return total\n')
    write(repo, 'lib.py', REWRITE_LIB.replace('return 1', 'return helper(4)') + helper2)
    c = commit(repo, 'Sum in closed form\n\nhelper() now uses the closed form; main() calls it.')
    return repo, {'a': a, 'b': b, 'c': c}


def make_wrap_repo(tmp):
    """A repo whose HEAD edits a file where every third line is long enough to
    wrap a few times: kills and long typed lines near the window's foot, and
    a far hunk to jump to."""
    repo = os.path.join(tmp, 'wrap')
    init(repo)
    rows = [f'w_{i} = {i}' + (', ' + ', '.join(f'item_{i}_{j}' for j in range(30)) if i % 3 == 0 else '') + '\n'
            for i in range(300)]
    write(repo, 'wrap.py', ''.join(rows))
    commit(repo, 'Add wrap.py')
    for at in (240, 60, 30):
        del rows[at:at + 4]
        rows[at:at] = [f'n_{at}_{j} = [' + ', '.join(f'"value {j} {k}"' for k in range(12)) + ']\n'
                       for j in range(3)]
    write(repo, 'wrap.py', ''.join(rows))
    commit(repo, 'Rewrite some long lines')
    return repo


VOICE_LIB = RANGE_LIB_A + '''

TABLE = [
    1,
    2,
    3,
    4,
]
'''

VOICE_BODY = '''frob() now doubles, see https://example.com/p and deadbeef1234 for why.

```
other(2) == 6
```

The rework of other() keeps every caller working, because it still returns a number of the same kind, and the
callers never needed to know how it got there in the first place.

Signed-off-by: T <t@example.com>'''


def make_voice_repo(tmp):
    """lib.py defines frob() and other(); HEAD changes both and adds
    draw_card() at the end, with a body that has a URL and a hash in a
    sentence naming frob(), a fenced code block and a 35-word sentence naming
    other(), and a trailer; nothing in it names draw_card. Returns (repo,
    {'root', 'head'})."""
    repo = os.path.join(tmp, 'voice')
    init(repo)
    write(repo, 'lib.py', VOICE_LIB)
    root = commit(repo, 'Add the library')
    write(repo, 'lib.py', VOICE_LIB.replace('x + 1', 'x * 2').replace('x - 1', 'x * 3')
          + '\n\ndef draw_card(cv):\n    width = 3\n    return width\n')
    head = commit(repo, 'Add draw_card and rework frob\n\n' + VOICE_BODY)
    return repo, {'root': root, 'head': head}


class FakeEngine:
    """A stand-in for videobuilder's Kokoro engine, for `vb.speech.say`: the
    phonemes are the text, and each phrase comes out as a tone about as long
    as the real voice would take to say it. `calls` counts syntheses; with
    `fail_after`, every synthesis after that many raises (a killed run)."""
    model_id = 'fake'

    def __init__(self, fail_after=None):
        self.calls = 0
        self.fail_after = fail_after

    def voices(self):
        return ['af_heart', 'bf_isabella']

    def phonemize(self, text, lang):
        return text

    def synth(self, phonemes, voice, speed, lang):
        import array
        import math
        from videobuilder import speech
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise RuntimeError('killed')
        self.calls += 1
        n = int(speech.RATE * max(0.4, 0.045 * len(phonemes)) / speed)
        return array.array('h', (int(8000 * math.sin(2 * math.pi * 440 * k / speech.RATE)) for k in range(n)))

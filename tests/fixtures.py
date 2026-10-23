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
    delete: bin.dat is deleted.
    """
    repo = os.path.join(tmp, 'repo')
    init(repo)
    write(repo, 'src/lib.py', LIB_1)
    root = commit(repo, 'Add the library')
    write(repo, 'src/lib.py', LIB_2)
    write(repo, 'src/app.py', APP)
    write(repo, 'tests/test_app.py', TEST_APP)
    write(repo, 'bin.dat', b'\x00\x01\x02binary\x00')
    change = commit(repo, 'Use the library from an app\n\nThe app doubles instead of incrementing.')
    os.remove(os.path.join(repo, 'bin.dat'))
    delete = commit(repo, 'Drop the binary')
    return repo, {'root': root, 'change': change, 'delete': delete}


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

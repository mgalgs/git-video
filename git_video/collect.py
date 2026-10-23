"""Git facts about a change set: which commits, which files, which hunks, and
the new text of each file. Nothing here knows about shots or drawing."""
import os
import re
import subprocess
from dataclasses import dataclass, field

EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'


class GitError(RuntimeError):
    pass


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    added: list          # [[a, b], ...] inclusive ranges of new line numbers
    removed_at: list     # new line numbers after which removed lines sat (0: before line 1)


@dataclass
class FileChange:
    path: str
    old_path: str | None
    status: str          # A, M, D, R, T (C counts as A)
    added: int
    deleted: int
    binary: bool
    hunks: list
    text: str | None     # the file at the target, None when deleted, binary or not a blob
    lines: int


@dataclass
class Commit:
    sha: str
    short: str
    subject: str
    body: str
    author: str
    date: str            # ISO 8601, the author date


@dataclass
class Change:
    repo: str
    base: str
    target: str
    label: str
    is_range: bool
    commits: list        # oldest first; one entry for a single rev
    files: list
    added: int
    deleted: int
    _texts: dict = field(default_factory=dict, repr=False)

    def text(self, path):
        """The text of any file at the target (a definition's file need not be in
        the change); None when it is not a text blob there."""
        for f in self.files:
            if f.path == path:
                return f.text
        if path not in self._texts:
            self._texts[path] = blob_text(self.repo, self.target, path)
        return self._texts[path]


def git(repo, *args, ok=(0,)):
    r = subprocess.run(['git', '-C', repo, *args], capture_output=True)
    if r.returncode not in ok:
        msg = r.stderr.decode('utf-8', 'replace').strip() or f'git {args[0]} failed'
        raise GitError(msg)
    return r.stdout.decode('utf-8', 'replace')


def toplevel(repo):
    if not os.path.isdir(repo):
        raise GitError(f'cannot use {repo}: not a directory')
    try:
        return git(repo, 'rev-parse', '--show-toplevel').strip() or repo
    except GitError:
        git(repo, 'rev-parse', '--git-dir')   # not a repository at all: raises with git's message
        return repo                           # a bare repository


def _commit(repo, rev):
    return git(repo, 'rev-parse', '--verify', '--quiet', f'{rev}^{{commit}}').strip()


def _verify(repo, rev):
    try:
        sha = _commit(repo, rev)
    except GitError:
        sha = ''
    if not sha:
        raise GitError(f'unknown revision {rev!r}')
    return sha


def short(repo, sha):
    return git(repo, 'rev-parse', '--short', sha).strip()


def resolve(repo, spec):
    """(base, target, label, is_range) for a rev `X` (X^..X; the empty tree
    for a root commit) or a range `A..B`."""
    if '...' in spec:
        raise GitError(f'{spec}: symmetric ranges (A...B) are not supported; use A..B')
    if '..' in spec:
        a, b = spec.split('..', 1)
        base, target = _verify(repo, a or 'HEAD'), _verify(repo, b or 'HEAD')
        return base, target, f'{short(repo, base)}-{short(repo, target)}', True
    target = _verify(repo, spec)
    parents = git(repo, 'rev-list', '--parents', '-n1', target).split()[1:]
    base = parents[0] if parents else EMPTY_TREE
    return base, target, short(repo, target), False


def commit_meta(repo, base, target, is_range):
    """The commits of the change, oldest first. A single rev's carries its body;
    a range's commits carry their subjects only."""
    if not is_range:
        out = git(repo, 'log', '-1', '--format=%H%n%h%n%an%n%aI%n%s%n%b', target)
        sha, sh, author, date, subject, *body = out.split('\n')
        return [Commit(sha, sh, subject, '\n'.join(body).strip(), author, date)]
    out = git(repo, 'log', '--reverse', '-z', '--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s', f'{base}..{target}')
    commits = []
    for rec in filter(None, out.split('\0')):
        sha, sh, author, date, subject = rec.strip('\n').split('\x1f')
        commits.append(Commit(sha, sh, subject, '', author, date))
    if not commits:   # B is not ahead of A: still describe B
        out = git(repo, 'log', '-1', '--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s', target)
        sha, sh, author, date, subject = out.strip('\n').split('\x1f')
        commits.append(Commit(sha, sh, subject, '', author, date))
    return commits


_DIFF = ('-M', '--no-color', '--no-ext-diff', '--no-textconv')


def _name_status(repo, base, target):
    """[(status, old_path, path)] in git's diff order."""
    parts = git(repo, 'diff', '--name-status', '-z', *_DIFF, base, target).split('\0')
    out, i = [], 0
    while i < len(parts) and parts[i]:
        st = parts[i][0]
        if st in 'RC':
            old, new = parts[i + 1], parts[i + 2]
            out.append(('R' if st == 'R' else 'A', old if st == 'R' else None, new))
            i += 3
        else:
            out.append((st, None, parts[i + 1]))
            i += 2
    return out


def _numstat(repo, base, target):
    """{path: (added, deleted, binary)}, keyed by the new path."""
    parts = git(repo, 'diff', '--numstat', '-z', *_DIFF, base, target).split('\0')
    out, i = {}, 0
    while i < len(parts) and parts[i]:
        a, d, path = parts[i].split('\t', 2)
        if path == '':   # a rename: old and new path follow
            path = parts[i + 2]
            i += 3
        else:
            i += 1
        binary = a == '-'
        out[path] = (0 if binary else int(a), 0 if binary else int(d), binary)
    return out


_HEADER = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def parse_hunks(patch):
    """The hunks of one file's patch text."""
    hunks, h, n, prev = [], None, 0, ''
    for line in patch.split('\n'):
        m = _HEADER.match(line)
        if m:
            a, b, c, d = (int(g) if g is not None else 1 for g in m.groups())
            h = Hunk(a, b, c, d, [], [])
            hunks.append(h)
            n = c - 1 if d > 0 else c   # the last new line before the hunk's first line
            prev = ''
            continue
        if h is None or not line or line[0] not in '+- ':
            continue
        if line[0] == '+':
            n += 1
            if h.added and h.added[-1][1] == n - 1:
                h.added[-1][1] = n
            else:
                h.added.append([n, n])
        elif line[0] == '-':
            if prev != '-':
                h.removed_at.append(n)
        else:
            n += 1
        prev = line[0]
    return hunks


def _patches(repo, base, target):
    """Patch text per file section, in diff order."""
    out = git(repo, 'diff', '-U3', *_DIFF, base, target)
    sections = re.split(r'^(?=diff --git )', out, flags=re.M)
    return [s for s in sections if s.startswith('diff --git ')]


def blob_text(repo, rev, path):
    if git(repo, 'cat-file', '-t', f'{rev}:{path}', ok=(0, 128)).strip() != 'blob':
        return None
    r = subprocess.run(['git', '-C', repo, 'cat-file', 'blob', f'{rev}:{path}'], capture_output=True)
    if r.returncode or b'\0' in r.stdout[:8000]:
        return None
    return r.stdout.decode('utf-8', 'replace')


_TEST = re.compile(r'(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]*$|_test\.[^/]*$|\.(test|spec)\.[^/]*$')


def is_test(path):
    return bool(_TEST.search(path))


def collect(repo, spec):
    """Everything the planner needs to know about `spec` in `repo`."""
    repo = toplevel(repo)
    base, target, label, is_range = resolve(repo, spec)
    commits = commit_meta(repo, base, target, is_range)
    entries = _name_status(repo, base, target)
    stats = _numstat(repo, base, target)
    patches = _patches(repo, base, target)
    if len(patches) != len(entries):   # can't pair them in order: ask per file
        patches = [git(repo, 'diff', '-U3', *_DIFF, base, target, '--', *filter(None, (old, path)))
                   for _, old, path in entries]
    files = []
    for (status, old, path), patch in zip(entries, patches):
        a, d, binary = stats.get(path, (0, 0, False))
        text = None if status == 'D' or binary else blob_text(repo, target, path)
        if text is None and status != 'D':
            binary = True
        lines = text.count('\n') + (0 if text.endswith('\n') or not text else 1) if text else 0
        files.append(FileChange(path, old, status, a, d, binary, parse_hunks(patch), text, lines))
    files = [f for f in files if not is_test(f.path)] + [f for f in files if is_test(f.path)]
    return Change(repo, base, target, label, is_range, commits, files,
                  sum(f.added for f in files), sum(f.deleted for f in files))

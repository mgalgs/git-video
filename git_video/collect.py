"""Git facts about a change set: which commits, which files, which hunks, and
the new text of each file. Nothing here knows about shots or drawing."""
import os
import re
import subprocess
from dataclasses import dataclass, field

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
    # One [at, [old line texts], new_count] per run of removed lines: `at` as in
    # removed_at, new_count the added lines right after the run (0: a pure deletion).
    segments: list = field(default_factory=list)
    commit: str | None = None   # the sha that owns most of the added lines (a range); the target (one rev)


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
    coauthors: list = field(default_factory=list)   # names from Co-Authored-By: trailers, in order


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
    tree: list = field(default_factory=list)   # [(path, size)] of every blob at the target
    next: 'Commit | None' = None               # the commit after the target on HEAD's line, if any
    merged: int = 0      # --series under --duration: this many small patches played as one beat (a range)
    _texts: dict = field(default_factory=dict, repr=False)
    _old_texts: dict = field(default_factory=dict, repr=False)

    def text(self, path):
        """The text of any file at the target (a definition's file need not be in
        the change); None when it is not a text blob there."""
        for f in self.files:
            if f.path == path:
                return f.text
        if path not in self._texts:
            self._texts[path] = blob_text(self.repo, self.target, path)
        return self._texts[path]

    def old_text(self, path):
        """The text a changed file had at the base (by its old path when it was
        renamed): '' for a file the change adds, None when it was not a text
        blob there (binary)."""
        if path not in self._old_texts:
            f = next((f for f in self.files if f.path == path), None)
            if f is not None and f.binary:
                text = None
            elif f is not None and f.status == 'A':
                text = ''
            else:
                text = blob_text(self.repo, self.base, f.old_path or path if f is not None else path)
            self._old_texts[path] = text
        return self._old_texts[path]


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


def empty_tree(repo):
    """The empty tree's id in this repository's object format (SHA-1 or SHA-256)."""
    return git(repo, 'hash-object', '-t', 'tree', os.devnull).strip()


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
    base = parents[0] if parents else empty_tree(repo)
    return base, target, short(repo, target), False


_TRAILERS = '%(trailers:key=Co-Authored-By,valueonly,separator=%x1e)'
# Every field \x1f-separated: the trailer block and the body are multi-line.
_FORMAT = f'%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1f{_TRAILERS}%x1f%(trailers)%x1f%b'
_TRAILER_LINE = re.compile(r'^[A-Za-z0-9][\w-]*:\s')
_EMAIL = re.compile(r'^(.*?)\s*<[^>]*>\s*$')


def coauthor_names(values):
    """Names from Co-Authored-By: values (`Name <email>`), email stripped, order
    kept, each name once."""
    out = []
    for v in values:
        m = _EMAIL.match(v.strip())
        name = (m.group(1) if m else v).strip()
        if name and name not in out:
            out.append(name)
    return out


def strip_trailers(body, trailers):
    """`body` without its trailer block (Co-Authored-By:, Signed-off-by:, any
    `Token: value` lines at the end), which git reports as `trailers`."""
    body = body.strip()
    block = trailers.strip()
    if not block:
        return body
    if body.endswith(block):
        return body[:-len(block)].strip()
    head, _, last = body.rpartition('\n\n')   # git reformatted the block: drop a last paragraph of trailers
    if all(_TRAILER_LINE.match(ln) or ln[:1].isspace() for ln in last.split('\n')):
        return head.strip()
    return body


def _parse(rec):
    sha, sh, author, date, subject, coauthors, trailers, body = rec.strip('\n').split('\x1f', 7)
    return Commit(sha, sh, subject, strip_trailers(body, trailers), author, date,
                  coauthor_names(coauthors.split('\x1e')))


def _one(repo, rev):
    """The Commit of one rev, with its body (trailers stripped)."""
    return _parse(git(repo, 'log', '-1', f'--format={_FORMAT}', rev))


def commit_meta(repo, base, target, is_range):
    """The commits of the change, oldest first, each with its body (trailers
    stripped)."""
    if not is_range:
        return [_one(repo, target)]
    out = git(repo, 'log', '--reverse', '-z', f'--format={_FORMAT}', f'{base}..{target}')
    commits = [_parse(rec) for rec in filter(None, out.split('\0'))]
    if not commits:   # B is not ahead of A: still describe B
        commits.append(_one(repo, target))
    return commits


def next_commit(repo, target):
    """The commit right after `target` on the way to HEAD (its child on the
    current branch), or None when `target` is HEAD or not an ancestor of it."""
    out = git(repo, 'rev-list', '--topo-order', '--reverse', '--ancestry-path', f'{target}..HEAD', ok=(0, 128))
    first = out.split('\n', 1)[0].strip()
    return _one(repo, first) if first else None


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
    hunks, h, n, prev, seg = [], None, 0, '', None
    for line in patch.split('\n'):
        m = _HEADER.match(line)
        if m:
            a, b, c, d = (int(g) if g is not None else 1 for g in m.groups())
            h = Hunk(a, b, c, d, [], [])
            hunks.append(h)
            n = c - 1 if d > 0 else c   # the last new line before the hunk's first line
            prev, seg = '', None
            continue
        if h is None or not line or line[0] not in '+- ':
            continue
        if line[0] == '+':
            n += 1
            if h.added and h.added[-1][1] == n - 1:
                h.added[-1][1] = n
            else:
                h.added.append([n, n])
            if seg is not None:
                seg[2] += 1
        elif line[0] == '-':
            if prev != '-':
                h.removed_at.append(n)
                seg = [n, [], 0]
                h.segments.append(seg)
            seg[1].append(line[1:])
        else:
            n += 1
            seg = None
        prev = line[0]
    return hunks


def _patches(repo, base, target):
    """Patch text per file section, in diff order."""
    out = git(repo, 'diff', '-U3', *_DIFF, base, target)
    sections = re.split(r'^(?=diff --git )', out, flags=re.M)
    return [s for s in sections if s.startswith('diff --git ')]


_BLAME_HEADER = re.compile(r'^([0-9a-f]{40}|[0-9a-f]{64}) \d+ (\d+)')


def blame_owners(porcelain):
    """{final line: sha} from `git blame --porcelain`, boundary commits (older
    than the range) left out."""
    owners, boundary, sha = {}, set(), None
    for line in porcelain.split('\n'):
        m = _BLAME_HEADER.match(line)
        if m:
            sha = m.group(1)
            owners[int(m.group(2))] = sha
        elif line == 'boundary' and sha:
            boundary.add(sha)
    return {ln: s for ln, s in owners.items() if s not in boundary}


def attribute(repo, base, target, files, commits):
    """Set each hunk's `commit` to the range commit that last touched most of
    its added lines (ties: the later commit), by one `git blame` per file
    restricted to those lines. A hunk with no added line, or only lines older
    than the range, gets None; a failing blame leaves its file unattributed."""
    order = {c.sha: k for k, c in enumerate(commits)}
    for f in files:
        ranges = [r for h in f.hunks for r in h.added]
        if f.text is None or not ranges:
            continue
        args = [x for a, b in ranges for x in ('-L', f'{a},{b}')]
        try:
            owners = blame_owners(git(repo, 'blame', '--porcelain', *args, f'{base}..{target}', '--', f.path))
        except GitError:
            continue
        for h in f.hunks:
            votes = {}
            for a, b in h.added:
                for ln in range(a, b + 1):
                    if ln in owners:
                        votes[owners[ln]] = votes.get(owners[ln], 0) + 1
            if votes:
                h.commit = max(votes, key=lambda s: (votes[s], order.get(s, -1)))


def blob_text(repo, rev, path):
    if git(repo, 'cat-file', '-t', f'{rev}:{path}', ok=(0, 128)).strip() != 'blob':
        return None
    r = subprocess.run(['git', '-C', repo, 'cat-file', 'blob', f'{rev}:{path}'], capture_output=True)
    if r.returncode or b'\0' in r.stdout[:8000]:
        return None
    return r.stdout.decode('utf-8', 'replace')


def ls_tree(repo, rev):
    """[(path, size)] for every blob at `rev` (symlinks included; submodules,
    which have no blob, left out)."""
    out = []
    for rec in filter(None, git(repo, 'ls-tree', '-r', '-l', '-z', rev).split('\0')):
        meta, path = rec.split('\t', 1)
        _, kind, _, size = meta.split(None, 3)
        if kind == 'blob':
            out.append((path, 0 if size.strip() == '-' else int(size)))
    return out


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
    if is_range:
        attribute(repo, base, target, files, commits)
    else:
        for f in files:
            for h in f.hunks:
                h.commit = target
    return Change(repo, base, target, label, is_range, commits, files,
                  sum(f.added for f in files), sum(f.deleted for f in files),
                  tree=ls_tree(repo, target), next=next_commit(repo, target))


def series(repo, spec):
    """(the combined change, [one change per commit, oldest first]) for
    --series: each commit of a range as its own X^..X change, so each plays on
    top of the one before. A single rev, or a range of one commit, is its own
    only patch."""
    combined = collect(repo, spec)
    if not combined.is_range or len(combined.commits) < 2:
        return combined, [combined]
    return combined, [collect(combined.repo, c.sha) for c in combined.commits]

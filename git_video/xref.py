"""Cross references without a language server.

Candidate identifiers are Pygments `Name` tokens on a hunk's added lines;
their definitions are found with `git grep` at the target revision against a
small table of per-language definition patterns, one grep per language.
"""
import re
from dataclasses import dataclass

from pygments.token import Name

from . import collect, highlight

# Pygments lexer name -> (pathspecs, ERE for git grep, [Python regexes with name/kind groups]).
_PY = ('*.py', '*.pyi')
_JS = ('*.js', '*.jsx', '*.mjs', '*.cjs', '*.ts', '*.tsx', '*.mts', '*.cts')
_C = ('*.c', '*.h', '*.cc', '*.cpp', '*.cxx', '*.hh', '*.hpp', '*.hxx', '*.java', '*.cs', '*.m', '*.mm')
_SH = ('*.sh', '*.bash', '*.zsh', '*.ksh')
_LISP = ('*.el', '*.lisp', '*.lsp', '*.cl', '*.scm', '*.ss', '*.rkt', '*.clj', '*.cljs', '*.cljc', '*.edn')

_js = (_JS, r'^\s*(export\s+)?(default\s+)?(async\s+)?(function\*?|class|const|let|var)\s+[A-Za-z_$][A-Za-z0-9_$]*',
       [r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?P<kind>function|class)\*?\s+(?P<name>[A-Za-z_$][\w$]*)',
        r'^\s*(?:export\s+)?(?P<kind>const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*='])
_c = (_C, r'^[A-Za-z_][A-Za-z0-9_ \*:<>,&]*[ \*&:]\*?[A-Za-z_][A-Za-z0-9_]* *\(',
      [r'^[A-Za-z_][\w \*:<>,&]*[ \*&:]\*?(?P<name>[A-Za-z_]\w*) *\(',
       r'^\s*(?:typedef\s+)?(?P<kind>struct|class|enum|union|interface)\s+(?P<name>[A-Za-z_]\w*)\s*[{:]?\s*$'])
_sh = (_SH, r'^\s*(function\s+)?[A-Za-z_][A-Za-z0-9_-]*\s*\(\)',
       [r'^\s*(?:function\s+)?(?P<name>[A-Za-z_][\w-]*)\s*\(\)'])
_lisp = (_LISP, r'^\s*\(def[A-Za-z*-]*\s+[^ ()]+',
         [r'^\s*\((?P<kind>def[\w*-]*)\s+(?P<name>[^\s()]+)'])

LANGS = {
    'Python': (_PY, r'^\s*(async\s+)?(def|class)\s+[A-Za-z_][A-Za-z0-9_]*',
               [r'^\s*(?:async\s+)?(?P<kind>def|class)\s+(?P<name>[A-Za-z_]\w*)']),
    'JavaScript': _js, 'TypeScript': _js, 'TSX': _js, 'JSX': _js,
    'Go': (('*.go',), r'^\s*(func|type)\s',
           [r'^\s*(?P<kind>func)\s+(?:\([^)]*\)\s*)?(?P<name>\w+)\s*[\[(]',
            r'^\s*(?P<kind>type)\s+(?P<name>\w+)']),
    'Rust': (('*.rs',), r'^\s*(pub(\([^)]*\))?\s+)?(fn|struct|enum|trait|type)\s+[A-Za-z_]',
             [r'^\s*(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|const|unsafe|extern\s+"\w+")\s+)*'
              r'(?P<kind>fn|struct|enum|trait|type)\s+(?P<name>\w+)']),
    'C': _c, 'C++': _c, 'Java': _c, 'C#': _c, 'Objective-C': _c,
    'Bash': _sh,
    'EmacsLisp': _lisp, 'Common Lisp': _lisp, 'Scheme': _lisp, 'Clojure': _lisp,
}

KEYWORDS = frozenset('if for while switch return sizeof catch else do new delete case'.split())
FUNCTION_KINDS = frozenset('def class function func fn struct enum trait type interface union defun defmacro '
                           'defn defclass cl-defun defsubst define'.split())
# Name.Function and Name.Class mark a definition site (`def frob`), not a use.
SKIP = (Name.Function, Name.Class, Name.Builtin, Name.Namespace, Name.Decorator, Name.Attribute, Name.Tag, Name.Label, Name.Exception)
AMBIGUOUS = 5     # a name defined more often than this is skipped (main, setUp, ...)
MIN_LEN = 3


@dataclass(frozen=True)
class Definition:
    path: str
    line: int
    kind: str
    col: int = 0     # 0-based column of the name, tabs expanded


@dataclass(frozen=True)
class Candidate:
    line: int        # 1-based
    col: int         # 0-based, tabs expanded
    name: str
    is_call: bool


_indexes = {}


def lang_of(path):
    name = highlight.lexer_for(path).name
    return name if name in LANGS else None


def index(repo, target, lang):
    """{name: [Definition]} for every definition of `lang` in the target tree."""
    key = (repo, target, lang)
    if key in _indexes:
        return _indexes[key]
    specs, ere, pys = LANGS[lang]
    out = collect.git(repo, 'grep', '-z', '-n', '-I', '-E', '--no-color', '-e', ere, target, '--', *specs,
                      ok=(0, 1))
    pats = [re.compile(p) for p in pys]
    defs = {}
    prefix = f'{target}:'
    rows = out.split('\n')
    for row in rows:
        parts = row.split('\0', 2)
        if len(parts) < 3:
            continue
        path, line, text = parts
        path = path[len(prefix):] if path.startswith(prefix) else path
        for p in pats:
            m = p.match(text)
            if m and m.group('name') not in KEYWORDS:
                kind = m.groupdict().get('kind') or 'function'
                col = len(text[:m.start('name')].expandtabs(highlight.TAB))
                defs.setdefault(m.group('name'), []).append(Definition(path, int(line), kind, col))
                break
    _indexes[key] = defs
    return defs


def candidates(text, path, lines):
    """The candidate identifiers on the given 1-based `lines` of the file, in order."""
    toks = highlight.tokens_by_line(text, path)
    out = []
    for ln in lines:
        if not 1 <= ln <= len(toks):
            continue
        col, row = 0, toks[ln - 1]
        for i, (ttype, value) in enumerate(row):
            if (ttype in Name and not any(ttype in s for s in SKIP) and len(value) >= MIN_LEN
                    and value.isidentifier()):
                rest = ''.join(v for _, v in row[i + 1:i + 3]).lstrip()
                out.append(Candidate(ln, col, value, rest.startswith('(')))
            col += len(value)
    return out


def resolve(cand, defs, use_path):
    """The definition `cand` most likely means, or None: never one on the very
    line being looked at (a name defined there is not a use at all), preferring another file, then a function or class."""
    every = defs.get(cand.name, ())
    if any(d.path == use_path and d.line == cand.line for d in every):
        return None   # the name is being defined here (`function handle(`), not used
    found = list(every)
    if not found or len(every) > AMBIGUOUS:
        return None
    return min(found, key=lambda d: (d.path == use_path, d.kind not in FUNCTION_KINDS, d.path, d.line))


def rank_key(cand, d, use_path):
    """Sort key: a definition elsewhere, a call, a function or class, a use in
    source rather than a test, a longer name."""
    return (d.path == use_path, not cand.is_call, d.kind not in FUNCTION_KINDS, collect.is_test(use_path),
            -len(cand.name))

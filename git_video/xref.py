"""Cross references without a language server.

Candidate identifiers are Pygments `Name` tokens on a hunk's added lines;
their definitions are found with `git grep` at the target revision against a
small table of per-language definition patterns, one grep per language.
"""
import re
from dataclasses import dataclass, replace

from pygments.token import Name, Text

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
# C-ish: a top-level `type name(`, an indented method declaration (`public int
# total(...) {`: no `;`, not a `return f(` statement), or a struct/class/enum.
_c = (_C, r'^\s*[A-Za-z_][A-Za-z0-9_ \*:<>,&]*[ \*&:]\*?[A-Za-z_][A-Za-z0-9_]* *\('
          r'|^\s*([A-Za-z]+\s+)*(struct|class|enum|union|interface)\s+[A-Za-z_]',
      [r'^[A-Za-z_][\w \*:<>,&]*[ \*&:]\*?(?P<name>[A-Za-z_]\w*) *\(',
       r'^\s+(?!(?:return|else|new|delete|throw|case|goto|await|yield|co_return)\b)'
       r'[A-Za-z_][\w \*:<>,&]*[ \*&:]\*?(?P<name>[A-Za-z_]\w*) *\([^;]*$',
       r'^\s*(?:(?:public|private|protected|internal|static|final|abstract|sealed|partial)\s+)*'
       r'(?:typedef\s+)?(?P<kind>struct|class|enum|union|interface)\s+(?P<name>[A-Za-z_]\w*)[^;=()]*$'])
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
# Lisp names are hyphenated (`compute-total`); shell names may be too.
WORD = re.compile(r'[A-Za-z_][\w-]*\w|[A-Za-z_]\w*')
MIN_LEN = 3


@dataclass(frozen=True)
class Definition:
    path: str
    line: int
    kind: str
    col: int = 0     # 0-based column of the name, tabs expanded
    owner: str | None = None   # `class` for a method, a function kind for a nested function; None: top level


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
    defs, indents = {}, {}
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
                indents.setdefault(path, {})[int(line)] = _indent(text.expandtabs(highlight.TAB))
                break
    defs = _owned(defs, indents)
    _indexes[key] = defs
    return defs


CLASS_KINDS = frozenset('class struct interface enum union trait'.split())


def _owned(defs, indents):
    """`defs` with each Definition's `owner` set: the kind of the nearest
    definition above it in its file indented less (`class` for a method,
    a function's kind for a nested function), None at top level."""
    by_path = {}
    for name, ds in defs.items():
        for d in ds:
            by_path.setdefault(d.path, []).append((d.line, name, d))
    owner = {}
    for path, rows in by_path.items():
        stack = []   # (indent, kind)
        for line, name, d in sorted(rows, key=lambda r: r[0]):
            ind = indents.get(path, {}).get(line, 0)
            while stack and stack[-1][0] >= ind:
                stack.pop()
            owner[path, line] = ('class' if stack[-1][1] in CLASS_KINDS else stack[-1][1]) if stack else None
            stack.append((ind, d.kind))
    return {name: [replace(d, owner=owner.get((d.path, d.line))) for d in ds] for name, ds in defs.items()}


def candidates(text, path, lines):
    """The candidate identifiers on the given 1-based `lines` of the file, in order."""
    toks = highlight.tokens_by_line(text, path)
    # Pygments' Bash lexer gives a command name (`compute_total 3`) plain Text.
    shell = lang_of(path) == 'Bash'
    out = []
    for ln in lines:
        if not 1 <= ln <= len(toks):
            continue
        col, row = 0, toks[ln - 1]
        for i, (ttype, value) in enumerate(row):
            named = (ttype in Name and not any(ttype in s for s in SKIP)) or (shell and ttype is Text)
            if named and len(value) >= MIN_LEN and WORD.fullmatch(value):
                rest = ''.join(v for _, v in row[i + 1:i + 3]).lstrip()
                out.append(Candidate(ln, col, value, rest.startswith('(')))
            col += len(value)
    return out


QUALIFIED = re.compile(r'([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\.\s*$')
SELF = frozenset(('self', 'cls', 'this'))
# `x.replace(...)` on a str, list, dict or set: the one `def replace` in the repo is not what it means.
BUILTIN_METHODS = frozenset(n for t in (str, bytes, list, dict, set) for n in dir(t) if not n.startswith('_'))


def resolve(cand, defs, use_path, text=None):
    """The definition `cand` means, or None. Never one on the very line being
    looked at (a name defined there is not a use at all). Otherwise the first
    rung that names exactly one definition:

    (a) one in the same class as the call, else in the same file: for a bare
        `draw()` or `self.draw()`, never `x.draw()` with any other `x`;
    (b) one in a module the calling file imports by name (`from x import
        draw`, or `x.draw()` where `x` is an imported module);
    (c) the only definition of the name anywhere.

    A rung that finds two stops there: no dive. A missing dive is fine; a
    wrong one is not. `text` is the calling file's text (without it, (a)
    goes by file alone and (b) is skipped)."""
    every = defs.get(cand.name, ())
    if not every or any(d.path == use_path and d.line == cand.line for d in every):
        return None   # the name is being defined here (`function handle(`), not used
    lines = text.split('\n') if text is not None else None
    qual = None
    if lines is not None and 0 < cand.line <= len(lines):
        m = QUALIFIED.search(lines[cand.line - 1].expandtabs(highlight.TAB)[:cand.col])
        qual = m.group(1) if m else None
    here = lines[cand.line - 1] if lines is not None and 0 < cand.line <= len(lines) else ''
    if _PY_FROM.match(here) or _PY_IMPORT.match(here) or _JS_IMPORT.match(here):
        # an import line names a module's own definition, or a module: (b) or nothing
        hits = [d for d in every if imported(use_path, here)[0].get(cand.name, _Paths()).has(d.path)]
        return hits[0] if len(hits) == 1 else None
    if qual is None and not cand.is_call and lines is not None and _bound(lines, cand):
        return None   # a variable, parameter or field of that name, not the function
    # (a) the same class, else the same file
    if qual is None or qual in SELF:
        mine = [d for d in every if d.path == use_path]
        python = lang_of(use_path) == 'Python'
        if lines is not None and (len(mine) > 1 or qual in SELF or python):
            classes = [(n, d) for n, ds in defs.items() for d in ds if d.path == use_path and d.kind == 'class']
            holder = enclosures(lines, sorted(classes, key=lambda nd: nd[1].line))

            def cls(line):
                return holder[line - 1] if 0 < line <= len(holder) else None

            home = cls(cand.line)
            same = [d for d in mine if home is not None and cls(d.line) == home]
            if qual in SELF:   # `self.draw`: a method of this class, nothing else
                mine = same
            elif python:   # a bare Python name is never a method: a function it can reach
                mine = [d for d in mine if cls(d.line) is None and _reaches(lines, d, cand.line)]
            elif same:   # Java, C++, ...: a bare name reaches its own class's method first
                mine = same
            else:
                mine = [d for d in mine if cls(d.line) is None] or mine
        if len(mine) == 1:
            return mine[0]
        if mine or qual in SELF:
            return None
    # (b) a module the file imports, by name or as the `x` of `x.name`
    if text is not None:
        names, modules = imported(use_path, text)
        paths = names.get(cand.name) if qual is None else modules.get(qual)
        if paths:
            hits = [d for d in every if paths.has(d.path)]
            if len(hits) == 1:
                return hits[0]
            if hits:
                return None
    # (c) the only one there is, and one the call can reach: a bare name a
    # top-level definition, `x.name` a method (not a str or dict method's name)
    if len(every) != 1:
        return None
    d = every[0]
    python = lang_of(use_path) == 'Python'
    if qual is None:
        return d if d.owner is None else None
    if python:   # `x.name` in Python: a method, as top-level functions come through (b)
        return d if d.owner == 'class' and cand.name not in BUILTIN_METHODS else None
    return d if d.owner in (None, 'class') else None   # Go's `func (r *T) Read` is top level


_SCOPE = re.compile(r'^\s*(?:async\s+)?(?:def|class|function\*?|func|fn)\b')


def _bound(lines, cand):
    """Whether `cand`'s bare name is bound in a scope around it: assigned or
    annotated, a loop or `as` target, a declared variable, or a parameter of
    an enclosing function, in an enclosing block or at the top level. Such a
    name is that variable, whatever function shares it."""
    name = re.escape(cand.name)
    binds = re.compile(rf'(?<![\w.$]){name}\s*(?::[^=\n]*)?=(?!=)|^\s*{name}\s*:'
                       rf'|\bfor\b[^:\n]*(?<![\w.$]){name}\b[^:\n]*\bin\b|\bas\s+{name}\b'
                       rf'|\b(?:const|let|var)\s+{name}\b')
    param = re.compile(rf'[(,]\s*\**{name}\b')
    tab = [s.expandtabs(highlight.TAB) for s in lines]
    ln = min(cand.line, len(tab)) - 1
    if ln < 0:
        return False
    ind = _indent(tab[ln]) if tab[ln].strip() else 0
    blocks = []   # (header line, its indent) of each enclosing def/class, innermost first
    if _SCOPE.match(tab[ln]):   # a parameter on the def line itself
        blocks.append((ln, -1))
    for k in range(ln, -1, -1):
        s = tab[k]
        if s.strip() and _indent(s) < ind:
            ind = _indent(s)
            if _SCOPE.match(s):
                blocks.append((k, ind))
    for k, hind in blocks:
        head = ' '.join(tab[k:k + 10])
        params = head[head.find('('):head.find(')') + 1] if '(' in head else ''
        if not re.match(r'\s*class\b', head) and param.search(params):   # a class's bases are uses
            return True
        if hind < 0:
            continue
        end = k + 1
        while end < len(tab) and (not tab[end].strip() or _indent(tab[end]) > hind):
            end += 1
        if any(binds.search(s) for s in tab[k + 1:end]):
            return True
    return any(binds.search(s) for s in tab if s.strip() and _indent(s) == 0)


def _reaches(lines, d, line):
    """Whether a call on `line` can see `d` by its bare name: `d` is top level,
    or nested and the call sits after it in the same block (no line between
    them, nor the call's, is indented less than `d`'s own)."""
    if d.owner is None:
        return True
    if not 0 < d.line < line <= len(lines):
        return False
    ind = _indent(lines[d.line - 1].expandtabs(highlight.TAB))
    return all(not s.strip() or _indent(s.expandtabs(highlight.TAB)) >= ind for s in lines[d.line:line])


class _Paths(set):
    """Paths a module can be; `*/x` also matches x under a source root (src/x)."""
    def has(self, path):
        return path in self or any(p.startswith('*/') and path.endswith(p[1:]) for p in self)


def _module_paths(use_path, module, level=0, lang='Python'):
    """The repo paths `module` (as an import statement names it) can be, from `use_path`."""
    if lang == 'Python':
        parts = [p for p in module.split('.') if p]
        base = []
        if level:
            base = use_path.split('/')[:-1]
            if level - 1 > len(base):
                return set()
            base = base[:len(base) - (level - 1)]
        stem = '/'.join(base + parts)
        if not stem:
            return set()
        out = {f'{stem}.py', f'{stem}.pyi', f'{stem}/__init__.py'}
        return out if level else out | {f'*/{x}' for x in out}
    if not module.startswith('.'):   # JS: only a relative specifier names a file here
        return set()
    parts = use_path.split('/')[:-1]
    for seg in module.split('/'):
        if seg == '..':
            parts = parts[:-1]
        elif seg not in ('.', ''):
            parts.append(seg)
    stem = '/'.join(parts)
    exts = ('', '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.mts', '.cts')
    return {stem + e for e in exts} | {f'{stem}/index{e}' for e in exts[1:]}


_PY_FROM = re.compile(r'^\s*from\s+(\.*)([\w.]*)\s+import\s+\(?([^)#]*)')
_PY_IMPORT = re.compile(r'^\s*import\s+([^#]+)')
_JS_IMPORT = re.compile(r"""^\s*import\s+(.+?)\s+from\s+['"]([^'"]+)['"]""")
_JS = frozenset(('JavaScript', 'TypeScript', 'TSX', 'JSX'))


def imported(use_path, text):
    """({name: _Paths}, {module alias: _Paths}) for the file at `use_path`:
    where each name it imports by name is defined, and the modules it can
    call into as `alias.name`. Python and JavaScript-like imports."""
    lang = lang_of(use_path)
    names, modules = {}, {}

    def add(table, key, paths):
        if paths:
            table.setdefault(key, _Paths()).update(paths)

    for line in text.split('\n'):
        if lang == 'Python':
            m = _PY_FROM.match(line)
            if m:
                level, mod = len(m.group(1)), m.group(2)
                for part in m.group(3).split(','):
                    bits = part.split()
                    if not bits or not bits[0].isidentifier():
                        continue
                    name = bits[0]
                    alias = bits[2] if len(bits) == 3 and bits[1] == 'as' else name
                    if alias == name:   # `from x import draw`: draw is x's
                        add(names, name, _module_paths(use_path, mod, level))
                    # `from pkg import mod [as m]`: mod may be a module, used as `m.f`
                    add(modules, alias, _module_paths(use_path, f'{mod}.{name}' if mod else name, level))
                continue
            m = _PY_IMPORT.match(line)
            for part in (m.group(1).split(',') if m else ()):
                bits = part.split()
                if bits:
                    alias = bits[2] if len(bits) == 3 and bits[1] == 'as' else bits[0]
                    add(modules, alias, _module_paths(use_path, bits[0]))
        elif lang in _JS:
            m = _JS_IMPORT.match(line)
            if not m:
                continue
            what, paths = m.group(1), _module_paths(use_path, m.group(2), lang=lang)
            star = re.search(r'\*\s+as\s+([\w$]+)', what)
            if star:
                add(modules, star.group(1), paths)
            braces = re.search(r'\{([^}]*)\}', what)
            for part in (braces.group(1).split(',') if braces else ()):
                bits = part.split()
                if bits and bits[0] == bits[-1]:
                    add(names, bits[0], paths)
            default = re.match(r'([\w$]+)\s*(,|$)', what)
            if default:
                add(modules, default.group(1), paths)
    return names, modules


def rank_key(cand, d, use_path):
    """Sort key: a definition elsewhere, a call, a function or class, a use in
    source rather than a test, a longer name."""
    return (d.path == use_path, not cand.is_call, d.kind not in FUNCTION_KINDS, collect.is_test(use_path),
            -len(cand.name))


def _indent(s):
    return len(s) - len(s.lstrip())


def enclosures(lines, defs):
    """For each 1-based line of `lines` (the file's text, split), at index
    line - 1, the innermost of `defs` ([(name, Definition)] of one file) whose
    body holds it, as (name, Definition), or None at top level or on a blank
    line. A definition holds a line when every non-blank line after its own,
    up to and including that one, is indented deeper than the definition's
    own line. One pass, so a whole file costs no more than reading it."""
    starts = {}
    for name, d in defs:
        if 0 < d.line <= len(lines):
            starts.setdefault(d.line, (name, d))   # the first one listed wins a shared line
    out, open_ = [], []   # open_: [(base indent, (name, d))] still holding every line so far, by line
    for n, s in enumerate(lines, 1):
        if s.strip():
            ind = _indent(s.expandtabs(highlight.TAB))
            open_ = [o for o in open_ if ind > o[0]]
        if n in starts:
            open_.append((_indent(s.expandtabs(highlight.TAB)), starts[n]))
        out.append(open_[-1][1] if open_ and s.strip() else None)
    return out


def file_defs(repo, target, path):
    """[(name, Definition)] defined in `path` at `target`, by line."""
    lang = lang_of(path)
    if lang is None:
        return []
    return sorted(((name, d) for name, ds in index(repo, target, lang).items() for d in ds if d.path == path),
                  key=lambda nd: nd[1].line)

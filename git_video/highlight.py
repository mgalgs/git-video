"""Pygments tokens per line, and the colours they are drawn in.

Files are lexed once (whole, so multi-line strings and comments come out
right) and cached; the film draws from the cached runs and never re-lexes.
"""
from functools import lru_cache

from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.token import Token
from pygments.util import ClassNotFound

TAB = 4

# Dark, doom-one-like. Lookups walk a token type up its parents to the first hit.
FG = '#bbc2cf'
THEME = {
    Token: FG,
    Token.Keyword: '#c678dd',
    Token.Keyword.Constant: '#a9a1e1',
    Token.Name.Function: '#61afef',
    Token.Name.Class: '#e5c07b',
    Token.Name.Builtin: '#56b6c2',
    Token.Name.Builtin.Pseudo: '#a9a1e1',
    Token.Name.Decorator: '#e5c07b',
    Token.Name.Tag: '#c678dd',
    Token.Name.Variable: '#dcaeea',
    Token.Name.Constant: '#a9a1e1',
    Token.String: '#98c379',
    Token.String.Doc: '#7f9f6a',
    Token.Number: '#da8548',
    Token.Comment: '#5b6268',
    Token.Operator: '#56b6c2',
    Token.Operator.Word: '#c678dd',
    Token.Punctuation: FG,
    Token.Generic.Inserted: '#98c379',
    Token.Generic.Deleted: '#ff6c6b',
    Token.Generic.Heading: '#61afef',
    Token.Generic.Subheading: '#c678dd',
}


def color_of(ttype):
    while ttype not in THEME:
        ttype = ttype.parent
    return THEME[ttype]


@lru_cache(maxsize=512)
def lexer_for(path):
    """A Pygments lexer for the file name, plain text when none fits. Leading and
    trailing newlines are kept so token lines are the file's lines."""
    try:
        return get_lexer_for_filename(path, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return TextLexer(stripnl=False, ensurenl=False)


@lru_cache(maxsize=256)
def _tokens(text, path):
    lexer = lexer_for(path)
    lines, cur, col = [], [], 0
    for ttype, value in lexer.get_tokens(text):
        parts = value.split('\n')
        for i, part in enumerate(parts):
            if i:
                lines.append(tuple(cur))
                cur, col = [], 0
            if not part:
                continue
            if '\t' in part:
                out = []
                for ch in part:
                    if ch == '\t':
                        n = TAB - col % TAB
                        out.append(' ' * n)
                        col += n
                    else:
                        out.append(ch)
                        col += 1
                part = ''.join(out)
            else:
                col += len(part)
            cur.append((ttype, part))
    lines.append(tuple(cur))
    if text.endswith('\n'):
        lines.pop()   # the empty line after the last newline is not a line of the file
    return tuple(lines)


def tokens_by_line(text, path):
    """((token type, text), ...) per line of `text`, tabs expanded to 4 columns.
    Cached on (text, file name): the lexer comes from the name."""
    return _tokens(text, path)


@lru_cache(maxsize=256)
def _runs(text, path):
    out = []
    for line in _tokens(text, path):
        runs = []
        for ttype, value in line:
            c = color_of(ttype)
            if runs and runs[-1][0] == c:
                runs[-1] = (c, runs[-1][1] + value)
            else:
                runs.append((c, value))
        out.append(tuple(runs))
    return tuple(out)


def highlight_lines(text, path):
    """((colour, text), ...) per line: adjacent tokens of one colour merged, tabs
    expanded to 4. Cached, so asking again for the same file returns the same object."""
    return _runs(text, path)


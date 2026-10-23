"""The `git video` command line."""
import argparse
import os
import sys

DEFAULT_VOICE = 'bf_isabella'
FORMATS = ('16x9', '9x16', '1x1')

DESCRIPTION = 'Create a video walk-through of a change set.'

EPILOG = """\
Modes:

tour    A solid xref session in an Emacs-looking editor.
        Clicks through the relevant bits of code, highlighting things
        along the way, clicking into relevant definitions when
        interesting. Keyboard clicking noises. --verbose to control
        amount of side exploration.

develop   A pair programming session of the entire change set from top
          to bottom (not implemented yet).

A single rev X shows the change X^..X (a root commit is shown against the
empty tree); A..B shows the combined diff of the range.
"""


def _expand_bare(argv):
    """Give the optional-value flags their value inline, so `--verbose HEAD~1`
    reads HEAD~1 as the rev rather than as the verbosity."""
    out = []
    for a in argv:
        if a == '--verbose':
            a = '--verbose=1'
        elif a == '--voice':
            a = f'--voice={DEFAULT_VOICE}'
        out.append(a)
    return out


def parser():
    p = argparse.ArgumentParser(
        prog='git video', usage='git video <rev|range> [options]', description=DESCRIPTION, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('rev', nargs='?', default='HEAD', metavar='<rev|range>',
                   help='a commit (shown as X^..X) or a range A..B (default HEAD)')
    p.add_argument('--mode', choices=('tour', 'develop'), default='tour', help='tour (default) or develop')
    p.add_argument('--series', action='store_true',
                   help='treat the patches in the range separately; does not apply to tour, which ignores it')
    p.add_argument('--verbose', type=int, choices=(1, 2, 3), default=0, metavar='N',
                   help='--verbose[=1,2,3]: side exploration (xref visits). Unset: one visit in the whole '
                        'tour; 1: up to one per file; 2: up to one per hunk; 3: every resolvable identifier '
                        '(capped). A bare --verbose means 1')
    p.add_argument('--voice', metavar='VOICE-PROFILE',
                   help=f'--voice[=<voice-profile>]: include a voice over (default profile {DEFAULT_VOICE}; '
                        'not implemented yet)')
    p.add_argument('--format', choices=(*FORMATS, 'all'), default='16x9', help='output shape (default 16x9)')
    p.add_argument('-o', '--out', metavar='FILE',
                   help='output MP4 (default git-video-<short-sha>.mp4, or git-video-<a>-<b>.mp4 for a range)')
    p.add_argument('--jobs', type=int, metavar='N', help='render workers (default: one per CPU)')
    p.add_argument('--plan-json', action='store_true', help='print the shot plan as JSON and exit')
    p.add_argument('-C', dest='C', action='append', metavar='PATH',
                   help='run as if started in PATH (repeatable, like git -C)')
    p.add_argument('--font', default='monospace', metavar='FAMILY', help='editor font family (default monospace)')
    p.add_argument('--stills', metavar='DIR',
                   help="write a PNG of each key moment plus sheet.png to DIR and exit without rendering")
    return p


def parse(argv):
    return parser().parse_args(_expand_bare(list(argv)))


def repo_path(C):
    """The repository directory for the -C options given (each relative to the last)."""
    if not C:
        return os.getcwd()
    return os.path.join(*C)


def output_paths(label, out, formats):
    """[(format, path)]: one MP4 per format. With several formats, each name gets
    `-<format>` before its extension."""
    root, ext = os.path.splitext(out) if out else (f'git-video-{label}', '.mp4')
    ext = ext or '.mp4'
    if len(formats) == 1 and out:
        return [(formats[0], out)]
    if len(formats) == 1:
        return [(formats[0], root + ext)]
    return [(f, f'{root}-{f}{ext}') for f in formats]


def _fail(msg, code):
    print(f'git video: {msg}', file=sys.stderr)
    return code


def main(argv=None):
    args = parse(sys.argv[1:] if argv is None else argv)
    if args.mode == 'develop':
        return _fail('--mode develop is not implemented yet', 2)
    if args.voice is not None:
        return _fail('--voice is not implemented yet', 2)
    from . import collect, plan
    try:
        change = collect.collect(repo_path(args.C), args.rev)
    except collect.GitError as e:
        return _fail(str(e), 1)
    tour = plan.plan(change, args.verbose)
    if args.plan_json:
        print(tour.to_json())
        return 0
    return _fail('rendering is not wired yet', 2)

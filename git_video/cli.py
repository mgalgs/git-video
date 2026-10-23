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
          to bottom. Including rewrites when --series is provided (each
          patch builds on the last). Programming as a performance art
          where the code is the main character (credit: bisqwit).
          Can control elision and frequency of speed-up sequences with
          --verbose. Default is to aim for roughly 60s per 500 lines of
          code.

A single rev X shows the change X^..X (a root commit is shown against the
empty tree); A..B shows the combined diff of the range, or with --series
each of its commits in turn.
"""


def _expand_bare(argv):
    """Give the optional-value flags their value inline, so `--verbose HEAD~1`
    reads HEAD~1 as the rev rather than as the verbosity, while `--verbose 2`
    still means level 2."""
    out = []
    argv = list(argv)
    i = 0
    while i < len(argv):
        a = argv[i]
        i += 1
        if a == '--verbose':
            if i < len(argv) and argv[i] in ('1', '2', '3'):
                a = f'--verbose={argv[i]}'
                i += 1
            else:
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
                   help="treat a range's patches separately: one tour or one performance per commit, each on "
                        'top of the one before, with a card between them (a single rev is unchanged)')
    p.add_argument('--verbose', type=int, choices=(1, 2, 3), default=0, metavar='N',
                   help='--verbose[=1,2,3]: tour: side exploration (xref visits). Unset: one visit in the '
                        'whole tour; 1: up to one per file; 2: up to one per hunk; 3: every resolvable identifier '
                        '(capped). develop: unset elides long typed runs and speeds up freely; 1 elides only very '
                        'long runs; 2 never elides but still speeds up; 3 types everything at natural speed. '
                        'A bare --verbose means 1')
    p.add_argument('--voice', metavar='VOICE-PROFILE',
                   help=f'--voice[=<voice-profile>]: narrate the change, with captions: not reading the code but '
                        f'explaining the intent behind it, in a Kokoro voice '
                        f"(default {DEFAULT_VOICE}); needs the voice extra, pip install 'git-video[voice]', "
                        'and the Kokoro model files')
    p.add_argument('--format', choices=(*FORMATS, 'all'), default='16x9', help='output shape (default 16x9)')
    p.add_argument('-o', '--out', metavar='FILE',
                   help='output MP4 (default git-video-<short-sha>.mp4, or git-video-<a>-<b>.mp4 for a range; with '
                        '--format all, -<format> goes before the extension); a missing directory is created')
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
    from . import collect, plan
    if args.voice is not None:   # before any git work: no model, no render
        from . import voice
        try:
            engine = voice.make_engine()
            voice.check_voice(engine, args.voice)
        except voice.VoiceError as e:
            return _fail(str(e), 2)
    try:
        if args.series:
            change, patches = collect.series(repo_path(args.C), args.rev)
        else:
            change = collect.collect(repo_path(args.C), args.rev)
            patches = [change]
    except collect.GitError as e:
        return _fail(str(e), 1)
    if args.voice is not None:
        try:
            tour = voice.narrate(change, patches, args.verbose, args.mode, engine, args.voice)
        except voice.VoiceError as e:
            return _fail(str(e), 2)
    else:
        tour = plan.build(change, args.verbose, args.mode, patches)
    if args.plan_json:
        print(tour.to_json())
        return 0
    from . import film
    formats = list(FORMATS) if args.format == 'all' else [args.format]
    tour = film.Tour(tour, change, formats=formats, font=args.font, patches=patches)
    if args.stills:
        for path in write_stills(tour.film, formats, args.stills):
            print(path)
        return 0
    from videobuilder.render import render
    for fmt, out in output_paths(change.label, args.out, formats):
        render(tour.film, out, format=fmt, jobs=args.jobs)
        print(out)
    return 0


def _slug(s):
    return ''.join(c if c.isalnum() or c in '-_.' else '-' for c in s).strip('-')[:60]


def write_stills(film, formats, out_dir):
    """A PNG per keyframe and a contact sheet per format, in `out_dir`; returns the paths."""
    from videobuilder.cli import contact_sheet
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for fmt in formats:
        suffix = f'-{fmt}' if len(formats) > 1 else ''
        for k, (n, label) in enumerate(film.keyframes):
            path = os.path.join(out_dir, f'{k:02d}-{_slug(label)}{suffix}.png')
            film.frame(n, fmt).save_png(path)
            paths.append(path)
        sheet = os.path.join(out_dir, f'sheet{suffix}.png')
        contact_sheet(film, list(film.keyframes), fmt).save(sheet)
        paths.append(sheet)
    return paths
